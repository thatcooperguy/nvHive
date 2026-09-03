"""Tests for the Wizard concierge: per-turn hidden-specialist selection.

Pure-function tests: fake ``wizard_context`` / findings dicts, synthetic
profile lists, no network and no engine. A few tests read the packaged Agent
Library through ``list_profiles(home_dir=tmp_path)`` to pin that every rule
still points at a profile that ships.

The router's TF-IDF classifier is a tie-breaker the concierge consults; tests
that check rule logic pin it with ``neutral_classifier`` (or a fixed result)
so a corpus change never moves a routing assertion by 0.02.
"""

from __future__ import annotations

import re
import time
from types import SimpleNamespace

import pytest

from nvh.integrations.wizard.concierge import (
    AUTO_PROFILE,
    MIN_SCORE,
    RESIDUE_CONFIDENCE,
    SPECIALIST_RULES,
    STATE_BOOST,
    STICKY_CONFIDENCE,
    SpecialistChoice,
    SpecialistRule,
    available_specialists,
    derive_state,
    resolve_auto_profile,
    select_specialist,
)
from nvh.integrations.wizard.profiles import AgentProfile
from nvh.providers.base import TaskType

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

RULE_PROFILES: tuple[str, ...] = tuple(dict.fromkeys(r.profile for r in SPECIALIST_RULES))
OPS_PROFILES = {
    "install-medic", "gpu-triage", "model-sommelier", "model-librarian", "vram-planner",
    "provider-keysmith", "device-settings",
}
MODEL_DESK = ("model-sommelier", "vram-planner", "model-librarian")
CODING_PROFILES = {"bug-hunter", "deep-reviewer", "backend-implementer"}
#: One state boost, as the confidence formula sees it (0.15 per point of score).
BOOST_CONF = 0.15 * STATE_BOOST


def _profiles(*names: str, **overrides: dict) -> list[AgentProfile]:
    """Synthetic profile list; ``overrides[name]`` patches dataclass fields."""
    out = []
    for n in names:
        kw = {"name": n, "title": n.title(), "description": "", "system_prompt": ""}
        kw.update(overrides.get(n, {}))
        out.append(AgentProfile(**kw))
    return out


ALL = _profiles(*RULE_PROFILES)


def _ctx(**overrides):
    """Healthy wizard_context()-shaped snapshot; override per test."""
    base = {
        "gpu": {"detected": True, "unified_memory": False, "memory_total_gb": 24},
        "platform": {"device_class": "workstation", "unified_memory": False},
        "storage": {"available": True, "ok": True, "warnings": []},
        "providers": [{"name": "openai", "healthy": True}],
        "ollama_models": [{"name": "llama3.1:8b"}],
        "recent_jobs": [],
        "receipts": {"count": 3, "unhealthy": 0},
        "vault": {"initialized": True},
    }
    base.update(overrides)
    return base


def _spark_ctx():
    return _ctx(
        gpu={"detected": True, "unified_memory": True, "memory_total_gb": 128},
        platform={"device_class": "dgx-spark", "unified_memory": True, "memory_total_gb": 128,
                  "os": "linux", "arch": "aarch64", "is_dgx_os": True, "can_sudo": True},
    )


def _fresh_ctx():
    return _ctx(ollama_models=[], receipts={"count": 0, "unhealthy": 0}, providers=[])


def _fresh_spark_ctx():
    """A DGX Spark straight out of the box: no models, no receipts, no keys."""
    return _spark_ctx() | {"ollama_models": [], "receipts": {"count": 0, "unhealthy": 0}, "providers": []}


def _spark_ollama_down_ctx():
    """A lived-in Spark whose Ollama is not answering: ``_ollama_models()``
    returns ``[]`` on any exception, so ``no_models`` is true while receipts
    (and so ``first_run``) say this is not a first run."""
    return _spark_ctx() | {"ollama_models": []}


def _pick(question: str, **kw) -> SpecialistChoice:
    kw.setdefault("profiles", ALL)
    kw.setdefault("context", _ctx())
    return select_specialist(question, **kw)


def _rule(profile: str) -> SpecialistRule:
    return next(r for r in SPECIALIST_RULES if r.profile == profile)


def _fixed_classifier(monkeypatch, task_type: TaskType, confidence: float) -> None:
    """Pin ``nvh.core.router.classify_task`` (imported lazily by the concierge)."""
    result = SimpleNamespace(task_type=task_type, confidence=confidence)
    monkeypatch.setattr("nvh.core.router.classify_task", lambda question: result)


@pytest.fixture
def neutral_classifier(monkeypatch):
    """Classifier that never agrees with a rule and never clears the residue floor."""
    _fixed_classifier(monkeypatch, TaskType("conversation"), 0.0)


TRACEBACK = """I ran the installer and got this:
Traceback (most recent call last):
  File "setup.py", line 3, in <module>
    import torch
ModuleNotFoundError: No module named 'torch'
pip install torch also fails"""

CODE_BUG = """```python
def add(a, b):
    return a - b
```
this returns the wrong result when I call add(2, 3), why?"""


# ---------------------------------------------------------------------------
# Ops routing
# ---------------------------------------------------------------------------


def test_traceback_routes_to_install_medic() -> None:
    c = _pick(TRACEBACK)
    assert c.profile == "install-medic"
    assert c.confidence >= 0.5
    assert "traceback" in c.matched
    assert c.reason.startswith("install-medic:")


def test_nvidia_smi_no_devices_routes_to_gpu_triage() -> None:
    c = _pick("nvidia-smi says no devices were found")
    assert c.profile == "gpu-triage"
    assert any("nvidia-smi" in m for m in c.matched)


def test_spark_model_fit_goes_to_the_sommelier_and_notes_unified_memory() -> None:
    # "which model fits" is a recommendation, so the sommelier takes it even
    # though "128 GB" is a sizing token; the Spark note still rides along.
    c = _pick("which model fits my 128 GB spark?", context=_spark_ctx())
    assert c.profile == "model-sommelier"
    assert "unified memory" in c.reason.lower()
    assert "MemAvailable" in c.reason


def test_bare_which_model_is_the_sommeliers_on_and_off_a_spark() -> None:
    plain = _pick("which model should I use?")
    spark = _pick("which model should I use?", context=_spark_ctx())
    assert plain.profile == spark.profile == "model-sommelier"
    # The device and unified_memory predicates are one group: a Spark lifts
    # the sommelier by one boost, not two, and the reason names both facts.
    # (Confidence is rounded to two places, hence the 0.01 tolerance.)
    assert spark.confidence == pytest.approx(plain.confidence + BOOST_CONF, abs=0.011)
    assert "DGX Spark" in spark.reason and "unified memory" in spark.reason.lower()
    fresh = _pick("which model should I use?", context=_fresh_spark_ctx(), history=[])
    assert fresh.profile == "model-sommelier"
    assert fresh.confidence == pytest.approx(plain.confidence + 2 * BOOST_CONF, abs=0.011)
    assert "no local Ollama models" in fresh.reason


def test_vague_trouble_tie_is_broken_by_state() -> None:
    # Both rig doctors carry the rig-trouble pattern, so "what's wrong with
    # this box?" ties at 2.5 and the state decides. A bare "what's wrong?" is
    # nobody's any more (review 2026-09-02, R3; see
    # test_vague_trouble_words_need_the_rig): the vague word is weak.
    failed = _ctx(recent_jobs=[{"id": "j1", "kind": "ollama", "status": "failed"}])
    c = _pick("what's wrong with this box?", context=failed)
    assert c.profile == "install-medic"
    assert "install job failed" in c.reason

    no_gpu = _ctx(gpu={"detected": False})
    c = _pick("what's wrong with this box?", context=no_gpu, findings=[{"id": "gpu-missing"}])
    assert c.profile == "gpu-triage"
    assert "no NVIDIA GPU" in c.reason
    # State amplifies a match; it never makes one.
    assert _pick("what's wrong?", context=failed).profile is None


def test_provider_key_401_routes_to_keysmith() -> None:
    c = _pick("my API key returns 401 from openai")
    assert c.profile == "provider-keysmith"
    assert "re:401" in c.matched


def test_slow_tokens_route_to_latency_tuner() -> None:
    assert _pick("generation is really slow, only 3 tok/s").profile == "latency-tuner"


def test_finetune_routes_to_advisor() -> None:
    c = _pick("how do I fine-tune llama with unsloth on my dataset?")
    assert c.profile == "finetune-advisor"


def test_first_run_get_started_routes_to_setup_concierge() -> None:
    c = _pick("how do I get started?", context=_fresh_ctx(), history=[])
    assert c.profile == "setup-concierge"
    assert "first run" in c.reason
    assert "no local Ollama models" in c.reason


def test_state_alone_never_selects_a_specialist() -> None:
    # A fresh box must not send a poem to the install medic / librarian.
    c = _pick("write me a poem about autumn", context=_fresh_ctx(), history=[])
    assert c.profile is None
    # Nor may findings alone select anything when the words carry no signal.
    c = _pick("tell me a joke", findings=[{"id": "gpu-missing"}, {"id": "job-failed-1"}])
    assert c.profile is None


# ---------------------------------------------------------------------------
# Setup concierge: first-run / onboarding intent
# ---------------------------------------------------------------------------

# Onboarding questions and the context they arrive in; every one is the
# concierge's. The Spark owner's first question carries device:dgx-spark
# (with models already pulled, so it is the words plus the device boost).
SETUP_POSITIVES = [
    ("how do i get started", {}),
    ("just got my dgx spark, what should I do first", {"context": _spark_ctx()}),
    ("set this up for me", {"context": _fresh_ctx(), "history": []}),
    ("new machine, where do i start", {}),
    ("install nvhive on this box", {}),
    ("configure nvhive for me please", {}),
    ("I just unboxed my spark, first time user", {"context": _spark_ctx()}),
    ("how do I begin?", {}),
    ("where do I start?", {"context": _fresh_spark_ctx(), "history": []}),
]

# Setup words next to another specialist's domain are that specialist's;
# "set up" / "setup" alone carry no signal. Every entry is checked on a
# workstation, a Spark and a fresh Spark, so the concierge's state boosts
# never turn one of these into an onboarding turn.
SETUP_NEGATIVES = {
    "how do I set up ssh keys on the spark?": "shell-teacher",
    "set up docker": "container-wrangler",
    "set up home assistant": "home-assistant",
    "install torch": "install-medic",
    "which model fits": "model-sommelier",
    "help me with setup": None,
    "first time seeing this error": "install-medic",
    "just got this error from ollama": {"install-medic", "model-librarian"},
    "getting started with fine-tuning": "finetune-advisor",
    # Review 2026-09-02: an onboarding phrase beside a trouble word or a
    # model question is the rig doctor's / the model desk's, however fresh
    # the box (the "just got my ... spark" pattern used to win on state).
    "my gpu is broken, I just got my spark": "gpu-triage",
    "the installer failed, where do I start": "install-medic",
    "just got my spark, which model should I pull first?": "model-sommelier",
    "just got my spark, which 70B model fits?": "model-sommelier",
    "just got my spark, will a 70B fit in 128 GB?": "vram-planner",
}


@pytest.mark.parametrize("q,kw", SETUP_POSITIVES, ids=[q for q, _ in SETUP_POSITIVES])
def test_first_run_intent_routes_to_setup_concierge(q, kw, neutral_classifier) -> None:
    c = _pick(q, **kw)
    assert c.profile == "setup-concierge", c.reason
    assert c.confidence >= 0.5
    assert c.reason.startswith("setup-concierge:")


@pytest.mark.parametrize("q,expected", sorted(SETUP_NEGATIVES.items(), key=lambda kv: kv[0]))
def test_setup_words_beside_a_domain_do_not_hijack(q, expected, neutral_classifier) -> None:
    for ctx in (_ctx(), _spark_ctx(), _fresh_spark_ctx()):
        c = _pick(q, context=ctx, history=[])
        assert c.profile != "setup-concierge", c.reason
        if isinstance(expected, set):
            assert c.profile in expected, c.reason
        else:
            assert c.profile == expected, c.reason


def test_bare_setup_is_weak_and_named_in_the_hint(neutral_classifier) -> None:
    c = _pick("help me with setup", context=_fresh_ctx(), history=[])
    assert c.profile is None
    assert "setup-concierge: weak 'setup' needs a second signal" in c.reason
    # Beside a strong onboarding word it counts and lifts confidence.
    bare = _pick("where do i start")
    with_setup = _pick("where do i start with setup")
    assert bare.profile == with_setup.profile == "setup-concierge"
    assert with_setup.confidence > bare.confidence
    assert "setup" in with_setup.matched


def test_fresh_spark_first_question_names_the_machine_and_the_first_run(neutral_classifier) -> None:
    c = _pick("how do I get started?", context=_fresh_spark_ctx(), history=[])
    assert c.profile == "setup-concierge"
    assert "fresh workspace (first run)" in c.reason
    assert "DGX Spark" in c.reason and "MemAvailable" in c.reason
    assert "no local Ollama models" in c.reason
    # The same words off a fresh box still route, with less confidence.
    plain = _pick("how do I get started?")
    assert plain.profile == "setup-concierge"
    assert c.confidence > plain.confidence


def test_first_run_state_belongs_to_the_concierge_not_the_model_desk(neutral_classifier) -> None:
    assert any("first_run" in expr for expr in _rule("setup-concierge").state)
    for profile in MODEL_DESK:
        assert not any("first_run" in expr for expr in _rule(profile).state), profile
    # "which model" questions are the sommelier's, fresh box or not; on a
    # fresh box no_models lifts it and the reason says so.
    c = _pick("which model should I use?", context=_fresh_ctx(), history=[])
    assert c.profile == "model-sommelier" and "no local Ollama models" in c.reason
    assert _pick("which model should I use?", context=_fresh_spark_ctx(), history=[]).profile == "model-sommelier"
    # A trouble word on a fresh box is still the medic's.
    c = _pick("the installer failed", context=_fresh_ctx(), history=[])
    assert c.profile == "install-medic"


def test_setup_concierge_rule_is_gated() -> None:
    rule = _rule("setup-concierge")
    assert {"set up", "setup", "first time"} <= set(rule.weak_keywords)
    assert not {"set up", "setup", "first time", "install", "configure"} & set(rule.keywords)
    assert {"ssh", "docker", "home assistant", "torch"} <= set(rule.excludes)
    # The rig doctor's and the model desk's words are vetoes (review 2026-09-02).
    assert {"gpu", "error", "failed", "broken", "model", "fits", "vram", "quant"} <= set(rule.excludes)
    # State is two expressions at most: first_run / no_models count once,
    # the device only on a first run. No bare predicate stacks on its own.
    assert rule.state == (
        "first_run|no_models", "first_run&device:dgx-spark", "first_run&device:rtx-spark",
    )
    assert rule.phrase_once
    moved = {"get started", "getting started", "where do i start", "what should i do first", "first steps"}
    assert moved <= set(rule.keywords)
    for profile in MODEL_DESK:
        assert not moved & set(_rule(profile).keywords), profile
    # Placement: after the rig doctor (trouble ties stay with the medic),
    # before the model desk (onboarding ties are the concierge's), whose
    # own order puts the sommelier first so a model tie is its.
    order = [r.profile for r in SPECIALIST_RULES]
    assert order.index("gpu-triage") < order.index("setup-concierge") < order.index("model-sommelier")
    assert order.index("model-sommelier") < order.index("vram-planner") < order.index("model-librarian")


def test_concierge_state_is_capped_at_two_boosts(neutral_classifier) -> None:
    """Review 2026-09-02 (1): the four bare predicates used to stack to +1.8
    on a fresh Spark and +1.2 on any Spark whose Ollama was merely down."""
    plain = _pick("how do I get started?")
    assert plain.profile == "setup-concierge" and plain.confidence == 0.5
    # Fresh Spark: first_run|no_models (once) + first_run&device = two boosts,
    # and the reason still names all three facts.
    fresh = _pick("how do I get started?", context=_fresh_spark_ctx(), history=[])
    assert fresh.profile == "setup-concierge"
    assert fresh.confidence == pytest.approx(plain.confidence + 2 * BOOST_CONF)
    for note in ("fresh workspace (first run)", "no local Ollama models", "DGX Spark"):
        assert note in fresh.reason, note
    # Spark with Ollama down but receipts on disk: no_models alone, one
    # boost; the device does not count because this is not a first run.
    down = _pick("how do I get started?", context=_spark_ollama_down_ctx(), history=[])
    assert down.profile == "setup-concierge"
    assert down.confidence == pytest.approx(plain.confidence + BOOST_CONF)
    assert "no local Ollama models" in down.reason and "DGX Spark" not in down.reason
    # Fresh workstation: first_run and no_models both true, still one boost.
    fresh_ws = _pick("how do I get started?", context=_fresh_ctx(), history=[])
    assert fresh_ws.confidence == pytest.approx(plain.confidence + BOOST_CONF)
    # So one onboarding word no longer out-scores a specialist's own word
    # plus its own state boost: "slow" on a Spark is the tuner's.
    assert _pick("new box, why is it slow?", context=_spark_ollama_down_ctx(), history=[]).profile == "latency-tuner"


def test_trouble_words_veto_the_concierge_whatever_the_state(neutral_classifier) -> None:
    """Review 2026-09-02 (1): 'my gpu is broken, I just got my spark' reaches
    gpu-triage on a fresh Spark, with or without the gpu-missing finding."""
    for findings in (None, [{"id": "gpu-missing"}]):
        c = _pick("my gpu is broken, I just got my spark", context=_fresh_spark_ctx(),
                  history=[], findings=findings)
        assert c.profile == "gpu-triage", c.reason
    c = _pick("my gpu is broken, I just got this spark", context=_fresh_spark_ctx(), history=[])
    assert c.profile == "gpu-triage"
    c = _pick("the installer failed, where do I start", context=_fresh_spark_ctx(), history=[])
    assert c.profile == "install-medic"
    # The general-Wizard hint names the veto when nothing else matches.
    c = _pick("new here, the thing is broken", context=_fresh_ctx(), history=[])
    assert c.profile in {"install-medic", "gpu-triage"}


def test_one_phrase_scores_once_in_the_concierge(neutral_classifier) -> None:
    """Review 2026-09-02 (2): 'just installed nvhive' used to earn the
    keyword's 1.0 and the install pattern's 1.5 for the same three words."""
    c = _pick("just installed nvhive")
    assert c.profile == "setup-concierge"
    assert c.matched == ("re:just installed nvhive",)
    assert c.confidence == _pick("install nvhive on this box").confidence
    # ... and as a pattern the phrase beats the medic's one-word "installed",
    # where the old keyword only tied and lost.
    c = _pick("I just installed it, what now?")
    assert c.profile == "setup-concierge", c.reason
    # A keyword inside the "just got my ... spark" pattern is not counted twice.
    c = _pick("just got my new spark", context=_spark_ctx())
    assert c.profile == "setup-concierge"
    assert c.matched == ("re:just got my new spark",)
    # Keywords outside the pattern still count on top of it. (The pattern's
    # span now runs through the device adjective to the noun, "my dgx spark".)
    c = _pick("just got my dgx spark, how do I get started?")
    assert len(c.matched) == 2 and "get started" in c.matched
    assert any(m.startswith("re:just got my dgx") for m in c.matched), c.matched


def test_model_questions_on_a_new_box_are_the_model_desks(neutral_classifier) -> None:
    """Review 2026-09-02 (3): the 'just got my ... spark' pattern plus the
    stacked boosts used to steal model-selection questions on a fresh Spark."""
    c = _pick("just got my spark, which model should I pull first?", context=_fresh_spark_ctx(), history=[])
    assert c.profile == "model-sommelier", c.reason
    assert "no local Ollama models" in c.reason
    c = _pick("just got my spark, which 70B model fits?", context=_fresh_spark_ctx(), history=[])
    assert c.profile == "model-sommelier", c.reason
    # Pure sizing arithmetic with an onboarding preamble is the planner's.
    c = _pick("just got my spark, will a 70B fit in 128 GB?", context=_fresh_spark_ctx(), history=[])
    assert c.profile == "vram-planner", c.reason
    # The onboarding phrase alone still is the concierge's on the same box.
    c = _pick("just got my spark, what should I do first?", context=_fresh_spark_ctx(), history=[])
    assert c.profile == "setup-concierge"


# ---------------------------------------------------------------------------
# The model desk: sommelier (recommend / fit), planner (arithmetic), librarian (shelf)
# ---------------------------------------------------------------------------

MODEL_DESK_QUESTIONS = [
    # The sommelier: which model, what fits, MoE vs dense, quant, context.
    ("which model should I run for coding on this box?", "spark", "model-sommelier"),
    ("what fits on my spark?", "spark", "model-sommelier"),
    ("recommend a model for python coding", "ws", "model-sommelier"),
    ("best model for coding on this box", "spark", "model-sommelier"),
    ("MoE vs dense on the spark?", "spark", "model-sommelier"),
    ("which quant should I use, q4_k_m or q8_0?", "ws", "model-sommelier"),
    ("what context length should I use for coding?", "ws", "model-sommelier"),
    ("I have no models yet, what should I pull?", "fresh", "model-sommelier"),
    ("should I run qwen3:32b or llama3.1:70b for coding?", "spark", "model-sommelier"),
    ("which of my installed models is best for coding?", "ws", "model-sommelier"),
    ("new here, how do I run a model?", "fresh", "model-sommelier"),
    # The planner: sizing arithmetic only.
    ("will 70B Q4 fit in 24 GB?", "ws", "vram-planner"),
    ("how much memory does a 32B model need at 32k context?", "ws", "vram-planner"),
    ("how big a model can I run on a 3090?", "ws", "vram-planner"),
    ("does a 70B fit on a 3090?", "ws", "vram-planner"),
    ("is llama3.1:70b too big for my 3090?", "spark", "vram-planner"),
    ("ollama keeps getting OOM killed", "ws", "vram-planner"),
    # The librarian: the shelf.
    ("delete unused models", "ws", "model-librarian"),
    ("what's installed?", "ws", "model-librarian"),
    ("how much disk are my models taking?", "ws", "model-librarian"),
    ("ollama rm llama3.1:70b", "ws", "model-librarian"),
    ("what models are installed", "ws", "model-librarian"),
    ("which models do I have", "ws", "model-librarian"),
    ("free up disk space", "ws", "model-librarian"),
    ("ollama pull qwen3:8b", "ws", "model-librarian"),
]
_CONTEXTS = {"ws": _ctx, "spark": _spark_ctx, "fresh": _fresh_ctx}


@pytest.mark.parametrize(
    "q,which,expected", MODEL_DESK_QUESTIONS, ids=[q for q, _, _ in MODEL_DESK_QUESTIONS],
)
def test_model_desk_routes_recommend_size_and_shelf_questions(q, which, expected, neutral_classifier) -> None:
    c = _pick(q, context=_CONTEXTS[which](), history=[])
    assert c.profile == expected, c.reason
    assert c.confidence >= 0.5


def test_model_desk_rules_share_no_strong_keyword() -> None:
    rules = {p: _rule(p) for p in MODEL_DESK}
    for a in MODEL_DESK:
        for b in MODEL_DESK:
            if a < b:
                both = set(rules[a].keywords) & set(rules[b].keywords)
                assert not both, f"{a} and {b} both claim {sorted(both)}"
    # The sommelier carries the state; the planner is arithmetic and has none.
    assert rules["model-sommelier"].state == ("device:dgx-spark|device:rtx-spark|unified_memory", "no_models")
    assert rules["vram-planner"].state == () and rules["model-librarian"].state == ()
    assert all(r.phrase_once for r in rules.values())
    # Shelf verbs veto the sommelier so "which models can I delete" is the librarian's.
    assert {"delete", "remove", "unused", "disk"} <= set(rules["model-sommelier"].excludes)
    assert _pick("which models can I delete?").profile == "model-librarian"


def test_bare_model_tag_ties_to_the_sommelier(neutral_classifier) -> None:
    # A size token is the planner's pattern, a family+version the sommelier's,
    # a tag the librarian's: 1.5 each, and table order gives it to the sommelier.
    c = _pick("llama3.1:70b")
    assert c.profile == "model-sommelier"
    # One pattern (1.5) against the planner's two patterns (3.0: the fit
    # check "does a 70B fit" and the size token; "fit on" lies inside the
    # first and scores once): 1.5 points of score is 0.225 of confidence,
    # before two-place rounding.
    assert c.confidence == pytest.approx(_pick("does a 70B fit on a 3090?").confidence - 0.225, abs=0.011)
    # The words around the tag decide.
    assert _pick("ollama rm llama3.1:70b").profile == "model-librarian"
    assert _pick("is llama3.1:70b too big for my 3090?").profile == "vram-planner"
    assert _pick("is llama3.1:70b any good for coding?").profile == "model-sommelier"


def test_setup_selection_is_deterministic(neutral_classifier) -> None:
    kw = {"context": _fresh_spark_ctx(), "history": [], "profiles": ALL}
    q = "just got my dgx spark, how do I get started?"
    first = select_specialist(q, **kw)
    assert first.profile == "setup-concierge"
    for _ in range(5):
        assert select_specialist(q, **kw) == first
    assert select_specialist(q, **(kw | {"profiles": list(reversed(ALL))})) == first


def test_setup_concierge_routes_in_shipped_library(tmp_path, neutral_classifier) -> None:
    assert "setup-concierge" in available_specialists(tmp_path)
    c = select_specialist("how do I get started?", context=_fresh_ctx(), history=[], home_dir=tmp_path)
    assert c.profile == "setup-concierge"
    assert "get started" in c.matched


# ---------------------------------------------------------------------------
# Smart home (K1): routing requires a smart-home object
# ---------------------------------------------------------------------------

# The eight reproduced false positives, with where they belong instead. A set
# means either member is fine (the two profiles bind the same tools).
HA_FALSE_POSITIVES = {
    "how do I turn on GPU persistence mode": "gpu-triage",
    "turn on flash attention in ollama": {"model-librarian", "latency-tuner"},
    # 2026-09-03: enabling a *service* is a device setting (the settings
    # desk's enable/disable pattern). Teaching ssh — keys, scp, config — is
    # still the shell teacher's; see test_ssh_service_versus_ssh_keys.
    "how do I turn on ssh on the spark": "device-settings",
    "what's the temperature of my gpu": "gpu-triage",
    "light model for coding": "model-sommelier",  # a model pick, not a light
    "my dim sum recipe": None,
    "switch off the nvhive api server": None,
    "how do I turn off telemetry in ollama": "model-librarian",
    # "entities" is NER / database vocabulary as often as Home Assistant's.
    "extract entities from this text": None,
    "entities table schema": None,
}


@pytest.mark.parametrize("q,expected", sorted(HA_FALSE_POSITIVES.items(), key=lambda kv: kv[0]))
def test_generic_verbs_and_rooms_never_route_to_home_assistant(
    q: str, expected, neutral_classifier,
) -> None:
    c = _pick(q)
    assert c.profile != "home-assistant", c.reason
    if isinstance(expected, set):
        assert c.profile in expected, c.reason
    else:
        assert c.profile == expected, c.reason


@pytest.mark.parametrize("q", [
    "turn off the living room lights",
    "set the thermostat to 68",
    "is the garage door open",
    "light.kitchen state",
    "dim the lights",
    "lock the front door",
    "open the garage door",
    "what's the temperature in the bedroom",
    "start the vacuum",
    "list my home assistant entities",
])
def test_smart_home_objects_route_to_home_assistant(q: str) -> None:
    c = _pick(q)
    assert c.profile == "home-assistant", c.reason


def test_lights_route_to_home_assistant_in_shipped_library(tmp_path) -> None:
    assert "home-assistant" in available_specialists(tmp_path)
    c = select_specialist("turn off the living room lights", context=_ctx(), home_dir=tmp_path)
    assert c.profile == "home-assistant"
    assert "lights" in c.matched
    # The entity id alone is a smart-home object too.
    c = select_specialist("light.kitchen state", context=_ctx(), home_dir=tmp_path)
    assert c.profile == "home-assistant"
    assert any(m.startswith("re:light.kitchen") for m in c.matched)


def test_smart_home_outranks_generic_trouble_words() -> None:
    # "not working" is an install-medic word; the 1.2 weight keeps lights at home.
    assert _pick("the lights are not working").profile == "home-assistant"
    assert _pick("the lights don't work").profile == "home-assistant"


def test_home_assistant_rule_is_object_gated(neutral_classifier) -> None:
    rule = _rule("home-assistant")
    # Generic verbs, adjectives, rooms and question shells are not strong triggers.
    for generic in (
        "turn on", "turn off", "switch on", "switch off", "dim", "light", "outlet", "kitchen",
        "living room", "what's the temperature", "temperature in the", "is the door",
        "unlock the", "lock the", "heating", "garage", "scene", "automation", "entity", "entities",
    ):
        assert generic not in rule.keywords, generic
    # Rooms and NER / database nouns are weak: they count next to an object, never alone.
    assert {"kitchen", "living room", "bedroom", "entity", "entities"} <= set(rule.weak_keywords)
    for q in ("kitchen", "turn it on", "turn on", "dim", "living room", "the light in my office flickers"):
        assert _pick(q).profile is None, q
    # The verb-object pattern takes its object from the noun list.
    assert _pick("turn on the lamp").profile == "home-assistant"
    assert _pick("turn on the server").profile is None
    # Rig vocabulary is a veto.
    assert {"gpu", "cuda", "ollama", "model", "ssh", "telemetry", "flash attention",
            "api server", "nvhive"} <= set(rule.excludes)


def test_home_assistant_veto_beats_objects_and_continuity(neutral_classifier) -> None:
    # An object *and* a rig word: the rig wins.
    assert _pick("set the gpu temperature limit to 80").profile == "gpu-triage"
    assert _pick("turn on the gpu fan").profile == "gpu-triage"
    assert _pick("set fan speed to 80%").profile == "gpu-triage"
    # A previous smart-home turn does not drag a GPU question back home.
    c = _pick("how do I turn on GPU persistence mode", sticky="home-assistant")
    assert c.profile == "gpu-triage"
    # The general-Wizard reason names the veto so the UI trace explains it.
    c = _pick("switch off the nvhive api server")
    assert c.profile is None
    assert "home-assistant vetoed by 'nvhive'" in c.reason


def test_docker_shell_and_comfyui_routes() -> None:
    assert _pick("docker run --gpus all fails: nvidia runtime not found").profile == (
        "container-wrangler"
    )
    assert _pick("install docker for me").profile == "container-wrangler"
    assert _pick("bash: permission denied when I run ./start.sh").profile == "shell-teacher"
    assert _pick("how do I set up ssh keys on the spark?").profile == "shell-teacher"
    assert _pick("my comfyui workflow has a red missing node").profile == (
        "comfyui-workflow-debugger"
    )


# ---------------------------------------------------------------------------
# Weak keywords (K6): low-precision words need a second signal
# ---------------------------------------------------------------------------

# Questions a bare generic keyword used to hijack, with the rule that fires now
# (None: the general Wizard, which binds every tool).
K6_HIJACKS = {
    "install node": "install-medic",                       # was comfyui ("node")
    "my github workflow fails": "install-medic",           # was comfyui ("workflow")
    "the power adapter for my spark": None,                # was finetune-advisor ("adapter")
    "rtx 50 series power limit": "gpu-triage",             # was math-stepper ("series", "limit")
    "form factor of the spark": None,                      # was math-stepper ("factor")
    "pci express lanes on the spark": None,                # was backend-implementer ("express")
    "what parameters does this function take": None,       # was vram-planner ("parameters")
    "review my ollama config": "model-librarian",          # was deep-reviewer ("review my")
    "review my setup": None,                               # was deep-reviewer
    "I can't remember the port for the api server": None,  # was daily-notes-coach ("remember")
    "energy efficiency of the spark": None,                # was science-explainer ("energy")
    "the token limit": None,                               # was provider-keysmith ("token")
    "windows registry key for cuda path": "gpu-triage",    # was container-wrangler ("registry")
    "export the model to onnx": None,                      # was shell-teacher ("export")
    "what is a spark": None,                               # was code-tutor ("what is a")
    "machine learning on the spark": None,                 # was code-tutor ("learning")
    "the cpu scheduler": None,                             # was comfyui ("scheduler")
    "random seed for the run": None,                       # was comfyui ("seed")
    "the secret to fast inference": None,                  # was provider-keysmith ("secret")
    "what's the latest driver for my gpu": "gpu-triage",
    "compare q4 vs q8": "model-sommelier",                # quant trade-offs are the sommelier's
    "benchmark my gpu": "gpu-triage",
    "verify my api key": "provider-keysmith",
    "last week my gpu died": "gpu-triage",
    "help me make a decision on which gpu": "gpu-triage",
}


@pytest.mark.parametrize("q,expected", sorted(K6_HIJACKS.items(), key=lambda kv: kv[0]))
def test_bare_generic_keywords_do_not_hijack_rig_questions(q, expected, neutral_classifier) -> None:
    c = _pick(q)
    assert c.profile == expected, c.reason


def test_weak_keyword_counts_beside_a_strong_keyword_or_pattern(neutral_classifier) -> None:
    # "latest" alone is nothing; next to "news" it counts and lifts confidence.
    assert _pick("what's the latest on this?").profile is None
    c = _pick("what's the latest news on the RTX Spark launch?")
    assert c.profile == "deep-researcher"
    assert {"news", "latest"} <= set(c.matched)
    # "solve" alone is nothing; next to an equation pattern it counts.
    assert _pick("solve it for me").profile is None
    c = _pick("solve 2x + 5 = 15")
    assert c.profile == "math-stepper"
    assert "solve" in c.matched
    # "review this" alone is nothing; next to "diff" it is a review.
    assert _pick("review this patch").profile is None
    c = _pick("review this PR diff please")
    assert c.profile == "deep-reviewer"
    assert {"diff", "review this"} <= set(c.matched)


def test_weak_keyword_counts_with_continuity_but_not_state_or_classifier(monkeypatch) -> None:
    _fixed_classifier(monkeypatch, TaskType("conversation"), 0.0)
    # Continuity is a second signal: the researcher keeps a weak follow-up.
    c = _pick("and compare the latest?", sticky="deep-researcher")
    assert c.profile == "deep-researcher"
    assert {"compare", "latest"} <= set(c.matched)
    assert "continuing from the previous turn" in c.reason
    # State is not: device:dgx-spark is true on every Spark turn, so it must
    # not turn the planner's weak "parameters" into a strong one.
    assert _pick("what parameters does this function take", context=_spark_ctx()).profile is None
    # The classifier is not: it reads the same words. A confident MATH label
    # does not count "vector" in tier 1; the residue tier takes it on its own
    # terms, with its own confidence and label.
    _fixed_classifier(monkeypatch, TaskType.MATH, 0.9)
    c = _pick("vector database on the spark")
    assert c.matched == ("task:math",)
    assert c.confidence == RESIDUE_CONFIDENCE


def test_low_confidence_classifier_earns_no_tier_one_bonus(monkeypatch) -> None:
    # Below the residue floor the classifier is noise: a one-word tie between
    # the librarian ("ollama") and the reviewer ("review my", weak) must not
    # be tipped by a 0.29 code_review label.
    _fixed_classifier(monkeypatch, TaskType("code_review"), 0.29)
    assert _pick("review my ollama config").profile == "model-librarian"
    # Above the floor it is a bonus for a rule that already matched words.
    _fixed_classifier(monkeypatch, TaskType("code_review"), 0.6)
    c = _pick("check my code and the diff")
    assert c.profile == "deep-reviewer"
    assert "task type code_review" in c.reason


def test_generic_rooms_and_nouns_lift_confidence_next_to_an_object() -> None:
    bare = _pick("turn off the lights")
    roomed = _pick("turn off the kitchen lights")
    assert bare.profile == roomed.profile == "home-assistant"
    assert roomed.confidence > bare.confidence
    assert "kitchen" in roomed.matched


def test_entities_is_weak_and_counts_only_beside_a_smart_home_signal(neutral_classifier) -> None:
    """Regression: "entities" used to be a strong keyword, so generic NER and
    database questions went to the smart-home specialist."""
    for q in ("extract entities from this text", "entities table schema", "named entities in the article"):
        c = _pick(q)
        assert c.profile is None, c.reason
        assert "weak 'entities' needs a second signal" in c.reason
    # Beside a platform word it still counts and lifts confidence.
    c = _pick("list my home assistant entities")
    assert c.profile == "home-assistant"
    assert {"home assistant", "entities"} <= set(c.matched)
    assert c.confidence > _pick("list my home assistant").confidence


def test_spark_note_names_the_pool_size_only_when_context_supplies_it() -> None:
    """The DGX Spark note must not hard-code a memory figure: it says
    'unified memory shared with the OS' unless ``memory_total_gb`` is known."""
    sized = _pick("which model fits my spark?", context=_spark_ctx())
    assert sized.profile == "model-sommelier"
    assert "128 GB unified memory" in sized.reason and "MemAvailable" in sized.reason

    unsized_ctx = _spark_ctx()
    unsized_ctx["platform"] = {k: v for k, v in unsized_ctx["platform"].items() if k != "memory_total_gb"}
    unsized_ctx["gpu"] = {"detected": True, "unified_memory": True}
    unsized = _pick("which model fits my spark?", context=unsized_ctx)
    assert unsized.profile == "model-sommelier"
    assert "unified memory shared with the OS" in unsized.reason
    assert "MemAvailable" in unsized.reason
    assert not re.search(r"\d+\s?GB", unsized.reason), unsized.reason


# ---------------------------------------------------------------------------
# Coding / research / notes / tutors
# ---------------------------------------------------------------------------


def test_code_fence_with_bug_routes_to_a_coding_profile() -> None:
    c = _pick(CODE_BUG)
    assert c.profile in CODING_PROFILES
    assert c.profile == "bug-hunter"
    assert "re:```" in c.matched


def test_implementation_request_routes_to_backend_implementer() -> None:
    assert _pick("write a python function that sorts a list of dicts by key").profile == (
        "backend-implementer"
    )
    assert _pick("review this PR diff please").profile == "deep-reviewer"


def test_bare_coding_request_falls_through_to_classifier_residue(monkeypatch) -> None:
    # No keyword hit; the classifier says code_generation with room to spare.
    _fixed_classifier(monkeypatch, TaskType("code_generation"), 0.6)
    c = _pick("ok now write the function")
    assert c.profile == "backend-implementer"
    assert c.matched == ("task:code_generation",)
    assert c.confidence < 0.5
    assert "task classifier says code_generation (0.60)" in c.reason
    # Below the floor the residue tier stays silent and the Wizard answers.
    _fixed_classifier(monkeypatch, TaskType("code_generation"), 0.2)
    assert _pick("ok now write the function").profile is None


def test_research_and_fact_check_routes() -> None:
    assert _pick("what's the latest news on the RTX Spark launch?").profile == "deep-researcher"
    assert _pick("is it true that apt upgrade breaks the driver?").profile == "fact-checker"
    assert _pick("summarise https://example.com/post for me").profile == "deep-researcher"


def test_recall_question_routes_to_notes_profile() -> None:
    c = _pick("what did we decide about the vault layout last week?")
    assert c.profile in {"vault-rag", "daily-notes-coach"}
    assert c.profile == "vault-rag"
    # Capture intent goes to the coach even when the payload is technical.
    c = _pick("remember this: the docker group fix is usermod -aG docker $USER")
    assert c.profile == "daily-notes-coach"


def test_tutor_split_by_domain() -> None:
    assert _pick("explain how recursion works").profile == "code-tutor"
    assert _pick("explain why the sky is blue").profile == "science-explainer"
    assert _pick("teach me about entropy").profile == "science-explainer"
    c = _pick("solve 2x + 5 = 15 step by step")
    assert c.profile == "math-stepper"
    assert _pick("according to the document, what is the warranty period?").profile == "doc-qa"


# ---------------------------------------------------------------------------
# General persona: greetings, chit-chat, no signal
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("q", ["hi", "Hello there!", "thanks!", "thank you so much", "ok", ""])
def test_greetings_and_empty_return_general_wizard(q: str) -> None:
    c = _pick(q)
    assert c.profile is None
    assert c.reason.startswith("general Wizard")


@pytest.mark.parametrize("q", [
    "what is the capital of France?",
    "tell me a joke",
    "translate this to spanish: hello",
    "can you help me?",
])
def test_no_signal_returns_general_wizard(q: str) -> None:
    from nvh.integrations.wizard.concierge import GENERAL_NO_MATCH_REASON

    c = _pick(q)
    assert c.profile is None
    # The chat envelope carries this as ``profile_reason`` and the UI shows
    # it as the attribution tooltip: a short human sentence, not a code.
    assert c.reason.startswith("general Wizard: no specialist matched")
    assert c.reason.startswith(GENERAL_NO_MATCH_REASON)


def test_every_general_wizard_reason_is_a_short_human_sentence() -> None:
    """Greeting, empty question, no match and the near-miss hints all read as
    ``general Wizard: <why>``; nothing longer than a tooltip line."""
    cases = {
        "hi": "general Wizard: greeting or chit-chat",
        "": "general Wizard: empty question",
        "tell me a joke": "general Wizard: no specialist matched",
    }
    for q, expected in cases.items():
        c = _pick(q)
        assert c.profile is None and c.reason.startswith(expected), (q, c.reason)
        assert len(c.reason) < 160, c.reason
    # No specialist profiles at all: still a sentence, still the same prefix.
    none = _pick("what is the capital of France?", profiles=())
    assert none.profile is None and none.reason == "general Wizard: no specialist profiles available"


# ---------------------------------------------------------------------------
# Stickiness / continuity (K3)
# ---------------------------------------------------------------------------


def test_stickiness_keeps_previous_specialist_on_weak_followup() -> None:
    history = [
        {"role": "user", "content": "nvidia-smi says no devices"},
        {"role": "assistant", "content": "Let's check the driver.", "used_profile": "gpu-triage"},
    ]
    c = _pick("and then?", history=history)
    assert c.profile == "gpu-triage"
    assert c.matched == ("sticky:gpu-triage",)
    # Explicit kwarg wins over history.
    c = _pick("and then?", history=history, sticky="install-medic")
    assert c.profile == "install-medic"


def test_history_used_profile_drives_tier_three_continuity(neutral_classifier) -> None:
    history = [
        {"role": "user", "content": "nvidia-smi says no devices"},
        {"role": "assistant", "content": "Let's check the driver.", "used_profile": "gpu-triage"},
        {"role": "user", "content": "ok"},  # a trailing user turn is skipped
    ]
    c = _pick("and then?", history=history)
    assert c.profile == "gpu-triage"
    assert c.matched == ("sticky:gpu-triage",)
    assert c.confidence == STICKY_CONFIDENCE
    assert "continuing from the previous turn" in c.reason
    # The most recent assistant entry wins over older ones.
    history += [
        {"role": "assistant", "content": "Try pulling a model.", "used_profile": "model-librarian"},
    ]
    assert _pick("and then?", history=history).profile == "model-librarian"


def test_history_without_used_profile_carries_nothing(neutral_classifier) -> None:
    history = [
        {"role": "user", "content": "nvidia-smi says no devices"},
        {"role": "assistant", "content": "Let's check the driver."},
    ]
    assert _pick("and then?", history=history).profile is None
    # The general Wizard having answered (None / "") is not a specialist either.
    for empty in (None, ""):
        history[-1]["used_profile"] = empty
        assert _pick("and then?", history=history).profile is None
    # Malformed entries are ignored rather than raising.
    assert _pick("and then?", history=["not a mapping", 42]).profile is None


def test_strong_new_signal_overrides_stickiness() -> None:
    c = _pick("ok, now turn off the kitchen lights", sticky="gpu-triage")
    assert c.profile == "home-assistant"


def test_sticky_unknown_profile_is_ignored() -> None:
    c = _pick("and then?", sticky="no-such-profile")
    assert c.profile is None


# ---------------------------------------------------------------------------
# Rule table hygiene
# ---------------------------------------------------------------------------


def test_missing_profiles_drop_rules_without_error() -> None:
    only_medic = _profiles("install-medic", "wizard")
    assert available_specialists(profiles=only_medic) == ("install-medic",)
    c = select_specialist("turn off the living room lights", profiles=only_medic, context=_ctx())
    assert c.profile is None
    c = select_specialist(TRACEBACK, profiles=only_medic, context=_ctx())
    assert c.profile == "install-medic"


def test_requires_tools_drops_rule_when_whitelist_lacks_tool() -> None:
    vault_rule = _rule("vault-rag")
    assert "rag_ask_vault" in vault_rule.requires_tools
    profiles = _profiles("vault-rag", **{"vault-rag": {"tools_allowed": ["web_search"]}})
    assert available_specialists(profiles=profiles) == ()
    profiles = _profiles("vault-rag", **{"vault-rag": {"tools_allowed": ["rag_ask_vault"]}})
    assert available_specialists(profiles=profiles) == ("vault-rag",)


def test_every_rule_profile_ships_in_the_agent_library(tmp_path) -> None:
    shipped = set(available_specialists(tmp_path))
    missing = set(RULE_PROFILES) - shipped
    assert not missing, f"rules point at profiles that do not ship: {sorted(missing)}"
    assert {"home-assistant", "setup-concierge", "model-sommelier"} <= shipped


def test_rule_table_is_well_formed() -> None:
    for r in SPECIALIST_RULES:
        assert isinstance(r, SpecialistRule)
        assert r.keywords or r.patterns, f"{r.profile} has no strong text trigger"
        assert r.weight > 0
        for group in (r.keywords, r.weak_keywords, r.excludes):
            assert all(k == k.lower() for k in group), f"{r.profile}: keywords must be lowercase"
        both = set(r.keywords) & set(r.weak_keywords)
        assert not both, f"{r.profile}: strong and weak at once: {sorted(both)}"
        clash = set(r.excludes) & (set(r.keywords) | set(r.weak_keywords))
        assert not clash, f"{r.profile}: excluded and matched at once: {sorted(clash)}"
        # State expressions: predicates joined by | and &, no empty member,
        # and no predicate repeated across a rule's expressions as a bare
        # entry (that is the stacking the concierge review caught).
        bare = [expr for expr in r.state if "|" not in expr and "&" not in expr]
        assert len(bare) == len(set(bare)), f"{r.profile}: duplicate state predicate"
        for expr in r.state:
            members = [p for alt in expr.split("|") for p in alt.split("&")]
            assert all(m.strip() and m == m.strip() for m in members), f"{r.profile}: bad state {expr!r}"


def test_state_expressions_fire_any_of_and_all_of(neutral_classifier) -> None:
    """``a|b`` is one boost when either is true; ``a&b`` needs both. The
    reason lists every true member once, however many expressions name it."""
    from nvh.integrations.wizard import concierge as mod

    rule = SpecialistRule(
        profile="probe", keywords=("probe me",),
        state=("first_run|no_models", "first_run&device:dgx-spark", "no_models"),
    )

    # ``select_specialist`` has no ``rules=`` knob, so score the synthetic
    # rule through the scorer with the module's own state derivation.
    def score(ctx, history=None):
        state = derive_state(ctx, None, history)
        s = mod._score_rule(rule, "probe me", "probe me", state, None, None)
        return round(s.score, 2), tuple(s.states)

    assert score(_ctx()) == (1.0, ())  # nothing true: text only
    # no_models alone: the any-of group and the bare entry fire, the all-of does not.
    assert score(_spark_ollama_down_ctx()) == (round(1.0 + 2 * STATE_BOOST, 2), ("no_models",))
    # first_run and no_models both true: the any-of group is still one boost.
    assert score(_fresh_ctx(), history=[]) == (round(1.0 + 2 * STATE_BOOST, 2), ("first_run", "no_models"))
    # ... and the all-of group fires only when the device joins a first run.
    assert score(_fresh_spark_ctx(), history=[]) == (
        round(1.0 + 3 * STATE_BOOST, 2), ("first_run", "no_models", "device:dgx-spark"),
    )
    assert score(_spark_ctx()) == (1.0, ())  # a lived-in Spark: neither group


def test_derive_state_predicates() -> None:
    state = derive_state(
        _fresh_ctx() | {"platform": {"device_class": "dgx-spark", "unified_memory": True}},
        findings=[{"id": "gpu-missing"}, {"id": "job-failed-42"}, {"id": "provider-unhealthy-groq"}],
        history=[],
    )
    assert {"gpu-missing", "gpu_missing", "job-failed", "failed_job", "provider_unhealthy",
            "no_models", "no_providers", "device:dgx-spark", "unified_memory", "first_run"} <= state
    # first_run needs an empty history.
    assert "first_run" not in derive_state(_fresh_ctx(), history=[{"role": "user", "content": "x"}])
    # No context -> only finding-derived predicates.
    assert derive_state(None, findings=[{"id": "no-local-models"}]) == {
        "no-local-models", "no_local_models",
    }


# ---------------------------------------------------------------------------
# resolve_auto_profile: the chat.py hook (K2)
# ---------------------------------------------------------------------------


def test_explicit_pin_bypasses_selection() -> None:
    assert resolve_auto_profile("coder", TRACEBACK, profiles=ALL) == ("coder", None)
    assert resolve_auto_profile("home-assistant", "hi", profiles=ALL) == ("home-assistant", None)


def test_wizard_is_an_explicit_pin_of_the_general_persona() -> None:
    # Decision (1): AUTO names are None, "" and "auto" only. Pinning "wizard"
    # opts out of specialist routing even when the question screams install.
    assert resolve_auto_profile("wizard", TRACEBACK, profiles=ALL, context=_ctx()) == ("wizard", None)
    assert resolve_auto_profile("Wizard", TRACEBACK, profiles=ALL, context=_ctx()) == ("Wizard", None)
    assert AUTO_PROFILE == "auto"


@pytest.mark.parametrize("requested", [None, "", AUTO_PROFILE, " Auto "])
def test_auto_names_run_selection(requested) -> None:
    name, choice = resolve_auto_profile(requested, TRACEBACK, profiles=ALL, context=_ctx())
    assert name == "install-medic"
    assert isinstance(choice, SpecialistChoice)
    assert choice.profile == name


def test_auto_with_no_signal_yields_general_wizard() -> None:
    name, choice = resolve_auto_profile("auto", "hi", profiles=ALL)
    assert name is None
    assert choice is not None and choice.profile is None


# ---------------------------------------------------------------------------
# Determinism / purity / budget
# ---------------------------------------------------------------------------


def test_selection_is_deterministic() -> None:
    kw = {"context": _spark_ctx(), "findings": [{"id": "gpu-missing"}], "profiles": ALL}
    first = select_specialist("which model fits and why is it slow?", **kw)
    for _ in range(5):
        assert select_specialist("which model fits and why is it slow?", **kw) == first
    # Profile order and dict key order must not matter.
    shuffled = list(reversed(ALL))
    assert select_specialist("which model fits and why is it slow?", **(kw | {"profiles": shuffled})) == first


def test_selection_stays_within_the_latency_budget() -> None:
    # ~3 ms per turn is the design budget (measured ~0.3 ms); the guard is
    # loose enough for a busy CI runner and tight enough to catch a regex blow-up.
    questions = list(HA_FALSE_POSITIVES) + list(K6_HIJACKS) + [TRACEBACK, CODE_BUG, "and then?"]
    for q in questions:  # warm the compiled-regex cache
        _pick(q)
    start = time.perf_counter()
    rounds = 3
    for _ in range(rounds):
        for q in questions:
            _pick(q)
    per_call_ms = (time.perf_counter() - start) * 1000 / (rounds * len(questions))
    assert per_call_ms < 15.0, f"{per_call_ms:.2f} ms per selection"


def test_choice_is_frozen() -> None:
    c = _pick(TRACEBACK)
    with pytest.raises(Exception):
        c.profile = "x"  # type: ignore[misc]
    assert c.confidence <= 1.0
    assert MIN_SCORE == 1.0


# ---------------------------------------------------------------------------
# Review 2026-09-02 (C1-C3): derived trouble vetoes, sized recommendations,
# shelf + size, and the signal-count tie-break
# ---------------------------------------------------------------------------

# Trouble phrases the medic / triage own as strong keywords or patterns, each
# beside an onboarding phrase. On a fresh Spark the concierge's pattern (1.5)
# plus two state boosts (1.2) used to out-score the rig doctor's one word.
TROUBLE_BESIDE_ONBOARDING = {
    "just got my spark, connection refused when I open the UI": "install-medic",
    "new spark, ollama isn't running": "install-medic",
    "getting started but port already in use": "install-medic",
    "just unboxed my dgx, no module named nvh": "install-medic",
    "fresh install, it won't boot": "install-medic",
    "just got my spark, exit code 1 from the installer": "install-medic",
    "brand new box, ollama is not running": "install-medic",
    "just got my spark, ModuleNotFoundError when I run nvh": "install-medic",
    "just got my spark, dmesg shows xid 79": "gpu-triage",
    "brand new box and it's already throttling": "gpu-triage",
}


@pytest.mark.parametrize(
    "q,expected", sorted(TROUBLE_BESIDE_ONBOARDING.items(), key=lambda kv: kv[0]),
)
def test_trouble_signals_beat_onboarding_boosts_on_a_fresh_spark(q, expected, neutral_classifier) -> None:
    for ctx in (_fresh_spark_ctx(), _spark_ollama_down_ctx(), _fresh_ctx(), _ctx()):
        c = _pick(q, context=ctx, history=[])
        assert c.profile == expected, (q, c.reason)


def test_concierge_vetoes_derive_from_the_rig_doctor() -> None:
    """C1: the veto set is the medic's and the triage's own strong keywords
    and patterns, not a hand list, minus the install verbs the concierge
    binds to nvhive in its own patterns."""
    from nvh.integrations.wizard import concierge as mod

    rule = _rule("setup-concierge")
    medic, triage = _rule("install-medic"), _rule("gpu-triage")
    claimed = set(mod._CONCIERGE_CLAIMED_VERBS)
    assert claimed == {"install", "installed", "installing"}
    assert (set(medic.keywords) | set(triage.keywords)) - claimed <= set(rule.excludes)
    assert not claimed & set(rule.excludes)
    # The carve-out is justified by the concierge's own patterns naming them.
    for verb in claimed:
        assert any(verb in p for p in rule.patterns), verb
    assert set(medic.patterns) | set(triage.patterns) <= set(rule.exclude_patterns)
    # Trouble phrases the old hand list missed are vetoes now.
    for phrase in (
        "connection refused", "port already in use", "no module named", "won't boot",
        "exit code", "isn't running", "is not running", "xid", "throttling",
    ):
        assert phrase in rule.excludes, phrase
    # ... while the bound install phrases still route to the concierge.
    assert _pick("install nvhive on this box").profile == "setup-concierge"
    fresh = _pick("just installed nvhive", context=_fresh_spark_ctx(), history=[])
    assert fresh.profile == "setup-concierge"
    # A medic keyword added tomorrow is a concierge veto the same day.
    words, patterns = mod._veto_vocabulary(
        (
            SpecialistRule(profile="a", keywords=("boom", "install")),
            SpecialistRule(profile="b", keywords=("boom",), patterns=(r"\bkaboom\b",)),
        ),
        claimed=("install",),
    )
    assert words == ("boom",) and patterns == (r"\bkaboom\b",)


def test_exclude_patterns_veto_like_keywords(neutral_classifier) -> None:
    from nvh.integrations.wizard import concierge as mod

    rule = SpecialistRule(
        profile="probe", keywords=("probe me",), exclude_patterns=(r"\bxid\b\s*\d+",),
    )
    ok = mod._score_rule(rule, "probe me", "probe me", frozenset(), None, None)
    assert ok.score == 1.0 and ok.vetoed_by is None
    vetoed = mod._score_rule(rule, "probe me, XID 79", "probe me, xid 79", frozenset(), None, None)
    assert vetoed.score == 0.0 and vetoed.vetoed_by == "re:xid 79"
    # In the table: an Xid beside an onboarding word is the triage's.
    c = _pick("new here, dmesg shows xid 79", context=_fresh_ctx(), history=[])
    assert c.profile == "gpu-triage", c.reason


# C2: recommendation questions that name the model by size or family.
SIZED_OR_NAMED_RECOMMENDATIONS = [
    "which 70b should i run",
    "is a 30b moe worth it here",
    "should i pull the 120b",
    "what 30b would you recommend",
    "should I run the 120b or the 20b?",
    "which 70b should I run on my spark?",
    "which qwen3 should I run",
    "should I pull gpt-oss",
    "is nemotron worth it here",
    "is gemma3 good for chat",
]


@pytest.mark.parametrize("q", SIZED_OR_NAMED_RECOMMENDATIONS)
def test_sized_or_named_recommendations_are_the_sommeliers(q, neutral_classifier) -> None:
    ws = _pick(q, context=_ctx(), history=[])
    assert ws.profile == "model-sommelier", ws.reason
    assert ws.confidence >= 0.5
    # On a Spark the sommelier's device boost applies, which the planner's
    # bare size-token pattern never carried.
    spark = _pick(q, context=_spark_ctx(), history=[])
    assert spark.profile == "model-sommelier", spark.reason
    assert spark.confidence > ws.confidence and "DGX Spark" in spark.reason


@pytest.mark.parametrize("q", [
    "will 70B Q4 fit in 24 GB?",
    "how much memory does a 32B model need at 32k context?",
    "does a 70B fit on a 3090?",
    "is llama3.1:70b too big for my 3090?",
    "what 70b can I run on 24 gb",
])
def test_sizing_arithmetic_stays_with_the_planner(q, neutral_classifier) -> None:
    for ctx in (_ctx(), _spark_ctx()):
        c = _pick(q, context=ctx, history=[])
        assert c.profile == "vram-planner", c.reason


# C3: shelf questions that mention a size or a family.
SHELF_WITH_A_SIZE = [
    "delete the 70b models I never use",
    "how much disk do my 30b models take",
    "remove the 120b",
    "prune my 70b models",
    "uninstall the 30b",
    "list installed 70b models",
    "what's installed under 30b",
    "get rid of qwen3",
    "delete gemma3 and pull the 30b",
]


@pytest.mark.parametrize("q", SHELF_WITH_A_SIZE)
def test_shelf_questions_with_a_size_are_the_librarians(q, neutral_classifier) -> None:
    for ctx in (_ctx(), _spark_ctx(), _fresh_ctx()):
        c = _pick(q, context=ctx, history=[])
        assert c.profile == "model-librarian", c.reason
        assert c.confidence >= 0.5


def test_shelf_vocabulary_vetoes_the_planner_and_the_sommelier() -> None:
    from nvh.integrations.wizard import concierge as mod

    planner, sommelier = _rule("vram-planner"), _rule("model-sommelier")
    assert set(mod._SHELF_VETOES) <= set(planner.excludes)
    assert set(mod._SHELF_VETOES) <= set(sommelier.excludes)
    assert set(mod._SHELF_LISTING) | set(mod._DISK_SHELF_PHRASES) <= set(planner.excludes)
    # Listing is not a sommelier veto: "which of my installed models is best".
    assert not set(mod._SHELF_LISTING) & set(sommelier.excludes)
    assert _pick("which of my installed models is best for coding?").profile == "model-sommelier"
    # Bare "disk" vetoes the sommelier, not the planner: "spill to disk" is
    # sizing talk, "how much disk" is the shelf's.
    assert "disk" in sommelier.excludes and "disk" not in planner.excludes
    assert _pick("does the 70b fit in vram or spill to disk?").profile == "vram-planner"
    assert _pick("how much disk does the 70b take?").profile == "model-librarian"
    # Bare shelf verbs stay weak for the librarian: "delete my api key" and
    # "delete this file" are not shelf questions.
    assert {"delete", "remove", "prune"} <= set(_rule("model-librarian").weak_keywords)
    assert _pick("delete my api key").profile == "provider-keysmith"
    assert _pick("delete this file").profile != "model-librarian"


def test_ties_resolve_by_distinct_signals_then_table_order() -> None:
    from nvh.integrations.wizard import concierge as mod

    def scored(name: str, score: float, *matched: str) -> mod._Scored:
        return mod._Scored(rule=SpecialistRule(profile=name, keywords=(name,)), score=score, matched=list(matched))

    a = scored("a", 1.5, "re:x")
    b = scored("b", 1.5, "y", "z")
    c = scored("c", 1.5, "re:w")
    assert mod._best([a, b, c]) is b          # more signals for the same score
    assert mod._best([a, c]) is a             # equal signals: table order
    assert mod._best([c, a]) is c
    higher = scored("d", 1.6, "v")
    assert mod._best([a, b, higher]) is higher   # score first, always
    # A lead below the rounding grain (1e-6) is float noise from summing the
    # same weights in a different order, not a lead: the signal count decides.
    # Real scores never sit that close -- every weight step is >= 0.1.
    noisy = scored("e", 1.5 + 1e-9, "u")
    exact = scored("f", 1.5, "t", "s")
    assert noisy.score != 1.5 and mod._best([noisy, exact]) is exact
    assert mod._best([scored("g", 1.6, "u"), exact]).rule.profile == "g"


def test_new_rule_fields_are_well_formed() -> None:
    for r in SPECIALIST_RULES:
        for p in r.exclude_patterns:
            re.compile(p)
        assert not set(r.exclude_patterns) & set(r.patterns), f"{r.profile}: pattern is also a veto"
    assert _rule("setup-concierge").exclude_patterns
    assert not _rule("install-medic").exclude_patterns


# ---------------------------------------------------------------------------
# Review 2026-09-02 (F1-F5): claim-shaped fact checks, family-named sizing,
# the set-up object, fetch verbs, weak device nouns -- and the routing probe
# ---------------------------------------------------------------------------

_ALL_CONTEXTS = _CONTEXTS | {"fresh_spark": _fresh_spark_ctx}


def _pick_on(q: str, which: str) -> SpecialistChoice:
    return _pick(q, context=_ALL_CONTEXTS[which](), history=[])


# F1: a bare "really" is emphasis, not a claim. Each rig sentence names where
# it goes instead; none of them is a fact check on any box.
RIG_SENTENCES_ARE_NOT_CLAIMS = {
    "my gpu is running really hot": "gpu-triage",
    "the fans are spinning really loud": "gpu-triage",
    "is the driver really broken or is it me?": "gpu-triage",
    "does my 70b really fit in 128 gb?": "vram-planner",
    "can ollama really use the full 128 gb?": "vram-planner",
    "is it true that a 70b fits in 128 gb?": "vram-planner",
    "does it really matter which quant I use?": "model-sommelier",
}
CLAIMS_ARE_FACT_CHECKS = [
    "is it true that apt upgrade breaks the driver?",
    "is that really true?",
    "is this actually the case?",
    "did they really say that?",
    "fact check this: the spark has 256 GB of memory",
    "true or false: the spark has 128 gb",
    "is this legit: https://example.com/spark-256gb",
    'is this accurate? "the gb10 has 20 arm cores"',
]


@pytest.mark.parametrize("q,expected", sorted(RIG_SENTENCES_ARE_NOT_CLAIMS.items()))
def test_rig_sentences_with_really_are_not_fact_checks(q, expected, neutral_classifier) -> None:
    """F1: the loose '(is|are|does) ... really' pattern (1.5) out-scored one rig
    keyword (1.0), so a hot GPU on a Spark went to the fact-checker."""
    for which in ("ws", "spark", "fresh_spark"):
        c = _pick_on(q, which)
        assert c.profile != "fact-checker", (q, which, c.reason)
        assert c.profile == expected, (q, which, c.reason)


@pytest.mark.parametrize("q", CLAIMS_ARE_FACT_CHECKS)
def test_claim_shaped_questions_stay_with_the_fact_checker(q, neutral_classifier) -> None:
    for which in ("ws", "spark"):
        c = _pick_on(q, which)
        assert c.profile == "fact-checker", (q, which, c.reason)
        assert c.confidence >= 0.5


def test_fact_checker_needs_a_claim_object_and_vetoes_rig_symptoms(neutral_classifier) -> None:
    rule = _rule("fact-checker")
    # Rig symptoms and sizing words veto; device nouns do not ("is it true
    # that apt upgrade breaks the driver?" is a claim about the driver).
    assert {"running hot", "throttling", "xid", "broken", "not working", "fit", "vram", "oom"} <= set(rule.excludes)
    assert not {"gpu", "driver", "cuda", "spark", "model"} & set(rule.excludes)
    # "did they really" / "does it really" are emphasis until a claim pattern joins them.
    assert {"did they really", "does it really"} <= set(rule.weak_keywords)
    assert _pick("does it really matter?").profile is None
    c = _pick("did they really say that?")
    assert c.profile == "fact-checker" and "did they really" in c.matched
    # A bare "really" beside an auxiliary is no pattern hit at all.
    c = _pick("are they really coming tonight?")
    assert c.profile is None and "fact-checker" not in c.reason, c.reason


def test_loud_fans_are_the_rigs_and_switched_fans_the_homes(neutral_classifier) -> None:
    assert _pick_on("the fans are spinning really loud", "spark").profile == "gpu-triage"
    assert _pick("fan is screaming at 100%").profile == "gpu-triage"
    assert _pick("turn on the ceiling fan").profile == "home-assistant"
    assert _pick("are the fans on?").profile == "home-assistant"


# F2: sizing asked of a named model is the planner's, on a workstation and on
# a Spark; a recommendation shell around the same names stays the sommelier's.
FAMILY_NAMED_SIZING = [
    "how much vram does qwen3:32b need?",
    "will llama3.1:70b fit on my card?",
    "does qwen3:32b fit in 128 gb?",
    "enough vram for gemma3:27b?",
    "is the 120b too big for my spark?",
    "how much memory for llama3.1:70b?",
    "can I run qwen3:32b on my spark?",
    "is the model too big for 24 gb?",
]
FAMILY_NAMED_PICKS = [
    "which qwen3 should I run on my spark?",
    "should I pull the 120b or the 20b?",
    "is gemma3 good for chat?",
    "what fits on my spark?",
    "which model fits my 128 GB spark?",
    "which model would fit my spark?",
    "should I run qwen3:32b or llama3.1:70b for coding?",
]


@pytest.mark.parametrize("q", FAMILY_NAMED_SIZING)
def test_family_named_sizing_is_the_planners_even_on_a_spark(q, neutral_classifier) -> None:
    """F2: the sommelier's family + version pattern (1.5) beat the planner's
    keywords (1.0) and the Spark boost flipped the ties, against the module
    docstring that gives the planner sizing arithmetic."""
    for which in ("ws", "spark", "fresh_spark"):
        c = _pick_on(q, which)
        assert c.profile == "vram-planner", (q, which, c.reason)
        assert c.confidence >= 0.5


@pytest.mark.parametrize("q", FAMILY_NAMED_PICKS)
def test_recommendation_shells_stay_with_the_sommelier(q, neutral_classifier) -> None:
    for which in ("ws", "spark"):
        c = _pick_on(q, which)
        assert c.profile == "model-sommelier", (q, which, c.reason)


def test_arithmetic_phrases_veto_the_sommelier_but_not_bare_fit() -> None:
    from nvh.integrations.wizard import concierge as mod

    sommelier, planner = _rule("model-sommelier"), _rule("vram-planner")
    assert set(mod._ARITHMETIC_VETOES) <= set(sommelier.excludes)
    assert mod._FIT_CHECK in sommelier.exclude_patterns and mod._FIT_CHECK in planner.patterns
    # Not "fit" itself: "what fits on my spark" and "which model would fit" are picks.
    assert not {"fit", "fits", "what fits"} & set(sommelier.excludes)
    assert {"fit", "fits"} <= set(sommelier.weak_keywords)


# F3: "set up" with the machine or the product as its object is onboarding;
# with another specialist's noun it is that specialist's, whatever the state.
SETUP_OBJECTS = [
    "set up my spark",
    "help me set up the spark",
    "configure this thing",
    "get my spark ready",
    "setting up my new dgx",
    "configure my box",
    "get the box up and running",
    "help me set this up",
    "help me get set up",
    "setup this machine",
]
SETUP_OBJECTS_ELSEWHERE = {
    "set up docker on my spark": "container-wrangler",
    "help me set up ssh on the spark": "shell-teacher",
    "configure this thing for home assistant": "home-assistant",
    "set up my spark, it keeps failing": "install-medic",
    "get my spark ready for fine-tuning": "finetune-advisor",
    "set up the system prompt for my agent": None,
    "help me with setup": None,
}


@pytest.mark.parametrize("q", SETUP_OBJECTS)
def test_set_up_with_the_machine_as_object_is_the_concierges(q, neutral_classifier) -> None:
    """F3: the set-up pattern only took 'set <this|it|the spark> up', so 'set up
    my spark' fell to the general Wizard even on a first run."""
    for which in ("ws", "spark", "fresh", "fresh_spark"):
        c = _pick_on(q, which)
        assert c.profile == "setup-concierge", (q, which, c.reason)
        assert c.confidence >= 0.5


@pytest.mark.parametrize("q,expected", sorted(SETUP_OBJECTS_ELSEWHERE.items(), key=lambda kv: kv[0]))
def test_set_up_with_another_domains_object_is_not_the_concierges(q, expected, neutral_classifier) -> None:
    for which in ("ws", "spark", "fresh_spark"):
        c = _pick_on(q, which)
        assert c.profile != "setup-concierge", (q, which, c.reason)
        assert c.profile == expected, (q, which, c.reason)


# F4: a fetch verb with a model object is the shelf's; which / what / should
# in front of the verb is a recommendation.
FETCH_VERBS_ARE_THE_SHELFS = [
    "pull qwen3",
    "download the 70b",
    "grab gemma3 for me",
    "fetch llama3.1:70b",
    "install a model",
    "can you download qwen3:32b",
    "I want to pull the 30b",
    "how do I pull a model",
]
FETCH_RECOMMENDATIONS_ARE_THE_SOMMELIERS = [
    "which model should I pull?",
    "should I pull the 120b?",
    "what to pull for coding?",
    "recommend a model to pull for python",
    "what should I download first?",
    "what should I pull first?",
]


@pytest.mark.parametrize("q", FETCH_VERBS_ARE_THE_SHELFS)
def test_fetch_verbs_with_a_model_object_are_the_librarians(q, neutral_classifier) -> None:
    """F4: the librarian's verb + object pattern listed delete / remove / prune
    but not pull / download, so 'pull qwen3' was the sommelier's family
    pattern and 'download the 70b' the planner's size token."""
    for which in ("ws", "spark", "fresh_spark"):
        c = _pick_on(q, which)
        assert c.profile == "model-librarian", (q, which, c.reason)
        assert c.confidence >= 0.5


@pytest.mark.parametrize("q", FETCH_RECOMMENDATIONS_ARE_THE_SOMMELIERS)
def test_which_what_should_in_front_of_a_fetch_verb_is_a_recommendation(q, neutral_classifier) -> None:
    for which in ("ws", "spark", "fresh_spark"):
        c = _pick_on(q, which)
        assert c.profile == "model-sommelier", (q, which, c.reason)


def test_fetch_verbs_need_a_model_object(neutral_classifier) -> None:
    from nvh.integrations.wizard import concierge as mod

    # No model object, or the inference engine: not the shelf's.
    for q in ("pull the latest changes", "download the pdf", "install llama.cpp", "install torch",
              "fetch me a coffee"):
        assert _pick(q).profile != "model-librarian", q
    assert _pick("install llama.cpp").profile == "install-medic"
    # The imperative vetoes the sommelier and the planner; the recommendation
    # shell vetoes the librarian.
    assert mod._FETCH_IMPERATIVE in _rule("model-sommelier").exclude_patterns
    assert mod._FETCH_IMPERATIVE in _rule("vram-planner").exclude_patterns
    assert set(mod._FETCH_RECOMMENDATION) <= set(_rule("model-librarian").exclude_patterns)


# F5: bare device nouns are weak triage keywords.
def test_bare_device_nouns_are_weak_triage_keywords(neutral_classifier) -> None:
    """F5: gb10 / blackwell / grace were strong triage keywords, so 'just got
    my gb10 spark, how do I get started?' was vetoed out of the concierge
    (derived veto) and routed to gpu-triage."""
    triage, concierge = _rule("gpu-triage"), _rule("setup-concierge")
    nouns = {"gb10", "gb300", "blackwell", "grace", "sm_121"}
    assert nouns <= set(triage.weak_keywords)
    assert not nouns & set(triage.keywords)
    assert not nouns & set(concierge.excludes)
    for which in ("spark", "fresh_spark", "ws"):
        c = _pick_on("just got my gb10 spark, how do I get started?", which)
        assert c.profile == "setup-concierge", (which, c.reason)
        assert "re:just got my gb10 spark" in c.matched
    # Beside a trouble word or a pattern the noun counts and is named.
    c = _pick_on("my gb10 shows xid 79", "spark")
    assert c.profile == "gpu-triage" and "gb10" in c.matched
    c = _pick_on("the blackwell gpu is throttling", "spark")
    assert c.profile == "gpu-triage" and "blackwell" in c.matched
    assert _pick_on("my gb10 is overheating", "spark").profile == "gpu-triage"
    # Alone it is nothing, and the hint says so.
    for q in ("blackwell", "grace cpu cores", "sm_121"):
        assert _pick(q).profile is None, q
    assert "gpu-triage: weak 'blackwell' needs a second signal" in _pick("blackwell").reason


# ---------------------------------------------------------------------------
# Review 2026-09-02 (R1-R4): the tuner's one boost and derived vetoes, the
# fine-tune desk's vetoes, weak vague trouble words, the shell teacher's
# phrase_once and the wrangler's docker socket
# ---------------------------------------------------------------------------


def test_latency_tuner_state_is_one_group_and_the_medic_vetoes_it(neutral_classifier) -> None:
    """R1: ``device:dgx-spark`` and ``unified_memory`` were two bare boosts,
    both true on every Spark, so one engine name (1.0 + 1.2) beat two
    install-medic trouble words (2.0): "vllm install failed with exit code
    1" was a speed question."""
    from nvh.integrations.wizard import concierge as mod

    tuner, medic = _rule("latency-tuner"), _rule("install-medic")
    assert tuner.state == ("device:dgx-spark|device:rtx-spark|unified_memory",)
    assert tuner.state[0] == _rule("model-sommelier").state[0]
    # The vetoes are the medic's own strong words and patterns, derived the
    # way the concierge derives the rig doctor's; a medic word added
    # tomorrow is a tuner veto the same day.
    assert (tuner.excludes, tuner.exclude_patterns) == mod._veto_vocabulary((medic,))
    assert set(tuner.excludes) == set(medic.keywords)
    assert set(tuner.exclude_patterns) == set(medic.patterns)
    # Not the triage's: its strong words name the hardware the tuner tunes.
    assert not {"gpu", "vram", "cuda", "driver", "nvidia-smi"} & set(tuner.excludes)
    for q in (
        "vllm install failed with exit code 1",
        "llama.cpp build is failing with an error",
        "flash-attn wheel failed to build",
        "tensorrt import error",
        "pip install vllm takes forever",
    ):
        for which in ("spark", "fresh_spark", "ws"):
            c = _pick_on(q, which)
            assert c.profile == "install-medic", (q, which, c.reason)
    # One boost, not two: a Spark lifts the tuner by 0.6 and names both facts.
    plain = _pick("generation is slow")
    spark = _pick("generation is slow", context=_spark_ctx())
    assert plain.profile == spark.profile == "latency-tuner"
    assert spark.confidence == pytest.approx(plain.confidence + BOOST_CONF, abs=0.011)
    assert "DGX Spark" in spark.reason and "unified memory" in spark.reason.lower()
    # Speed questions that name the hardware or the engine are still its.
    assert _pick_on("only 3 tok/s on my gpu", "spark").profile == "latency-tuner"
    assert _pick_on("vllm is slow on my spark", "spark").profile == "latency-tuner"
    assert _pick_on("how do I speed up llama.cpp on the spark?", "spark").profile == "latency-tuner"
    # The engine is not a model family: bare "llama" must not fire on
    # "llama.cpp", or a fresh Spark's two boosts (2.2) beat the medic's two
    # trouble words (2.0) and an engine build failure becomes a model pick.
    assert set(mod._ENGINE_NOT_A_FAMILY) <= set(_rule("model-sommelier").excludes)
    assert _pick_on("llama.cpp build is failing with an error", "fresh_spark").profile == (
        "install-medic"
    )
    assert _pick_on("which model should I run with llama.cpp?", "spark").profile == "latency-tuner"
    # A real family name is untouched by the carve-out.
    assert _pick_on("which llama3.1 should I run?", "spark").profile == "model-sommelier"
    # Its own words veto the setup concierge in turn: with one boost each a
    # Spark whose Ollama is down would tie "new box" and "slow" and hand the
    # turn to the earlier rule. A slow new box is not a tour.
    assert (mod._TUNER_VETO_WORDS, mod._TUNER_VETO_PATTERNS) == mod._veto_vocabulary((tuner,))
    assert set(mod._TUNER_VETO_WORDS) <= set(_rule("setup-concierge").excludes)
    assert set(mod._TUNER_VETO_PATTERNS) <= set(_rule("setup-concierge").exclude_patterns)
    c = _pick("new box, why is it slow?", context=_spark_ollama_down_ctx(), history=[])
    assert c.profile == "latency-tuner", c.reason


def test_finetune_vocabulary_vetoes_the_planner_and_the_sommelier(neutral_classifier) -> None:
    """R2: the planner's bare size token (1.5) out-scored the advisor's
    'fine-tune' keyword (1.2), so 'how do I fine-tune a 7b on this' was a
    fit check."""
    from nvh.integrations.wizard import concierge as mod

    advisor = _rule("finetune-advisor")
    assert mod._FINETUNE_VETO_WORDS == tuple(advisor.keywords)
    for profile in ("vram-planner", "model-sommelier"):
        assert set(advisor.keywords) <= set(_rule(profile).excludes), profile
        # Weak "adapter" / "checkpoint" are not vetoes ("the power adapter").
        assert not set(advisor.weak_keywords) & set(_rule(profile).excludes), profile
    for q in (
        "how do I fine-tune a 7b on this",
        "how much vram does qlora on a 70b need?",
        "best base model for lora",
        "which model should I fine-tune for coding?",
        "can I train a 7b with unsloth on my spark?",
        "will my dataset fit for finetuning llama3.1:8b?",
    ):
        for which in ("spark", "ws"):
            c = _pick_on(q, which)
            assert c.profile == "finetune-advisor", (q, which, c.reason)
    # Without the training words the same shapes are the desk's.
    assert _pick("how much vram does a 70b need?").profile == "vram-planner"
    assert _pick("which model should I run for coding?").profile == "model-sommelier"
    assert _pick("the power adapter for my spark").profile is None


# R3: a vague trouble word needs the rig. Left: nobody's (the general
# Wizard binds every tool). Right: the rig noun beside the word decides
# between the two doctors with the rest of the sentence.
VAGUE_WITHOUT_THE_RIG = [
    "fix this sentence",
    "my argument is broken",
    "the toaster is not working",
    "what's wrong?",
    "fix it",
    "repair the fence",
    "my printer stopped working",
    "the plot of this novel doesn't work",
]
VAGUE_WITH_THE_RIG = {
    "what's wrong with this box?": "install-medic",
    "ollama isn't running": "install-medic",
    "nvhive is broken": "install-medic",
    "the ui doesn't work": "install-medic",
    "the wizard keeps failing": "install-medic",
    "fix ollama": "install-medic",
    "repair my setup": "install-medic",
    "new here, the thing is broken": "install-medic",
    "just got my spark, it isn't working": "install-medic",
    "set up my spark, it keeps failing": "install-medic",
    "my gpu is broken": "gpu-triage",
    "the driver stopped working": "gpu-triage",
    "my gpu is broken, I just got my spark": "gpu-triage",
}


@pytest.mark.parametrize("q", VAGUE_WITHOUT_THE_RIG)
def test_vague_trouble_words_without_the_rig_are_nobodys(q, neutral_classifier) -> None:
    for which in ("ws", "spark", "fresh_spark"):
        c = _pick_on(q, which)
        assert c.profile is None, (q, which, c.reason)
    # The hint names the weak word so the UI trace explains the miss.
    assert "needs a second signal" in _pick(q).reason


@pytest.mark.parametrize("q,expected", sorted(VAGUE_WITH_THE_RIG.items(), key=lambda kv: kv[0]))
def test_vague_trouble_words_need_the_rig(q, expected, neutral_classifier) -> None:
    for which in ("ws", "spark", "fresh_spark"):
        c = _pick_on(q, which)
        assert c.profile == expected, (q, which, c.reason)
        assert c.confidence >= 0.5


def test_vague_trouble_words_are_weak_and_the_rig_pattern_is_shared(neutral_classifier) -> None:
    """R3: the words were strong on both rig doctors, so one of them alone
    (1.0) reached MIN_SCORE whatever its object."""
    from nvh.integrations.wizard import concierge as mod

    medic, triage, concierge, tuner = (
        _rule("install-medic"), _rule("gpu-triage"), _rule("setup-concierge"), _rule("latency-tuner"),
    )
    vague = set(mod._VAGUE_TROUBLE)
    for rule in (medic, triage):
        assert vague <= set(rule.weak_keywords), rule.profile
        assert not vague & set(rule.keywords), rule.profile
        assert mod._RIG_TROUBLE in rule.patterns, rule.profile
    # Weak words leave the derived veto sets; the pattern joins them. The
    # concierge keeps the words as a hand veto: a new box that "isn't
    # working" is not asking for a tour.
    assert not vague & set(mod._RIG_DOCTOR_VETO_WORDS)
    assert vague <= set(concierge.excludes)
    assert mod._RIG_TROUBLE in concierge.exclude_patterns
    assert mod._RIG_TROUBLE in tuner.exclude_patterns
    # Continuity is the second signal on a follow-up: the doctor's weak word
    # never steals "fix it" from the specialist who was answering.
    assert _pick("fix it", sticky="bug-hunter").profile == "bug-hunter"
    assert _pick("what's wrong?", sticky="deep-reviewer").profile == "deep-reviewer"
    c = _pick("it's broken again", sticky="install-medic")
    assert c.profile == "install-medic" and "broken" in c.matched
    # Beside the rule's own strong word the vague word counts and is named.
    c = _pick("my gpu is broken")
    assert c.profile == "gpu-triage" and {"gpu", "broken"} <= set(c.matched)
    # The rig-trouble pattern names its match; the tie goes to the medic.
    c = _pick("ollama isn't running")
    assert c.profile == "install-medic" and "re:ollama isn't running" in c.matched
    # Hand-listed elsewhere too: the fact-checker still vetoes on them.
    assert vague <= set(_rule("fact-checker").excludes)
    # ... and the smart home outranks them as before, now by a clear margin.
    assert _pick("the lights are not working").profile == "home-assistant"


def test_shell_teacher_scores_a_phrase_once_and_the_docker_socket_is_the_wranglers(
    neutral_classifier,
) -> None:
    """R4: 'permission denied' was a strong shell keyword *and* an identical
    shell pattern (2.5), beating the wrangler's two words (2.2) on the
    verbatim Docker socket error."""
    assert _rule("shell-teacher").phrase_once
    socket_errors = (
        "permission denied while trying to connect to the Docker daemon socket at "
        "unix:///var/run/docker.sock",
        'Got permission denied while trying to connect to the Docker daemon socket at '
        'unix:///var/run/docker.sock: Get "http://%2Fvar%2Frun%2Fdocker.sock/v1.24/containers/json": '
        "dial unix /var/run/docker.sock: connect: permission denied",
        "Cannot connect to the Docker daemon at unix:///var/run/docker.sock. Is the docker daemon running?",
    )
    for q in socket_errors:
        for which in ("ws", "spark"):
            c = _pick_on(q, which)
            assert c.profile == "container-wrangler", (q[:60], which, c.reason)
            assert any(m.startswith("re:") and "docker" in m for m in c.matched), c.matched
    # The shell teacher keeps its own: one phrase, one score.
    c = _pick("bash: permission denied when I run ./start.sh")
    assert c.profile == "shell-teacher"
    assert "bash" in c.matched and "re:permission denied" in c.matched
    assert "permission denied" not in c.matched
    c = _pick("permission denied")
    assert c.profile == "shell-teacher" and c.matched == ("re:permission denied",)
    c = _pick("sudo chmod 755 start.sh")
    assert c.profile == "shell-teacher"
    assert not {"sudo", "chmod"} & set(c.matched)
    assert {"re:sudo", "re:chmod 755"} <= set(c.matched)
    assert _pick("how do I set up ssh keys on the spark?").profile == "shell-teacher"


# ---------------------------------------------------------------------------
# The device settings desk (2026-09-03, proposal §3.4 / §5 "Sudo reality")
# ---------------------------------------------------------------------------

# One setting at a time: services, the login session, the firewall, the
# hostname, packages, updates, group membership. Checked on a workstation, a
# Spark and a fresh Spark so neither the device boost nor its absence moves
# any of them.
DEVICE_SETTINGS_POSITIVES = [
    "how do I enable ssh on the spark?",
    "turn off auto-login",
    "add me to the docker group",
    "my headless spark keeps suspending after 20 minutes",
    "is it safe to run apt upgrade on dgx os?",
    "set up ufw but keep me reachable over tailscale",
    "install htop with apt",
    "enable the tailscaled service at boot",
    "change my hostname to spark-01",
    "sudo apt install nvtop",
    "snap install code",
    "systemctl enable tailscaled",
    "should I turn on unattended upgrades?",
    "the gdm greeter keeps putting my box to sleep",
    "update the nvidia driver",
    "upgrade my gpu driver",
    "how do I do a driver upgrade on dgx os?",
]

# The neighbours, and where each one belongs instead. A privileged verb is
# not enough: the fault, the claim, the package ecosystem, the daemon and the
# tour each keep their own.
DEVICE_SETTINGS_NEGATIVES = {
    # The rig doctor keeps hardware faults, whatever setting word joins them.
    "my gb10 shows xid 79 after I enabled ssh": "gpu-triage",
    "nvidia-smi has failed because it couldn't communicate with the driver": "gpu-triage",
    # Python packaging is an environment problem, not a device setting.
    "apt install torch": "install-medic",
    "pip install vllm fails to build a wheel": "install-medic",
    # The Docker daemon is the wrangler's; the docker *group* is not.
    "Cannot connect to the Docker daemon at unix:///var/run/docker.sock. "
    "Is the docker daemon running?": "container-wrangler",
    "install docker for me": "container-wrangler",
    "set up docker": "container-wrangler",
    # ssh keys are teaching, not a settings change.
    "how do I set up ssh keys on the spark?": "shell-teacher",
    "bash: permission denied when I run ./start.sh": "shell-teacher",
    # A claim about a setting wants an answer, not an upgrade.
    "is it true that apt upgrade breaks the driver?": "fact-checker",
    # The product's own first run is the concierge's tour.
    "install nvhive on this box": "setup-concierge",
    "configure nvhive for me please": "setup-concierge",
    # The model desk, the smart home and the notes coach.
    "which model should I install on my spark?": "model-sommelier",
    "turn on the kitchen lights": "home-assistant",
    "remember this: the docker group fix is usermod -aG docker $USER": "daily-notes-coach",
}


@pytest.mark.parametrize("q", DEVICE_SETTINGS_POSITIVES)
def test_device_settings_questions_route_to_the_settings_desk(q, neutral_classifier) -> None:
    for which in ("ws", "spark", "fresh_spark"):
        c = _pick_on(q, which)
        assert c.profile == "device-settings", (q, which, c.reason)
        assert c.confidence >= 0.5, (q, which, c.confidence)


@pytest.mark.parametrize(
    "q,expected", sorted(DEVICE_SETTINGS_NEGATIVES.items(), key=lambda kv: kv[0]),
)
def test_privileged_verbs_alone_do_not_reach_the_settings_desk(q, expected, neutral_classifier) -> None:
    for which in ("ws", "spark", "fresh_spark"):
        c = _pick_on(q, which)
        assert c.profile != "device-settings", (q, which, c.reason)
        assert c.profile == expected, (q, which, c.reason)


def test_device_settings_rule_is_gated() -> None:
    from nvh.integrations.wizard import concierge as mod

    rule = _rule("device-settings")
    # Settings verbs and nouns are strong; the low-precision words that also
    # open a shell, rig or notes question are weak.
    assert {"enable ssh", "ufw", "tailscale", "hostname", "docker group", "apt upgrade",
            "snap install", "systemctl enable", "headless", "gdm"} <= set(rule.keywords)
    assert {"ssh", "docker", "apt", "sudo", "permission denied", "suspend", "sleep",
            "usermod", "service", "port"} <= set(rule.weak_keywords)
    assert not set(rule.keywords) & set(rule.weak_keywords)
    # The four derived / hand veto sets, each with its own owner.
    assert set(mod._HW_FAULT_VETOES) <= set(rule.excludes)
    assert set(mod._PYTHON_PACKAGING_VETOES) <= set(rule.excludes)
    assert set(mod._MODEL_DESK_VETOES) <= set(rule.excludes)
    assert set(mod._SMART_HOME_VETOES) <= set(rule.excludes)
    # The fact checker's vetoes are derived from its own rule, so a keyword
    # added there is a settings veto the same day.
    assert (mod._CLAIM_VETO_WORDS, mod._CLAIM_VETO_PATTERNS) == mod._veto_vocabulary(
        (mod._FACT_CHECKER,),
    )
    assert set(mod._CLAIM_VETO_WORDS) <= set(rule.excludes)
    assert set(mod._CLAIM_VETO_PATTERNS) <= set(rule.exclude_patterns)
    assert set(mod._HW_FAULT_VETO_PATTERNS) <= set(rule.exclude_patterns)
    # The triage's bare nouns are NOT vetoes: they name the things a setting
    # configures, so "driver update" and "gpu persistence mode" survive.
    assert not {"driver", "drivers", "gpu", "secure boot", "persistence mode"} & set(rule.excludes)
    assert "driver update" in rule.keywords
    # One group, one boost: every member is a fact about the same machine.
    assert rule.state == ("device:dgx-spark|device:rtx-spark|has_root|can_sudo|privileged_allowed",)
    assert rule.phrase_once and rule.weight == 1.4
    # Placement: after the rig doctor and the concierge, before the model desk.
    order = [r.profile for r in SPECIALIST_RULES]
    assert order.index("setup-concierge") < order.index("device-settings")
    assert order.index("device-settings") < order.index("model-sommelier")
    assert order.index("gpu-triage") < order.index("device-settings")


def test_docker_group_is_the_settings_desks_and_the_daemon_the_wranglers(neutral_classifier) -> None:
    """The documented Docker boundary. The *daemon* is the wrangler's, the
    verbatim socket dump included — the same constant is its pattern and this
    rule's veto, so the split is a rule and not a scoring margin (one Spark
    state boost used to flip it). The *user's membership of the docker group*
    is a privileged change to the machine and lands here."""
    from nvh.integrations.wizard import concierge as mod

    assert mod._DOCKER_DAEMON_SOCKET in _rule("container-wrangler").patterns
    assert mod._DOCKER_DAEMON_SOCKET in _rule("device-settings").exclude_patterns
    for q in (
        "add me to the docker group",
        "docker says permission denied, do I need sudo?",
        "do I have to sudo docker every time?",
        "put my user in the docker group",
    ):
        for which in ("ws", "spark", "fresh_spark"):
            assert _pick_on(q, which).profile == "device-settings", (q, which)
    for q in (
        "permission denied while trying to connect to the Docker daemon socket at "
        "unix:///var/run/docker.sock",
        "Cannot connect to the Docker daemon at unix:///var/run/docker.sock. "
        "Is the docker daemon running?",
        "docker run --gpus all fails: nvidia runtime not found",
    ):
        for which in ("ws", "spark"):
            assert _pick_on(q, which).profile == "container-wrangler", (q[:60], which)
    # The shell teacher keeps a bare permission error on a file.
    assert _pick("bash: permission denied when I run ./start.sh").profile == "shell-teacher"
    assert _pick("permission denied").profile == "shell-teacher"


def test_ssh_service_versus_ssh_keys(neutral_classifier) -> None:
    """Enabling or disabling the *service* is a settings change; ssh keys,
    scp and the client config are the shell teacher's teaching."""
    for q in ("how do I enable ssh on the spark?", "how do I turn on ssh on the spark",
              "disable ssh"):
        assert _pick_on(q, "spark").profile == "device-settings", q
    for q in ("how do I set up ssh keys on the spark?", "help me set up ssh on the spark",
              "what does ssh-keygen -t ed25519 do?"):
        assert _pick_on(q, "spark").profile == "shell-teacher", q


def test_apt_upgrade_is_the_settings_desks_and_pip_the_medics(neutral_classifier) -> None:
    """The DGX OS trap belongs to the desk that can hold the driver packages;
    Python packaging stays an environment problem."""
    for q in ("is it safe to run apt upgrade on dgx os?", "apt-get upgrade",
              "sudo apt install nvtop", "install htop with apt"):
        for which in ("ws", "spark"):
            assert _pick_on(q, which).profile == "device-settings", (q, which)
    for q in ("apt install torch", "pip install vllm fails to build a wheel",
              "pip3 install -r requirements.txt fails"):
        for which in ("ws", "spark"):
            assert _pick_on(q, which).profile == "install-medic", (q, which)


def test_device_settings_state_is_one_group_and_one_boost(neutral_classifier) -> None:
    """The device, root, a working ``sudo -n`` and the privileged tier being
    on are facts about the same machine: one boost between them, so a
    settings word can never out-score a fault just because the box is a
    Spark."""
    plain = _pick("change my hostname to spark-01")
    spark = _pick("change my hostname to spark-01", context=_spark_ctx())
    assert plain.profile == spark.profile == "device-settings"
    assert spark.confidence == pytest.approx(plain.confidence + BOOST_CONF, abs=0.011)
    # The Spark context carries device:dgx-spark, can_sudo and (through them)
    # privileged_allowed; all three are named, and it is still one boost.
    for note in ("DGX Spark", "sudo works here without a password",
                 "privileged changes need your approval on a red card"):
        assert note in spark.reason, note


def test_privileged_allowed_predicate_reads_sudo_and_the_kill_switch(monkeypatch) -> None:
    """``privileged_allowed`` is "the owner can elevate, or can be handed the
    exact command, and the tier is switched on". ``in_sudo_group`` matters:
    on a stock DGX OS box the OOBE user is in the sudo group but a password
    is required, so ``can_sudo`` is usually False there."""
    from nvh.integrations.wizard import concierge as mod

    def state_for(**platform):
        return derive_state(_ctx(platform={"device_class": "dgx-spark", **platform}), None, [])

    assert "privileged_allowed" not in state_for()
    assert "in_sudo_group" not in state_for()
    # can_sudo alone, in_sudo_group alone and root each open the tier.
    assert "privileged_allowed" in state_for(can_sudo=True)
    assert {"in_sudo_group", "privileged_allowed"} <= state_for(in_sudo_group=True)
    assert "privileged_allowed" in state_for(has_root=True)
    # The kill switch closes it. The variable is spelled once, in the module
    # that enforces it; the concierge keeps no copy of the name or the falsy set.
    from nvh.integrations.wizard.tools import PRIVILEGED_ENV

    assert PRIVILEGED_ENV == "NVH_ALLOW_PRIVILEGED"
    assert not hasattr(mod, "PRIVILEGED_ENV_VAR") and not hasattr(mod, "_ENV_FALSY")
    for falsy in ("0", "false", "no", "off", "OFF", " False "):
        monkeypatch.setenv(PRIVILEGED_ENV, falsy)
        assert mod.privileged_tools_enabled() is False, falsy
        assert "privileged_allowed" not in state_for(can_sudo=True), falsy
        assert "can_sudo" in state_for(can_sudo=True), "the raw fact still shows"
    for truthy in ("1", "yes", "on", "true", ""):
        monkeypatch.setenv(PRIVILEGED_ENV, truthy)
        assert mod.privileged_tools_enabled() is True, truthy
    monkeypatch.delenv(PRIVILEGED_ENV, raising=False)
    assert mod.privileged_tools_enabled() is True, "default is on"


def test_privileged_predicate_defers_to_the_enforcing_module(monkeypatch) -> None:
    """The registry owns the kill switch; the concierge asks it rather than
    keeping a second opinion, and defaults to *on* only when the module is
    not importable at all — routing to a specialist whose tools are off
    wastes a turn, it never escalates anything (``execute()`` still refuses
    the call)."""
    import sys

    from nvh.integrations.wizard import concierge as mod
    from nvh.integrations.wizard import tools as tools_mod

    monkeypatch.delenv(tools_mod.PRIVILEGED_ENV, raising=False)
    # The enforcing module's answer is the answer, whatever the environment says.
    monkeypatch.setattr(tools_mod, "privileged_enabled", lambda: False)
    assert mod.privileged_tools_enabled() is False
    monkeypatch.setattr(tools_mod, "privileged_enabled", lambda: True)
    monkeypatch.setenv(tools_mod.PRIVILEGED_ENV, "0")
    assert mod.privileged_tools_enabled() is True, "no second opinion from the env"

    # The tools module itself failing to import is the only fallback, and it
    # is "on": there is no env read of its own to fall back to.
    monkeypatch.setitem(sys.modules, "nvh.integrations.wizard.tools", None)
    monkeypatch.setenv(tools_mod.PRIVILEGED_ENV, "0")
    assert mod.privileged_tools_enabled() is True, "default is on"


def test_device_settings_routes_in_shipped_library(tmp_path, neutral_classifier) -> None:
    assert "device-settings" in available_specialists(tmp_path)
    c = select_specialist(
        "add me to the docker group", context=_spark_ctx(), history=[], home_dir=tmp_path,
    )
    assert c.profile == "device-settings"
    assert c.reason.startswith("device-settings:")


# The routing surface in one table: eighty-two questions across twelve
# categories, each on the context a user would ask it from. A rule edit that
# moves any of these is a routing change and should be deliberate.
ROUTING_PROBE = [
    # onboarding
    ("how do I get started?", "fresh_spark", "setup-concierge"),
    ("just got my gb10 spark, how do I get started?", "fresh_spark", "setup-concierge"),
    ("set up my spark", "spark", "setup-concierge"),
    ("help me set up the spark", "fresh_spark", "setup-concierge"),
    ("configure this thing", "fresh", "setup-concierge"),
    ("get my spark ready", "spark", "setup-concierge"),
    ("I just unboxed my dgx, where do I begin?", "fresh_spark", "setup-concierge"),
    # trouble
    ("my gb10 shows xid 79", "spark", "gpu-triage"),
    ("my gpu is running really hot", "spark", "gpu-triage"),
    ("the fans are spinning really loud", "spark", "gpu-triage"),
    ("nvidia-smi says no devices were found", "ws", "gpu-triage"),
    ("is the driver really broken or is it me?", "spark", "gpu-triage"),
    ("ModuleNotFoundError: No module named 'nvh'", "ws", "install-medic"),
    ("ollama serve exits with code 1", "ws", "install-medic"),
    ("connection refused when I open the UI", "fresh_spark", "install-medic"),
    ("the installer failed, what's wrong?", "fresh", "install-medic"),
    # recommendation
    ("which model should I run for coding?", "spark", "model-sommelier"),
    ("what should I pull first?", "fresh_spark", "model-sommelier"),
    ("is gemma3 good for chat?", "ws", "model-sommelier"),
    ("should I pull the 120b or the 20b?", "spark", "model-sommelier"),
    ("which qwen3 should I run on my spark?", "spark", "model-sommelier"),
    ("MoE vs dense on the spark?", "spark", "model-sommelier"),
    ("which model should I pull?", "spark", "model-sommelier"),
    ("what fits on my spark?", "spark", "model-sommelier"),
    # sizing
    ("how much vram does qwen3:32b need?", "spark", "vram-planner"),
    ("will llama3.1:70b fit on my card?", "spark", "vram-planner"),
    ("does a 70B fit on a 3090?", "ws", "vram-planner"),
    ("how much memory does a 70b need at 32k context?", "spark", "vram-planner"),
    ("is 128 gb enough for the 120b?", "spark", "vram-planner"),
    ("enough vram for gemma3:27b?", "spark", "vram-planner"),
    ("ollama keeps getting OOM killed", "ws", "vram-planner"),
    ("is the 120b too big for my spark?", "spark", "vram-planner"),
    # shelf
    ("pull qwen3", "spark", "model-librarian"),
    ("download the 70b", "ws", "model-librarian"),
    ("grab gemma3 for me", "spark", "model-librarian"),
    ("fetch llama3.1:70b", "ws", "model-librarian"),
    ("install a model", "ws", "model-librarian"),
    ("what's installed?", "ws", "model-librarian"),
    ("delete the 70b models I never use", "ws", "model-librarian"),
    ("how much disk are my models taking?", "ws", "model-librarian"),
    # home assistant
    ("turn off the living room lights", "ws", "home-assistant"),
    ("set the thermostat to 68", "ws", "home-assistant"),
    ("is the garage door open?", "ws", "home-assistant"),
    ("start the vacuum", "ws", "home-assistant"),
    ("how do I turn on GPU persistence mode", "spark", "gpu-triage"),
    # research
    ("what's the latest news on the RTX Spark launch?", "ws", "deep-researcher"),
    ("summarise https://example.com/post for me", "ws", "deep-researcher"),
    ("look up the release notes for cuda 13", "ws", "deep-researcher"),
    ("research the state of the art in speculative decoding", "ws", "deep-researcher"),
    # fact-check
    ("is it true that apt upgrade breaks the driver?", "ws", "fact-checker"),
    ("fact check this: the spark has 256 GB of memory", "spark", "fact-checker"),
    ("did nvidia really say the spark ships in june? is that true?", "ws", "fact-checker"),
    ('is this accurate? "the gb10 has 20 arm cores"', "spark", "fact-checker"),
    # coding
    (CODE_BUG, "ws", "bug-hunter"),
    ("write a python function that sorts a list of dicts by key", "ws", "backend-implementer"),
    ("review this PR diff please", "ws", "deep-reviewer"),
    ("my script throws KeyError on the second run", "ws", "bug-hunter"),
    # notes
    ("what did we decide about the vault layout last week?", "ws", "vault-rag"),
    ("remember this: the docker group fix is usermod -aG docker $USER", "ws", "daily-notes-coach"),
    ("according to the document, what is the warranty period?", "ws", "doc-qa"),
    # review 2026-09-02 (R1-R4): trouble beats an engine name, fine-tune
    # beats a size token, a vague word needs the rig, the docker socket is
    # the wrangler's
    ("vllm install failed with exit code 1", "spark", "install-medic"),
    ("llama.cpp build is failing with an error", "spark", "install-medic"),
    ("vllm is slow on my spark", "spark", "latency-tuner"),
    ("how do I fine-tune a 7b on this", "spark", "finetune-advisor"),
    ("fix this sentence", "ws", None),
    ("my argument is broken", "ws", None),
    ("the toaster is not working", "ws", None),
    ("what's wrong with this box?", "fresh_spark", "install-medic"),
    (
        "permission denied while trying to connect to the Docker daemon socket at "
        "unix:///var/run/docker.sock",
        "ws",
        "container-wrangler",
    ),
    ("bash: permission denied when I run ./start.sh", "ws", "shell-teacher"),
    # 2026-09-03: the device settings desk (proposal §3.4). One setting at a
    # time — services, the login session, the firewall, the hostname,
    # packages, updates, group membership.
    ("how do I enable ssh on the spark?", "spark", "device-settings"),
    ("add me to the docker group", "spark", "device-settings"),
    ("my headless spark keeps suspending after 20 minutes", "spark", "device-settings"),
    ("is it safe to run apt upgrade on dgx os?", "spark", "device-settings"),
    ("set up ufw but keep me reachable over tailscale", "spark", "device-settings"),
    ("install htop with apt", "ws", "device-settings"),
    ("enable the tailscaled service at boot", "spark", "device-settings"),
    ("change my hostname to spark-01", "ws", "device-settings"),
    # A driver *update* is a settings change through the validated channel
    # (the desk that knows the DGX OS trap); a driver *fault* is the rig doctor's.
    ("update the nvidia driver", "spark", "device-settings"),
    ("upgrade my gpu driver", "spark", "device-settings"),
    # ... and the four neighbours it must not take: a switch at home, a model
    # pick, a hardware fault and a coding request.
    ("turn on the kitchen lights", "ws", "home-assistant"),
    ("recommend a model for python coding", "spark", "model-sommelier"),
    ("nvidia-smi has failed because it couldn't communicate with the driver", "spark",
     "gpu-triage"),
    ("write a python function that parses a csv file", "ws", "backend-implementer"),
]


@pytest.mark.parametrize(
    "q,which,expected", ROUTING_PROBE,
    ids=[f"{w}:{q.splitlines()[0][:44]}" for q, w, _ in ROUTING_PROBE],
)
def test_routing_probe(q, which, expected, neutral_classifier) -> None:
    c = _pick_on(q, which)
    assert c.profile == expected, (q, which, c.reason)
    assert c.confidence >= 0.5, (q, c.confidence)


def test_routing_probe_covers_the_surface() -> None:
    assert len(ROUTING_PROBE) == 84
    assert len({q for q, _, _ in ROUTING_PROBE}) == 84
    covered = {p for _, _, p in ROUTING_PROBE}
    assert {
        "setup-concierge", "install-medic", "gpu-triage", "model-sommelier", "vram-planner",
        "model-librarian", "home-assistant", "deep-researcher", "fact-checker", "bug-hunter",
        "backend-implementer", "deep-reviewer", "vault-rag", "daily-notes-coach", "doc-qa",
        "latency-tuner", "finetune-advisor", "container-wrangler", "shell-teacher",
        "device-settings", None,
    } <= covered
    assert {w for _, w, _ in ROUTING_PROBE} == set(_ALL_CONTEXTS)
