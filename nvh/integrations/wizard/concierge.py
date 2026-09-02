"""Concierge: pick the Wizard's hidden specialist for one turn.

The Wizard shows the user *one* assistant. Under the hood, each turn may be
answered by a specialist profile from the Agent Library (``install-medic``,
``gpu-triage``, ``vram-planner``, ``home-assistant``, ...). This module makes
that choice. It is deterministic, pure and fast: no network, no engine, no
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
             + STATE_BOOST * (true state predicates)   # only when text > 0
             + TASK_TYPE_BONUS                         # classifier agrees
             + STICKY_BONUS                            # previous specialist

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
   GPU persistence mode" is never smart-home).

   State predicates come from ``wizard_context()`` and the findings list
   (``failed_job``, ``gpu_missing``, ``no_models``, ``first_run``,
   ``device:dgx-spark``, ...). State never selects a specialist on its own:
   it amplifies a rule that already matched the words, so a fresh box does
   not route "write me a poem" to the install medic. The best profile wins
   when its score reaches :data:`MIN_SCORE`; ties go to the earlier rule.

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
        state=("vision_ready",),                # finding ids / predicates
        task_types=(TaskType.MULTIMODAL,),      # classifier tie-break
        weight=1.0,                             # >1 to win generic ties
        requires_tools=("analyze_image",),      # must be in tools_allowed
    )

Rules whose ``profile`` is not in the current profile list are dropped
silently, so a library change never breaks chat. Keep strong keywords
specific: a single hit already clears the threshold, so a word that also
lives in rig vocabulary ("node", "registry", "series", "adapter",
"parameters") belongs in ``weak_keywords``. Use ``weight > 1`` for domains
whose vocabulary collides with generic trouble words ("the lights are not
working" is smart-home, not an install problem). Ops rules sit first in
the table so an exact tie with a GPU / model word goes to the rig doctor.

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
    questions. ``excludes`` veto the rule outright
    when any of them appears. ``patterns`` are regexes compiled lazily with
    ``re.IGNORECASE | re.MULTILINE`` against the raw question. ``state`` names
    finding ids (``gpu-missing``) or context predicates (``no_models``,
    ``failed_job``, ``first_run``, ``device:dgx-spark``; see
    :func:`derive_state`). ``task_types`` is the classifier tie-break.
    ``requires_tools`` drops the rule when the profile's ``tools_allowed``
    whitelist is set and does not contain every listed tool.
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
    #: Free-form label used in reasons; defaults to the profile name.
    label: str = field(default="", compare=False)

    @property
    def display(self) -> str:
        return self.label or self.profile


# ---------------------------------------------------------------------------
# Rule table
# ---------------------------------------------------------------------------

# Words that mean "something is broken" without saying what. They sit on
# both halves of the "rig doctor" (install-medic, gpu-triage); state boosts
# and the other words in the question break the tie.
_VAGUE_TROUBLE: tuple[str, ...] = (
    "what's wrong", "whats wrong", "what is wrong", "not working", "doesn't work",
    "doesnt work", "isn't working", "isnt working", "stopped working", "won't start",
    "wont start", "broken", "fix it", "fix this", "repair", "something is off",
    "something's wrong", "keeps failing",
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

SPECIALIST_RULES: tuple[SpecialistRule, ...] = (
    # --- Ops: the rig doctor -----------------------------------------------
    SpecialistRule(
        profile="install-medic",
        keywords=_VAGUE_TROUBLE + (
            "install", "installed", "installing", "installation", "reinstall", "uninstall",
            "traceback", "exception", "error", "errors", "failed", "failing", "fails",
            "failure", "crash", "crashed", "crashes", "pip", "pip3", "apt", "apt-get",
            "conda", "wheel", "wheels", "dependency", "dependencies", "module not found",
            "no module named", "port already in use", "address already in use",
            "exit code", "non-zero exit", "setup failed", "connection refused",
            "ollama is not running", "ollama not running", "ollama serve", "startup",
            "won't boot", "requirements.txt",
        ),
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
        ),
        state=("failed_job", "receipts_unhealthy", "storage_unavailable", "storage_warnings"),
        weight=1.0,
    ),
    SpecialistRule(
        profile="gpu-triage",
        keywords=_VAGUE_TROUBLE + (
            "gpu", "gpus", "vram", "cuda", "driver", "drivers", "nvidia-smi", "nvidia smi",
            "nvml", "no devices", "no devices were found", "cudnn", "cublas", "nvcc",
            "compute capability", "gpu not detected", "no gpu", "cpu mode", "cpu only",
            "gpu utilization", "gpu temp", "gpu temperature", "thermal", "throttling",
            "throttle", "xid", "nvidia.ko", "kernel module", "dkms", "sm_121", "torch.cuda",
            "cuda_visible_devices", "is_available", "device not found", "gb10", "blackwell",
            "grace", "nouveau", "secure boot", "persistence mode", "nvidia-persistenced",
            "power limit", "power draw", "fan speed", "fan curve", "gpu fan", "gpu clocks",
        ),
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
        ),
        state=("gpu_missing",),
        weight=1.0,
    ),
    # --- Ops: the model sommelier -----------------------------------------
    # model-librarian sits before vram-planner so a bare "which model?" goes
    # to the librarian; "fits", sizes, quants or a unified-memory device
    # tip the tie to the planner.
    SpecialistRule(
        profile="model-librarian",
        keywords=(
            "which model", "what model", "best model", "recommend a model", "recommend me a model",
            "model recommendation", "model should i", "models do i have", "installed models",
            "what models", "list models", "list my models", "ollama list", "ollama pull",
            "ollama rm", "pull a model", "pull model", "pull the model", "download a model",
            "download model", "get a model", "new model", "delete a model", "remove a model",
            "remove models", "disk space", "model library", "ollama", "huggingface",
            "hugging face", "hf.co", "model card", "gemma", "llama", "llama3", "qwen",
            "mistral", "deepseek", "nemotron", "gpt-oss", "phi-4", "phi4", "first model",
            "get started", "getting started", "where do i start", "how do i start",
            "what should i do first", "first steps", "no models", "no local models",
            "which models", "smallest model",
        ),
        patterns=(
            r"\bollama (?:pull|run|list|ls|rm|show)\b",
            r"\b[a-z0-9._-]+:(?:\d+b|latest|instruct|q\d(?:_[a-z0-9]+)*)\b",
        ),
        state=("no_models", "first_run"),
        weight=1.0,
    ),
    SpecialistRule(
        profile="vram-planner",
        keywords=(
            "will it fit", "fit in", "fit on", "fit into", "quant", "quants",
            "quantization", "quantisation", "quantized", "quantised", "q4", "q5", "q6", "q8",
            "q4_k_m", "q5_k_m", "fp16", "bf16", "fp8", "nvfp4", "int4", "int8", "gguf", "awq",
            "gptq", "context length", "context window", "num_ctx", "kv cache", "out of memory",
            "oom", "how big a model", "how large a model", "largest model", "biggest model",
            "parameter count", "70b", "8b", "7b", "13b", "14b", "27b", "32b",
            "33b", "72b", "120b", "235b", "405b", "unified memory", "memory footprint",
            "how much memory", "how much vram", "memavailable", "moe", "mixture of experts",
            "dense model", "offload", "cpu offload", "which model", "what model", "best model",
            "recommend a model", "model recommendation", "model for my", "run locally",
            "enough memory", "enough vram",
        ),
        # "what parameters does this function take" / "does this fit my
        # workflow" are not sizing questions on their own.
        weak_keywords=("fit", "fits", "parameters", "can i run", "could i run", "can my"),
        patterns=(
            r"\b\d{1,4}\s?(?:gb|gib)\b",
            r"\b\d{1,3}(?:\.\d+)?\s?b\b(?![a-z])",
            r"out[- ]of[- ]memory|\boom\b|OutOfMemoryError|oom[- ]?kill(?:er|ed)?|"
            r"CUDA_ERROR_OUT_OF_MEMORY|killed process",
        ),
        state=("device:dgx-spark", "device:rtx-spark", "unified_memory"),
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
    SpecialistRule(
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
        patterns=(
            r"\d+(?:\.\d+)?\s?(?:tok|tokens?)\s?/\s?s(?:ec)?\b",
            r"\btok/s\b|\bt/s\b|\btps\b",
            r"\d+(?:\.\d+)?\s?gb/s\b",
        ),
        state=("device:dgx-spark", "unified_memory"),
        weight=1.0,
    ),
    SpecialistRule(
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
    ),
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
            "verify that", "is this accurate", "myth", "debunk",
            "did they really", "does it really", "claim that", "rumor", "rumour",
            "hoax", "misinformation", "source for", "evidence for", "is it real",
            "true or false",
        ),
        # "verify my api key", "is the reading accurate", "confirm that the gpu works".
        weak_keywords=("verify", "accurate", "claims", "correct that", "confirm that"),
        patterns=(
            r"\bis (?:it|that|this) (?:true|accurate|real|correct|actually)\b",
            r"\bfact[- ]?check\b|\btrue or false\b",
            r"\b(?:did|does|do|is|was|are|can) (?:\w+ ){1,3}really\b",
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

    strong_hits: list[str] = []
    weak_hits: list[str] = []
    if comp.keywords is not None:
        for m in comp.keywords.finditer(norm):
            tok = m.group(0)
            bucket = weak_hits if tok in comp.weak else strong_hits
            if tok not in bucket:
                bucket.append(tok)
    pat_hits: list[str] = []
    for p in comp.patterns:
        pm = p.search(raw)
        if pm is not None:
            snippet = _normalise(pm.group(0))[:40] or p.pattern[:40]
            pat_hits.append(f"re:{snippet}")

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
        for pred in rule.state:
            if pred in state:
                out.states.append(pred)
                out.score += STATE_BOOST
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

    # Tier 1: deterministic triggers. max() keeps the first maximum, and
    # dict order is rule order, so ties resolve to the earlier rule.
    best = max(best_by_profile.values(), key=lambda s: s.score)
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
