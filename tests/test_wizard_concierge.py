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
OPS_PROFILES = {"install-medic", "gpu-triage", "model-librarian", "vram-planner", "provider-keysmith"}
CODING_PROFILES = {"bug-hunter", "deep-reviewer", "backend-implementer"}


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


def test_spark_model_fit_prefers_vram_planner_and_notes_unified_memory() -> None:
    c = _pick("which model fits my 128 GB spark?", context=_spark_ctx())
    assert c.profile == "vram-planner"
    assert "unified memory" in c.reason.lower()
    assert "MemAvailable" in c.reason


def test_bare_which_model_goes_to_librarian_off_spark_and_planner_on_spark() -> None:
    assert _pick("which model should I use?").profile == "model-librarian"
    assert _pick("which model should I use?", context=_spark_ctx()).profile == "vram-planner"


def test_vague_trouble_tie_is_broken_by_state() -> None:
    failed = _ctx(recent_jobs=[{"id": "j1", "kind": "ollama", "status": "failed"}])
    c = _pick("what's wrong?", context=failed)
    assert c.profile == "install-medic"
    assert "install job failed" in c.reason

    no_gpu = _ctx(gpu={"detected": False})
    c = _pick("what's wrong?", context=no_gpu, findings=[{"id": "gpu-missing"}])
    assert c.profile == "gpu-triage"
    assert "no NVIDIA GPU" in c.reason


def test_provider_key_401_routes_to_keysmith() -> None:
    c = _pick("my API key returns 401 from openai")
    assert c.profile == "provider-keysmith"
    assert "re:401" in c.matched


def test_slow_tokens_route_to_latency_tuner() -> None:
    assert _pick("generation is really slow, only 3 tok/s").profile == "latency-tuner"


def test_finetune_routes_to_advisor() -> None:
    c = _pick("how do I fine-tune llama with unsloth on my dataset?")
    assert c.profile == "finetune-advisor"


def test_first_run_pushes_toward_setup_profile() -> None:
    c = _pick("how do I get started?", context=_fresh_ctx(), history=[])
    assert c.profile in OPS_PROFILES
    assert "first run" in c.reason


def test_state_alone_never_selects_a_specialist() -> None:
    # A fresh box must not send a poem to the install medic / librarian.
    c = _pick("write me a poem about autumn", context=_fresh_ctx(), history=[])
    assert c.profile is None
    # Nor may findings alone select anything when the words carry no signal.
    c = _pick("tell me a joke", findings=[{"id": "gpu-missing"}, {"id": "job-failed-1"}])
    assert c.profile is None


# ---------------------------------------------------------------------------
# Smart home (K1): routing requires a smart-home object
# ---------------------------------------------------------------------------

# The eight reproduced false positives, with where they belong instead. A set
# means either member is fine (the two profiles bind the same tools).
HA_FALSE_POSITIVES = {
    "how do I turn on GPU persistence mode": "gpu-triage",
    "turn on flash attention in ollama": {"model-librarian", "latency-tuner"},
    "how do I turn on ssh on the spark": "shell-teacher",
    "what's the temperature of my gpu": "gpu-triage",
    "light model for coding": None,
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
    "compare q4 vs q8": "vram-planner",
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
    assert sized.profile == "vram-planner"
    assert "128 GB unified memory" in sized.reason and "MemAvailable" in sized.reason

    unsized_ctx = _spark_ctx()
    unsized_ctx["platform"] = {k: v for k, v in unsized_ctx["platform"].items() if k != "memory_total_gb"}
    unsized_ctx["gpu"] = {"detected": True, "unified_memory": True}
    unsized = _pick("which model fits my spark?", context=unsized_ctx)
    assert unsized.profile == "vram-planner"
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
    assert "home-assistant" in shipped


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
