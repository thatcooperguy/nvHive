"""The one VRAM-tier -> local-model table every ladder reads.

Before this module six independent ladders mapped GPU memory to Ollama tags
(``install.sh``, ``nvh.utils.gpu.recommend_models``, ``nvh.cli.setup``,
``workstation.py``, ``studio_packs.py``, ``agentic.py``), three tables guessed
model sizes, and only ``gpu.get_ollama_optimizations`` knew the ``num_ctx`` /
``num_parallel`` / quant ladder. They disagreed with each other and several
carried tags that no longer exist on the registry. This module is the single
source those consumers migrate onto:

* :data:`LOCAL_MODEL_TIERS` -- ten budget bands (0-4 ... 96+ GB) with a pick per
  use case (``chat``, ``code``, ``vision``, ``reasoning``, ``embed``,
  ``cpu_fallback``) plus the tier's ``num_ctx`` / ``num_parallel`` / quant.
* :func:`tier_budget` -- the memory maths of ``gpu._memory_budget`` (unified
  pool minus a pool-sized OS reserve, :func:`unified_os_reserve_gb`, or
  discrete VRAM plus a capped CPU-offload bonus) on any GPUInfo-like object,
  so this module never imports ``nvh.utils.gpu``.
* :func:`pick` / :func:`recommended` / :func:`reason_for` -- tag and prose come
  from the same row, so a reason can never describe a different model.
* :func:`ordered_picks` / :func:`vision_picks` -- "strongest first" rankings
  for the consumers that pick among *installed* models.
* :func:`tier_table_markdown` / :func:`tier_table_shell` -- renderers for
  ``docs/MODELS.md`` and ``install.sh``.

Every tag here is verified against ``registry.ollama.ai`` by
``scripts/verify_local_model_tags.py`` (run it after editing the table; the
network-marked test in ``tests/test_local_models.py`` runs it under
``NVH_NETWORK_TESTS=1``). Sizes are the registry manifest's layer bytes in
decimal GB -- the number ``ollama list`` prints -- rounded to 0.1.

Import rule: ``nvh.utils.gpu`` will import this module, so nothing here may
import ``nvh.utils.gpu`` (or any module that does) at import time. The two
constants shared with gpu.py are duplicated below on purpose.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "CPU_OFFLOAD_CAP_GB",
    "CPU_ONLY_NUM_CTX",
    "LOCAL_MODEL_TIERS",
    "MOE_BANDWIDTH_THRESHOLD_GBPS",
    "RECOMMENDED_ORDER",
    "TIER_SNAP_GB",
    "UNIFIED_MEMORY_BANDWIDTH_GBPS",
    "UNIFIED_MEMORY_OS_RESERVE_GB",
    "UNIFIED_OS_RESERVE_FRACTION",
    "UNIFIED_OS_RESERVE_MAX_GB",
    "UNIFIED_OS_RESERVE_MIN_GB",
    "USE_CASES",
    "LocalModelPick",
    "LocalModelTier",
    "TierBudget",
    "all_picks",
    "all_tags",
    "moe_first",
    "num_ctx_for",
    "num_parallel_for",
    "ordered_picks",
    "pick",
    "pick_for_tag",
    "quant_for",
    "reason_for",
    "recommended",
    "size_table",
    "tier_budget",
    "tier_for",
    "tier_table_markdown",
    "tier_table_shell",
    "unified_os_reserve_gb",
    "vision_picks",
]

# --- constants ---------------------------------------------------------------

# Re-exported by nvh.utils.gpu under the same names (gpu.py imports this module,
# so the values live here). UNIFIED_MEMORY_OS_RESERVE_GB is the GB10 / DGX Spark
# figure -- OS + WebUI + desktop headroom on a 128 GB pool -- and the *ceiling*
# of the reserve curve below; it is not what every unified pool loses.
UNIFIED_MEMORY_OS_RESERVE_GB = 16.0   # OS + WebUI + desktop headroom on a 128 GB unified pool
UNIFIED_MEMORY_BANDWIDTH_GBPS = 273   # GB10 LPDDR5x, per NVIDIA's DGX Spark spec sheet

# The OS reserve a unified pool loses scales with the pool (unified_os_reserve_gb):
#
#     reserve = min(MAX, max(MIN, round(total_gb * FRACTION)))
#
#     pool (GB):    8   16   24   32   48   64   96  128  192+
#     reserve (GB): 4    4    4    4    6    8   12   16   16
#     budget (GB):  4   12   20   28   42   56   84  112  total-16
#
# One eighth of the pool, floored at 4 GB (what a desktop OS plus a browser hold
# on an 8-16 GB laptop) and capped at the GB10 figure. Before this curve every
# unified pool lost the flat 16 GB, so install-mac.sh planned a 16 GB Apple
# Silicon Mac against a 0 GB budget (gemma3:1b) where an 8B model fits.
# tier_table_shell() exports the three numbers as NVH_UNIFIED_OS_RESERVE_MIN_GB
# / _MAX_GB / _FRACTION so install.sh can reproduce the curve instead of typing it.
UNIFIED_OS_RESERVE_MIN_GB = 4.0
UNIFIED_OS_RESERVE_MAX_GB = UNIFIED_MEMORY_OS_RESERVE_GB
UNIFIED_OS_RESERVE_FRACTION = 0.125

# Below this memory bandwidth dense models are bandwidth-bound and MoE models
# (a few billion active parameters per token) are the better fit. Every
# discrete NVIDIA part with GDDR6X/GDDR7/HBM is above it; GB10 is well below.
MOE_BANDWIDTH_THRESHOLD_GBPS = 500

# gpu._memory_budget: CPU-offloaded layers are 5-10x slower than the GPU, so the
# RAM bonus is capped and only meant for "barely doesn't fit" cases.
CPU_OFFLOAD_CAP_GB = 16.0

# Drivers report totals a few hundred MiB under the nominal size (a 24 GB RTX
# 4090 shows 24564 MiB = 23.99 GB; an 80 GB H100 shows 81559 MiB = 79.65 GB).
# tier_for() -- and so every accessor that goes through it: pick, recommended,
# ordered_picks, num_ctx_for, num_parallel_for, quant_for, reason_for -- snaps
# the budget up by this much so those cards land in the tier their box says.
# No real card sits within 0.5 GB *below* a boundary otherwise. gpu.py reads
# its ladder through these accessors, so recommend_models and
# get_ollama_optimizations snap identically; install.sh adds the same half
# gigabyte (NVH_TIER_SNAP_MB from tier_table_shell) before its integer divide.
TIER_SNAP_GB = 0.5

# gpu.get_ollama_optimizations with an *empty* GPU list sizes the context to
# 2048. A GPU it saw but could not size (a 0 GB row) gets the 0-4 tier's 4096
# there, so num_ctx_for keys this on TierBudget.total_gpus, not sized_gpus.
CPU_ONLY_NUM_CTX = 2048

USE_CASES: tuple[str, ...] = ("chat", "code", "vision", "reasoning", "embed", "cpu_fallback")
# Pull-list order for recommended(); reasoning joins only on MoE-first pools.
RECOMMENDED_ORDER: tuple[str, ...] = ("chat", "code", "vision", "embed", "cpu_fallback")

_UNKNOWN_CC: tuple[int, int] = (0, 0)
_SHELL_OPEN_MAX = 999999  # "no upper bound" for install.sh integer compares


# --- schema ------------------------------------------------------------------


@dataclass(frozen=True)
class LocalModelPick:
    """One pullable Ollama tag and what it costs to run.

    ``weights_gb`` is the registry manifest size (decimal GB, what ``ollama
    list`` prints). ``runtime_gb`` adds KV cache and CUDA context headroom:
    +20% for dense models, +10% for MoE (few active parameters, small KV);
    see :func:`_pick`. ``min_compute_capability`` gates picks whose kernels
    need a newer architecture -- llama3.2-vision's BF16 paths crawl on Turing,
    so :func:`pick` falls back a tier there.
    """

    tag: str
    catalog_id: str
    quant: str
    weights_gb: float
    runtime_gb: float
    moe: bool = False
    vision: bool = False
    min_compute_capability: tuple[int, int] | None = None

    @property
    def name(self) -> str:
        """Registry name without the tag (``"qwen3:8b"`` -> ``"qwen3"``)."""
        return self.tag.partition(":")[0]

    @property
    def version(self) -> str:
        """The tag part (``"latest"`` when the tag carries none)."""
        return self.tag.partition(":")[2] or "latest"


@dataclass(frozen=True)
class LocalModelTier:
    """One budget band ``[min_gb, max_gb)`` and its picks per use case."""

    min_gb: float
    max_gb: float | None
    label: str
    num_ctx: int
    num_parallel: int
    default_quant: str
    picks: dict[str, LocalModelPick] = field(default_factory=dict)
    notes: str = ""

    def contains(self, budget_gb: float) -> bool:
        return budget_gb >= self.min_gb and (self.max_gb is None or budget_gb < self.max_gb)

    @property
    def range_label(self) -> str:
        """``"24-40"`` / ``"96+"`` -- the form used in docs and CLI output."""
        lo = _fmt_gb(self.min_gb)
        return f"{lo}+" if self.max_gb is None else f"{lo}-{_fmt_gb(self.max_gb)}"


@dataclass(frozen=True)
class TierBudget:
    """How much memory the ladder may plan against, and what kind of pool it is.

    Same maths as ``gpu._memory_budget``: on a unified pool (GB10 / DGX Spark,
    Apple Silicon) ``budget_gb`` is the pool minus its OS reserve
    (:func:`unified_os_reserve_gb`, 16 GB on a 128 GB GB10, 4 GB on a 16 GB
    Mac; :attr:`os_reserve_gb`) and ``offload_gb`` is 0 -- the same bytes must
    not be counted twice; on discrete GPUs ``budget_gb`` is the summed VRAM of
    every sized row and ``offload_gb`` is ``min(effective RAM, 16)``.

    ``total_gpus`` counts every row seen and ``sized_gpus`` those whose pool
    could be read (``vram_mb > 0``). They differ when NVML lists a GPU but
    cannot report its memory: that machine has a GPU (``total_gpus == 1``)
    and no VRAM to plan against (``sized_gpus == 0``). Only ``total_gpus ==
    0`` means "no GPU at all" -- the CPU-only ``num_ctx`` of 2048 and the "no
    GPU detected" reason key on it, not on ``sized_gpus``.
    """

    budget_gb: float
    offload_gb: float
    unified: bool
    bandwidth_gbps: float | None
    compute_capability: tuple[int, int]
    total_gb: float
    sized_gpus: int
    total_gpus: int

    @property
    def combined_gb(self) -> float:
        return self.budget_gb + self.offload_gb

    @property
    def os_reserve_gb(self) -> float:
        """The OS reserve taken off a unified pool (:func:`unified_os_reserve_gb`); 0 on discrete GPUs."""
        return unified_os_reserve_gb(self.total_gb) if self.unified else 0.0

    @property
    def gpu_memory_unreadable(self) -> bool:
        """A GPU was listed but none reported its memory (0 GB rows only)."""
        return self.total_gpus > 0 and self.sized_gpus == 0

    @property
    def compute_capability_known(self) -> bool:
        return self.compute_capability != _UNKNOWN_CC


# --- the table ---------------------------------------------------------------


def _runtime_gb(weights_gb: float, moe: bool) -> float:
    return round(weights_gb * (1.1 if moe else 1.2), 1)


def _pick(
    tag: str,
    catalog_id: str,
    quant: str,
    weights_gb: float,
    *,
    moe: bool = False,
    vision: bool = False,
    min_cc: tuple[int, int] | None = None,
) -> LocalModelPick:
    return LocalModelPick(
        tag=tag,
        catalog_id=catalog_id,
        quant=quant,
        weights_gb=weights_gb,
        runtime_gb=_runtime_gb(weights_gb, moe),
        moe=moe,
        vision=vision,
        min_compute_capability=min_cc,
    )


# Sizes: registry.ollama.ai manifest layer bytes / 1e9, probed 2026-09-02 by
# scripts/verify_local_model_tags.py. Quant: the digest of each default tag was
# matched against its explicit-quant alias the same day (moondream:latest ==
# 1.8b-v2-q4_0, nomic-embed-text:latest == 137m-v1.5-fp16, the rest q4_K_M);
# gpt-oss ships MXFP4 natively.
GEMMA3_1B = _pick("gemma3:1b", "gemma3-1b", "Q4_K_M", 0.8)
GEMMA3_4B = _pick("gemma3:4b", "gemma3-4b", "Q4_K_M", 3.3, vision=True)
QWEN3_1_7B = _pick("qwen3:1.7b", "qwen3-1.7b", "Q4_K_M", 1.4)
QWEN3_4B = _pick("qwen3:4b", "qwen3-4b", "Q4_K_M", 2.5)
QWEN3_8B = _pick("qwen3:8b", "qwen3-8b", "Q4_K_M", 5.2)
QWEN3_14B = _pick("qwen3:14b", "qwen3-14b", "Q4_K_M", 9.3)
QWEN3_30B_A3B = _pick("qwen3:30b-a3b", "qwen3-30b-a3b", "Q4_K_M", 18.6, moe=True)
QWEN3_CODER_30B = _pick("qwen3-coder:30b", "qwen3-coder-30b", "Q4_K_M", 18.6, moe=True)
GPT_OSS_20B = _pick("gpt-oss:20b", "gpt-oss-20b", "MXFP4", 13.8, moe=True)
GPT_OSS_120B = _pick("gpt-oss:120b", "gpt-oss-120b", "MXFP4", 65.4, moe=True)
# Nemotron 3 Nano Omni: 30B MoE (~3.5B active), image input, tools, 128K ctx.
NEMOTRON3_33B = _pick("nemotron3:33b", "nemotron3-33b", "Q4_K_M", 27.6, moe=True, vision=True)
NEMOTRON3_33B_Q8 = _pick(
    "nemotron3:33b-q8", "nemotron3-33b-q8", "Q8_0", 36.5, moe=True, vision=True
)
# Untagged names stay untagged: existing installs and capabilities.yaml carry
# "llama3.2-vision" / "moondream" / "nomic-embed-text" as :latest already.
MOONDREAM = _pick("moondream", "moondream", "Q4_0", 1.7, vision=True)
QWEN3_VL_8B = _pick("qwen3-vl:8b", "qwen3-vl-8b", "Q4_K_M", 6.1, vision=True)
LLAMA32_VISION = _pick(
    "llama3.2-vision", "llama32-vision", "Q4_K_M", 7.8, vision=True, min_cc=(8, 0)
)
NOMIC_EMBED = _pick("nomic-embed-text", "nomic-embed-text", "F16", 0.3)


def _tier(
    min_gb: float,
    max_gb: float | None,
    label: str,
    num_ctx: int,
    num_parallel: int,
    default_quant: str,
    *,
    chat: LocalModelPick,
    code: LocalModelPick,
    vision: LocalModelPick,
    reasoning: LocalModelPick,
    embed: LocalModelPick,
    cpu_fallback: LocalModelPick,
    notes: str = "",
) -> LocalModelTier:
    return LocalModelTier(
        min_gb=min_gb,
        max_gb=max_gb,
        label=label,
        num_ctx=num_ctx,
        num_parallel=num_parallel,
        default_quant=default_quant,
        picks={
            "chat": chat,
            "code": code,
            "vision": vision,
            "reasoning": reasoning,
            "embed": embed,
            "cpu_fallback": cpu_fallback,
        },
        notes=notes,
    )


# Invariants (tested): every pick's runtime_gb fits at the *bottom* of its
# tier (the 0-4 CPU tier fits in 4 GB of RAM instead). num_ctx / num_parallel
# / default_quant are the one ladder -- ctx >=96 -> 131072, >=48 -> 65536,
# >=24 -> 32768, >=16 -> 16384, >=12 -> 8192, else 4096; parallel >=48 -> 4,
# >=24 -> 2, else 1 -- and gpu.get_ollama_optimizations reads it through
# num_ctx_for / num_parallel_for / quant_for, so the two agree at every
# budget, driver-reported sizes included: tier_for() snaps by TIER_SNAP_GB
# (0.5) for both, so a 4090's 23.99 GB is the 24 GB tier (32768 ctx, 2
# parallel) and an H100's 79.65 GB the 80 GB tier on either side. The one
# thing gpu.py adds is detection: when NVML reports no compute capability it
# reads one off the GPU name before building the TierBudget, which this
# module never does -- quant_for on a bare tier_budget() of an H100 with an
# unknown capability is Q4_K_M, gpu.py's is the Hopper tier.
# tests/test_local_models.py pins the parity and that one difference.
LOCAL_MODEL_TIERS: tuple[LocalModelTier, ...] = (
    _tier(
        0, 4, "cpu", 4096, 1, "Q4_K_M",
        chat=GEMMA3_1B, code=QWEN3_1_7B, vision=MOONDREAM, reasoning=QWEN3_1_7B,
        embed=NOMIC_EMBED, cpu_fallback=GEMMA3_1B,
        notes="No GPU or under 4 GB: models run on the CPU from system RAM, so keep them tiny.",
    ),
    _tier(
        4, 8, "mini", 4096, 1, "Q4_K_M",
        chat=GEMMA3_4B, code=QWEN3_4B, vision=MOONDREAM, reasoning=QWEN3_4B,
        embed=NOMIC_EMBED, cpu_fallback=GEMMA3_1B,
        notes="Entry cards (RTX 3050 / laptop 4-6 GB): gemma3:4b sees images too.",
    ),
    _tier(
        8, 12, "small", 4096, 1, "Q4_K_M",
        chat=QWEN3_8B, code=QWEN3_8B, vision=MOONDREAM, reasoning=QWEN3_8B,
        embed=NOMIC_EMBED, cpu_fallback=GEMMA3_4B,
        notes="8-10 GB cards: one 8B model at a time; moondream stays for screenshots.",
    ),
    _tier(
        12, 16, "small-plus", 8192, 1, "Q4_K_M",
        chat=QWEN3_8B, code=QWEN3_8B, vision=QWEN3_VL_8B, reasoning=QWEN3_8B,
        embed=NOMIC_EMBED, cpu_fallback=GEMMA3_4B,
        notes="12 GB cards (RTX 3060 / 4070): room for a real vision model next to the 8B.",
    ),
    _tier(
        16, 24, "medium", 16384, 1, "Q4_K_M",
        chat=QWEN3_14B, code=QWEN3_14B, vision=LLAMA32_VISION, reasoning=GPT_OSS_20B,
        embed=NOMIC_EMBED, cpu_fallback=GEMMA3_4B,
        notes="16 GB cards and 24 GB unified laptops: first tier where a MoE (gpt-oss:20b) fits.",
    ),
    _tier(
        24, 40, "large", 32768, 2, "Q4_K_M",
        chat=QWEN3_30B_A3B, code=QWEN3_CODER_30B, vision=LLAMA32_VISION, reasoning=GPT_OSS_20B,
        embed=NOMIC_EMBED, cpu_fallback=GEMMA3_4B,
        notes="RTX 3090 / 4090 / 5090 / A10: 30B-class MoE chat and coder, two requests in parallel.",
    ),
    _tier(
        40, 48, "xl", 32768, 2, "Q4_K_M",
        chat=NEMOTRON3_33B, code=QWEN3_CODER_30B, vision=LLAMA32_VISION, reasoning=GPT_OSS_20B,
        embed=NOMIC_EMBED, cpu_fallback=GEMMA3_4B,
        notes="A100 40 GB / L40S 48 GB: Nemotron 3 Nano Omni (MoE, image input) leads.",
    ),
    _tier(
        48, 80, "workstation", 65536, 4, "Q4_K_M",
        chat=NEMOTRON3_33B_Q8, code=QWEN3_CODER_30B, vision=LLAMA32_VISION, reasoning=GPT_OSS_20B,
        embed=NOMIC_EMBED, cpu_fallback=GEMMA3_4B,
        notes="RTX 6000 Ada / dual 24 GB / 64 GB unified: Nemotron 3 at Q8 with 64K context.",
    ),
    _tier(
        80, 96, "datacenter", 65536, 4, "Q8_0 or F16",
        chat=NEMOTRON3_33B_Q8, code=QWEN3_CODER_30B, vision=LLAMA32_VISION, reasoning=GPT_OSS_120B,
        embed=NOMIC_EMBED, cpu_fallback=GEMMA3_4B,
        notes="A100 / H100 80 GB: gpt-oss:120b fits; Hopper+ HBM can afford Q8_0 or F16.",
    ),
    _tier(
        96, None, "max", 131072, 4, "Q8_0 or F16",
        chat=NEMOTRON3_33B_Q8, code=QWEN3_CODER_30B, vision=LLAMA32_VISION, reasoning=GPT_OSS_120B,
        embed=NOMIC_EMBED, cpu_fallback=GEMMA3_4B,
        notes=(
            "RTX PRO 6000 96 GB, DGX Spark 128 GB unified, multi-GPU: full 128K context; "
            "on a unified pool MoE models lead because 273 GB/s starves dense 70B+ weights."
        ),
    ),
)


# --- budget maths ------------------------------------------------------------


def _as_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _vram_mb(gpu: Any) -> float:
    """``vram_mb`` of a GPUInfo-like object, falling back to ``vram_gb``."""
    mb = getattr(gpu, "vram_mb", None)
    if mb is not None:
        return max(_as_float(mb), 0.0)
    return max(_as_float(getattr(gpu, "vram_gb", 0.0)) * 1024, 0.0)


def _compute_capability(gpu: Any) -> tuple[int, int]:
    cc = getattr(gpu, "compute_capability", None) if gpu is not None else None
    try:
        major, minor = cc  # type: ignore[misc]
        return (int(major), int(minor))
    except (TypeError, ValueError):
        return _UNKNOWN_CC


def _effective_ram_gb(sys_mem: Any) -> float:
    """``effective_for_llm_gb`` of a SystemMemoryInfo-like object (or ``available_ram_gb``)."""
    if sys_mem is None:
        return 0.0
    if isinstance(sys_mem, int | float):
        return max(float(sys_mem), 0.0)
    for attr in ("effective_for_llm_gb", "available_ram_gb"):
        value = getattr(sys_mem, attr, None)
        if value is not None:
            return max(_as_float(value), 0.0)
    return 0.0


def unified_os_reserve_gb(total_gb: float) -> float:
    """GB a unified pool of ``total_gb`` keeps for the OS, WebUI and desktop.

    ``min(UNIFIED_OS_RESERVE_MAX_GB, max(UNIFIED_OS_RESERVE_MIN_GB,
    round(total_gb * UNIFIED_OS_RESERVE_FRACTION)))`` -- an eighth of the pool,
    never under 4 GB, never over the GB10's 16 GB: 8-32 GB pools reserve 4,
    64 GB reserves 8, 96 GB reserves 12, 128 GB and up the full 16 (the curve
    is tabulated above the constants). :func:`tier_budget` subtracts it from
    every unified pool; install.sh reproduces it from the three
    ``NVH_UNIFIED_OS_RESERVE_*`` values :func:`tier_table_shell` exports.
    """
    total = max(_as_float(total_gb), 0.0)
    return float(
        min(UNIFIED_OS_RESERVE_MAX_GB, max(UNIFIED_OS_RESERVE_MIN_GB, round(total * UNIFIED_OS_RESERVE_FRACTION)))
    )


def tier_budget(
    gpus: Iterable[Any] | None,
    sys_mem: Any = None,
    *,
    bandwidth_gbps: float | None = None,
) -> TierBudget:
    """Memory budget for a list of GPUInfo-like rows -- the maths of ``gpu._memory_budget``.

    Rows are read through ``getattr`` (``vram_mb`` or ``vram_gb``,
    ``unified_memory``, ``compute_capability``) so ``nvh.utils.gpu.GPUInfo``,
    emulated GPUs and plain namespaces all work. Only rows with a readable pool
    (``vram_mb > 0``) count towards the budget, and the first sized row decides
    the memory model, exactly as ``gpu._primary_row`` does; every row counts
    towards ``total_gpus``, so a card whose memory could not be read is still a
    detected card. A unified pool loses :func:`unified_os_reserve_gb` of its
    total and gets no offload bonus. ``sys_mem`` is read via
    ``effective_for_llm_gb`` (or ``available_ram_gb``); ``None`` means no
    offload bonus. ``bandwidth_gbps`` lets a caller that knows the part's
    memory bandwidth feed :func:`moe_first`; a unified pool defaults to
    :data:`UNIFIED_MEMORY_BANDWIDTH_GBPS`.
    """
    rows = list(gpus or [])
    sized = [g for g in rows if _vram_mb(g) > 0]
    total_gb = sum(_vram_mb(g) for g in sized) / 1024
    primary = sized[0] if sized else (rows[0] if rows else None)
    cc = _compute_capability(primary)
    unified = bool(sized) and bool(getattr(sized[0], "unified_memory", False))
    if unified:
        bandwidth = float(UNIFIED_MEMORY_BANDWIDTH_GBPS) if bandwidth_gbps is None else float(bandwidth_gbps)
        return TierBudget(
            budget_gb=max(total_gb - unified_os_reserve_gb(total_gb), 0.0),
            offload_gb=0.0,
            unified=True,
            bandwidth_gbps=bandwidth,
            compute_capability=cc,
            total_gb=total_gb,
            sized_gpus=len(sized),
            total_gpus=len(rows),
        )
    return TierBudget(
        budget_gb=total_gb,
        offload_gb=min(_effective_ram_gb(sys_mem), CPU_OFFLOAD_CAP_GB),
        unified=False,
        bandwidth_gbps=None if bandwidth_gbps is None else float(bandwidth_gbps),
        compute_capability=cc,
        total_gb=total_gb,
        sized_gpus=len(sized),
        total_gpus=len(rows),
    )


def _budget_gb(budget: TierBudget | float) -> float:
    return budget.budget_gb if isinstance(budget, TierBudget) else float(budget)


# --- accessors ---------------------------------------------------------------


def tier_for(budget: TierBudget | float) -> LocalModelTier:
    """The tier a budget (or a bare GB figure) lands in, after :data:`TIER_SNAP_GB`."""
    gb = _budget_gb(budget) + TIER_SNAP_GB
    for tier in reversed(LOCAL_MODEL_TIERS):
        if gb >= tier.min_gb:
            return tier
    return LOCAL_MODEL_TIERS[0]


def moe_first(budget: TierBudget | float) -> bool:
    """True when the pool is bandwidth-bound (unified, or below the MoE threshold)."""
    if not isinstance(budget, TierBudget):
        return False
    if budget.unified:
        return True
    return budget.bandwidth_gbps is not None and budget.bandwidth_gbps < MOE_BANDWIDTH_THRESHOLD_GBPS


def _compute_ok(candidate: LocalModelPick, cc: tuple[int, int]) -> bool:
    """A pick with a compute floor is refused only when the capability is *known* to be lower."""
    if candidate.min_compute_capability is None or cc == _UNKNOWN_CC:
        return True
    return cc >= candidate.min_compute_capability


def pick(budget: TierBudget | float, use_case: str) -> LocalModelPick | None:
    """The tier's pick for ``use_case``, falling down the ladder when a floor is not met.

    llama3.2-vision needs compute capability 8.0 (BF16); on a Turing card with
    24 GB the vision pick therefore comes from the 12-16 tier (qwen3-vl:8b),
    the same swap ``gpu._recommend_vision_model`` made by hand.
    """
    if use_case not in USE_CASES:
        raise ValueError(f"unknown use case {use_case!r}; expected one of {USE_CASES}")
    cc = budget.compute_capability if isinstance(budget, TierBudget) else _UNKNOWN_CC
    start = LOCAL_MODEL_TIERS.index(tier_for(budget))
    for tier in reversed(LOCAL_MODEL_TIERS[: start + 1]):
        candidate = tier.picks.get(use_case)
        if candidate is not None and _compute_ok(candidate, cc):
            return candidate
    return None


def recommended(budget: TierBudget | float) -> list[LocalModelPick]:
    """The pull list for a budget: chat, code, vision, embed, cpu_fallback (unique by tag).

    On a MoE-first pool (:func:`moe_first`) the tier's MoE reasoning pick joins
    the list and MoE picks move to the front, so index 0 is a MoE model
    wherever one fits -- the DGX Spark guidance in docs/proposals.
    """
    order = list(RECOMMENDED_ORDER)
    prefer_moe = moe_first(budget)
    if prefer_moe:
        reasoning = pick(budget, "reasoning")
        if reasoning is not None and reasoning.moe:
            order.insert(2, "reasoning")
    picks: list[LocalModelPick] = []
    seen: set[str] = set()
    for use_case in order:
        candidate = pick(budget, use_case)
        if candidate is not None and candidate.tag not in seen:
            seen.add(candidate.tag)
            picks.append(candidate)
    if prefer_moe:
        picks = [p for p in picks if p.moe] + [p for p in picks if not p.moe]
    return picks


def num_ctx_for(budget: TierBudget | float) -> int:
    """``num_ctx`` for the budget's tier; 2048 only when no GPU was seen at all.

    ``gpu.get_ollama_optimizations`` returns 2048 for an *empty* GPU list and
    the 0-4 tier's 4096 for a GPU whose memory could not be read (a 0 GB
    memory-unavailable row), so this keys on ``total_gpus``, not
    ``sized_gpus``: the unreadable card is still a card.
    """
    if isinstance(budget, TierBudget) and budget.total_gpus == 0:
        return CPU_ONLY_NUM_CTX
    return tier_for(budget).num_ctx


def num_parallel_for(budget: TierBudget | float) -> int:
    return tier_for(budget).num_parallel


def quant_for(budget: TierBudget | float) -> str:
    """Quant ladder of ``gpu.get_ollama_optimizations``: the pool decides before the SMs do.

    A unified LPDDR5x pool is bandwidth-bound whatever its compute capability,
    so it never inherits the Hopper "Q8_0 or F16" tier; Hopper+ (CC >= 9.0)
    with an 80 GB+ budget can afford it; everything else is Q4_K_M.
    """
    if not isinstance(budget, TierBudget):
        return tier_for(budget).default_quant
    if budget.unified:
        return "Q4_K_M"
    if budget.compute_capability >= (9, 0) and tier_for(budget).min_gb >= 80:
        return "Q8_0 or F16"
    return "Q4_K_M"


def reason_for(budget: TierBudget | float, model: LocalModelPick) -> str:
    """Prose for a pick, generated from the same row so tag and reason cannot disagree."""
    tier = tier_for(budget)
    gb = _budget_gb(budget)
    kind = "MoE" if model.moe else "dense"
    parts = [
        f"{model.tag} — {tier.label} tier ({gb:.0f} GB budget): {kind} {model.quant}, "
        f"~{model.weights_gb:g} GB on disk, ~{model.runtime_gb:g} GB loaded at {tier.num_ctx} ctx"
    ]
    if model.vision:
        parts.append("vision-capable")
    if isinstance(budget, TierBudget):
        if budget.total_gpus == 0:
            parts.append("no GPU detected — runs on the CPU from system RAM (slow but functional)")
        elif budget.gpu_memory_unreadable:
            parts.append(
                "GPU detected but its memory could not be read — sized as if no VRAM were "
                "available; runs from system RAM until the driver reports the pool"
            )
        if budget.unified:
            parts.append(
                f"unified memory: ~{gb:.0f} GB of {budget.total_gb:.0f} GB usable after the "
                f"{budget.os_reserve_gb:.0f} GB OS reserve; system RAM is not an extra "
                "CPU-offload pool here"
            )
        if moe_first(budget):
            bandwidth = budget.bandwidth_gbps or UNIFIED_MEMORY_BANDWIDTH_GBPS
            if model.moe:
                parts.append(
                    f"MoE preferred on a ~{bandwidth:.0f} GB/s pool — a few billion active "
                    "parameters per token keep it fast where dense models are bandwidth-bound"
                )
            else:
                parts.append(
                    f"dense — bandwidth-bound at ~{bandwidth:.0f} GB/s; a MoE pick is preferred "
                    "where one fits"
                )
        elif budget.offload_gb > 0 and budget.sized_gpus > 0:
            parts.append(
                f"{budget.offload_gb:.0f} GB of system RAM is available for CPU offload if a "
                "larger model is tried"
            )
        if budget.sized_gpus > 1:
            parts.append(f"Ollama will use all {budget.sized_gpus} GPUs automatically")
    if model.min_compute_capability is not None:
        major, minor = model.min_compute_capability
        parts.append(f"needs compute capability {major}.{minor}+ (BF16 kernels)")
    return "; ".join(parts)


def ordered_picks(budget: TierBudget | float | None, *use_cases: str) -> list[LocalModelPick]:
    """Distinct picks for ``use_cases`` the budget can hold, strongest first.

    Walks every tier up to the budget's (``None`` means the whole table -- the
    "which installed model is strongest" ranking local_chat and
    ollama_provider keep), takes the named columns (every :data:`USE_CASES`
    column when none is given), drops a pick whose compute floor a *known*
    capability fails, dedupes by tag and sorts by ``runtime_gb`` descending
    (ties by tag). On a MoE-first budget (:func:`moe_first`) the MoE picks
    lead, each group still largest first.
    """
    columns = use_cases or USE_CASES
    unknown = [u for u in columns if u not in USE_CASES]
    if unknown:
        raise ValueError(f"unknown use case(s) {unknown!r}; expected one of {USE_CASES}")
    if budget is None:
        tiers: tuple[LocalModelTier, ...] = LOCAL_MODEL_TIERS
        cc = _UNKNOWN_CC
    else:
        tiers = LOCAL_MODEL_TIERS[: LOCAL_MODEL_TIERS.index(tier_for(budget)) + 1]
        cc = budget.compute_capability if isinstance(budget, TierBudget) else _UNKNOWN_CC
    seen: dict[str, LocalModelPick] = {}
    for tier in tiers:
        for use_case in columns:
            candidate = tier.picks.get(use_case)
            if candidate is not None and _compute_ok(candidate, cc):
                seen.setdefault(candidate.tag, candidate)
    ordered = sorted(seen.values(), key=lambda p: (-p.runtime_gb, p.tag))
    if moe_first(budget):
        ordered = [p for p in ordered if p.moe] + [p for p in ordered if not p.moe]
    return ordered


def vision_picks() -> list[LocalModelPick]:
    """The dedicated vision models -- every tier's ``vision`` column -- largest first.

    Not every pick that *sees images*: gemma3:4b and the nemotron3 builds are
    vision-capable chat picks and stay out of this list (filter
    :func:`all_picks` on ``.vision`` for those). This is what the setup
    wizard reorders vision-first and what the Wizard treats as a vision model.
    """
    return ordered_picks(None, "vision")


def all_picks() -> list[LocalModelPick]:
    """Every distinct pick in the table, in first-appearance order."""
    seen: dict[str, LocalModelPick] = {}
    for tier in LOCAL_MODEL_TIERS:
        for use_case in USE_CASES:
            candidate = tier.picks.get(use_case)
            if candidate is not None and candidate.tag not in seen:
                seen[candidate.tag] = candidate
    return list(seen.values())


def all_tags() -> list[str]:
    """Every tag in the table, sorted."""
    return sorted(p.tag for p in all_picks())


def pick_for_tag(tag: str) -> LocalModelPick | None:
    """Look a pick up by tag (``"qwen3:8b"``); ``"name"`` also matches ``"name:latest"``."""
    wanted = tag if ":" in tag else f"{tag}:latest"
    for candidate in all_picks():
        have = candidate.tag if ":" in candidate.tag else f"{candidate.tag}:latest"
        if have == wanted:
            return candidate
    return None


def size_table() -> dict[str, float]:
    """``{tag: runtime_gb}`` -- the replacement for the three ad-hoc size dicts."""
    return {p.tag: p.runtime_gb for p in all_picks()}


# --- renderers ---------------------------------------------------------------


def _fmt_gb(value: float) -> str:
    return f"{value:g}"


def tier_table_markdown() -> str:
    """Two Markdown tables for docs/MODELS.md: the tier ladder and the tag sizes."""
    lines = [
        "| Budget (GB) | Tier | num_ctx | parallel | quant | chat | code | vision | reasoning | embed | CPU fallback |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for tier in LOCAL_MODEL_TIERS:
        cells = [
            tier.range_label,
            tier.label,
            str(tier.num_ctx),
            str(tier.num_parallel),
            tier.default_quant,
            *(f"`{tier.picks[u].tag}`" for u in USE_CASES),
        ]
        lines.append("| " + " | ".join(cells) + " |")
    lines += [
        "",
        "| Tag | Catalog id | Quant | On disk (GB) | Loaded (GB) | MoE | Vision | Min CC |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for p in sorted(all_picks(), key=lambda p: p.tag):
        cc = "" if p.min_compute_capability is None else ".".join(map(str, p.min_compute_capability))
        cells = [
            f"`{p.tag}`", p.catalog_id, p.quant, _fmt_gb(p.weights_gb), _fmt_gb(p.runtime_gb),
            "yes" if p.moe else "", "yes" if p.vision else "", cc,
        ]
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines) + "\n"


def tier_table_shell() -> str:
    """``NVH_TIER_<n>_*`` assignments for install.sh (POSIX sh; values double-quoted).

    The header carries the budget maths install.sh needs so it types none of
    them: ``NVH_UNIFIED_OS_RESERVE_GB`` (the GB10 figure, kept for older
    installers), the reserve curve as ``NVH_UNIFIED_OS_RESERVE_MIN_GB`` /
    ``_MAX_GB`` / ``_FRACTION`` (see :func:`unified_os_reserve_gb`; the
    fraction is a quoted decimal because the installer accepts only integers
    or double-quoted strings) and the tier snap as ``NVH_TIER_SNAP_MB``
    (:data:`TIER_SNAP_GB` in MiB, added before the integer divide).
    ``MAX`` of the open-ended tier is 999999 so ``[ "$VRAM_GB" -lt "$MAX" ]``
    works without a special case. Pure ASCII: the text is sourced by a shell.
    """
    lines = [
        "# Generated by nvh.core.local_models.tier_table_shell() - do not edit by hand.",
        f"NVH_TIER_COUNT={len(LOCAL_MODEL_TIERS)}",
        f"NVH_UNIFIED_OS_RESERVE_GB={int(UNIFIED_MEMORY_OS_RESERVE_GB)}",
        f"NVH_UNIFIED_OS_RESERVE_MIN_GB={int(UNIFIED_OS_RESERVE_MIN_GB)}",
        f"NVH_UNIFIED_OS_RESERVE_MAX_GB={int(UNIFIED_OS_RESERVE_MAX_GB)}",
        f'NVH_UNIFIED_OS_RESERVE_FRACTION="{UNIFIED_OS_RESERVE_FRACTION:g}"',
        f"NVH_TIER_SNAP_MB={int(TIER_SNAP_GB * 1024)}",
    ]
    for n, tier in enumerate(LOCAL_MODEL_TIERS):
        max_gb = _SHELL_OPEN_MAX if tier.max_gb is None else int(tier.max_gb)
        lines += [
            f"NVH_TIER_{n}_MIN={int(tier.min_gb)}",
            f"NVH_TIER_{n}_MAX={max_gb}",
            f'NVH_TIER_{n}_LABEL="{tier.label}"',
            f"NVH_TIER_{n}_CTX={tier.num_ctx}",
            f"NVH_TIER_{n}_PARALLEL={tier.num_parallel}",
        ]
        for use_case in USE_CASES:
            lines.append(f'NVH_TIER_{n}_{use_case.upper()}="{tier.picks[use_case].tag}"')
    return "\n".join(lines) + "\n"
