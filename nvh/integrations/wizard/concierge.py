"""Concierge: pick the Wizard's hidden specialist for one turn.

The Wizard shows the user *one* assistant. Under the hood, each turn may be
answered by a specialist profile from the Agent Library (``setup-concierge``,
``install-medic``, ``gpu-triage``, ``vram-planner``, ``home-assistant``, ...).
This module makes that choice. It is deterministic, pure and fast: no network, no engine, no
I/O beyond an optional :func:`~nvh.integrations.wizard.profiles.list_profiles`
call to learn which profiles exist right now.

Design (docs/proposals/SPARK_CONCIERGE_2026-09.md §3.1)
-------------------------------------------------------

Selection runs in three tiers, first hit wins:

1. **Deterministic triggers.** Every :class:`SpecialistRule` in
   :data:`SPECIALIST_RULES` scores the question::

       text  = weight * (KEYWORD_HIT * min(distinct keyword hits, 3)
                         + PATTERN_HIT * min(distinct pattern hits, 2))
       score = text
             + STATE_BOOST * (fired state expressions)  # only when text > 0
             + TASK_TYPE_BONUS                          # classifier agrees
             + STICKY_BONUS                             # previous specialist

   Keywords come in two strengths. A *strong* keyword is a signal on its
   own. A *weak* keyword (``weak_keywords``: "review", "latest", "node",
   "kitchen", ...) is counted only next to a second signal from the same
   rule: a strong keyword, a pattern hit or continuity from the previous
   turn. State is deliberately *not* a second signal: on a Spark
   ``device:dgx-spark`` is always true, so it would turn every weak word
   into a strong one. Nor is the task classifier: it reads the same words.
   The classifier's agreement earns ``TASK_TYPE_BONUS`` only when it is
   confident (``confidence >= RESIDUE_MIN_CONFIDENCE``). A rule's
   ``excludes`` are a veto: one hit zeroes the rule for this turn ("turn on
   GPU persistence mode" is never smart-home). A rule with
   ``phrase_once=True`` counts a phrase once: a keyword hit that lies inside
   one of its own pattern matches is dropped, so "just installed nvhive" is
   the install pattern's 1.5, not keyword + pattern 2.5.

   State predicates come from ``wizard_context()`` and the findings list
   (``failed_job``, ``gpu_missing``, ``no_models``, ``first_run``,
   ``device:dgx-spark``, ...). Each entry of a rule's ``state`` is an
   *expression* worth one ``STATE_BOOST``: a bare predicate, an any-of group
   (``"first_run|no_models"``: one boost however many members are true) or
   an all-of group (``"first_run&device:dgx-spark"``: the device counts only
   on a first run; ``&`` binds tighter than ``|``). Group predicates that are
   true together on the same machine, or every onboarding word on a Spark
   whose Ollama is merely down arrives pre-lifted by 1.2. State never
   selects a specialist on its own: it amplifies a rule that already matched
   the words, so a fresh box does not route "write me a poem" to the install
   medic. The best profile wins when its score reaches :data:`MIN_SCORE`;
   a tie goes to the rule with more distinct matched signals (keywords plus
   patterns), then to the earlier rule.

2. **Task classifier residue.** When no rule reaches the threshold,
   :func:`nvh.core.router.classify_task` runs once and the first rule whose
   ``task_types`` contains the classified type (coding, math) is chosen with
   low confidence. Conversation / QA types map to nothing on purpose.

3. **Continuity.** If the previous assistant turn used a specialist and the
   new question carries no signal ("and then?"), keep that specialist. The
   previous specialist is read from the most recent assistant entry's
   ``used_profile`` in ``history`` (entries without it carry nothing) or
   the explicit ``sticky=`` keyword, which wins.

Otherwise the general Wizard persona answers (``profile=None``) with a short
human reason — :data:`GENERAL_NO_MATCH_REASON` (``"general Wizard: no
specialist matched"``, plus one near-miss hint when there is one). Bare
greetings ("hi", "thanks!") always return the general persona
(``"general Wizard: greeting or chit-chat"``).

Adding a rule
-------------

Append a :class:`SpecialistRule` to :data:`SPECIALIST_RULES`:

    SpecialistRule(
        profile="media-librarian",              # must exist in the library
        keywords=("what's in this", "ocr"),     # whole-word, case-insensitive
        weak_keywords=("photo", "picture"),     # need a second signal
        patterns=(r"\\.(png|jpe?g|webp)\\b",),   # regex, compiled lazily
        excludes=("gpu", "ollama"),             # veto: never this specialist
        exclude_patterns=(r"\\bxid\\b\\s*\\d+",),  # regex veto, same effect
        state=("vision_ready", "device:dgx-spark|unified_memory"),  # one boost each
        task_types=(TaskType.MULTIMODAL,),      # classifier tie-break
        weight=1.0,                             # >1 to win generic ties
        requires_tools=("analyze_image",),      # must be in tools_allowed
        phrase_once=True,                       # keyword inside a pattern hit: once
    )

Rules whose ``profile`` is not in the current profile list are dropped
silently, so a library change never breaks chat. Keep strong keywords
specific: a single hit already clears the threshold, so a word that also
lives in rig vocabulary ("node", "registry", "series", "adapter",
"parameters") belongs in ``weak_keywords``. Use ``weight > 1`` for domains
whose one-word triggers tie a neighbour's ("install docker" is the
container specialist's, not the medic's). Ops rules sit first in the
table so an exact tie with a GPU / model word goes to the rig doctor.

The rule list, in table order: the rig doctor (``install-medic``,
``gpu-triage``), the setup concierge (``setup-concierge``), the model desk
(``model-sommelier``, ``vram-planner``, ``model-librarian``),
``provider-keysmith``, ``latency-tuner``, ``finetune-advisor``,
``home-assistant``, ``comfyui-workflow-debugger``, ``container-wrangler``,
``shell-teacher``, the coding pair (``bug-hunter``, ``deep-reviewer``,
``backend-implementer``), research (``deep-researcher``, ``fact-checker``),
notes (``vault-rag``, ``daily-notes-coach``, ``doc-qa``) and the tutors
(``code-tutor``, ``science-explainer``, ``math-stepper``).

The concierge sits between the rig doctor and the model desk. Every strong
keyword and pattern of the rig doctor (``install-medic`` + ``gpu-triage``:
``error``, ``connection refused``, ``exit code``, ``xid``, a traceback, ...)
is derived into its veto set by :func:`_veto_vocabulary`, minus the three
install verbs the concierge binds to nvhive in its own patterns, and the
model desk's words (``model``, ``fits``, ``quant``, ``vram``) veto it by
hand, so "my gpu is broken, I just got my spark" is the triage's, "just got
my spark, exit code 1" is the medic's and "just got my spark, which model
should I pull first?" is the sommelier's however fresh the box. Its state is
worth at most two boosts: ``first_run|no_models`` counts once whether one or
both are true, and the device counts only on a first run
(``first_run&device:dgx-spark``), so a Spark whose Ollama is merely down
lifts "how do I get started?" by 0.6, not 1.2. Its "set up" / "setup" are
weak, so "set up ssh keys" and "set up docker" stay with their own
specialists, and naming another specialist's domain (``ssh``, ``docker``,
``home assistant``, ``torch``) vetoes it too; with the machine or the product
as the object ("set up my spark", "help me set up the spark", "configure this
thing", "get the box ready"; :data:`_SETUP_TARGET`) the phrase is a pattern
and the concierge's. The rig doctor's bare device nouns (``gb10``,
``blackwell``, ``grace``) are *weak* triage keywords, so they never enter the
derived veto set and "just got my gb10 spark, how do I get started?" is
onboarding while "my gb10 shows xid 79" is still the triage's.

Within the model desk the three rules share no strong keyword. The
sommelier answers recommendation and fit questions ("which model should I
run", "what fits on my spark", "best model for coding", "MoE vs dense",
"which quant", "context length") and carries the device / unified-memory /
``no_models`` boosts. Its which / what / should-I / is-it-worth-it patterns
take the model noun, a size token or a family name as their object, so
"which 70b should I run", "should I pull the 120b" and "is a 30b MoE worth
it here" are recommendations too. The planner keeps sizing arithmetic
("will 70B Q4 fit in 24 GB", "how much memory", "kv cache", OOM) with no
state at all, and takes it even when the model is named: "how much vram
does qwen3:32b need" and "will llama3.1:70b fit on my card" are a 1.5
pattern (:data:`_FIT_CHECK` and the how-much / enough / too-big phrases)
that also vetoes the sommelier (:data:`_ARITHMETIC_VETOES`), so the Spark
boost cannot flip a fit check into a pick. The shelf vocabulary
(:data:`_SHELF_VETOES`) vetoes the planner as it does the sommelier. The
librarian keeps the shelf ("what's installed", "delete unused models", "disk
space", ``ollama rm``); its verb + object patterns accept a size, family or
tag as the object ("delete the 70b models I never use", "get rid of qwen3",
"pull qwen3", "download the 70b"), and a fetch verb as an imperative
(:data:`_FETCH_IMPERATIVE`) vetoes the sommelier and the planner, while a
recommendation shell in front of it ("which model should I pull", "should I
pull the 120b") vetoes the librarian instead. A bare size token (``70b``) is
the planner's pattern, a model family with a version (``qwen3``) is the
sommelier's and a tag (``llama3.1:70b``) the librarian's, so a bare tag is a
three-way tie that table order gives to the sommelier.

Every fact-checker pattern takes a claim-shaped object ("is it true", "is
that really the case", "did they actually say that", "fact check", a URL or
quoted claim beside a truth word); a bare "really" is emphasis. The rig's
symptoms and the model desk's arithmetic veto it, so "my gpu is running
really hot" is the triage's and "does my 70b really fit" the planner's, but
device nouns are not vetoes: "is it true that apt upgrade breaks the
driver?" stays a claim about the driver.

The rig doctor's vague trouble words (:data:`_VAGUE_TROUBLE`: "broken",
"not working", "fix this", "what's wrong", ...) are *weak* for the medic
and the triage (review 2026-09-02, R3). They count beside one of the
rule's own strong words ("my gpu is broken"), continuity ("fix it" after a
medic turn) or the shared :data:`_RIG_TROUBLE` pattern, which pairs one
with a rig noun (:data:`_RIG_NOUN`: the machine, the product, ``ollama``,
the GPU, the model, "this thing"). "fix this sentence", "my argument is
broken" and "the toaster is not working" are nobody's, and a bare "what's
wrong?" is the general Wizard's or, on a follow-up, the previous
specialist's. The words stay a veto for the setup concierge by hand: a new
box that "isn't working" is not asking for a tour. The latency tuner
derives its vetoes from the medic the way the concierge does from the rig
doctor (R1), and its Spark / unified-memory state is one group like the
sommelier's, so "vllm install failed with exit code 1" is the medic's
however fast the box; the tuner's own words veto the concierge in turn
("new box, why is it slow?" is a tuning question, not a tour). The
fine-tune advisor's vocabulary ("fine-tune", "lora", "unsloth", "dataset",
...) vetoes the planner and the sommelier (R2), so "how do I fine-tune a
7b on this" is not a sizing question. The shell teacher is ``phrase_once``
("permission denied" is its pattern's 1.5, not keyword + pattern 2.5) and
the Docker socket error is the container wrangler's own pattern (R4).

Wiring
------

:func:`resolve_auto_profile` is the one call ``chat.py`` needs: explicit
pins pass through untouched; ``None``/``""``/``"auto"`` run selection and
return the chosen profile name plus the choice so the ``used_profile``
caption can be emitted. ``"wizard"`` is an explicit pin of the general
persona, not an auto name: the user who picks it gets the general Wizard
with its own whitelist and no specialist routing.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

from nvh.integrations.wizard.profiles import AgentProfile
from nvh.providers.base import TaskType

logger = logging.getLogger(__name__)

__all__ = [
    "AUTO_PROFILE",
    "GENERAL_NO_MATCH_REASON",
    "MIN_SCORE",
    "SPECIALIST_RULES",
    "SpecialistChoice",
    "SpecialistRule",
    "active_rules",
    "available_specialists",
    "derive_state",
    "resolve_auto_profile",
    "select_specialist",
]

# ---------------------------------------------------------------------------
# Public constants
# ---------------------------------------------------------------------------

#: Sentinel profile name meaning "let the concierge choose".
AUTO_PROFILE = "auto"

#: Names that mean "no explicit pin" when passed as ``profile`` (``None``
#: is handled separately). ``"wizard"`` is *not* here: it pins the general
#: persona explicitly, so a user can opt out of specialist routing.
_AUTO_NAMES: frozenset[str] = frozenset({"", AUTO_PROFILE})

#: A rule must reach this score to select its specialist. One distinct
#: keyword hit at weight 1.0 is exactly enough; state, classifier and
#: continuity bonuses alone never are.
MIN_SCORE = 1.0
KEYWORD_HIT = 1.0
PATTERN_HIT = 1.5
MAX_KEYWORD_HITS = 3
MAX_PATTERN_HITS = 2
STATE_BOOST = 0.6
TASK_TYPE_BONUS = 0.5
STICKY_BONUS = 0.5
#: Classifier confidence needed for the residue tier (tier 2). The TF-IDF
#: classifier is noisy below ~0.3 ("my API key returns 401" scores
#: code_generation at 0.47, so tier 1 must catch such cases by keyword).
RESIDUE_MIN_CONFIDENCE = 0.3
RESIDUE_CONFIDENCE = 0.35
STICKY_CONFIDENCE = 0.3

#: ``reason`` of a plain general-Wizard turn — no rule hit, no classifier
#: residue, nothing to continue. Every ``profile=None`` reason starts with
#: ``"general Wizard: "`` and reads as a sentence: the chat envelope carries
#: it as ``profile_reason`` and the UI shows it as the attribution tooltip.
GENERAL_NO_MATCH_REASON = "general Wizard: no specialist matched"

_GREETING_RE = re.compile(
    r"^\s*(?:hi|hello|hey|yo|hiya|howdy|sup|good\s+(?:morning|afternoon|evening|night)|"
    r"thanks|thank\s+you|thx|ty|cheers|bye|goodbye|see\s+you|later|ok|okay|cool|nice|great)"
    r"(?:\s*[,!.]?\s*(?:there|wizard|nvhive|all|again|a\s+lot|so\s+much|you))?"
    r"[\s!.,?]*$",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SpecialistChoice:
    """Outcome of one selection. ``profile=None`` means the general Wizard."""

    profile: str | None
    reason: str
    confidence: float
    matched: tuple[str, ...] = ()


@dataclass(frozen=True)
class SpecialistRule:
    """One declarative trigger set for a library profile.

    ``keywords`` match as whole words/phrases, case-insensitively, against
    whitespace-normalised text and are a signal on their own.
    ``weak_keywords`` match the same way but count only when the rule also
    has a second signal this turn (a strong keyword, a pattern or
    continuity); use them for low-precision words that also appear in rig
    questions. ``excludes`` veto the rule outright when any of them appears;
    ``exclude_patterns`` are regex vetoes with the same effect, matched like
    ``patterns``. ``patterns`` are regexes compiled lazily with
    ``re.IGNORECASE | re.MULTILINE`` against the raw question. Each ``state``
    entry is an expression worth one ``STATE_BOOST`` when it fires: a finding
    id (``gpu-missing``) or context predicate (``no_models``, ``failed_job``,
    ``first_run``, ``device:dgx-spark``; see :func:`derive_state`), an any-of
    group joined by ``|`` (``"first_run|no_models"``) or an all-of group
    joined by ``&`` (``"first_run&device:dgx-spark"``; ``&`` binds tighter).
    ``task_types`` is the classifier tie-break. ``requires_tools`` drops the
    rule when the profile's ``tools_allowed`` whitelist is set and does not
    contain every listed tool. ``phrase_once`` drops a keyword hit that lies
    inside one of the rule's own pattern matches, so a phrase both a keyword
    and a pattern cover scores once.
    """

    profile: str
    keywords: tuple[str, ...] = ()
    patterns: tuple[str, ...] = ()
    state: tuple[str, ...] = ()
    task_types: tuple[TaskType, ...] = ()
    weight: float = 1.0
    requires_tools: tuple[str, ...] = ()
    weak_keywords: tuple[str, ...] = ()
    excludes: tuple[str, ...] = ()
    exclude_patterns: tuple[str, ...] = ()
    phrase_once: bool = False
    #: Free-form label used in reasons; defaults to the profile name.
    label: str = field(default="", compare=False)

    @property
    def display(self) -> str:
        return self.label or self.profile


# ---------------------------------------------------------------------------
# Rule table
# ---------------------------------------------------------------------------

# Words that mean "something is broken" without saying what. They are *weak*
# on both halves of the "rig doctor" (install-medic, gpu-triage; review
# 2026-09-02, R3): a sentence, an argument or a toaster can be broken too,
# so they count only beside the rule's own strong word, continuity, or the
# shared _RIG_TROUBLE pattern that pairs one with a rig noun. The two rules
# tie on that pattern; state boosts and the other words break the tie. The
# fact-checker and the setup concierge veto on them by hand.
_VAGUE_TROUBLE: tuple[str, ...] = (
    "what's wrong", "whats wrong", "what is wrong", "not working", "doesn't work",
    "doesnt work", "isn't working", "isnt working", "is not running", "isn't running",
    "isnt running", "stopped working", "won't start", "wont start", "broken", "fix it",
    "fix this", "repair", "something is off", "something's wrong", "keeps failing",
)

# Words that ask for an explanation without naming a domain. Shared by the
# three tutors; the domain vocabulary below decides which one answers.
_EXPLAIN_WORDS: tuple[str, ...] = (
    "explain", "explain like", "teach me", "teach", "tutorial", "eli5", "walk me through",
    "step by step", "step-by-step", "don't understand", "dont understand", "understand how",
    "understand why", "intuition", "difference between",
)
# Learning words that also live in rig questions ("machine learning on the
# spark", "learning rate", "proof of concept"): weak for every tutor.
_EXPLAIN_WEAK: tuple[str, ...] = (
    "learn", "learning", "beginner", "beginners", "new to", "confused", "concept",
)

# Smart-home objects. Every home-assistant trigger carries one of these (or
# a Home Assistant entity id / platform name): a bare verb ("turn on"), room
# ("kitchen") or adjective ("light", "dim") never routes on its own, so
# "turn on GPU persistence mode", "light model for coding" and "my dim sum
# recipe" stay out of the smart home.
_HA_OBJECT = (
    r"(?:lights?|lamps?|bulbs?|(?:led|light) strip|thermostat|heating|heaters?|hvac|"
    r"ac|air ?con(?:ditioning|ditioner)?|(?:ceiling |exhaust )?fans?|blinds?|shades?|"
    r"curtains?|shutters?|garage(?: door)?|(?:front|back|side|patio) door|doors?|"
    r"(?:door |smart )?locks?|deadbolt|(?:smart |wall )?plugs?|(?:smart )?outlets?|"
    r"(?:smart |light )switch(?:es)?|scenes?|(?:robot )?vacuum|roomba|tv|television|"
    r"media player|speakers?|sprinklers?|irrigation|alarm|cameras?|doorbell|humidifier|"
    r"dehumidifier|(?:air )?purifier|kettle|coffee maker|hot tub|pool pump)"
)
_HA_ROOM = (
    r"(?:living room|lounge|bedroom|kitchen|bathroom|hallway|hall|office|study|nursery|"
    r"basement|attic|garage|porch|patio|dining room|house|home|upstairs|downstairs)"
)

# Generic code shapes: a fence, a definition, an import, a brace lambda.
_CODE_SHAPE = (
    r"```",
    r"(?:^|\n)\s*(?:def |class |async def |function |import |from \w+ import |#include\s*<|"
    r"public (?:static |final )*\w+ \w+\(|fn \w+\(|func \w+\()",
)

# --- Ops: the setup concierge's object -------------------------------------
# The machine or the product as the object of "set up" / "configure" / "get
# ... ready" ("set up my spark", "configure this thing", "get the box ready").
# Never another specialist's noun: "set up docker" and "set up ssh keys" are
# vetoed, and "set up a system" / "the system prompt" is nobody's onboarding.
# The rig doctor's _RIG_NOUN reuses both, so they sit above it.
_SETUP_ADJ = (
    r"(?:(?:new|first|shiny|brand[- ]new|little|whole|own|nvidia|gb10|gb300|dgx|rtx|"
    r"blackwell|grace) )*"
)
_SETUP_TARGET = (
    r"(?:spark|dgx|box|machine|rig|thing|nvhive|nvh|workstation|computer|pc|desktop|laptop)"
)

# --- Ops: the rig as the object of a vague trouble word ----------------------
# "ollama isn't running", "nvhive is broken", "what's wrong with my spark",
# "the ui doesn't work", "set up my spark, it keeps failing", "fix this thing"
# (review 2026-09-02, R3). Product, machine and engine names count bare; a
# common noun needs a determiner ("the server", "my box", "this thing"), so
# "a server-side argument is broken" is nobody's rig. The device adjectives
# ride along ("my new gb10 box is broken"). The vague word may sit up to 120
# characters before or after the noun, so "just got my spark, it isn't
# working" pairs across the clause. Both rig-doctor rules carry the pattern
# (they tie on it; state and the other words decide), and through
# _veto_vocabulary it is a setup-concierge and latency-tuner veto too.
_RIG_NOUN = (
    r"(?:nvhive|nvh|ollama|spark|dgx|gb10|gb300|gpus?|cuda|drivers?|nvidia-smi|"
    r"install(?:er|ation)?|setup|venv|conda|web ?ui|dashboard|"
    r"(?:the|my|our|this|that|your|a|an) " + _SETUP_ADJ
    + r"(?:" + _SETUP_TARGET + r"|card|ui|api|server|service|daemon|wizard|chat|models?|build|"
    r"env(?:ironment)?|container|install|setup))"
)


def _alternation(phrases: Iterable[str]) -> str:
    """``phrases`` as one longest-first regex alternation (literal, escaped)."""
    alts = sorted({p.lower() for p in phrases}, key=len, reverse=True)
    return "(?:" + "|".join(re.escape(p) for p in alts) + ")"


_VAGUE_TROUBLE_RE = _alternation(_VAGUE_TROUBLE)
_RIG_TROUBLE = (
    r"\b" + _RIG_NOUN + r"\b[\s\S]{0,120}?\b" + _VAGUE_TROUBLE_RE + r"\b|"
    r"\b" + _VAGUE_TROUBLE_RE + r"\b[\s\S]{0,120}?\b" + _RIG_NOUN + r"\b|"
    # "fix ollama", "repair my setup": the bare verb with the rig as its object.
    r"\b(?:fix|repair)(?: \w+){0,2}? " + _RIG_NOUN + r"\b"
)

# --- Ops: the rig doctor -----------------------------------------------------
# Named so the setup concierge can derive its trouble vetoes from them (see
# _veto_vocabulary); they sit first in SPECIALIST_RULES.
_INSTALL_MEDIC = SpecialistRule(
    profile="install-medic",
    keywords=(
        "install", "installed", "installing", "installation", "reinstall", "uninstall",
        "traceback", "exception", "error", "errors", "failed", "failing", "fails",
        "failure", "crash", "crashed", "crashes", "pip", "pip3", "apt", "apt-get",
        "conda", "wheel", "wheels", "dependency", "dependencies", "module not found",
        "no module named", "port already in use", "address already in use",
        "exit code", "non-zero exit", "setup failed", "connection refused",
        "ollama is not running", "ollama not running", "ollama serve", "startup",
        "won't boot", "requirements.txt",
    ),
    # Vague trouble words need a second signal: a strong word above, the
    # _RIG_TROUBLE pattern below or continuity (review 2026-09-02, R3).
    weak_keywords=_VAGUE_TROUBLE,
    patterns=(
        r"Traceback \(most recent call last\)",
        r"\b(?:ModuleNotFoundError|ImportError|OSError|PermissionError|FileNotFoundError|"
        r"ConnectionRefusedError|ConnectionError|TimeoutError|HTTPError|URLError|"
        r"CalledProcessError|EnvironmentError|SubprocessError)\b",
        r"\bexit(?:ed)?\s+(?:with\s+)?(?:code|status)\s+[1-9]\d*\b",
        r"\berrno\s+\d+\b",
        r"\bE:\s+(?:Unable to|Failed|Could not|Package)\b",
        r"\bcould not (?:find a version|install|build wheels?)\b",
        r"\b(?:segmentation fault|core dumped)\b",
        _RIG_TROUBLE,
    ),
    state=("failed_job", "receipts_unhealthy", "storage_unavailable", "storage_warnings"),
    weight=1.0,
)
_GPU_TRIAGE = SpecialistRule(
    profile="gpu-triage",
    keywords=(
        "gpu", "gpus", "vram", "cuda", "driver", "drivers", "nvidia-smi", "nvidia smi",
        "nvml", "no devices", "no devices were found", "cudnn", "cublas", "nvcc",
        "compute capability", "gpu not detected", "no gpu", "cpu mode", "cpu only",
        "gpu utilization", "gpu temp", "gpu temperature", "thermal", "throttling",
        "throttle", "running hot", "too hot", "overheating", "overheats", "overheated",
        "fan noise", "loud fan", "loud fans", "xid", "nvidia.ko", "kernel module", "dkms",
        "torch.cuda", "cuda_visible_devices", "is_available", "device not found", "nouveau",
        "secure boot", "persistence mode", "nvidia-persistenced", "power limit", "power draw",
        "fan speed", "fan curve", "gpu fan", "gpu clocks",
    ),
    # Bare device nouns: the chip, the architecture, the CPU, the compute
    # capability. They name the machine, not a fault, so they count only
    # beside a trouble word or a pattern ("my gb10 shows xid 79"); alone,
    # "just got my gb10 spark, how do I get started?" is onboarding, not
    # triage (review 2026-09-02, F5). Weak keywords also stay out of the
    # setup concierge's derived veto set (_veto_vocabulary reads strong ones).
    # The vague trouble words are weak here too (R3; see _INSTALL_MEDIC).
    weak_keywords=_VAGUE_TROUBLE + ("gb10", "gb300", "blackwell", "grace", "sm_121"),
    patterns=(
        r"\bnvidia-smi\b",
        r"NVIDIA-SMI has failed",
        r"could(?:n't| not) communicate with the NVIDIA driver",
        r"\bno devices were found\b",
        r"driver/library version mismatch",
        r"\bxid\b\s*\d+",
        r"\bCUDA_ERROR_(?!OUT_OF_MEMORY)\w+",
        r"\bcudaError\w*\b",
        r"\bcuda (?:is )?not available\b|torch\.cuda\.is_available\(\)\s*(?:==|is|returns?)\s*False",
        # Fan noise on the rig: "the fans are spinning really loud", "fan is
        # screaming". A smart-home fan is the home-assistant rule's verb +
        # object ("turn on the ceiling fan"); a fan that *is loud* is the box.
        r"\bfans? (?:is|are|keeps?|sounds?|spinning|running|ramping|going)(?: \w+){0,2}? "
        r"(?:loud|noisy|screaming|roaring|whining|maxed(?: out)?|at 100 ?%|full (?:speed|blast|tilt))\b",
        _RIG_TROUBLE,
    ),
    state=("gpu_missing",),
    weight=1.0,
)
_RIG_DOCTOR_RULES: tuple[SpecialistRule, ...] = (_INSTALL_MEDIC, _GPU_TRIAGE)


def _veto_vocabulary(
    rules: Iterable[SpecialistRule], *, claimed: Iterable[str] = (),
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """The strong keywords and patterns of ``rules`` as one veto set, minus ``claimed``.

    A rule that must never out-score another on state alone derives its
    vetoes this way: every phrase that would send the turn to one of
    ``rules`` on its own also zeroes the deriving rule, and a keyword added
    to the source rule is a veto the day it lands. ``claimed`` names the
    words the deriving rule binds in its own patterns and must keep (the
    concierge's "install nvhive" / "just installed it"). Order is preserved
    and duplicates (``_RIG_TROUBLE`` sits on both rig-doctor rules) drop.
    Weak keywords are not read: they never route on their own.
    """
    drop = {w.lower() for w in claimed}
    words = tuple(dict.fromkeys(k for r in rules for k in r.keywords if k not in drop))
    patterns = tuple(dict.fromkeys(p for r in rules for p in r.patterns))
    return words, patterns


# The medic's install verbs the concierge binds to nvhive in its own patterns
# ("install nvhive", "just installed it"): a pattern's 1.5 beats the medic's
# bare keyword (1.0), so they need no veto and must not get one.
_CONCIERGE_CLAIMED_VERBS: tuple[str, ...] = ("install", "installed", "installing")
_RIG_DOCTOR_VETO_WORDS, _RIG_DOCTOR_VETO_PATTERNS = _veto_vocabulary(
    _RIG_DOCTOR_RULES, claimed=_CONCIERGE_CLAIMED_VERBS,
)

# --- Ops: the latency tuner and the fine-tune advisor ------------------------
# Named so their vetoes derive (review 2026-09-02, R1 / R2).
#
# The tuner's vetoes are the medic's strong words and patterns: an engine
# name ("vllm", "llama.cpp", "flash-attn", "tensorrt") beside "install
# failed" / "exit code 1" / "error" is an install problem, not a speed one,
# and the tuner's Spark boost must never out-score them. Only the medic's:
# the triage's strong words name the hardware the tuner tunes ("3 tok/s on
# my gpu", "vram bandwidth"). The tuner's own strong words are the setup
# concierge's vetoes in turn ("new box, why is it slow?" is a tuning
# question, not a tour).
_INSTALL_MEDIC_VETO_WORDS, _INSTALL_MEDIC_VETO_PATTERNS = _veto_vocabulary((_INSTALL_MEDIC,))
_LATENCY_TUNER = SpecialistRule(
    profile="latency-tuner",
    keywords=(
        "tok/s", "tokens/s", "tokens per second", "tokens/sec", "tps", "slow", "slowly",
        "sluggish", "laggy", "latency", "speed up", "too slow",
        "takes forever", "time to first token", "ttft", "throughput", "batch size",
        "num_gpu", "num_thread", "flash attention", "flash-attn", "speculative decoding",
        "tensor parallel", "vllm", "llama.cpp", "sglang", "tensorrt", "trt-llm",
        "prompt eval", "eval rate", "generation speed", "inference speed",
        "response time", "crawling", "crawls",
    ),
    # "fan speed", "internet speed", "faster download", "network bandwidth".
    weak_keywords=("speed", "faster", "bandwidth"),
    excludes=_INSTALL_MEDIC_VETO_WORDS,
    exclude_patterns=_INSTALL_MEDIC_VETO_PATTERNS,
    patterns=(
        r"\d+(?:\.\d+)?\s?(?:tok|tokens?)\s?/\s?s(?:ec)?\b",
        r"\btok/s\b|\bt/s\b|\btps\b",
        r"\d+(?:\.\d+)?\s?gb/s\b",
    ),
    # One group, as the sommelier's (R1): a Spark is both dgx-spark and
    # unified_memory, so two bare predicates stacked to 1.2 on every Spark
    # turn and one engine name (1.0 + 1.2) beat two medic words (2.0).
    state=("device:dgx-spark|device:rtx-spark|unified_memory",),
    weight=1.0,
)
_TUNER_VETO_WORDS, _TUNER_VETO_PATTERNS = _veto_vocabulary((_LATENCY_TUNER,))

# The fine-tune desk. Its strong words veto the planner and the sommelier
# (R2): "how do I fine-tune a 7b on this" carries the planner's bare size
# token (1.5) against the advisor's keyword (1.2), but the answer is a
# training recipe, not a fit check or a pick. Weak "adapter" / "checkpoint"
# stay out of the veto set ("the power adapter for my spark").
_FINETUNE_ADVISOR = SpecialistRule(
    profile="finetune-advisor",
    keywords=(
        "fine-tune", "fine tune", "finetune", "fine-tuning", "fine tuning", "finetuning",
        "lora", "qlora", "dora", "peft", "unsloth", "llama-factory", "llamafactory",
        "axolotl", "nemo", "trl", "sft", "dpo", "rlhf", "grpo", "training data",
        "dataset", "datasets", "epochs", "epoch", "learning rate", "train a model",
        "train my own", "train my", "training run",
        "merge lora", "distill", "distillation", "instruction tuning", "instruction-tune",
        "jsonl", "train on my",
    ),
    # "the power adapter for my spark", "resume from checkpoint" (ComfyUI).
    weak_keywords=("adapter", "adapters", "checkpoint"),
    weight=1.2,
)
_FINETUNE_VETO_WORDS, _ = _veto_vocabulary((_FINETUNE_ADVISOR,))

# --- Ops: the model desk's shared vocabulary ----------------------------------
# Model families the sommelier and the librarian recognise by name, with an
# optional version or tag glued on ("qwen3", "llama3.1:70b", "gpt-oss:120b",
# "nemotron"). The lookahead keeps "phi" out of "philosophy" and "llama" out
# of "llamas"; a family must start after a space, so "ollama" never counts.
# The inference engine is not a model family. _MODEL_FAMILY's lookahead keeps
# it out of the patterns; these keep it out of the sommelier's bare "llama"
# keyword, so an engine question ("llama.cpp build is failing", "which model
# should I run with llama.cpp") is the medic's or the tuner's, not a pick.
_ENGINE_NOT_A_FAMILY: tuple[str, ...] = ("llama.cpp", "llama-cpp", "llama cpp", "llamacpp")

_MODEL_FAMILY = (
    r"(?!llama[-._]?cpp\b)"  # the inference engine is not a model family
    r"(?:llama|qwen|gemma|mistral|mixtral|deepseek|nemotron|phi|granite|codestral|devstral|"
    r"starcoder|codellama|glm|kimi|olmo|smollm|falcon|gpt-oss)(?![a-z])[-.:\w]*"
)
# A parameter-count size token: "70b", "30B", "1.5b", "120b" (not "24 gb").
_SIZE_TOKEN = r"\d{1,3}(?:\.\d+)?\s?b(?![a-z0-9])"
# A model named by size or family, the object of "should I pull the 120b" /
# "is a 30b MoE worth it" / "delete the 70b models I never use".
_SIZED_OR_NAMED = "(?:" + _SIZE_TOKEN + "|" + _MODEL_FAMILY + ")"
# The object of a recommendation question: the noun, a size or a family.
_MODEL_OBJECT = "(?:(?:model|llm)s?|" + _SIZE_TOKEN + "|" + _MODEL_FAMILY + ")"

# Shelf vocabulary is the librarian's. It vetoes the sommelier ("which models
# can I delete" is not a recommendation) and the planner ("delete the 70b
# models I never use" is not arithmetic, whatever the size token says).
_SHELF_VETOES: tuple[str, ...] = (
    "delete", "remove", "prune", "uninstall", "get rid of", "unused", "disk space",
    "free up", "clean up", "cleanup", "ollama rm", "ollama list", "ollama ls",
)
# Bare "disk" vetoes the sommelier (a recommendation never needs the word) but
# not the planner: "spill to disk" / "mmap from disk" is sizing talk. The
# planner takes the shelf's disk *phrases* instead ("how much disk do my 30b
# models take" is the librarian's).
_DISK_SHELF_PHRASES: tuple[str, ...] = ("how much disk", "disk usage")
# Listing the shelf vetoes the planner too. Not the sommelier: "which of my
# installed models is best for coding" is a recommendation.
_SHELF_LISTING: tuple[str, ...] = (
    "list installed", "what's installed", "whats installed", "what is installed",
    "installed models", "models installed", "list models", "list my models", "show models",
    "show my models",
)

# A model tag as Ollama prints it: "llama3.1:70b", "deepcoder:14b",
# "hf.co/org/repo:q4_k_m", "mistral:latest".
_MODEL_TAG = r"[a-z0-9._/-]+:(?:\d+(?:\.\d+)?b|latest|instruct|q\d(?:_[a-z0-9]+)*)"
# The object of a shelf verb: the noun, a size, a family or a tag.
_SHELF_OBJECT = r"(?:models?\b|" + _SIZED_OR_NAMED + "|" + _MODEL_TAG + ")"
# Shelf verbs that fetch (review 2026-09-02, F4): "pull qwen3", "download the
# 70b", "grab gemma3 for me", "fetch llama3.1:70b", "install a model". The
# librarian's verb + object pattern takes them; as an *imperative* (sentence
# start, or after "please" / "can you" / "how do I" / "want to" ...) they also
# veto the sommelier and the planner, so "pull qwen3" is not a recommendation
# (the sommelier's family + version pattern plus the Spark boost) and
# "download the 70b" is not sizing (the planner's bare size token). A
# recommendation shell in front of the verb ("which model should I pull",
# "should I pull the 120b", "what to pull") has no lead here and vetoes the
# librarian instead (_FETCH_RECOMMENDATION), so those stay the sommelier's.
_FETCH_VERB = r"(?:pull|download|grab|fetch|install)"
_FETCH_FILLER = r"(?: (?:me|us|the|a|an|that|this|my|some|another|all|both|latest|newest|new))*"
_IMPERATIVE_LEAD = (
    r"(?:^|[.!?;:,]\s*|\b(?:please|just|go|now|and|then|also|ok|okay|to|gonna|let'?s|"
    r"can you|could you|would you|will you|help me|need to|want to|like to|try to|"
    r"how (?:do|can|should|would) (?:i|we))\s+)"
)
_FETCH_IMPERATIVE = _IMPERATIVE_LEAD + _FETCH_VERB + _FETCH_FILLER + " " + _SHELF_OBJECT
_FETCH_RECOMMENDATION: tuple[str, ...] = (
    r"\b(?:which|what) (?:\w+ ){0,3}?(?:should|would|do you|to|can|could) (?:\w+ ){0,2}?"
    + _FETCH_VERB + r"\b",
    r"\bshould (?:i|we) (?:\w+ ){0,2}?" + _FETCH_VERB + r"\b",
    r"\brecommend\w* (?:\w+ ){0,3}?(?:to )?" + _FETCH_VERB + r"\b",
)

# Sizing arithmetic is the planner's (review 2026-09-02, F2). These phrases
# veto the sommelier so a family name beside them ("how much vram does
# qwen3:32b need") is a number question, not a pick. GB figures are not here
# ("which model fits my 128 GB spark" is a recommendation) and neither is
# bare "fit" ("what fits on my spark", "which model would fit" are the
# sommelier's): the fit *check* below needs a named or sized subject.
_ARITHMETIC_VETOES: tuple[str, ...] = (
    "how much vram", "how much memory", "how much ram", "enough vram", "enough memory",
    "enough ram", "too big", "too large", "will it fit", "will that fit", "out of memory",
    "oom", "kv cache", "kv-cache", "memory footprint",
)
# "will llama3.1:70b fit on my card", "does a 70b fit in 24 gb", "is the 120b
# too big for my spark", "can I run qwen3:32b on my spark": a yes/no check on
# a named or sized model. Not "which model would fit" (the object is the bare
# noun and nothing stands between the auxiliary and the verb) and not
# "should I run qwen3:32b or llama3.1:70b" ("should" is a recommendation).
_FIT_CHECK = (
    r"\b(?:will|would|does|do|can|could|is|are) (?:a |an |the |this |that |my |our )?"
    r"(?:\w+ ){0,2}?(?:" + _SIZED_OR_NAMED + r"|it|that|this|the model|this model|that model)"
    r"(?: \w+){0,3}? (?:fits?|too (?:big|large|heavy))\b|"
    r"\b(?:can|could|will|would) (?:i|we) (?:\w+ )?(?:run|load|fit|squeeze) (?:a |an |the |this |that )?"
    + _SIZED_OR_NAMED + r"(?: \w+){0,2}? (?:on|in|into|within|at|with) (?:my |the |a |an |this )?"
    r"(?:\d{1,4}\s?(?:gb|gib)|\w*gb|card|gpu|v?ram|memory|spark|dgx|box|machine|rig|\d{4})\b"
)
_ARITHMETIC_VETO_PATTERNS: tuple[str, ...] = (_FIT_CHECK,)

SPECIALIST_RULES: tuple[SpecialistRule, ...] = (
    # --- Ops: the rig doctor -----------------------------------------------
    _INSTALL_MEDIC,
    _GPU_TRIAGE,
    # --- Ops: the setup concierge -------------------------------------------
    # The first-run guide (proposal §3.2). It sits after the rig doctor so a
    # trouble word that ties stays with the medic, and before the model desk
    # so an onboarding phrase on a fresh box ("how do I get started?") is the
    # concierge's. Strong keywords are onboarding phrases only. "set up" /
    # "setup" / "first time" / "configure" are weak: "set up ssh keys" and
    # "set up docker" belong elsewhere, and a bare "set this up", "how do I
    # begin", "just installed it" or "install nvhive" reaches the concierge
    # through the patterns instead (a pattern's 1.5 beats the medic's
    # one-word "install" / "installed", where a keyword's 1.0 would only tie
    # and lose). "just got" is a pattern with a device noun ("just got my dgx
    # spark"), so "just got this error" stays with the medic even on a Spark.
    #
    # Three guards keep it from stealing turns (review 2026-09-02):
    # (1) state is at most two boosts — first_run|no_models counts once, and
    #     the device only on a first run — so a Spark whose Ollama is merely
    #     down (no_models true, first_run false) lifts an onboarding word by
    #     0.6, not 1.2, and a fresh Spark by 1.2, not 1.8;
    # (2) phrase_once: "just installed nvhive" is one pattern hit (1.5), not
    #     keyword + pattern (2.5), and "just got my new spark" does not also
    #     count "new spark";
    # (3) the rig doctor's vocabulary and the model desk's are vetoes: "my
    #     gpu is broken, I just got my spark" is gpu-triage's and "just got
    #     my spark, which model should I pull first?" is the sommelier's,
    #     whatever the state boosts would have added. The rig doctor's set
    #     is derived from the medic's and the triage's own strong keywords
    #     and patterns (_veto_vocabulary), so "connection refused", "exit
    #     code 1", "xid 79" or a traceback on a fresh Spark can never be
    #     out-scored by the onboarding pattern plus two state boosts.
    SpecialistRule(
        profile="setup-concierge",
        keywords=(
            "get started", "getting started", "first time here", "first time user",
            "first-time user", "new here", "new user", "new machine", "new box", "new spark",
            "new dgx", "brand new", "just arrived", "unbox", "unboxed",
            "unboxing", "onboard", "onboarding", "what should i do first", "what do i do first",
            "what do i need first", "where do i start", "where do i begin", "where to start",
            "where to begin", "first run", "first-run", "first steps", "first launch",
            "first boot", "fresh install", "initial setup", "quick start", "quickstart",
        ),
        weak_keywords=("set up", "setup", "set-up", "first time", "configure"),
        excludes=(
            # Other specialists' domains.
            "ssh", "ssh key", "ssh keys", "ssh-keygen", "docker", "dockerfile", "podman",
            "container", "containers", "kubernetes", "k8s", "home assistant", "homeassistant",
            "home-assistant", "hass", "torch", "pytorch", "tensorflow", "comfyui", "comfy",
            "fine-tune", "fine tune", "finetune", "fine-tuning", "lora", "unsloth",
            # The model desk's: a model question on a new box is a model question.
            "model", "models", "fit", "fits", "vram", "quant", "quants", "quantization",
            "quantisation", "quantized", "quantised", "gguf", "context length", "context window",
            "num_ctx", "moe",
        )
        # The rig doctor's, derived: trouble on a fresh box is a repair, not
        # a tour, and every strong medic / triage word says trouble.
        + _RIG_DOCTOR_VETO_WORDS
        # The doctor's vague trouble words are weak there (R3: they need a
        # rig noun beside them) but stay a veto here by hand: "new here, it's
        # broken" is not asking for a tour, whoever ends up taking it.
        + _VAGUE_TROUBLE
        # The tuner's, derived (R1): with one Spark boost each, "new box, why
        # is it slow?" on a Spark whose Ollama is down would tie the tuner's
        # "slow" and go to the earlier rule; a slow new box is not a tour.
        + _TUNER_VETO_WORDS,
        exclude_patterns=_RIG_DOCTOR_VETO_PATTERNS + _TUNER_VETO_PATTERNS,
        patterns=(
            # "how do I get started" is the keyword's; "how do I set this up"
            # is the set-up pattern's. Neither phrase lives in two places.
            r"\bhow (?:do|can|should|would) (?:i|we) (?:get set up|get going|begin)\b",
            r"\bhow (?:do|can|should) (?:i|we) start\s*[?!.]*\s*$",
            # "set this up", "help me set the spark up".
            r"\bset (?:this|it|things|everything|nvhive|nvh|(?:the|my|our|this) "
            + _SETUP_ADJ + _SETUP_TARGET + r") up\b",
            # "set up my spark", "help me set up the spark", "setting up this
            # box", "setup my new dgx" (review 2026-09-02, F3). The object is
            # the machine or the product (_SETUP_TARGET); another specialist's
            # noun ("set up docker", "set up ssh keys") is vetoed above.
            r"\bset(?:ting)?[- ]?up (?:my|the|this|our|your) " + _SETUP_ADJ + _SETUP_TARGET + r"\b",
            # "help me set up" / "help me get set up" as the whole ask.
            r"\bhelp me (?:to )?(?:get )?set[- ]?up\s*[?!.,]*\s*$",
            # "configure this thing", "configure my spark".
            r"\bconfigure (?:this|my|the|our|your) " + _SETUP_ADJ + _SETUP_TARGET + r"\b",
            # "get my spark ready", "get the box up and running".
            r"\bget (?:my|the|this|our|your) " + _SETUP_ADJ + _SETUP_TARGET
            + r" (?:ready|going|started|online|set up|up and running)\b",
            r"\b(?:just installed(?: (?:nvhive|nvh|it|this|everything))?|"
            r"(?:install|installed|installing|configure|configuring|set ?up|setting up|"
            r"new to|start(?:ing)? with) (?:nvhive|nvh))\b",
            # "just got my dgx spark", "just got my gb10 spark": the device
            # adjectives (_SETUP_ADJ) ride along, so the chip name in front
            # of the noun does not break the phrase.
            r"\bjust (?:got|bought|received|unboxed|picked up) (?:my|a|an|the|our|this) "
            + _SETUP_ADJ
            + r"(?:dgx|spark|rtx|box|machine|rig|workstation|laptop|desktop|pc|computer|unit|system)\b",
        ),
        state=("first_run|no_models", "first_run&device:dgx-spark", "first_run&device:rtx-spark"),
        phrase_once=True,
        weight=1.0,
    ),
    # --- Ops: the model desk ------------------------------------------------
    # Three rules, no strong keyword in common. The sommelier (first, so a
    # tie is its) answers "which model should I run / what fits / best model
    # for coding on this box / MoE vs dense / which quant / context length"
    # and carries the device, unified-memory and no_models boosts as two
    # groups (a Spark is both dgx-spark and unified_memory: one boost). The
    # planner keeps sizing arithmetic — "will 70B Q4 fit in 24 GB", "how much
    # memory", "kv cache", OOM — with no state. The librarian keeps the shelf:
    # "what's installed", "delete unused models", "disk space", ``ollama rm``.
    # A size token (70b) is the planner's pattern, a model family with a
    # version (qwen3, llama3.1) the sommelier's, a tag (llama3.1:70b) the
    # librarian's; the words around them decide, and a bare tag ties to the
    # sommelier. All three are phrase_once so "ollama pull", "out of memory"
    # and "which model fits" each score once.
    SpecialistRule(
        profile="model-sommelier",
        keywords=(
            "which model", "what model", "which llm", "what llm", "best model", "best llm",
            "good model", "right model", "decent model", "recommend a model",
            "recommend me a model", "recommend a local model", "recommend an llm",
            "model recommendation", "model recommendations", "model should i", "model would you",
            "model do you recommend", "model do you suggest", "model to use", "model to run",
            "model to start with", "model fits", "model that fits", "model would fit",
            "model will fit", "models fit", "what fits", "what would fit", "what can i run",
            "what could i run", "what should i run", "what should i pull", "which should i pull",
            "what to pull", "which one should i pull", "what should i download",
            "which should i download", "what can i pull", "what could i pull", "run a model",
            "run a local model",
            "run local models", "run an llm", "run a local llm", "run llms", "first model",
            "starter model", "smallest model", "no models", "no local models", "moe",
            "mixture of experts", "mixture-of-experts", "moe model", "moe models", "dense model",
            "dense models", "quant", "quants", "quantization", "quantisation", "quantized",
            "quantised", "which quant", "what quant", "q4", "q5", "q6", "q8", "q4_k_m", "q5_k_m",
            "q6_k", "q8_0", "q4_0", "iq4_xs", "gguf", "awq", "gptq", "exl2", "nvfp4", "fp8",
            "fp16", "bf16", "int4", "int8", "context length", "context window", "num_ctx",
            "coding model", "code model", "chat model", "reasoning model", "vision model",
            "embedding model", "model for coding", "model for code", "llama", "qwen", "gemma",
            "mistral", "mixtral", "deepseek", "nemotron", "gpt-oss", "granite", "codestral",
            "devstral",
        ),
        # "does this fit my workflow", "the power adapter for my spark", "run
        # it locally": place and purpose words count next to a model word.
        weak_keywords=(
            "fit", "fits", "locally", "run locally", "on this box", "on this machine", "this box",
            "this machine", "my spark", "the spark", "my box", "my machine", "for coding",
            "for code", "for python", "for chat", "for rag", "for agents", "for writing",
            "for summarization", "for summarisation", "for tool calling", "for tool use",
            "should i run", "should i use", "should i pull", "which models", "what models",
            "model for", "models for", "trade-off", "tradeoff", "trade off", "faster", "smarter",
            "better",
        ),
        # Shelf management is the librarian's; sizing arithmetic asked of a
        # named model is the planner's (review 2026-09-02, F2): "how much
        # vram does qwen3:32b need" and "will llama3.1:70b fit on my card"
        # name a family, but the answer is a number, not a pick. An
        # imperative fetch ("pull qwen3") is the librarian's (F4). The
        # recommendation shells ("which qwen3 should I run", "should I pull
        # the 120b", "what fits on my spark") match none of these. Fine-tune
        # vocabulary is the advisor's (R2): "best base model for lora" is a
        # training question. The engine spellings carve "llama.cpp" out of
        # the bare "llama" family keyword the way _MODEL_FAMILY's lookahead
        # carves it out of the patterns: without them "llama.cpp build is
        # failing with an error" scored 'llama' (1.0) and, on a fresh Spark,
        # two boosts (2.2) beat the medic's two trouble words (2.0).
        excludes=_SHELF_VETOES + ("disk",) + _ARITHMETIC_VETOES + _FINETUNE_VETO_WORDS
        + _ENGINE_NOT_A_FAMILY,
        exclude_patterns=_ARITHMETIC_VETO_PATTERNS + (_FETCH_IMPERATIVE,),
        patterns=(
            # "which model fits", "what 70b model should I run", "which of my
            # installed models is best for coding", "which 70b should I run",
            # "what 30b would you recommend", "which qwen3 should I run": the
            # object is the model noun, a size token or a family name.
            r"\b(?:which|what) (?:\w+ ){0,3}" + _MODEL_OBJECT + r" (?:\w+ ){0,2}"
            r"(?:fits?|should|would|do you|to run|to use|can i|could i|for|on my|on this|here|"
            r"best|good|better)\b",
            # "should I pull the 120b", "should I run qwen3:32b or llama3.1:70b".
            r"\bshould (?:i|we) (?:\w+ ){0,3}" + _SIZED_OR_NAMED,
            # "is a 30b MoE worth it here", "is nemotron any good for coding".
            r"\b(?:is|are|would) (?:a |an |the |this |that |my )?" + _SIZED_OR_NAMED
            + r"(?: \w+){0,3}? (?:worth|any good|good|better|decent|overkill|enough|fine|okay|ok|"
            r"recommended|the (?:right|best) (?:pick|choice|call))\b",
            # A model family with a version: qwen3, llama3.1, gemma3, phi-4.
            r"\b(?:llama|qwen|gemma|mistral|mixtral|deepseek|nemotron|phi|granite|codestral|"
            r"devstral|starcoder|codellama|glm|kimi|olmo|smollm|falcon)[-.]?\d",
            r"\b(?:moe|mixture[- ]of[- ]experts) (?:vs\.?|versus|or) dense\b|"
            r"\bdense (?:vs\.?|versus|or) (?:moe|mixture[- ]of[- ]experts)\b",
        ),
        state=("device:dgx-spark|device:rtx-spark|unified_memory", "no_models"),
        phrase_once=True,
        weight=1.0,
    ),
    SpecialistRule(
        profile="vram-planner",
        keywords=(
            "will it fit", "will that fit", "fit in", "fit on", "fit into", "fits in", "fits on",
            "fits into", "too big", "too large", "big enough", "how big a model",
            "how large a model", "largest model", "biggest model", "max model size",
            "maximum model size", "parameter count", "kv cache", "kv-cache", "out of memory",
            "oom", "memory footprint", "how much memory", "how much vram", "how much ram",
            "memavailable", "enough memory", "enough vram", "enough ram", "unified memory",
            "offload", "cpu offload", "offloading", "headroom", "memory budget", "vram budget",
            "bytes per parameter", "weights take", "model weights", "weight memory",
        ),
        # "what parameters does this function take" / "does this fit my
        # workflow" / "can I run this script" are not sizing questions alone.
        weak_keywords=("fit", "parameters", "can i run", "could i run", "can my", "context", "tokens"),
        # The shelf is the librarian's: "delete the 70b models I never use",
        # "list installed 70b models" and "how much disk do my 30b models
        # take" are not arithmetic, whatever the size token says. Bare "disk"
        # is not a veto here ("spill to disk" is). Fine-tune vocabulary is the
        # advisor's (R2): "how do I fine-tune a 7b on this" carries a size
        # token but asks for a training recipe, not a fit check.
        excludes=_SHELF_VETOES + _SHELF_LISTING + _DISK_SHELF_PHRASES + _FINETUNE_VETO_WORDS,
        # "download the 70b" is a shelf action, whatever the size token says.
        exclude_patterns=(_FETCH_IMPERATIVE,),
        patterns=(
            r"\b\d{1,4}\s?(?:gb|gib|tb)\b",
            r"\b\d{1,3}(?:\.\d+)?\s?b\b(?![a-z])",
            r"out[- ]of[- ]memory|\boom\b|OutOfMemoryError|oom[- ]?kill(?:er|ed)?|"
            r"CUDA_ERROR_OUT_OF_MEMORY|killed process",
            r"\b\d{1,3}k\s?(?:context|ctx|tokens?)\b",
            # Arithmetic asked of a named or sized model (review 2026-09-02,
            # F2): "how much vram does qwen3:32b need", "will llama3.1:70b
            # fit on my card", "enough vram for gemma3:27b", "is the 120b too
            # big for my spark". A pattern's 1.5 matches the sommelier's
            # family + version pattern, and the same phrases veto the
            # sommelier (_ARITHMETIC_VETOES / _FIT_CHECK), so the Spark
            # boost cannot flip a family-named sizing question back.
            r"\bhow much (?:v?ram|(?:gpu |unified |system )?memory|headroom)\b",
            _FIT_CHECK,
            r"\b(?:enough|sufficient) (?:v?ram|memory|headroom) (?:for|to)\b",
            r"\btoo (?:big|large|heavy) (?:for|to)\b",
        ),
        phrase_once=True,
        weight=1.0,
    ),
    SpecialistRule(
        profile="model-librarian",
        keywords=(
            "what's installed", "whats installed", "what is installed", "what do i have installed",
            "what have i installed", "what have i got", "installed models", "models installed",
            "models do i have", "models have i", "models are installed", "list models",
            "list my models", "list the models", "show models", "show my models", "ollama",
            "ollama models", "pull a model", "pull model", "pull the model", "pull that model",
            "pull this model", "download a model", "download model", "download the model",
            "download that model", "delete a model", "delete the model", "delete models",
            "delete unused", "remove a model", "remove the model", "remove models",
            "remove unused", "unused models", "old models", "stale models", "uninstall a model",
            "disk space", "disk usage", "how much disk", "how much space", "taking up space",
            "free up space", "free up disk", "on disk", "model library", "my shelf", "the shelf",
            "model shelf", "huggingface", "hugging face", "hf.co", "model card", "modelfile",
            "already pulled", "already downloaded", "what's pulled", "whats pulled",
            "list installed",
        ),
        # "export the model to onnx", "delete my notes", "pull the latest
        # changes": shelf verbs and model nouns count next to each other.
        weak_keywords=(
            "delete", "remove", "prune", "rm", "pull", "pulled", "download", "downloaded",
            "my models", "local models", "models", "model", "disk", "space", "shelf", "clean up",
            "cleanup", "which models", "what models", "llama", "qwen", "gemma", "mistral",
            "deepseek", "nemotron", "gpt-oss",
        ),
        patterns=(
            r"\bollama (?:pull|run|list|ls|rm|show|cp|create|push)\b",
            r"\b[a-z0-9._-]+:(?:\d+b|latest|instruct|q\d(?:_[a-z0-9]+)*)\b",
            # "what's installed", "what do I have pulled", "what have I got on disk".
            r"\bwhat(?:'s|s| is| do i have| have i(?: got)?) (?:already |currently )?"
            r"(?:installed|pulled|downloaded|on (?:the |my )?(?:shelf|disk|box|machine))\b",
            # "what models are installed", "which models do I have", "how many
            # models can I delete".
            r"\b(?:which|what|how many) (?:local |ollama |llm )?models? (?:\w+ ){0,2}"
            r"(?:do i have|have i|are (?:installed|pulled|downloaded|available|on)|is installed|"
            r"installed|pulled|downloaded|on disk|on (?:the |my )shelf|"
            r"can i (?:delete|remove|drop|prune)|should i (?:delete|remove|drop|prune|keep)|"
            r"to (?:delete|remove|keep))\b",
            # "delete unused models", "get rid of the models I never use",
            # "free up disk space".
            # "delete the 70b models I never use", "remove the 120b", "get rid
            # of qwen3": the object is a model noun, a size or a family.
            r"\b(?:delete|remove|rm|uninstall|prune|purge|get rid of|clean(?: up| out)?|"
            r"clear(?: out)?|free up|drop)\b(?: \w+){0,4}? "
            r"(?:(?:models?|ollama|shelf|disk space|space)\b|" + _SIZED_OR_NAMED + r")",
            # "pull qwen3", "download the 70b", "grab gemma3 for me", "fetch
            # llama3.1:70b", "install a model" (review 2026-09-02, F4): a
            # fetch verb with a shelf object. Not "pull the latest changes"
            # or "download the pdf" (no model object), and not "install
            # llama.cpp" (_MODEL_FAMILY carves the engine out).
            r"\b" + _FETCH_VERB + r"\b" + _FETCH_FILLER + " " + _SHELF_OBJECT,
            # "list installed 70b models", "show me all the models I have".
            r"\b(?:list|show|see|view|display)(?: (?:me|all|my|the|our|every|each|installed|"
            r"local|pulled|downloaded|available|ollama|current))*(?: \w+){0,2}? "
            r"(?:models?|ollama|shelf)\b",
        ),
        # A recommendation shell in front of a fetch verb is the sommelier's:
        # "which model should I pull", "should I pull the 120b", "what to
        # pull", "recommend a model to pull". Without the veto the fetch
        # pattern above would tie the sommelier's own pattern at 1.5.
        exclude_patterns=_FETCH_RECOMMENDATION,
        phrase_once=True,
        weight=1.0,
    ),
    SpecialistRule(
        profile="provider-keysmith",
        keywords=(
            "api key", "api-key", "apikey", "api keys", "access token", "bearer",
            "credential", "credentials", "provider", "providers", "openai",
            "anthropic", "claude", "groq", "gemini", "google ai", "mistral api", "openrouter",
            "together ai", "deepseek api", "nvidia api", "build.nvidia.com", "quota",
            "rate limit", "rate limited", "rate-limited", "billing", "unauthorized",
            "unauthorised", "invalid key", "invalid api key", "insufficient_quota",
            "authentication", "auth failed", "env var", "environment variable",
            "free tier", "cloud model", "cloud provider", "validate my key", "validate key",
            "new key", "add a key", "set a key", "configure a key",
        ),
        # "the token limit", "the key question", "the secret to fast inference".
        weak_keywords=("token", "secret", "secrets", "my key", "the key"),
        patterns=(
            r"\b(?:401|403|429)\b",
            r"\bsk-[A-Za-z0-9_-]{8,}",
            r"\bnvapi-[A-Za-z0-9_-]{8,}",
            r"\bgsk_[A-Za-z0-9_-]{8,}",
            r"\bAIza[A-Za-z0-9_-]{8,}",
            r"invalid[_ ]api[_ ]key|incorrect api key|authentication[_ ]error|"
            r"insufficient[_ ]quota|rate[_ ]limit(?:ed)?\b|invalid x-api-key",
            r"\b[A-Z][A-Z0-9]*_API_KEY\b",
        ),
        state=("no_providers", "provider_unhealthy"),
        weight=1.0,
    ),
    # --- Ops: the tuner and the fine-tune desk ------------------------------
    # Defined above the table, named, so their vetoes derive: the tuner's
    # are the medic's words (R1), the advisor's words veto the model desk (R2).
    _LATENCY_TUNER,
    _FINETUNE_ADVISOR,
    # --- Smart home ---------------------------------------------------------
    # Object-gated: strong keywords are smart-home objects or platforms,
    # every pattern takes its object from _HA_OBJECT, rooms and ambiguous
    # nouns are weak ("entity" / "entities" are NER and database vocabulary
    # too: "extract entities from this text", "entities table schema"), and
    # rig vocabulary is a veto.
    SpecialistRule(
        profile="home-assistant",
        keywords=(
            "home assistant", "homeassistant", "home-assistant", "hass", "hassio", "hass.io",
            "home automation", "smart home", "smarthome", "entity_id", "entity id",
            "lights", "light bulb", "light bulbs", "bulb", "bulbs", "lamp", "lamps",
            "light switch", "light strip", "led strip", "porch light", "christmas lights",
            "thermostat", "thermostats", "the heating", "air conditioning", "air conditioner",
            "hvac", "ceiling fan", "smart plug", "smart plugs", "smart outlet", "smart switch",
            "smart lock", "door lock", "deadbolt", "garage door", "blinds", "roller shutter",
            "roller shutters", "robot vacuum", "roomba", "sprinkler", "sprinklers",
            "motion sensor", "door sensor", "doorbell", "smart speaker", "media_player",
            "zigbee", "z-wave", "zwave", "esphome", "tasmota", "mqtt", "philips hue",
            "hue bulb", "hue bulbs", "hue lights", "ecobee", "nest thermostat", "sonoff",
            "shelly",
        ),
        weak_keywords=(
            "entity", "entities", "automation", "automations", "scene", "scenes", "vacuum", "curtains",
            "shades", "shutters", "garage", "front door", "back door", "fan", "fans", "the ac",
            "heating", "the tv", "media player", "speaker", "speakers", "camera", "hue", "nest",
            "brightness", "living room", "lounge", "bedroom", "kitchen", "bathroom", "hallway",
            "porch", "patio", "nursery", "basement", "attic", "upstairs", "downstairs",
            "dining room",
        ),
        excludes=(
            "gpu", "gpus", "cuda", "nvidia", "nvidia-smi", "driver", "drivers", "vram",
            "ollama", "model", "models", "llm", "vllm", "ssh", "telemetry", "flash attention",
            "api server", "nvhive", "nvh", "persistence mode", "fan speed", "fan curve",
            "kernel", "bios", "docker", "python", "pip", "apt", "systemd", "systemctl",
            "comfyui",
        ),
        patterns=(
            # Entity ids: light.kitchen, switch.desk_lamp, cover.garage_door.
            r"\b(?:light|switch|sensor|binary_sensor|climate|cover|lock|fan|media_player|"
            r"automation|scene|script|vacuum|camera|input_boolean|input_number|number|select|"
            r"humidifier|water_heater|alarm_control_panel|weather|zone|person|device_tracker)"
            r"\.[a-z0-9_]+\b",
            # Verb + object: "turn off the living room lights", "dim the lamp",
            # "open the garage door", "lock the front door", "start the vacuum".
            r"\b(?:turn|switch|flip|dim|brighten|open|close|shut|raise|lower|lock|unlock|"
            r"start|stop|pause|toggle|activate|arm|disarm|mute|unmute)(?: (?:on|off|up|down))?"
            r" (?:the |my |our |all |all the |every |each )?(?:\w+ ){0,2}" + _HA_OBJECT + r"\b",
            # Setpoints: "set the thermostat to 68", "set the bedroom temperature to 20".
            r"\b(?:set|change|adjust|put) (?:the |my |our )?(?:\w+ ){0,2}"
            r"(?:thermostat|temperature|temp|heating|heat|ac|air ?con\w*|hvac|lights?|lamps?|"
            r"blinds?|shades?|(?:ceiling )?fan)\b(?: \w+){0,2} to \d+\s?(?:°|degrees|%|percent)?",
            # State: "is the garage door open", "are the lights off", "the door is locked".
            r"\b(?:is|are) (?:the |my |our )?(?:\w+ ){0,2}" + _HA_OBJECT
            + r" (?:still |currently )?(?:on|off|open|closed|locked|unlocked|running|home)\b",
            r"\b" + _HA_OBJECT + r" (?:is|are) (?:still |currently )?"
            r"(?:on|off|open|closed|locked|unlocked)\b",
            # Room climate: "what's the temperature in the living room".
            r"\b(?:temperature|temp|humidity) (?:in|of) the " + _HA_ROOM + r"\b",
            r"\bhow (?:warm|cold|hot) is (?:it in )?the " + _HA_ROOM + r"\b",
        ),
        weight=1.2,
    ),
    # --- Media / containers / shell ----------------------------------------
    SpecialistRule(
        profile="comfyui-workflow-debugger",
        keywords=(
            "comfyui", "comfy ui", "comfy",
            "custom node", "custom nodes", "ksampler", "checkpoint loader", "vae decode",
            "clip text encode", "controlnet", "ipadapter", "ip-adapter", "animatediff",
            "workflow.json", "missing node", "missing nodes", "red node", "red nodes",
            "comfyui manager", "stable diffusion", "sdxl", "sd1.5",
            "img2img", "txt2img", "inpaint", "inpainting",
        ),
        # "install node", "my github workflow fails", "random seed", "the cpu
        # scheduler": diffusion vocabulary only counts next to a ComfyUI word.
        weak_keywords=(
            "workflow", "workflows", "node", "nodes", "vae", "flux", "diffusion", "upscale",
            "upscaler", "latent", "sampler", "scheduler", "cfg", "denoise", "seed",
        ),
        patterns=(
            r"\bcomfy(?:ui)?\b",
            r"\b(?:KSampler|VAEDecode|VAEEncode|CLIPTextEncode|CheckpointLoaderSimple|"
            r"LoraLoader|EmptyLatentImage)\b",
            r"\bsd(?:xl|1\.5|3(?:\.5)?)\b",
        ),
        weight=1.2,
    ),
    SpecialistRule(
        profile="container-wrangler",
        keywords=(
            "docker", "dockerfile", "docker compose", "docker-compose", "compose.yaml",
            "compose.yml", "container", "containers", "containerized", "containerised",
            "podman", "nvidia-container-toolkit", "container toolkit", "nvidia runtime",
            "nvidia-docker", "ngc", "nvcr.io", "nvcr", "docker image", "container image",
            "pull the image", "kubernetes", "k8s", "kubectl", "helm",
            "bind mount", "docker group", "docker.sock", "entrypoint", "cgroup",
            "oci", "docker daemon", "ngc catalog", "nim container",
        ),
        # "windows registry key for cuda path" is not a container question.
        weak_keywords=("registry", "pod", "pods"),
        patterns=(
            r"\bdocker (?:run|ps|pull|build|compose|logs|exec|images?|start|stop|rm)\b",
            r"\bnvcr\.io/\S+",
            r"--gpus\s+all\b",
            r"\bFROM \S+:\S+",
            # The daemon socket, as Docker prints it (review 2026-09-02, R4):
            # "permission denied while trying to connect to the Docker daemon
            # socket at unix:///var/run/docker.sock", "Cannot connect to the
            # Docker daemon at unix:///var/run/docker.sock. Is the docker
            # daemon running?". The shell teacher's "permission denied" is
            # its pattern's 1.5 alone (phrase_once); this is the wrangler's.
            r"connect to the docker daemon|docker daemon socket|is the docker daemon running|"
            r"(?:unix://|/var/run/|/run/)\S*docker\.sock",
        ),
        # 1.1 so "install docker" goes to the container specialist rather
        # than the generic install medic on a one-word tie.
        weight=1.1,
    ),
    SpecialistRule(
        profile="shell-teacher",
        keywords=(
            "bash", "zsh", "shell", "command line", "command-line", "chmod",
            "chown", "sudo", "permission denied", "operation not permitted",
            "command not found", "no such file or directory", "$path",
            "cron", "crontab", "systemd", "systemctl", "grep", "awk", "sed", "find command",
            "ssh", "ssh key", "ssh keys", "ssh-keygen", "authorized_keys", "scp", "rsync",
            "tmux", ".bashrc", ".zshrc", "symlink", "ln -s",
            "curl", "wget", "one-liner", "one liner", "which command", "what command",
            "man page", "shell script", "script.sh", "xargs", "chsh", "ls -la", "cd into",
            "file permissions", "executable bit",
        ),
        # "export the model to onnx", "the nvh cli", "tool permissions".
        weak_keywords=(
            "terminal", "cli", "permissions", "export", "alias", "tar", "env var",
            "environment variable",
        ),
        patterns=(
            r"(?:^|\n)\s*\$\s+\S+",
            r"\bbash: .*: command not found",
            r"\bpermission denied\b|\boperation not permitted\b",
            r"\bsudo\b",
            r"\bchmod (?:\d{3,4}|[ugoa]*[+-][rwxX]+)\b",
        ),
        # "permission denied", "sudo" and "chmod 755" are each a keyword and
        # a pattern; they score once (review 2026-09-02, R4), so the verbatim
        # Docker socket error (two wrangler words and its socket pattern) is
        # not out-scored by one shell phrase counted twice.
        phrase_once=True,
        weight=1.0,
    ),
    # --- Coding pair ----------------------------------------------------------
    SpecialistRule(
        profile="bug-hunter",
        keywords=(
            "bug", "bugs", "debug", "debugging", "stack trace", "stacktrace", "traceback",
            "root cause", "flaky",
            "wrong result", "wrong output", "wrong answer", "returns the wrong", "off by one",
            "off-by-one", "my code", "my function", "my script", "my program", "test fails",
            "tests fail", "failing test", "assertionerror", "nullpointer", "null pointer",
            "undefined is not", "typeerror", "keyerror", "indexerror", "valueerror",
            "attributeerror", "nameerror", "segfault", "race condition", "deadlock",
            "memory leak", "infinite loop", "unexpected behavior", "unexpected behaviour",
            "not what i expected",
            "doesn't return", "returns none", "returns null", "returns undefined",
        ),
        # "regression model", "why is this so slow", "ollama hangs".
        weak_keywords=(
            "regression", "intermittent", "reproduce", "repro", "why does this", "why is this",
            "why doesn't this", "hangs",
        ),
        patterns=_CODE_SHAPE + (
            r"\b(?:TypeError|KeyError|IndexError|ValueError|AttributeError|NameError|"
            r"AssertionError|ZeroDivisionError|RecursionError|UnboundLocalError|"
            r"NullPointerException|ReferenceError|SyntaxError|IndentationError)\b",
        ),
        task_types=(TaskType.CODE_DEBUG,),
        weight=1.0,
    ),
    SpecialistRule(
        profile="deep-reviewer",
        keywords=(
            "code review", "pull request", "merge request", "the pr", "this pr", "my pr",
            "diff", "check my code",
            "security review", "code smell", "code smells", "lgtm",
            "nitpick", "production-ready", "production ready",
            "what do you think of this code", "feedback on my code",
        ),
        # "review my ollama config", "review my setup", "the driver patch".
        weak_keywords=(
            "review", "review this", "review my", "patch", "critique", "is this correct",
            "look over", "best practices", "audit this", "any issues with",
        ),
        patterns=(
            r"```",
            r"(?:^|\n)diff --git |(?:^|\n)@@ -\d+|(?:^|\n)(?:\+\+\+|---) [ab]/",
        ),
        task_types=(TaskType.CODE_REVIEW,),
        weight=1.0,
    ),
    SpecialistRule(
        profile="backend-implementer",
        keywords=(
            "implement", "implementation", "write a function", "write a script",
            "write a class", "write code", "write me a function", "write me a script",
            "create a function", "create an endpoint",
            "create a script", "api endpoint", "rest api", "fastapi", "flask",
            "django", "middleware", "refactor", "refactoring", "rewrite this",
            "port this", "convert this code", "add a feature", "boilerplate", "scaffold",
            "generate code", "cli tool", "unit test", "unit tests", "write tests", "pytest",
            "python script", "python function", "in python", "in typescript", "in rust",
            "in golang", "in javascript", "in c++", "in java", "in c#", "sql query",
            "regex for", "type hints", "async version", "make it async",
        ),
        # "pci express lanes", "the ollama endpoint", "build a pc", "sign in, go to".
        weak_keywords=("endpoint", "build a", "build me a", "express", "in go"),
        patterns=_CODE_SHAPE,
        task_types=(TaskType.CODE_GENERATION,),
        weight=1.0,
    ),
    # --- Research -----------------------------------------------------------
    SpecialistRule(
        profile="deep-researcher",
        keywords=(
            "news", "look up", "lookup", "search for",
            "search the web", "google", "find out", "research", "what's new", "whats new",
            "release notes", "up to date",
            "up-to-date", "pros and cons",
            "state of the art", "sota", "leaderboard",
            "arxiv", "cite", "citations", "who is",
        ),
        # "what's the latest driver for my gpu", "compare q4 vs q8", "benchmark
        # my gpu": research words that also describe a rig question.
        weak_keywords=(
            "latest", "recent", "recently", "announced", "this week", "this month", "compare",
            "comparison", "versus", "vs", "benchmark", "benchmarks", "paper", "sources",
            "what happened", "roadmap", "pricing", "how much does", "when will",
            "release date", "reviews",
        ),
        patterns=(
            r"https?://\S+",
            r"\bwww\.\S+\.\S+",
            r"\barxiv\.org/\S+|\barXiv:\d{4}\.\d{4,5}\b",
        ),
        weight=1.0,
    ),
    SpecialistRule(
        profile="fact-checker",
        keywords=(
            "is it true", "is that true", "true that", "fact check", "fact-check", "factcheck",
            "verify that", "is this accurate", "myth", "debunk", "claim that", "rumor", "rumour",
            "hoax", "misinformation", "source for", "evidence for", "is it real",
            "true or false",
        ),
        # "verify my api key", "is the reading accurate", "confirm that the gpu
        # works", "does it really matter": a bare "really" beside a pronoun is
        # emphasis, not a claim; it counts next to a claim-shaped pattern.
        weak_keywords=(
            "verify", "accurate", "claims", "correct that", "confirm that", "did they really",
            "does it really",
        ),
        # The rig's symptoms and the model desk's arithmetic are never a claim
        # to check (review 2026-09-02, F1): "is the driver really broken" is
        # the triage's, "does my 70b really fit" the planner's. Device nouns
        # are not vetoes: "is it true that apt upgrade breaks the driver?" is
        # a claim about the driver and stays here.
        excludes=_VAGUE_TROUBLE + (
            "running hot", "too hot", "overheating", "throttling", "throttle", "thermal", "xid",
            "nvidia-smi", "crash", "crashed", "crashes", "traceback", "exit code",
            "connection refused", "out of memory", "oom", "slow", "tok/s", "tokens/s", "fit",
            "fits", "vram", "quant", "quants", "gguf", "how much memory", "how much vram",
            "too big", "too large",
        ),
        patterns=(
            # Every pattern takes a claim-shaped object: "is it true", "is
            # that really the case", "did they actually say that", "true or
            # false", "fact check", a URL or quoted claim beside a truth word.
            # A bare "(is|are|does) ... really" is not one: "my gpu is running
            # really hot" and "the fans are spinning really loud" are the
            # triage's (review 2026-09-02, F1).
            r"\bis (?:it|that|this) (?:really |actually )?"
            r"(?:true|accurate|real|correct|legit|the case|a thing|a myth|a hoax|fake|bogus)\b",
            r"\b(?:really|actually|genuinely|truly) "
            r"(?:true|the case|real|legit|accurate|correct|a thing|happen(?:ed)?|say that|said that)\b",
            r"\bfact[- ]?check\b|\btrue or false\b",
            r"\b(?:did|does|do|is|was|are|were|has|have|can|could|will|would) (?:\w+ ){1,4}?"
            r"(?:really|actually) (?:say|said|claim(?:ed)?|announce[d]?|confirm(?:ed)?|den(?:y|ied)|"
            r"admit(?:ted)?|happen(?:ed)?|exist|mean|true|the case)\b",
            # "is this legit: https://...", 'https://... - is that real?',
            # 'is it true: "the spark has 256 GB"'.
            r"\b(?:true|accurate|real|legit|fake|bogus|hoax)\b[^.?!\n]{0,20}:\s*"
            r"(?:https?://\S+|\"[^\"\n]{8,}\")|"
            r"https?://\S+\s*[-—,:]?\s*(?:is (?:this|that|it) )?"
            r"(?:true|accurate|real|legit|fake|bogus|a hoax)\b",
        ),
        weight=1.0,
    ),
    # --- Notes and vault ----------------------------------------------------
    SpecialistRule(
        profile="vault-rag",
        keywords=(
            "my notes", "my note", "in my vault", "the vault", "vault", "obsidian",
            "what did we decide", "what did i decide", "what did we say", "what did i write",
            "did i write", "did we discuss", "remind me what", "remember when",
            "do you remember", "you remember",
            "according to my notes", "in my notes", "from my notes", "search my notes",
            "find in my notes", "my markdown", "meeting notes",
            "what did we agree", "we agreed",
        ),
        # "last week my gpu died", "help me make a decision", "i told you the
        # install fails": recall words that also open a rig question.
        weak_keywords=(
            "we discussed", "as i mentioned", "i mentioned", "i told you", "we talked about",
            "last time", "last week", "yesterday we", "earlier we", "decision", "decisions",
        ),
        patterns=(
            r"\[\[[^\]]+\]\]",
            r"\b(?:do you|you) remember\b|\bremember (?:when|what|how|where|who|why|that time)\b|"
            r"\bwhat did (?:we|i|you) (?:decide|say|discuss|agree|conclude|write|note)\b|"
            r"\bdid (?:we|i) (?:decide|discuss|write|note|say|agree)\b",
        ),
        state=("vault_ready",),
        requires_tools=("rag_ask_vault",),
        weight=1.0,
    ),
    SpecialistRule(
        profile="daily-notes-coach",
        keywords=(
            "daily note", "daily notes", "journal", "journaling", "today's note", "todays note",
            "remember this", "remember that", "note this", "note that down",
            "write this down", "write that down", "jot", "jot down", "log this", "save this",
            "capture this", "add to my notes", "add a note", "make a note", "take a note",
            "to do list", "remind me to", "weekly review",
            "review my week", "morning pages", "plan my day", "plan my week",
        ),
        # "i can't remember the port for the api server", "a TODO in the code".
        weak_keywords=(
            "remember", "todo", "to-do", "reminder", "reflect", "reflection", "habit", "habits",
            "standup", "stand-up", "end of day",
        ),
        patterns=(
            # An imperative capture at the start of the message: "remember
            # this: ...", "note down that ...", "save this for later".
            r"(?:^|\n)\s*(?:please\s+)?(?:remember|note|jot|log|save|capture|write)"
            r"\s+(?:this|that|down|it)\b",
        ),
        weight=1.0,
    ),
    SpecialistRule(
        profile="doc-qa",
        keywords=(
            "according to the document", "according to the doc", "according to the pdf",
            "according to the paper", "in the document", "in the pdf", "in the paper",
            "the attached", "attached file", "the uploaded", "uploaded file",
            "uploaded document", "ingested", "ingest", "the document says", "the doc says",
            "this document", "this pdf", "the pdf", "the manual", "the spec says",
            "quote from", "where in the document", "summarize the document",
            "summarize this document", "summarise the document", "summarize the pdf",
            "what does the document", "from the document", "knowledge base", "corpus",
            "the file i uploaded", "the file i added", "the datasheet", "the whitepaper",
            "user guide", "the handbook",
        ),
        patterns=(r"\b\S+\.(?:pdf|docx?|pptx?|epub)\b",),
        task_types=(TaskType.LONG_CONTEXT_ANALYSIS,),
        weight=1.0,
    ),
    # --- Tutors ---------------------------------------------------------------
    # code-tutor is the default tutor: the explain words alone select it, so
    # its own vocabulary stays narrow (learning concepts, not "python" or
    # "function", which would steal implementation requests from the
    # coding pair). The other two tutors need a domain word to win the tie.
    SpecialistRule(
        profile="code-tutor",
        keywords=_EXPLAIN_WORDS + (
            "recursion", "pointer", "pointers", "closure", "closures", "decorator",
            "decorators", "data structure", "data structures",
            "big o", "big-o", "linked list", "hash map", "object oriented", "object-oriented",
            "inheritance", "polymorphism", "what does this code",
            "how does this code",
        ),
        # "what is a spark", "what are the specs", "the generator": generic
        # question openers need a teaching word or a CS term beside them.
        weak_keywords=_EXPLAIN_WEAK + (
            "generator", "generators", "what does this do", "what is a", "what's a", "what are",
        ),
        patterns=(r"\bhow (?:does|do) \w+(?: \w+)? work\b",),
        weight=1.0,
    ),
    SpecialistRule(
        profile="science-explainer",
        keywords=_EXPLAIN_WORDS + (
            "physics", "chemistry", "biology", "quantum", "photosynthesis", "dna", "rna",
            "thermodynamics", "relativity", "black hole", "galaxy", "rainbow", "tides",
            "volcano", "earthquake", "how do plants", "how does the brain", "how does the body",
        ),
        # "energy efficiency of the spark", "jupyter cell", "electron app",
        # "atom editor", "neural network neurons": domain nouns need a
        # teaching word ("explain why the sky is blue", "teach me about entropy").
        weak_keywords=_EXPLAIN_WEAK + (
            "science", "scientific", "entropy", "evolution", "gravity", "electron", "electrons",
            "molecule", "molecules", "atom", "atoms", "climate", "neuron", "neurons", "cell",
            "cells", "protein", "proteins", "vaccine", "vaccines", "immune", "planet", "planets",
            "star", "stars", "universe", "the sky", "sky", "moon", "sun", "chemical", "reaction",
            "energy", "magnet", "magnetic", "light waves", "sound waves", "weather", "ocean",
        ),
        weight=1.0,
    ),
    SpecialistRule(
        profile="math-stepper",
        keywords=_EXPLAIN_WORDS + (
            "equation", "equations", "integral", "integrate",
            "derivative", "differentiate", "calculus", "algebra",
            "eigenvalue", "eigenvalues", "theorem",
            "logarithm", "geometry", "trigonometry", "quadratic", "combinatorics",
            "math", "maths", "mathematics",
        ),
        # "rtx 50 series power limit", "form factor", "vector database",
        # "compute capability", "proof of concept": math words that also
        # describe hardware need an equation, a teaching word or the classifier.
        weak_keywords=_EXPLAIN_WEAK + (
            "solve", "solve for", "matrix", "matrices", "probability", "proof", "prove",
            "factor", "factorise", "factorize", "polynomial", "statistics", "median",
            "standard deviation", "variance", "fraction", "fractions", "percent", "percentage",
            "arithmetic", "sum of", "limit", "limits", "series", "vector", "vectors", "exponent",
            "exponential", "permutation", "permutations", "combination", "combinations",
            "calculate", "compute",
        ),
        patterns=(
            r"\b\d+\s*[x×*]\s*\d+\b",
            r"\b[a-z]\s*\^\s*\d\b|\b[a-z]\d\s*[+\-]\s*\d",
            r"\b\d+\s*[+\-*/=]\s*\d+\b",
            r"\b[a-z]\s*=\s*[-+]?\d",
            r"\b(?:sin|cos|tan|log|ln|sqrt|lim|sum)\s*\(|[∑∫√π]",
        ),
        task_types=(TaskType.MATH,),
        weight=1.0,
    ),
)

#: Human-readable notes appended to a reason when the named state fired.
#: ``{pool}`` in the DGX Spark note is filled by :func:`_state_note` from the
#: context's ``memory_total_gb`` — the Spark ships in more than one memory
#: size, so no figure is hard-coded here.
_STATE_NOTES: dict[str, str] = {
    "device:dgx-spark": (
        "DGX Spark: {pool} is one shared pool (read MemAvailable, "
        "not VRAM); MoE models first"
    ),
    "device:rtx-spark": "RTX Spark: unified memory shared with Windows, budget the shared pool",
    "unified_memory": "unified memory: plan against the shared pool, not a VRAM figure",
    "failed_job": "a recent install job failed",
    "gpu_missing": "no NVIDIA GPU is detected right now",
    "no_models": "no local Ollama models are installed yet",
    "no_providers": "no cloud provider is configured",
    "provider_unhealthy": "a configured provider is failing its health check",
    "first_run": "fresh workspace (first run)",
    "receipts_unhealthy": "an install receipt needs attention",
    "storage_unavailable": "the NVH_HOME storage probe failed",
    "storage_warnings": "storage reported warnings",
    "vault_ready": "the vault is initialised",
}

#: What the DGX Spark note calls the pool when the context carries no
#: ``memory_total_gb``.
_SPARK_POOL_UNSIZED = "unified memory shared with the OS"


def _memory_total_gb(context: Mapping[str, Any] | None) -> float | None:
    """``memory_total_gb`` from the platform or GPU block, when it is a real number."""
    for block in ("platform", "gpu"):
        value = ((context or {}).get(block) or {}).get("memory_total_gb")
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
            return float(value)
    return None


def _state_note(pred: str, context: Mapping[str, Any] | None) -> str:
    """Human-readable note for a fired state predicate.

    The DGX Spark note names the pool size only when the context supplies
    ``memory_total_gb``; otherwise it says "unified memory shared with the
    OS" rather than hard-coding a figure.
    """
    if pred == "device:dgx-spark":
        total = _memory_total_gb(context)
        pool = f"{total:g} GB unified memory" if total is not None else _SPARK_POOL_UNSIZED
        return _STATE_NOTES[pred].format(pool=pool)
    return _STATE_NOTES.get(pred, f"state {pred}")


# ---------------------------------------------------------------------------
# Compiled-regex cache
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Compiled:
    """A rule's regexes, compiled once (see :func:`_compiled`)."""

    #: Strong and weak keywords in one longest-first alternation, so "review
    #: this" is one hit and never also counts as "review".
    keywords: re.Pattern[str] | None
    #: Keyword tokens that are weak (need a second signal to count).
    weak: frozenset[str]
    patterns: tuple[re.Pattern[str], ...]
    #: Veto words; any hit zeroes the rule for the turn.
    excludes: re.Pattern[str] | None
    #: Veto regexes (``exclude_patterns``); any hit zeroes the rule too.
    exclude_patterns: tuple[re.Pattern[str], ...]


def _keyword_re(words: Iterable[str]) -> re.Pattern[str] | None:
    # Longest first so "api key" wins over "key" inside one alternation.
    alts = sorted({w.lower() for w in words}, key=len, reverse=True)
    if not alts:
        return None
    return re.compile(r"(?<![a-z0-9])(?:" + "|".join(re.escape(k) for k in alts) + r")(?![a-z0-9])")


@lru_cache(maxsize=256)
def _compiled(rule: SpecialistRule) -> _Compiled:
    """Compile a rule's keyword alternations and regex patterns once."""
    strong = {k.lower() for k in rule.keywords}
    weak = {k.lower() for k in rule.weak_keywords} - strong
    return _Compiled(
        keywords=_keyword_re(strong | weak),
        weak=frozenset(weak),
        patterns=tuple(re.compile(p, re.IGNORECASE | re.MULTILINE) for p in rule.patterns),
        excludes=_keyword_re(rule.excludes),
        exclude_patterns=tuple(
            re.compile(p, re.IGNORECASE | re.MULTILINE) for p in rule.exclude_patterns
        ),
    )


_WS_RE = re.compile(r"\s+")


def _normalise(text: str) -> str:
    return _WS_RE.sub(" ", text.lower()).strip()


# ---------------------------------------------------------------------------
# Profile resolution
# ---------------------------------------------------------------------------


def _profile_map(
    profiles: Iterable[AgentProfile] | None, home_dir: str | Path | None,
) -> dict[str, AgentProfile]:
    if profiles is None:
        try:
            from nvh.integrations.wizard.profiles import list_profiles

            profiles = list_profiles(home_dir=home_dir)
        except Exception as exc:  # broken profile store: never break chat
            logger.debug("concierge: list_profiles failed (%s); no specialists", exc)
            profiles = ()
    return {p.name: p for p in profiles}


def _rule_applies(rule: SpecialistRule, profile: AgentProfile | None) -> bool:
    if profile is None:
        return False
    if rule.requires_tools and profile.tools_allowed is not None:
        allowed = set(profile.tools_allowed)
        if not all(t in allowed for t in rule.requires_tools):
            return False
    return True


def active_rules(
    profiles: Iterable[AgentProfile] | None = None,
    *,
    home_dir: str | Path | None = None,
    rules: Sequence[SpecialistRule] = SPECIALIST_RULES,
) -> tuple[SpecialistRule, ...]:
    """Rules whose profile exists (and binds the tools the rule requires)."""
    by_name = _profile_map(profiles, home_dir)
    return tuple(r for r in rules if _rule_applies(r, by_name.get(r.profile)))


def available_specialists(
    home_dir: str | Path | None = None,
    *,
    profiles: Iterable[AgentProfile] | None = None,
) -> tuple[str, ...]:
    """Distinct specialist profile names the concierge can route to, in rule order."""
    seen: dict[str, None] = {}
    for r in active_rules(profiles, home_dir=home_dir):
        seen.setdefault(r.profile, None)
    return tuple(seen)


# ---------------------------------------------------------------------------
# State derivation
# ---------------------------------------------------------------------------


def derive_state(
    context: Mapping[str, Any] | None,
    findings: Iterable[Mapping[str, Any]] | None = None,
    history: Sequence[Mapping[str, Any]] | None = None,
) -> frozenset[str]:
    """Return every true state predicate for this turn.

    Finding ids are included verbatim and with ``-`` mapped to ``_``
    (``gpu-missing`` and ``gpu_missing`` both work in a rule). Prefixed ids
    such as ``job-failed-<id>`` also add their family (``job-failed``,
    ``failed_job``). Context predicates: ``no_models``, ``failed_job``,
    ``gpu_missing``, ``no_providers``, ``provider_unhealthy``,
    ``unified_memory``, ``receipts_unhealthy``, ``storage_unavailable``,
    ``storage_warnings``, ``vault_ready``, ``has_root``, ``can_sudo``,
    ``device:<device_class>`` and ``first_run`` (no models, no receipts, no
    history).
    """
    state: set[str] = set()
    for f in findings or ():
        fid = str(f.get("id") or "")
        if not fid:
            continue
        state.add(fid)
        state.add(fid.replace("-", "_"))
        for family, alias in (
            ("job-failed", "failed_job"),
            ("provider-unhealthy", "provider_unhealthy"),
        ):
            if fid == family or fid.startswith(family + "-"):
                state.update((family, alias))
    if context is None:
        return frozenset(state)

    models = context.get("ollama_models") or []
    if not models:
        state.add("no_models")
    jobs = context.get("recent_jobs") or []
    if any((j or {}).get("status") in {"failed", "interrupted"} for j in jobs):
        state.add("failed_job")
    gpu = context.get("gpu") or {}
    if gpu and gpu.get("detected") is False:
        state.add("gpu_missing")
    providers = context.get("providers")
    if providers is not None and not providers:
        state.add("no_providers")
    if any((p or {}).get("healthy") is False for p in providers or []):
        state.add("provider_unhealthy")
    platform = context.get("platform") or {}
    device = platform.get("device_class")
    if device and device != "unknown":
        state.add(f"device:{device}")
    if platform.get("unified_memory") or gpu.get("unified_memory"):
        state.add("unified_memory")
    if platform.get("has_root"):
        state.add("has_root")
    if platform.get("can_sudo"):
        state.add("can_sudo")
    receipts = context.get("receipts") or {}
    if receipts.get("unhealthy"):
        state.add("receipts_unhealthy")
    storage = context.get("storage") or {}
    if storage.get("available") is False:
        state.add("storage_unavailable")
    if storage.get("warnings"):
        state.add("storage_warnings")
    vault = context.get("vault") or {}
    if vault.get("initialized"):
        state.add("vault_ready")
    if not models and not (receipts.get("count") or 0) and not history:
        state.add("first_run")
    return frozenset(state)


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


@dataclass
class _Scored:
    rule: SpecialistRule
    score: float = 0.0
    text: float = 0.0
    matched: list[str] = field(default_factory=list)
    states: list[str] = field(default_factory=list)
    task_hit: TaskType | None = None
    sticky: bool = False
    #: The exclude word that vetoed the rule this turn, if any.
    vetoed_by: str | None = None
    #: Weak keywords seen but not counted (no second signal).
    uncounted: list[str] = field(default_factory=list)


def _state_members(expr: str, state: frozenset[str]) -> list[str] | None:
    """True predicates of a fired ``state`` expression, or ``None`` if it did not fire.

    ``expr`` is one :attr:`SpecialistRule.state` entry: predicates joined by
    ``|`` (any of) and ``&`` (all of; binds tighter), so
    ``"first_run|no_models"`` fires when either is true and
    ``"first_run&device:dgx-spark"`` only when both are. The members returned
    are the true predicates, in order, for the reason's state notes.
    """
    fired: list[str] = []
    for alt in expr.split("|"):
        preds = [p.strip() for p in alt.split("&") if p.strip()]
        if preds and all(p in state for p in preds):
            fired.extend(p for p in preds if p not in fired)
    return fired or None


def _score_rule(
    rule: SpecialistRule,
    raw: str,
    norm: str,
    state: frozenset[str],
    task: TaskType | None,
    sticky: str | None,
) -> _Scored:
    """Score one rule. ``task`` is the classifier's type only when it is confident."""
    comp = _compiled(rule)
    out = _Scored(rule=rule)

    if comp.excludes is not None:
        veto = comp.excludes.search(norm)
        if veto is not None:
            out.vetoed_by = veto.group(0)
            return out
    for p in comp.exclude_patterns:
        veto = p.search(raw)
        if veto is not None:
            out.vetoed_by = "re:" + (_normalise(veto.group(0))[:40] or p.pattern[:40])
            return out

    # Patterns first: with ``phrase_once`` their spans (on the normalised
    # text, where the keywords are matched) hide the keywords inside them.
    pat_hits: list[str] = []
    covered: list[tuple[int, int]] = []
    for p in comp.patterns:
        pm = p.search(raw)
        if pm is None:
            continue
        snippet = _normalise(pm.group(0))[:40] or p.pattern[:40]
        pat_hits.append(f"re:{snippet}")
        if rule.phrase_once:
            covered.extend(m.span() for m in p.finditer(norm))

    strong_hits: list[str] = []
    weak_hits: list[str] = []
    if comp.keywords is not None:
        for m in comp.keywords.finditer(norm):
            if covered and any(a < m.end() and m.start() < b for a, b in covered):
                continue
            tok = m.group(0)
            bucket = weak_hits if tok in comp.weak else strong_hits
            if tok not in bucket:
                bucket.append(tok)

    task_agrees = task is not None and task in rule.task_types
    is_sticky = sticky is not None and rule.profile == sticky
    # A weak keyword needs a second signal from this rule: a curated strong
    # keyword, a structural pattern or continuity. State is not one (it is
    # near-permanent: device:dgx-spark is true on every Spark turn) and the
    # classifier is not one either (it reads the same words, so "vector"
    # scoring MATH cannot vouch for "vector" being maths).
    second_signal = bool(strong_hits) or bool(pat_hits) or is_sticky
    if weak_hits and not second_signal:
        out.uncounted = weak_hits
        weak_hits = []
    kw_hits = strong_hits + weak_hits

    out.text = rule.weight * (
        KEYWORD_HIT * min(len(kw_hits), MAX_KEYWORD_HITS)
        + PATTERN_HIT * min(len(pat_hits), MAX_PATTERN_HITS)
    )
    out.matched.extend(kw_hits)
    out.matched.extend(pat_hits)
    out.score = out.text

    if out.text > 0:
        for expr in rule.state:
            members = _state_members(expr, state)
            if members is None:
                continue
            out.score += STATE_BOOST
            out.states.extend(m for m in members if m not in out.states)
    if task_agrees:
        out.task_hit = task
        out.score += TASK_TYPE_BONUS
    if is_sticky:
        out.sticky = True
        out.score += STICKY_BONUS
    return out


def _classify(question: str) -> Any | None:
    """Run the router's task classifier; ``None`` if it is unavailable."""
    try:
        from nvh.core.router import classify_task

        return classify_task(question)
    except Exception as exc:  # optional tie-breaker only
        logger.debug("concierge: classify_task unavailable (%s)", exc)
        return None


def _sticky_from(history: Sequence[Mapping[str, Any]] | None) -> str | None:
    """The specialist that answered the most recent assistant turn, if recorded."""
    for turn in reversed(history or ()):
        if not isinstance(turn, Mapping):
            continue
        if turn.get("role") == "assistant":
            used = turn.get("used_profile")
            return str(used) if used else None
    return None


def _best(scored: Iterable[_Scored]) -> _Scored:
    """The winner: highest score, then more distinct matched signals, then table order.

    ``max`` keeps the first maximum and the caller passes rules in table
    order, so an exact tie on both keys goes to the earlier rule. The score
    is rounded so two sums of the same weights compare equal whatever the
    order the floats were added in. The signal count breaks "delete the 70b
    models I never use": the librarian's verb + object pattern and the
    planner's bare size token are 1.5 each, but a rule with two signals for
    the same score has read more of the question than one with one.
    """
    return max(scored, key=lambda s: (round(s.score, 6), len(s.matched)))


def _confidence(score: float) -> float:
    return round(min(1.0, 0.5 + 0.15 * (score - MIN_SCORE)), 2)


def _reason(best: _Scored, context: Mapping[str, Any] | None = None) -> str:
    parts: list[str] = []
    if best.matched:
        shown = ", ".join(repr(m) for m in best.matched[:4])
        if len(best.matched) > 4:
            shown += f" (+{len(best.matched) - 4} more)"
        parts.append(f"matched {shown}")
    for pred in best.states:
        parts.append(_state_note(pred, context))
    if best.task_hit is not None:
        parts.append(f"task type {best.task_hit.value}")
    if best.sticky:
        parts.append("continuing from the previous turn")
    return f"{best.rule.display}: " + "; ".join(parts)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def select_specialist(
    question: str,
    *,
    context: Mapping[str, Any] | None = None,
    findings: Iterable[Mapping[str, Any]] | None = None,
    history: Sequence[Mapping[str, Any]] | None = None,
    home_dir: str | Path | None = None,
    profiles: Iterable[AgentProfile] | None = None,
    sticky: str | None = None,
) -> SpecialistChoice:
    """Choose the specialist profile for ``question`` (``None`` = general Wizard).

    ``context`` is a ``wizard_context()`` snapshot, ``findings`` the
    ``derive_findings()`` output as dicts (``Finding.to_dict()``), ``history``
    prior turns (``{role, content, used_profile?}``). ``profiles`` overrides
    the profile list (tests, callers that already loaded it); otherwise
    ``list_profiles(home_dir)`` is consulted. ``sticky`` names the previous
    specialist explicitly and wins over ``history[-1]["used_profile"]``.
    """
    text = (question or "").strip()
    if not text:
        return SpecialistChoice(None, "general Wizard: empty question", 1.0)
    if _GREETING_RE.match(text):
        return SpecialistChoice(None, "general Wizard: greeting or chit-chat", 1.0)

    rules = active_rules(profiles, home_dir=home_dir)
    if not rules:
        return SpecialistChoice(None, "general Wizard: no specialist profiles available", 1.0)
    available = {r.profile for r in rules}

    if sticky is None:
        sticky = _sticky_from(history)
    if sticky is not None and sticky not in available:
        sticky = None

    state = derive_state(context, findings, history)
    cls = _classify(text)
    task: TaskType | None = getattr(cls, "task_type", None)
    task_conf = float(getattr(cls, "confidence", 0.0) or 0.0)
    # The classifier earns the tier-1 bonus only when it is confident; it is
    # noisy below the floor (the same floor tier 2 uses).
    task_signal = task if task_conf >= RESIDUE_MIN_CONFIDENCE else None

    norm = _normalise(text)
    best_by_profile: dict[str, _Scored] = {}
    for rule in rules:
        s = _score_rule(rule, text, norm, state, task_signal, sticky)
        cur = best_by_profile.get(rule.profile)
        if cur is None or s.score > cur.score:
            best_by_profile[rule.profile] = s

    # Tier 1: deterministic triggers. Dict order is rule order, so an exact
    # tie on score and signal count resolves to the earlier rule.
    best = _best(best_by_profile.values())
    if best.text > 0 and best.score >= MIN_SCORE:
        return SpecialistChoice(
            best.rule.profile, _reason(best, context), _confidence(best.score), tuple(best.matched),
        )

    # Tier 2: classifier residue (coding / math only, by construction).
    if task is not None and task_conf >= RESIDUE_MIN_CONFIDENCE:
        for rule in rules:
            if task in rule.task_types:
                return SpecialistChoice(
                    rule.profile,
                    f"{rule.display}: task classifier says {task.value} ({task_conf:.2f})",
                    RESIDUE_CONFIDENCE,
                    (f"task:{task.value}",),
                )

    # Tier 3: continuity on a weak follow-up.
    if sticky is not None:
        return SpecialistChoice(
            sticky,
            f"{sticky}: no new signal, continuing from the previous turn",
            STICKY_CONFIDENCE,
            (f"sticky:{sticky}",),
        )

    # The general Wizard answers. The reason is what the UI tooltip shows,
    # so it is a short human sentence (:data:`GENERAL_NO_MATCH_REASON`),
    # optionally followed by one near-miss hint explaining why nobody won.
    hint = ""
    if best.score > 0:
        hint = f" (closest: {best.rule.profile} at {best.score:.1f} < {MIN_SCORE:.1f})"
    else:
        for s in best_by_profile.values():
            if s.vetoed_by is not None:
                hint = f" ({s.rule.profile} vetoed by {s.vetoed_by!r})"
                break
            if s.uncounted:
                hint = f" ({s.rule.profile}: weak {s.uncounted[0]!r} needs a second signal)"
                break
    return SpecialistChoice(None, f"{GENERAL_NO_MATCH_REASON}{hint}", 0.6)


def resolve_auto_profile(
    requested: str | None,
    question: str,
    **kw: Any,
) -> tuple[str | None, SpecialistChoice | None]:
    """The chat.py hook: ``(profile_name, choice)``.

    An explicit pin (any name outside ``None``/``""``/``"auto"``, including
    ``"wizard"`` for the general persona) is returned untouched with
    ``choice=None``. Otherwise :func:`select_specialist` runs with ``**kw``
    (``context``, ``findings``, ``history``, ``home_dir``, ``profiles``,
    ``sticky``) and the chosen profile name (``None`` for the general
    Wizard) is returned with the :class:`SpecialistChoice` that explains it.
    """
    if requested is not None and requested.strip().lower() not in _AUTO_NAMES:
        return requested, None
    choice = select_specialist(question, **kw)
    return choice.profile, choice
