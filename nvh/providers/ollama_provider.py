"""Ollama (local) provider adapter via LiteLLM."""

from __future__ import annotations

import json
import os
import re
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from urllib.parse import urlsplit

import httpx
import litellm

from nvh.core import local_models
from nvh.providers.base import (
    CompletionResponse,
    FinishReason,
    HealthStatus,
    Message,
    ModelInfo,
    ProviderUnavailableError,  # noqa: F401 — also used directly for connection errors
    StreamChunk,
    Usage,
)
from nvh.providers.openai_compatible import _build_messages, _map_error
from nvh.utils.ollama import ollama_base_url

_AUTO_MODEL_CHOICES = {
    "auto",
    "auto-pick",
    "auto pick",
    "auto-pick best available",
    "recommended",
    "recommended model",
    "best available",
    "default",
    "none",
}

# ``NVH_OLLAMA_NUM_CTX``: a positive integer replaces the VRAM-tier ``num_ctx``
# (still capped at the model's own context), ``0`` sends no ``num_ctx`` at all,
# unset or unparsable defers to detection. Same family as NVH_OLLAMA_URL. It is
# also the only way to size ``num_ctx`` for a daemon that is not on this
# machine, or on a box with no visible GPU -- see :func:`_detect_num_ctx`.
NUM_CTX_ENV = "NVH_OLLAMA_NUM_CTX"

# ``ollama_base_url`` spells every loopback alias as 127.0.0.1; ``[::1]`` and a
# caller-supplied ``localhost`` are the other ways to say "this machine".
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def _fallback_model_preference() -> tuple[str, ...]:
    """Text-capable picks largest first -- chat, code, reasoning -- then vision-only picks; never embed.

    Both halves are :func:`nvh.core.local_models.ordered_picks` over the whole
    table (``budget=None``): the strongest installed model wins.
    """
    text = local_models.ordered_picks(None, "chat", "code", "reasoning")
    text_tags = {p.tag for p in text}
    vision_only = [p for p in local_models.vision_picks() if p.tag not in text_tags]
    return tuple(p.tag for p in text) + tuple(p.tag for p in vision_only)


def _vision_model_preference() -> tuple[str, ...]:
    """Every pick that sees images, largest first (dedicated vision picks and image-capable chat picks alike)."""
    return tuple(p.tag for p in local_models.ordered_picks(None) if p.vision)


# Both ladders are derived from nvh.core.local_models, so a stale default can
# only ever fall back to a tag the registry still serves.
_FALLBACK_MODEL_PREFERENCE = _fallback_model_preference()
_VISION_MODEL_PREFERENCE = _vision_model_preference()

# The fallback ladder's two halves: chat lands on a vision-only pick (moondream,
# llama3.2-vision, qwen3-vl) only when no text-capable pick is installed.
_VISION_ONLY_TAGS = frozenset(_FALLBACK_MODEL_PREFERENCE) - {
    p.tag for p in local_models.ordered_picks(None, "chat", "code", "reasoning")
}

# --- what can chat at all ----------------------------------------------------
#
# Every tier pulls nomic-embed-text and the daemon lists newest first, so the
# "nothing on the ladder" fallback used to hand the embedding model to chat and
# vision. The table's embed picks are known by name; anything else named like
# an embedding or reranking model (bge-m3, all-minilm, mxbai-embed-large,
# e5-mistral, bge-reranker-v2-m3, snowflake-arctic-embed2) is caught by shape.
_TABLE_EMBED_NAMES = frozenset(
    tier.picks["embed"].name for tier in local_models.LOCAL_MODEL_TIERS if "embed" in tier.picks
)
_NON_CHAT_NAME_RE = re.compile(
    r"(?<![a-z0-9])(?:embed|rerank|bge(?![a-z0-9])|e5(?![a-z0-9])|minilm)", re.IGNORECASE
)


def _is_chat_capable(name: str) -> bool:
    """False for an installed tag that cannot chat: a table embed pick, or a name shaped like an embedding / reranker model."""
    base = name.partition(":")[0]
    return base not in _TABLE_EMBED_NAMES and _NON_CHAT_NAME_RE.search(base) is None


def _ladder(tags: tuple[str, ...]) -> tuple[local_models.LocalModelPick, ...]:
    """The picks behind a tag ladder, in ladder order (every tag is a table tag, so none is dropped)."""
    picks = (local_models.pick_for_tag(tag) for tag in tags)
    return tuple(p for p in picks if p is not None)


# The same two ladders as picks: the installed-model walk below needs each
# rung's registry name and parameter size, not just its tag.
_FALLBACK_LADDER = _ladder(_FALLBACK_MODEL_PREFERENCE)
_VISION_LADDER = _ladder(_VISION_MODEL_PREFERENCE)

# --- which installed tag stands in for a ladder rung -------------------------
#
# An installed tag stands in for a rung in one of two ways. *Exactly*:
# ``name:tag`` equal, a bare ``name`` read as ``name:latest`` (the registry's
# own convention, so an installed ``moondream:latest`` is the ``moondream``
# rung). *By family*: an installed ``qwen3:14b-q8_0`` may stand in for the
# ``qwen3:14b`` rung -- but only when it cannot be a *smaller* model than the
# rung names: the family has a single size in the table (``nemotron3``,
# ``qwen3-vl``, ``llama3.2-vision``, ``moondream``), or the member's parameter
# count, parsed off its tag (``33b-q8`` -> 33, ``30b-a3b`` -> 30, ``137m`` ->
# 0.137), is at least the rung's. Before that guard the ladders were walked by
# family alone, so an installed ``gpt-oss:20b`` satisfied the ``gpt-oss:120b``
# rung and beat an installed ``nemotron3:33b-q8``, and the text-only
# ``gemma3:1b`` satisfied the vision ladder's ``gemma3:4b`` rung ahead of
# moondream. A member whose tag carries no size at all (``qwen3:latest``) is
# accepted only at its family's *smallest* rung on the ladder -- never above a
# rung it may not reach.
#
# Standing in for a rung says which ladder (and which family) a tag belongs
# to; it does not say how strong the tag is. Candidates are ranked by their
# own parsed parameter size -- an exact table tag first among equals, then the
# table's own order -- not by the rung they stand in for: an installed
# ``gemma3:27b`` used to be pinned at the ``gemma3:4b`` rung, the family's top
# rung in the table, and so lost to an exact ``qwen3:8b``. When this machine's
# VRAM budget is known a candidate that fits it ranks above one that does not
# (:func:`_estimate_runtime_gb`), so the same install picks ``gemma3:27b`` on a
# 24 GB card and ``qwen3:8b`` on a 12 GB one. See :func:`_strongest_installed`.
_PARAM_SIZE_RE = re.compile(r"(?<![a-z0-9.])(\d+(?:\.\d+)?)([bm])(?![a-z0-9])", re.IGNORECASE)


def _canonical_tag(tag: str) -> str:
    """``"moondream"`` is ``"moondream:latest"`` -- the registry's own convention."""
    return tag if ":" in tag else f"{tag}:latest"


def _param_size_b(tag: str) -> float | None:
    """Parameter count in billions read off a tag, or None when it carries none.

    ``"nemotron3:33b-q8"`` -> 33, ``"qwen3:30b-a3b"`` -> 30 (the total, not
    the active count), ``"gemma3:1b"`` -> 1, ``"nomic-embed-text:137m"`` ->
    0.137; ``"moondream"``, ``"llama3.2-vision"`` and ``"qwen3:latest"`` ->
    None. The tag part is read first, the name second.
    """
    name, _, version = tag.partition(":")
    for part in (version, name):
        match = _PARAM_SIZE_RE.search(part)
        if match:
            value = float(match.group(1))
            return value / 1000.0 if match.group(2).lower() == "m" else value
    return None


def _family_sizes() -> dict[str, frozenset[float]]:
    """``{registry name: {parameter sizes the table carries for it}}`` (empty set when no tag names one)."""
    sizes: dict[str, set[float]] = {}
    for candidate in local_models.all_picks():
        bucket = sizes.setdefault(candidate.name, set())
        size = _param_size_b(candidate.tag)
        if size is not None:
            bucket.add(size)
    return {name: frozenset(found) for name, found in sizes.items()}


_FAMILY_SIZES = _family_sizes()


def _family_moe_flags() -> dict[str, bool | None]:
    """``{registry name: moe}`` from the table; None when the family carries both (``qwen3``)."""
    flags: dict[str, set[bool]] = {}
    for candidate in local_models.all_picks():
        flags.setdefault(candidate.name, set()).add(candidate.moe)
    return {name: (next(iter(found)) if len(found) == 1 else None) for name, found in flags.items()}


_FAMILY_MOE = _family_moe_flags()

# ``30b-a3b``: a total and an active count, the registry's way of writing a MoE tag.
_MOE_TAG_RE = re.compile(r"\d+(?:\.\d+)?b-a\d+(?:\.\d+)?b", re.IGNORECASE)

# --- the memory an installed member costs ------------------------------------
#
# A table pick carries its ``runtime_gb``; a family member the table does not
# list (``gemma3:27b``, ``qwen3:14b-q8_0``) is estimated the way the table was
# built: weights are the parameter count times a per-quant GB/B figure read off
# the registry's manifests (Q4_K_M ~0.63 -- gemma3:27b 17 GB, qwen3:32b 20 GB,
# llama3.3:70b 43 GB -- Q8_0 ~1.07, F16 ~2.0), and runtime adds the KV-cache /
# CUDA headroom ``local_models._pick`` adds: +20% dense, +10% MoE.
_Q4_GB_PER_B = 0.63
_GB_PER_B_BY_QUANT = {
    "q2": 0.40, "q3": 0.50, "q4": _Q4_GB_PER_B, "q5": 0.72, "q6": 0.82, "q8": 1.07,
    "f16": 2.0, "fp16": 2.0, "bf16": 2.0, "mxfp4": 0.60,
}
_QUANT_RE = re.compile(r"(?<![a-z0-9])(q[2-8]|fp16|bf16|f16|mxfp4)(?![0-9])", re.IGNORECASE)
_DENSE_RUNTIME_FACTOR = 1.2
_MOE_RUNTIME_FACTOR = 1.1


def _is_moe(name: str) -> bool:
    """MoE from the table when the family is uniform (``gpt-oss``, ``nemotron3``), else from the ``30b-a3b`` tag shape."""
    flag = _FAMILY_MOE.get(name.partition(":")[0])
    if flag is not None:
        return flag
    return _MOE_TAG_RE.search(name) is not None


def _gb_per_b(name: str) -> float:
    """Weights per billion parameters for the quant named in a tag; Q4_K_M when it names none."""
    match = _QUANT_RE.search(name.partition(":")[2])
    if match is None:
        return _Q4_GB_PER_B
    return _GB_PER_B_BY_QUANT.get(match.group(1).lower(), _Q4_GB_PER_B)


def _estimate_runtime_gb(name: str, size_b: float, moe: bool) -> float:
    """Loaded size of an installed member the table does not list (``gemma3:27b`` -> ~20 GB)."""
    return round(size_b * _gb_per_b(name) * (_MOE_RUNTIME_FACTOR if moe else _DENSE_RUNTIME_FACTOR), 1)


def _pick_size_b(pick: local_models.LocalModelPick) -> float:
    """A table pick's parameter count: parsed off its tag, or read back from its runtime for an untagged pick (``moondream``)."""
    size = _param_size_b(pick.tag)
    if size is not None:
        return size
    return pick.runtime_gb / (_MOE_RUNTIME_FACTOR if pick.moe else _DENSE_RUNTIME_FACTOR) / _Q4_GB_PER_B


def _family_member_satisfies(name: str, rung: local_models.LocalModelPick, *, smallest_rung: bool) -> bool:
    """True when installed ``name`` is a member of ``rung``'s family that cannot be smaller than the rung.

    A single-size family always qualifies; otherwise the member's parsed size
    must reach the rung's, and a member with no size in its tag qualifies only
    when ``smallest_rung`` says this is the family's last rung on the ladder.
    """
    if name.partition(":")[0] != rung.name:
        return False
    if len(_FAMILY_SIZES.get(rung.name, ())) <= 1:
        return True
    installed = _param_size_b(name)
    if installed is None:
        return smallest_rung
    wanted = _param_size_b(rung.tag)
    return wanted is None or installed >= wanted


def _legacy_vision_names() -> tuple[str, ...]:
    """The llava-era names an existing install may still carry (recognised, never recommended)."""
    try:
        from nvh.core.vision_tools import LEGACY_VISION_NAMES
    except Exception:
        return ()
    return tuple(LEGACY_VISION_NAMES)


@dataclass(frozen=True)
class _Candidate:
    """An installed tag that stands in for a rung of one ladder, with what the ranking needs to know about it."""

    name: str
    rung: local_models.LocalModelPick
    exact: bool
    size_b: float  # parsed parameter count (read back from the runtime for an untagged table pick)
    runtime_gb: float  # the table's figure for an exact tag, estimated for a member
    order: int  # the daemon's listing order, the last tiebreak


def _candidate_for(
    name: str, ladder: tuple[local_models.LocalModelPick, ...], order: int
) -> _Candidate | None:
    """``name`` as a candidate on ``ladder``, or None when no rung of it accepts the tag.

    An exact table tag carries the table's own size and runtime (a tag the
    ladder does not list -- ``qwen3:8b`` on the vision ladder, an embedding
    pick anywhere -- is no candidate). A family member takes the strongest rung
    of its family that :func:`_family_member_satisfies`, its size parsed off
    its own tag and its runtime estimated from that; a member with no size in
    its tag sits at the family's smallest rung with that rung's figures.
    """
    exact = local_models.pick_for_tag(name)
    if exact is not None:
        if all(rung.tag != exact.tag for rung in ladder):
            return None
        return _Candidate(name, exact, True, _pick_size_b(exact), exact.runtime_gb, order)
    rungs = [rung for rung in ladder if rung.name == name.partition(":")[0]]
    if not rungs:
        return None
    installed = _param_size_b(name)
    if installed is None:
        smallest = rungs[-1]
        return _Candidate(name, smallest, False, _pick_size_b(smallest), smallest.runtime_gb, order)
    for rung in rungs:
        if _family_member_satisfies(name, rung, smallest_rung=rung is rungs[-1]):
            runtime = _estimate_runtime_gb(name, installed, _is_moe(name))
            return _Candidate(name, rung, False, installed, runtime, order)
    return None


def _strongest_installed(
    names: list[str],
    ladder: tuple[local_models.LocalModelPick, ...],
    *,
    exclude: str | None = None,
    legacy_vision: bool = False,
    text_first: bool = False,
    budget_gb: float | None = None,
) -> str | None:
    """The strongest installed tag that stands in for some rung of ``ladder``, or None.

    Every eligible tag becomes a :class:`_Candidate` (:func:`_candidate_for`
    says when a tag counts) and the candidates are ranked, best first, by:

    1. ``text_first``: a text-capable pick before a vision-only one (the
       fallback ladder's two halves), so chat never lands on moondream while a
       chat pick is installed;
    2. fit: with ``budget_gb`` known, a candidate whose table or estimated
       runtime fits this machine's VRAM (plus the table's half-gigabyte snap)
       before one that would spill into system RAM; when nothing fits, the
       smallest overflow wins;
    3. size: parsed parameter count, largest first, across families -- an
       installed ``gemma3:27b`` beats an exact ``qwen3:8b`` (it used to be
       pinned at the ``gemma3:4b`` rung);
    4. an exact table tag before a family member of the same size
       (``qwen3:8b`` over ``qwen3:8b-q8_0``), then the ladder's own order,
       then the daemon's listing order.

    ``exclude`` drops the tag that just failed (compared canonically, so
    ``moondream`` excludes ``moondream:latest``). With ``legacy_vision`` a
    llava-era install (:data:`nvh.core.vision_tools.LEGACY_VISION_NAMES`) is
    accepted when nothing on the ladder is installed, so an existing llava /
    minicpm-v keeps answering image questions without ever being recommended.
    """
    excluded = _canonical_tag(exclude) if exclude else None
    eligible = [n for n in names if excluded is None or _canonical_tag(n) != excluded]
    if not eligible:
        return None
    candidates = [c for c in (_candidate_for(n, ladder, i) for i, n in enumerate(eligible)) if c is not None]
    if candidates:
        ladder_index = {rung.tag: i for i, rung in enumerate(ladder)}
        limit = None if budget_gb is None else budget_gb + local_models.TIER_SNAP_GB

        def rank(c: _Candidate) -> tuple[bool, bool, float, bool, int, int]:
            fits = limit is None or c.runtime_gb <= limit
            return (
                text_first and c.rung.tag in _VISION_ONLY_TAGS,
                not fits,
                -c.size_b if fits else c.runtime_gb,
                not c.exact,
                ladder_index[c.rung.tag],
                c.order,
            )

        return min(candidates, key=rank).name
    if legacy_vision:
        legacy = _legacy_vision_names()
        for name in eligible:
            if name.partition(":")[0] in legacy:
                return name
    return None

# Registry names whose every pick in the table sees images ("nemotron3",
# "qwen3-vl", ...). "gemma3" is absent on purpose: gemma3:1b is text-only, so
# only its exact vision tag is flagged.
_VISION_MODEL_NAMES = frozenset(
    name
    for name in {p.name for p in local_models.all_picks()}
    if all(p.vision for p in local_models.all_picks() if p.name == name)
)


def _supports_vision(tag: str) -> bool:
    """Vision flag for an installed tag: the table's exact tag, a name that is vision throughout, or a llava-era name.

    The legacy names (:data:`nvh.core.vision_tools.LEGACY_VISION_NAMES`) are
    the same set the auto-pick still routes image questions to; ``list_models``
    used to report them not vision-capable while HEAD had flagged them True.
    """
    exact = local_models.pick_for_tag(tag)
    if exact is not None:
        return exact.vision
    name = tag.partition(":")[0]
    return name in _VISION_MODEL_NAMES or name in _legacy_vision_names()


def _num_ctx_from_env() -> int | None:
    """``NVH_OLLAMA_NUM_CTX`` as an int (``0`` = disabled); None when unset or unparsable."""
    raw = os.environ.get(NUM_CTX_ENV, "").strip()
    if not raw:
        return None
    try:
        return max(int(raw), 0)
    except ValueError:
        return None


def _daemon_is_local(base_url: str) -> bool:
    """True when ``base_url`` is a loopback address, so this machine's GPUs are the daemon's GPUs.

    A remote daemon (``OLLAMA_BASE_URL=http://spark.local:11434``) runs on
    hardware the client cannot see; sizing ``num_ctx`` from the *client's*
    VRAM would be a guess about the wrong box, so detection is skipped and
    only :data:`NUM_CTX_ENV` (or the ``num_ctx`` kwarg) can set it.
    """
    try:
        host = urlsplit(base_url).hostname or ""
    except ValueError:
        return False
    return host.strip("[]").lower() in _LOOPBACK_HOSTS


def _detect_tier_budget() -> local_models.TierBudget | None:
    """This machine's :class:`~nvh.core.local_models.TierBudget`, or None when detection fails.

    GPU rows and RAM come from ``nvh.utils.gpu`` (imported lazily -- it is the
    heavier module). Never raises: a provider must construct and answer on a
    box with no NVML. Shared by :func:`_detect_num_ctx` and the installed-model
    walk's budget fit (:meth:`OllamaProvider._resolved_budget_gb`).
    """
    try:
        from nvh.utils.gpu import detect_gpus, detect_system_memory

        return local_models.tier_budget(detect_gpus(), detect_system_memory())
    except Exception:
        return None


def _detect_num_ctx() -> int | None:
    """``num_ctx`` for this machine's VRAM tier; None when nothing should be sent.

    The tier maths are :func:`nvh.core.local_models.tier_budget` (through
    :func:`_detect_tier_budget`) and :func:`nvh.core.local_models.num_ctx_for`.
    Never raises.

    None is returned both when detection fails and when no GPU is seen at all
    (``TierBudget.total_gpus == 0``: no pynvml or nvidia-smi, Ollama in a
    container the client cannot introspect). The table's CPU-only figure of
    :data:`nvh.core.local_models.CPU_ONLY_NUM_CTX` (2048) sits *below*
    Ollama's own default, so pushing it would shrink the context of a daemon
    that may well have a GPU; sending no option lets the daemon's default
    apply. A GPU that was listed but whose memory could not be read still
    counts as a GPU and gets the 0-4 tier's ``num_ctx``.
    """
    budget = _detect_tier_budget()
    if budget is None or budget.total_gpus == 0:
        return None
    try:
        value = int(local_models.num_ctx_for(budget))
    except Exception:
        return None
    return value if value > 0 else None


def _rootless_ollama_unavailable_message(base_url: str) -> str:
    return (
        f"Ollama is not responding at {base_url}. "
        "Open nvWizard Setup and press Install Runtime or Fix My Setup. "
        "nvHive repairs the rootless Ollama runtime under NVH_HOME; no sudo, apt, "
        "or system install should be needed. Advanced override: nvh studio --install rootless-ollama -y"
    )


async def _raise_for_ollama_status(resp: httpx.Response) -> None:
    """``raise_for_status`` that keeps the daemon's own error text.

    Ollama refuses a chat request to a model that cannot chat with ``400
    {"error": "\\"nomic-embed-text\\" does not support chat"}`` (``generate``
    on the other endpoint). httpx's own message is just ``Client error '400
    Bad Request'``, which :meth:`OllamaProvider._should_try_installed_fallback`
    could not tell from any other bad request, so the embedding model was kept
    and the user saw the 400. The body is read only once the status has failed
    (a streamed response has not been read yet), so a good answer pays nothing.
    """
    try:
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        detail = ""
        try:
            body = await exc.response.aread()
            detail = str(json.loads(body).get("error") or "").strip()
        except Exception:
            detail = ""
        if detail:
            raise RuntimeError(f"{exc.response.status_code} from Ollama: {detail}") from exc
        raise


def _ollama_daemon_reachable(base_url: str) -> bool:
    """Return True iff Ollama responds to /api/tags within 2s.

    We use this as a ground-truth check BEFORE raising "Ollama is not
    running" based on an error message substring. Many litellm errors
    contain substrings like "connect", "connection", or "HTTPConnectionPool"
    even when the daemon is up and the real issue is model-not-found,
    timeout, or auth. Actually probing the daemon eliminates false
    positives that confuse users.
    """
    try:
        resp = httpx.get(f"{base_url.rstrip('/')}/api/tags", timeout=2)
        return resp.status_code == 200
    except Exception:
        return False


class OllamaProvider:
    """Ollama local model adapter using LiteLLM."""

    def __init__(
        self,
        api_key: str = "",
        default_model: str = "ollama/gemma3:4b",
        fallback_model: str = "",
        base_url: str | None = None,
        provider_name: str = "ollama",
        timeout: int = 300,
        num_ctx: int | None = None,
    ):
        self._default_model = default_model
        self._fallback_model = fallback_model
        self._base_url = ollama_base_url(base_url)
        self._provider_name = provider_name
        self._timeout = timeout
        # num_ctx and the VRAM budget are resolved once, on the first request
        # that needs them, not here: constructing a provider must not touch
        # NVML. ``num_ctx=0`` disables the option.
        self._num_ctx_override = num_ctx
        self._explicit_num_ctx_cache: tuple[bool, int | None] | None = None
        self._tier_ctx: int | None = None
        self._tier_ctx_resolved = False
        self._budget_gb: float | None = None
        self._budget_resolved = False
        self._model_ctx_limits: dict[str, int | None] = {}

    @property
    def name(self) -> str:
        return self._provider_name

    def _explicit_num_ctx(self) -> tuple[bool, int | None]:
        """``(given, value)`` for the ``num_ctx`` kwarg / :data:`NUM_CTX_ENV` override, read once.

        ``given`` is True whenever either was set -- ``0`` included, when
        ``value`` is None: send nothing, to any model. An unparsable env value
        is not given at all and defers to the tier.
        """
        if self._explicit_num_ctx_cache is None:
            raw = self._num_ctx_override
            if raw is None:
                raw = _num_ctx_from_env()
            self._explicit_num_ctx_cache = (raw is not None, int(raw) if raw and raw > 0 else None)
        return self._explicit_num_ctx_cache

    def _tier_num_ctx(self) -> int | None:
        """The VRAM tier's ``num_ctx``, detected once, for a loopback daemon only.

        None means "send no num_ctx" -- Ollama then uses the model's default --
        which is what a failed detection, a box with no visible GPU, or a
        daemon on another machine (:func:`_daemon_is_local`) produce: the
        client's GPUs say nothing about a remote one.
        """
        if not self._tier_ctx_resolved:
            value = _detect_num_ctx() if _daemon_is_local(self._base_url) else None
            self._tier_ctx = int(value) if value and value > 0 else None
            self._tier_ctx_resolved = True
        return self._tier_ctx

    def _resolved_num_ctx(self) -> int | None:
        """What a table pick is sent: the kwarg, then the env override, then the VRAM tier (None = no ``num_ctx``)."""
        given, value = self._explicit_num_ctx()
        return value if given else self._tier_num_ctx()

    def _num_ctx_for(self, raw_model: str) -> int | None:
        """The ``num_ctx`` to send ``raw_model``, before the model-context cap; None sends none.

        An explicit figure (the ``num_ctx`` kwarg or :data:`NUM_CTX_ENV`)
        applies to every model. The detected tier figure applies only to a
        table pick (:func:`nvh.core.local_models.pick_for_tag`): the tier sized
        its context for those models. Any other model -- a custom Modelfile, an
        imported GGUF, a family member the auto-pick accepted -- gets no
        ``num_ctx``, so its own Modelfile ``num_ctx`` or Ollama's default
        applies instead of a figure the tier never planned for; detection is
        not even run for it. It used to be attached to every native request,
        capped only by the model's trained context.
        """
        given, value = self._explicit_num_ctx()
        if given:
            return value
        if local_models.pick_for_tag(raw_model) is None:
            return None
        return self._tier_num_ctx()

    def _resolved_budget_gb(self) -> float | None:
        """This machine's VRAM budget for ranking installed models, detected once; None = no fit check.

        Loopback daemons only (the client's GPUs say nothing about a remote
        box), and only when a GPU's memory could be read: a ``TierBudget``
        with no sized GPU has nothing to fit against. Never raises.
        """
        if not self._budget_resolved:
            budget = _detect_tier_budget() if _daemon_is_local(self._base_url) else None
            self._budget_gb = budget.budget_gb if budget is not None and budget.sized_gpus > 0 else None
            self._budget_resolved = True
        return self._budget_gb

    async def _model_context_limit(self, raw_model: str) -> int | None:
        """The model's own context length from ``/api/show`` (``model_info.<arch>.context_length``).

        Cached per tag for the life of the instance, failures included: the
        cap is best-effort and must never add a round trip to every request
        or turn a chat into an error.
        """
        if raw_model in self._model_ctx_limits:
            return self._model_ctx_limits[raw_model]
        limit: int | None = None
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    f"{self._base_url.rstrip('/')}/api/show",
                    json={"model": raw_model},
                    timeout=5.0,
                )
                resp.raise_for_status()
                info = resp.json().get("model_info") or {}
                for key, value in info.items():
                    if str(key).endswith(".context_length"):
                        limit = int(value)
                        break
        except Exception:
            limit = None
        self._model_ctx_limits[raw_model] = limit if limit and limit > 0 else None
        return self._model_ctx_limits[raw_model]

    async def _options(self, raw_model: str, temperature: float, max_tokens: int) -> dict[str, Any]:
        """Ollama ``options`` for one request: sampling, plus the ``num_ctx`` :meth:`_num_ctx_for` grants, never above the model's own."""
        options: dict[str, Any] = {"temperature": temperature, "num_predict": max_tokens}
        num_ctx = self._num_ctx_for(raw_model)
        if num_ctx is not None:
            limit = await self._model_context_limit(raw_model)
            options["num_ctx"] = min(num_ctx, limit) if limit else num_ctx
        return options

    def _get_model(self, model: str | None) -> str:
        m = (model or "").strip()
        if not m or m.lower() in _AUTO_MODEL_CHOICES:
            m = self._default_model
        # LiteLLM requires the ollama/ prefix for routing
        if m and not m.startswith("ollama/"):
            m = f"ollama/{m}"
        return m

    @staticmethod
    def _is_auto_model_selection(model: str | None) -> bool:
        m = (model or "").strip().lower()
        return not m or m in _AUTO_MODEL_CHOICES

    def _kwargs(self, model: str) -> dict[str, Any]:
        kw: dict[str, Any] = {"model": model, "api_base": self._base_url}
        return kw

    @staticmethod
    def _looks_like_missing_model(exc: Exception) -> bool:
        text = str(exc).lower()
        return "404" in text or ("model" in text and "not found" in text)

    @staticmethod
    def _looks_like_unsupported_model(exc: Exception) -> bool:
        """Ollama's 400 for a model that cannot serve the endpoint: ``"nomic-embed-text" does not support chat`` (or ``generate``).

        The text reaches here through :func:`_raise_for_ollama_status`; the
        model is as unavailable as a missing one and gets the same retry.
        """
        return "does not support" in str(exc).lower()

    @staticmethod
    def _should_try_installed_fallback(exc: Exception) -> bool:
        text = str(exc).lower()
        return (
            OllamaProvider._looks_like_missing_model(exc)
            or OllamaProvider._looks_like_unsupported_model(exc)
            or "timeout" in text
            or "timed out" in text
            or "stalled" in text
            or "no tokens" in text
            or "no text" in text
            or "empty" in text
        )

    async def _installed_model_fallback(
        self,
        attempted_model: str,
        *,
        prefer_vision: bool = False,
    ) -> str | None:
        """Return a usable installed model when the configured default is stale."""
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(f"{self._base_url.rstrip('/')}/api/tags", timeout=3)
                resp.raise_for_status()
                data = resp.json()
        except Exception:
            return None

        names = [
            str(item.get("name", "")).strip()
            for item in data.get("models", [])
            if item.get("name")
        ]
        if not names:
            return None

        attempted_raw = attempted_model.removeprefix("ollama/")
        exclude = None if attempted_model == "__auto__" else attempted_raw
        budget_gb = self._resolved_budget_gb()
        chosen = _strongest_installed(
            names,
            _VISION_LADDER if prefer_vision else _FALLBACK_LADDER,
            exclude=exclude,
            legacy_vision=prefer_vision,
            text_first=not prefer_vision,
            budget_gb=budget_gb,
        )
        if chosen is not None:
            return f"ollama/{chosen}"

        # Nothing on the ladder is installed. Fall back to what is, but never to
        # a model that cannot chat: every tier pulls nomic-embed-text and the
        # daemon lists newest first, so "whatever is listed first" used to hand
        # the embedding model to chat and vision. Among the chat-capable tags
        # one that fits the budget wins, then the largest parsed size (sizeless
        # tags last), then the daemon's order. When only non-chat models are
        # installed the caller keeps its own error -- the 404 / 400 that
        # _map_error turns into ModelNotFoundError / InvalidRequestError --
        # instead of a doomed retry.
        excluded = _canonical_tag(exclude) if exclude else None
        usable = [
            n for n in names
            if (excluded is None or _canonical_tag(n) != excluded) and _is_chat_capable(n)
        ]
        if not usable:
            return None
        limit = None if budget_gb is None else budget_gb + local_models.TIER_SNAP_GB

        def rank(name: str) -> tuple[bool, bool, float]:
            size = _param_size_b(name)
            fits = limit is None or size is None or _estimate_runtime_gb(name, size, _is_moe(name)) <= limit
            return (not fits, size is None, -(size or 0.0))

        return f"ollama/{min(usable, key=rank)}"

    @staticmethod
    def _messages_for_ollama(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Convert OpenAI-style multimodal messages to Ollama native messages."""
        converted: list[dict[str, Any]] = []
        for msg in messages:
            content = msg.get("content", "")
            if not isinstance(content, list):
                converted.append(dict(msg))
                continue

            text_parts: list[str] = []
            images: list[str] = []
            for part in content:
                if isinstance(part, str):
                    text_parts.append(part)
                    continue
                if not isinstance(part, dict):
                    text_parts.append(str(part))
                    continue
                kind = str(part.get("type", "")).lower()
                if kind == "text":
                    text_parts.append(str(part.get("text", "")))
                    continue
                if kind == "image_url":
                    image_url = part.get("image_url")
                    url = ""
                    if isinstance(image_url, dict):
                        url = str(image_url.get("url", ""))
                    elif image_url:
                        url = str(image_url)
                    if url.startswith("data:image/") and "," in url:
                        images.append(url.split(",", 1)[1])
                    elif url and re.fullmatch(r"[A-Za-z0-9+/=\s]+", url):
                        images.append(url.strip())
                    elif url:
                        text_parts.append(f"[Image URL attached: {url}]")

            out = {k: v for k, v in msg.items() if k != "content"}
            out["content"] = "\n".join(part for part in text_parts if part).strip()
            if images:
                out["images"] = images
            converted.append(out)
        return converted

    async def complete(
        self,
        messages: list[Message],
        model: str | None = None,
        temperature: float = 1.0,
        max_tokens: int = 4096,
        system_prompt: str | None = None,
        **kwargs: Any,
    ) -> CompletionResponse:
        prefer_vision = bool(kwargs.pop("prefer_vision", False))
        auto_model = self._is_auto_model_selection(model)
        model_name = self._get_model(model)
        initial_model_name = model_name
        msgs = _build_messages(messages, system_prompt)
        start = time.monotonic()

        if auto_model:
            installed = await self._installed_model_fallback(
                "__auto__",
                prefer_vision=prefer_vision,
            )
            if installed:
                model_name = installed

        # Prefer Ollama's native API for local desktop installs. It avoids a
        # class of LiteLLM edge cases where a freshly loaded local model reports
        # usage but returns no text, which made first-run quick tests feel broken.
        direct_error: Exception | None = None
        try:
            content = await self._direct_complete(
                msgs, model_name, temperature, max_tokens,
            )
            if content.strip():
                elapsed = int((time.monotonic() - start) * 1000)
                output_tokens = max(1, self.estimate_tokens(content))
                prompt_tokens = sum(
                    self.estimate_tokens(str(message.get("content", "")))
                    for message in msgs
                )
                return CompletionResponse(
                    content=content,
                    model=model_name,
                    provider=self._provider_name,
                    usage=Usage(
                        input_tokens=prompt_tokens,
                        output_tokens=output_tokens,
                        total_tokens=prompt_tokens + output_tokens,
                    ),
                    cost_usd=Decimal("0"),
                    latency_ms=elapsed,
                    finish_reason=FinishReason.STOP,
                    metadata={"transport": "ollama-api"},
                )
            if auto_model:
                fallback_model = await self._installed_model_fallback(
                    model_name,
                    prefer_vision=prefer_vision,
                )
                if fallback_model:
                    content = await self._direct_complete(
                        msgs, fallback_model, temperature, max_tokens,
                    )
                    if content.strip():
                        elapsed = int((time.monotonic() - start) * 1000)
                        output_tokens = max(1, self.estimate_tokens(content))
                        prompt_tokens = sum(
                            self.estimate_tokens(str(message.get("content", "")))
                            for message in msgs
                        )
                        return CompletionResponse(
                            content=content,
                            model=fallback_model,
                            provider=self._provider_name,
                            usage=Usage(
                                input_tokens=prompt_tokens,
                                output_tokens=output_tokens,
                                total_tokens=prompt_tokens + output_tokens,
                            ),
                            cost_usd=Decimal("0"),
                            latency_ms=elapsed,
                            finish_reason=FinishReason.STOP,
                            metadata={
                                "transport": "ollama-api",
                                "fallback_model": fallback_model,
                                "fallback_reason": "empty response",
                            },
                        )
            direct_error = RuntimeError(f"Provider '{self._provider_name}' returned no text from {model_name}")
        except Exception as exc:
            direct_error = exc
            if self._should_try_installed_fallback(exc):
                fallback_model = await self._installed_model_fallback(
                    model_name,
                    prefer_vision=prefer_vision,
                )
                if fallback_model:
                    try:
                        content = await self._direct_complete(
                            msgs, fallback_model, temperature, max_tokens,
                        )
                        if content.strip():
                            elapsed = int((time.monotonic() - start) * 1000)
                            output_tokens = max(1, self.estimate_tokens(content))
                            prompt_tokens = sum(
                                self.estimate_tokens(str(message.get("content", "")))
                                for message in msgs
                            )
                            return CompletionResponse(
                                content=content,
                                model=fallback_model,
                                provider=self._provider_name,
                                usage=Usage(
                                    input_tokens=prompt_tokens,
                                    output_tokens=output_tokens,
                                    total_tokens=prompt_tokens + output_tokens,
                                ),
                                cost_usd=Decimal("0"),
                                latency_ms=elapsed,
                                finish_reason=FinishReason.STOP,
                                metadata={
                                    "transport": "ollama-api",
                                    "fallback_model": fallback_model,
                                },
                            )
                    except Exception as retry_error:
                        direct_error = retry_error
        if direct_error is not None:
            err_str = str(direct_error).lower()
            looks_like_conn = (
                "connection" in err_str
                or "refused" in err_str
                or "connect" in err_str
            )
            if looks_like_conn and not _ollama_daemon_reachable(self._base_url):
                raise ProviderUnavailableError(
                    _rootless_ollama_unavailable_message(self._base_url),
                    provider=self._provider_name,
                    original_error=direct_error,
                ) from direct_error

        try:
            response = await litellm.acompletion(
                messages=msgs,
                temperature=temperature,
                max_tokens=max_tokens,
                timeout=self._timeout,
                **self._kwargs(model_name),
                **kwargs,
            )
        except Exception as e:
            if self._should_try_installed_fallback(e):
                fallback_model = await self._installed_model_fallback(
                    model_name,
                    prefer_vision=prefer_vision,
                )
                if fallback_model:
                    try:
                        response = await litellm.acompletion(
                            messages=msgs,
                            temperature=temperature,
                            max_tokens=max_tokens,
                            timeout=self._timeout,
                            **self._kwargs(fallback_model),
                            **kwargs,
                        )
                        model_name = fallback_model
                    except Exception as retry_error:
                        e = retry_error
                    else:
                        elapsed = int((time.monotonic() - start) * 1000)
                        usage_data = response.usage
                        usage = Usage(
                            input_tokens=getattr(usage_data, "prompt_tokens", 0) or 0,
                            output_tokens=getattr(usage_data, "completion_tokens", 0) or 0,
                            total_tokens=getattr(usage_data, "total_tokens", 0) or 0,
                        )
                        content = response.choices[0].message.content or ""
                        return CompletionResponse(
                            content=content,
                            model=response.model or model_name,
                            provider=self._provider_name,
                            usage=usage,
                            cost_usd=Decimal("0"),
                            latency_ms=elapsed,
                            finish_reason=FinishReason.STOP,
                            metadata={"fallback_model": model_name},
                        )
            err_str = str(e).lower()
            looks_like_conn = (
                "connection" in err_str
                or "refused" in err_str
                or "connect" in err_str
            )
            # Only declare "not running" if the daemon actually isn't
            # answering — otherwise the real cause is model-not-found,
            # timeout, or some other transient issue and the user needs
            # the underlying error, not a misleading "start Ollama" hint.
            if looks_like_conn and not _ollama_daemon_reachable(self._base_url):
                raise ProviderUnavailableError(
                    _rootless_ollama_unavailable_message(self._base_url),
                    provider=self._provider_name,
                    original_error=e,
                ) from e
            raise _map_error(e, self._provider_name) from e

        elapsed = int((time.monotonic() - start) * 1000)
        usage_data = response.usage
        usage = Usage(
            input_tokens=getattr(usage_data, "prompt_tokens", 0) or 0,
            output_tokens=getattr(usage_data, "completion_tokens", 0) or 0,
            total_tokens=getattr(usage_data, "total_tokens", 0) or 0,
        )
        content = response.choices[0].message.content or ""

        # Fallback: some models (e.g. Gemma 4) return empty
        # content through LiteLLM. Call Ollama API directly.
        if not content and usage.output_tokens > 0:
            try:
                content = await self._direct_complete(
                    msgs, model_name, temperature, max_tokens,
                )
            except Exception:
                pass  # keep empty, don't crash

        metadata: dict[str, Any] = {}
        if model_name != initial_model_name:
            metadata["fallback_model"] = model_name

        return CompletionResponse(
            content=content,
            model=response.model or model_name,
            provider=self._provider_name,
            usage=usage,
            cost_usd=Decimal("0"),  # Local models are free
            latency_ms=elapsed,
            finish_reason=FinishReason.STOP,
            metadata=metadata,
        )

    async def _direct_complete(
        self,
        messages: list[dict],
        model: str,
        temperature: float,
        max_tokens: int,
    ) -> str:
        """Call Ollama API directly, bypassing LiteLLM.

        Fallback for models where LiteLLM returns empty content
        (e.g. Gemma 4 with code/structured responses).
        """
        # Strip ollama/ prefix for direct API call
        raw_model = model.removeprefix("ollama/")
        options = await self._options(raw_model, temperature, max_tokens)

        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{self._base_url}/api/chat",
                json={
                    "model": raw_model,
                    "messages": self._messages_for_ollama(messages),
                    "stream": False,
                    "options": options,
                },
                timeout=self._timeout,
            )
            await _raise_for_ollama_status(resp)
            data = resp.json()
            return data.get("message", {}).get("content", "")

    async def stream(
        self,
        messages: list[Message],
        model: str | None = None,
        temperature: float = 1.0,
        max_tokens: int = 4096,
        system_prompt: str | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[StreamChunk]:
        prefer_vision = bool(kwargs.pop("prefer_vision", False))
        auto_model = self._is_auto_model_selection(model)
        model_name = self._get_model(model)
        msgs = _build_messages(messages, system_prompt)

        if auto_model:
            installed = await self._installed_model_fallback(
                "__auto__",
                prefer_vision=prefer_vision,
            )
            if installed:
                model_name = installed

        try:
            async for chunk in self._direct_stream(msgs, model_name, temperature, max_tokens):
                yield chunk
            return
        except Exception as e:
            recovered = False
            if self._should_try_installed_fallback(e):
                fallback_model = await self._installed_model_fallback(
                    model_name,
                    prefer_vision=prefer_vision,
                )
                if fallback_model:
                    try:
                        async for chunk in self._direct_stream(msgs, fallback_model, temperature, max_tokens):
                            yield chunk
                        model_name = fallback_model
                        recovered = True
                    except Exception as retry_error:
                        e = retry_error
            if not recovered:
                err_str = str(e).lower()
                looks_like_conn = (
                    "connection" in err_str
                    or "refused" in err_str
                    or "connect" in err_str
                )
                if looks_like_conn and not _ollama_daemon_reachable(self._base_url):
                    raise ProviderUnavailableError(
                        _rootless_ollama_unavailable_message(self._base_url),
                        provider=self._provider_name,
                        original_error=e,
                    ) from e
                raise _map_error(e, self._provider_name) from e

    async def _direct_stream(
        self,
        messages: list[dict],
        model: str,
        temperature: float,
        max_tokens: int,
    ) -> AsyncIterator[StreamChunk]:
        """Stream directly from Ollama's native API.

        LiteLLM is useful for cloud providers, but local Ollama is more reliable
        when we keep the chat stream close to the daemon. This also makes the
        first local-model test less fragile on fresh VMs where the model is
        still loading into VRAM.
        """
        raw_model = model.removeprefix("ollama/")
        accumulated = ""
        options = await self._options(raw_model, temperature, max_tokens)
        timeout = httpx.Timeout(self._timeout, connect=5.0, read=self._timeout, write=30.0, pool=30.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream(
                "POST",
                f"{self._base_url.rstrip('/')}/api/chat",
                json={
                    "model": raw_model,
                    "messages": self._messages_for_ollama(messages),
                    "stream": True,
                    "options": options,
                },
            ) as resp:
                await _raise_for_ollama_status(resp)
                async for line in resp.aiter_lines():
                    if not line:
                        continue
                    data = json.loads(line)
                    if data.get("error"):
                        raise RuntimeError(str(data["error"]))
                    delta = str((data.get("message") or {}).get("content") or "")
                    accumulated += delta
                    is_final = bool(data.get("done"))
                    usage = None
                    if is_final:
                        prompt_tokens = int(data.get("prompt_eval_count") or 0)
                        output_tokens = int(data.get("eval_count") or self.estimate_tokens(accumulated))
                        usage = Usage(
                            input_tokens=prompt_tokens,
                            output_tokens=output_tokens,
                            total_tokens=prompt_tokens + output_tokens,
                        )
                    yield StreamChunk(
                        delta=delta,
                        is_final=is_final,
                        accumulated_content=accumulated,
                        model=model,
                        provider=self._provider_name,
                        usage=usage,
                        cost_usd=Decimal("0") if is_final else None,
                        finish_reason=FinishReason.STOP if is_final else None,
                    )
                    if is_final:
                        return

    async def list_models(self) -> list[ModelInfo]:
        """Discover models from the Ollama API."""
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(f"{self._base_url}/api/tags", timeout=5.0)
                resp.raise_for_status()
                data = resp.json()
                models = []
                for m in data.get("models", []):
                    name = m.get("name", "")
                    models.append(ModelInfo(
                        model_id=f"ollama/{name}",
                        provider=self._provider_name,
                        display_name=name,
                        supports_vision=_supports_vision(str(name)),
                    ))
                return models
        except Exception:
            return []

    async def health_check(self) -> HealthStatus:
        """Check if Ollama is running by hitting /api/tags."""
        start = time.monotonic()
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(f"{self._base_url}/api/tags", timeout=5.0)
                resp.raise_for_status()
                data = resp.json()
                elapsed = int((time.monotonic() - start) * 1000)
                model_count = len(data.get("models", []))
                return HealthStatus(
                    provider=self._provider_name,
                    healthy=True,
                    latency_ms=elapsed,
                    models_available=model_count,
                )
        except Exception as e:
            elapsed = int((time.monotonic() - start) * 1000)
            return HealthStatus(
                provider=self._provider_name,
                healthy=False,
                latency_ms=elapsed,
                error=str(e)[:200],
            )

    def estimate_tokens(self, text: str) -> int:
        return len(text) // 4
