"""nvh.core.local_models -- the one VRAM-tier -> local-model table.

Covers the budget maths (parity with ``gpu._memory_budget``), tier boundaries
(with the driver under-report snap), pick fallbacks (Turing vision), the
``num_ctx`` / ``num_parallel`` / quant ladder against ``gpu.get_ollama_optimizations``
live, the generated reasons, the table invariants, the two renderers as
snapshots, and the registry verifier offline. The last test probes
registry.ollama.ai for real and only runs under ``NVH_NETWORK_TESTS=1``.
"""

from __future__ import annotations

import ast
import importlib.util
import os
import re
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from nvh.core import local_models as lm
from nvh.utils import gpu
from nvh.utils.gpu import GPUInfo, SystemMemoryInfo

ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "nvh" / "core" / "local_models.py"
VERIFIER = ROOT / "scripts" / "verify_local_model_tags.py"

SPARK_RAM = SystemMemoryInfo(total_ram_gb=128.0, available_ram_gb=90.0, effective_for_llm_gb=63.0)
X86_RAM = SystemMemoryInfo(total_ram_gb=64.0, available_ram_gb=48.0, effective_for_llm_gb=33.6)
GiB_MB = 1024


def _gpu(
    name: str,
    vram_gb: float,
    *,
    unified: bool = False,
    cc: tuple[int, int] = (0, 0),
    index: int = 0,
) -> GPUInfo:
    vram_mb = int(vram_gb * GiB_MB)
    return GPUInfo(
        name=name,
        vram_mb=vram_mb,
        vram_gb=round(vram_mb / 1024, 2),
        driver_version="580.65",
        cuda_version="13.0",
        utilization_pct=0,
        memory_used_mb=0,
        memory_free_mb=vram_mb,
        index=index,
        compute_capability=cc,
        unified_memory=unified,
    )


def GB10() -> list[GPUInfo]:  # noqa: N802 - reads like the part number
    return [_gpu("NVIDIA GB10", 128, unified=True, cc=(12, 1))]


def RTX4090() -> list[GPUInfo]:  # noqa: N802
    return [_gpu("NVIDIA GeForce RTX 4090", 24, cc=(8, 9))]


def A100x2() -> list[GPUInfo]:  # noqa: N802
    return [
        _gpu("NVIDIA A100 80GB PCIe", 80, cc=(8, 0), index=0),
        _gpu("NVIDIA A100 80GB PCIe", 80, cc=(8, 0), index=1),
    ]


@pytest.fixture(scope="module")
def verifier():
    # Registered in sys.modules before exec: the script's @dataclass resolves its
    # postponed annotations through sys.modules[cls.__module__].
    spec = importlib.util.spec_from_file_location("verify_local_model_tags", VERIFIER)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
        yield module
    finally:
        sys.modules.pop(spec.name, None)


# ---------------------------------------------------------------------------
# Budget maths
# ---------------------------------------------------------------------------


def test_gb10_128gb_unified_budget():
    budget = lm.tier_budget(GB10(), SPARK_RAM)
    assert (budget.budget_gb, budget.offload_gb, budget.unified) == (112.0, 0.0, True)
    assert budget.total_gb == 128.0 and budget.sized_gpus == 1
    assert budget.bandwidth_gbps == 273.0 and budget.compute_capability == (12, 1)
    assert budget.combined_gb == 112.0
    assert lm.moe_first(budget) is True
    assert lm.tier_for(budget).label == "max"


def test_rtx4090_24gb_plus_64gb_ram_budget():
    budget = lm.tier_budget(RTX4090(), X86_RAM)
    assert (budget.budget_gb, budget.offload_gb, budget.unified) == (24.0, 16.0, False)
    assert budget.combined_gb == 40.0 and budget.bandwidth_gbps is None
    assert budget.compute_capability == (8, 9)
    assert lm.moe_first(budget) is False
    assert lm.tier_for(budget).label == "large"


def test_two_a100_budget_sums_sized_rows():
    budget = lm.tier_budget(A100x2(), X86_RAM)
    assert (budget.budget_gb, budget.offload_gb, budget.sized_gpus) == (160.0, 16.0, 2)
    assert budget.unified is False and budget.compute_capability == (8, 0)
    assert lm.tier_for(budget).label == "max"
    assert lm.quant_for(budget) == "Q4_K_M"          # Ampere: no Hopper Q8 tier
    assert lm.moe_first(budget) is False


def test_no_gpu_budget_is_cpu_tier():
    budget = lm.tier_budget([], X86_RAM)
    assert (budget.budget_gb, budget.offload_gb, budget.sized_gpus) == (0.0, 16.0, 0)
    assert lm.tier_for(budget).label == "cpu"
    assert lm.num_ctx_for(budget) == lm.CPU_ONLY_NUM_CTX == 2048
    assert lm.num_parallel_for(budget) == 1
    assert lm.quant_for(budget) == "Q4_K_M"
    assert lm.tier_budget(None, None).budget_gb == 0.0


def test_unsized_row_never_decides_the_memory_model():
    # A 0 GB row (memory-unavailable) listed first must not make a GB10 look discrete.
    rows = [_gpu("NVIDIA Ghost", 0, cc=(8, 9)), *GB10()]
    budget = lm.tier_budget(rows, SPARK_RAM)
    assert (budget.unified, budget.budget_gb, budget.offload_gb) == (True, 112.0, 0.0)
    assert budget.compute_capability == (12, 1) and budget.sized_gpus == 1


def test_duck_typed_rows_and_memory():
    assert lm.tier_budget([SimpleNamespace(vram_gb=24.0)], None).budget_gb == 24.0
    assert lm.tier_budget([SimpleNamespace(vram_mb="24576")], None).budget_gb == 24.0
    assert lm.tier_budget([SimpleNamespace(vram_mb=None, vram_gb=8)], None).budget_gb == 8.0
    assert lm.tier_budget([SimpleNamespace(vram_mb=8192)], 8.0).offload_gb == 8.0
    assert lm.tier_budget([SimpleNamespace(vram_mb=8192)], SimpleNamespace(available_ram_gb=20.0)).offload_gb == 16.0
    assert lm.tier_budget([SimpleNamespace(vram_mb=8192)], SimpleNamespace(effective_for_llm_gb=-3)).offload_gb == 0.0
    assert lm.tier_budget([SimpleNamespace(vram_mb=8192)], None).compute_capability == (0, 0)
    assert lm.tier_budget([SimpleNamespace(vram_mb=8192, compute_capability="bogus")], None).compute_capability == (0, 0)


def test_offload_bonus_is_capped_at_16gb():
    assert lm.CPU_OFFLOAD_CAP_GB == 16.0
    assert lm.tier_budget(RTX4090(), SimpleNamespace(effective_for_llm_gb=100.0)).offload_gb == 16.0
    assert lm.tier_budget(RTX4090(), SimpleNamespace(effective_for_llm_gb=6.5)).offload_gb == 6.5


def test_bandwidth_override_drives_moe_first():
    slow = lm.tier_budget(RTX4090(), X86_RAM, bandwidth_gbps=273)
    fast = lm.tier_budget(RTX4090(), X86_RAM, bandwidth_gbps=1008)
    assert lm.moe_first(slow) is True and lm.moe_first(fast) is False
    unified = lm.tier_budget(GB10(), SPARK_RAM, bandwidth_gbps=300)
    assert unified.bandwidth_gbps == 300.0 and lm.moe_first(unified) is True
    assert lm.moe_first(24.0) is False               # a bare number has no pool type
    assert lm.MOE_BANDWIDTH_THRESHOLD_GBPS == 500


@pytest.mark.parametrize("gpus, sys_mem", [(GB10(), SPARK_RAM), (RTX4090(), X86_RAM), (A100x2(), X86_RAM), ([], X86_RAM)])
def test_budget_matches_gpu_memory_budget(gpus, sys_mem):
    ours = lm.tier_budget(gpus, sys_mem)
    theirs = gpu._memory_budget(gpus, sys_mem)
    assert ours.budget_gb == theirs.model_budget_gb
    assert ours.offload_gb == theirs.cpu_offload_gb
    assert ours.unified == theirs.unified_memory
    assert ours.total_gb == theirs.total_vram_gb
    assert ours.combined_gb == theirs.combined_gb


def test_shared_constants_match_gpu_py():
    # gpu.py re-exports these from this module; this is the tripwire.
    assert lm.UNIFIED_MEMORY_OS_RESERVE_GB == gpu.UNIFIED_MEMORY_OS_RESERVE_GB == 16.0
    assert lm.UNIFIED_MEMORY_BANDWIDTH_GBPS == gpu.UNIFIED_MEMORY_BANDWIDTH_GBPS == 273
    assert gpu.unified_os_reserve_gb is lm.unified_os_reserve_gb


def test_module_imports_nothing_from_nvh():
    tree = ast.parse(MODULE.read_text(encoding="utf-8"))
    offenders = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            offenders += [a.name for a in node.names if a.name.startswith("nvh")]
        elif isinstance(node, ast.ImportFrom) and (node.module or "").startswith("nvh"):
            offenders.append(node.module)
    assert offenders == [], f"local_models must stay import-cycle-free: {offenders}"


# ---------------------------------------------------------------------------
# Tier boundaries
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "budget_gb, label",
    [
        (0, "cpu"), (3.4, "cpu"),
        (3.6, "mini"),        # 0.5 GB snap: a "4 GB" card reporting 3.6 GB stays a 4 GB card
        (4, "mini"), (7.4, "mini"),
        (8, "small"), (11.4, "small"),
        (12, "small-plus"), (15.4, "small-plus"),
        (16, "medium"), (23.4, "medium"),
        (23.99, "large"),     # RTX 4090 reports 24564 MiB
        (24, "large"), (39.4, "large"),
        (39.6, "xl"),         # A100 40 GB reports 40536 MiB
        (40, "xl"), (47.4, "xl"),
        (48, "workstation"), (79.4, "workstation"),
        (79.65, "datacenter"),  # H100 80 GB reports 81559 MiB
        (80, "datacenter"), (95.4, "datacenter"),
        (96, "max"), (112, "max"), (160, "max"), (1000, "max"),
    ],
)
def test_tier_for_boundaries(budget_gb, label):
    assert lm.tier_for(budget_gb).label == label
    assert lm.TIER_SNAP_GB == 0.5


def test_tiers_are_contiguous_and_ordered():
    tiers = lm.LOCAL_MODEL_TIERS
    assert len(tiers) == 10
    assert tiers[0].min_gb == 0 and tiers[-1].max_gb is None
    for lower, upper in zip(tiers, tiers[1:]):
        assert lower.max_gb == upper.min_gb, (lower.label, upper.label)
    assert [t.range_label for t in tiers] == [
        "0-4", "4-8", "8-12", "12-16", "16-24", "24-40", "40-48", "48-80", "80-96", "96+",
    ]
    assert tiers[5].contains(24) and tiers[5].contains(39.9) and not tiers[5].contains(40)
    assert tiers[-1].contains(10_000)


# ---------------------------------------------------------------------------
# Picks and fallbacks
# ---------------------------------------------------------------------------


def test_every_tier_covers_every_use_case():
    for tier in lm.LOCAL_MODEL_TIERS:
        assert set(tier.picks) == set(lm.USE_CASES), tier.label
        for use_case in lm.USE_CASES:
            assert lm.pick(tier.min_gb, use_case) is tier.picks[use_case], (tier.label, use_case)


@pytest.mark.parametrize(
    "vram_gb, cc, expected",
    [
        (48, (7, 5), "qwen3-vl:8b"),        # Quadro RTX 8000: Turing, no BF16 -> fall a tier
        (24, (7, 5), "qwen3-vl:8b"),        # RTX Titan / Quadro RTX 6000
        (8, (7, 5), "moondream"),           # RTX 2070: small tier never asked for BF16
        (48, (0, 0), "llama3.2-vision"),    # unknown capability is not refused (gpu.py parity)
        (48, (8, 6), "llama3.2-vision"),
        (24, (8, 9), "llama3.2-vision"),
        (16, (8, 0), "llama3.2-vision"),
        (12, (8, 9), "qwen3-vl:8b"),
        (112, (12, 1), "llama3.2-vision"),
    ],
)
def test_vision_pick_honours_compute_floor(vram_gb, cc, expected):
    budget = lm.tier_budget([_gpu("card", vram_gb, cc=cc)], None)
    assert lm.pick(budget, "vision").tag == expected


def test_compute_floor_only_on_llama32_vision():
    gated = [p.tag for p in lm.all_picks() if p.min_compute_capability is not None]
    assert gated == ["llama3.2-vision"]
    assert lm.LLAMA32_VISION.min_compute_capability == (8, 0)


def test_pick_rejects_unknown_use_case_and_accepts_floats():
    with pytest.raises(ValueError):
        lm.pick(24.0, "music")
    assert lm.pick(24.0, "chat").tag == "qwen3:30b-a3b"
    assert lm.pick(0.0, "chat").tag == "gemma3:1b"
    assert lm.pick(0.0, "embed").tag == "nomic-embed-text"


def test_recommended_rtx4090_order():
    tags = [p.tag for p in lm.recommended(lm.tier_budget(RTX4090(), X86_RAM))]
    assert tags == ["qwen3:30b-a3b", "qwen3-coder:30b", "llama3.2-vision", "nomic-embed-text", "gemma3:4b"]


def test_recommended_gb10_is_moe_first_and_promotes_reasoning():
    picks = lm.recommended(lm.tier_budget(GB10(), SPARK_RAM))
    assert [p.tag for p in picks] == [
        "nemotron3:33b-q8", "qwen3-coder:30b", "gpt-oss:120b",
        "llama3.2-vision", "nomic-embed-text", "gemma3:4b",
    ]
    assert [p.moe for p in picks] == [True, True, True, False, False, False]


def test_recommended_unified_32gb_laptop_leads_with_the_moe_picks():
    # RTX Spark class: a 32 GB unified pool loses a 4 GB reserve (not the GB10's 16) -> 28 GB
    # budget, the large tier; every MoE the tier names leads, reasoning promoted, dense picks after.
    budget = lm.tier_budget([_gpu("NVIDIA GB10", 32, unified=True, cc=(12, 1))], None)
    assert (budget.os_reserve_gb, budget.budget_gb, lm.tier_for(budget).label) == (4.0, 28.0, "large")
    tags = [p.tag for p in lm.recommended(budget)]
    assert tags == [
        "qwen3:30b-a3b", "qwen3-coder:30b", "gpt-oss:20b", "llama3.2-vision", "nomic-embed-text", "gemma3:4b",
    ]


def test_recommended_unified_24gb_laptop_is_the_medium_tier():
    # 24 GB unified -> 4 GB reserve -> 20 GB budget: gpt-oss:20b is the only MoE that fits and leads.
    budget = lm.tier_budget([_gpu("NVIDIA GB10", 24, unified=True, cc=(12, 1))], None)
    assert (budget.os_reserve_gb, budget.budget_gb, lm.tier_for(budget).label) == (4.0, 20.0, "medium")
    tags = [p.tag for p in lm.recommended(budget)]
    assert tags == ["gpt-oss:20b", "qwen3:14b", "llama3.2-vision", "nomic-embed-text", "gemma3:4b"]


def test_recommended_discrete_16gb_leaves_reasoning_to_pick():
    budget = lm.tier_budget([_gpu("RTX 4060 Ti", 16, cc=(8, 9))], X86_RAM)
    tags = [p.tag for p in lm.recommended(budget)]
    assert tags == ["qwen3:14b", "llama3.2-vision", "nomic-embed-text", "gemma3:4b"]
    assert lm.pick(budget, "reasoning").tag == "gpt-oss:20b"


def test_recommended_dedupes_shared_tags():
    tags = [p.tag for p in lm.recommended(8.0)]
    assert tags == ["qwen3:8b", "moondream", "nomic-embed-text", "gemma3:4b"]
    assert len(tags) == len(set(tags))


def test_recommended_turing_swaps_vision():
    budget = lm.tier_budget([_gpu("Quadro RTX 8000", 48, cc=(7, 5))], X86_RAM)
    tags = [p.tag for p in lm.recommended(budget)]
    assert tags == ["nemotron3:33b-q8", "qwen3-coder:30b", "qwen3-vl:8b", "nomic-embed-text", "gemma3:4b"]


# ---------------------------------------------------------------------------
# num_ctx / num_parallel / quant ladder (gpu.get_ollama_optimizations parity)
# ---------------------------------------------------------------------------


# Today's gpu.py ladder: ctx >=96 -> 131072, >=48 -> 65536, >=24 -> 32768,
# >=16 -> 16384, >=12 -> 8192, else 4096; parallel >=48 -> 4, >=24 -> 2, else 1.
EXPECTED_CTX_PARALLEL = [
    (0, 4096, 1), (3.9, 4096, 1), (4, 4096, 1), (8, 4096, 1), (11, 4096, 1),
    (12, 8192, 1), (15, 8192, 1),
    (16, 16384, 1), (23, 16384, 1),
    (24, 32768, 2), (32, 32768, 2), (40, 32768, 2), (47, 32768, 2),
    (48, 65536, 4), (64, 65536, 4), (80, 65536, 4), (95, 65536, 4),
    (96, 131072, 4), (112, 131072, 4), (160, 131072, 4),
]


@pytest.mark.parametrize("budget_gb, ctx, parallel", EXPECTED_CTX_PARALLEL)
def test_num_ctx_and_parallel_ladder(budget_gb, ctx, parallel):
    assert lm.num_ctx_for(budget_gb) == ctx
    assert lm.num_parallel_for(budget_gb) == parallel
    tier = lm.tier_for(budget_gb)
    assert (tier.num_ctx, tier.num_parallel) == (ctx, parallel)


def test_ctx_and_parallel_never_decrease_up_the_ladder():
    ctx = [t.num_ctx for t in lm.LOCAL_MODEL_TIERS]
    par = [t.num_parallel for t in lm.LOCAL_MODEL_TIERS]
    assert ctx == sorted(ctx) and par == sorted(par)
    assert ctx == [4096, 4096, 4096, 8192, 16384, 32768, 32768, 65536, 65536, 131072]
    assert par == [1, 1, 1, 1, 1, 2, 2, 4, 4, 4]


LADDER_CASES = [
    # (gpus, sys_mem, expected (ctx, parallel, quant))
    ([], None, (2048, 1, "Q4_K_M")),
    ([_gpu("RTX 2060", 6, cc=(7, 5))], X86_RAM, (4096, 1, "Q4_K_M")),
    ([_gpu("RTX 3060 Ti", 8, cc=(8, 6))], X86_RAM, (4096, 1, "Q4_K_M")),
    ([_gpu("RTX 3060", 12, cc=(8, 6))], X86_RAM, (8192, 1, "Q4_K_M")),
    ([_gpu("RTX 4060 Ti", 16, cc=(8, 9))], X86_RAM, (16384, 1, "Q4_K_M")),
    (RTX4090(), X86_RAM, (32768, 2, "Q4_K_M")),
    ([_gpu("RTX 5090", 32, cc=(10, 0))], X86_RAM, (32768, 2, "Q4_K_M")),
    ([_gpu("L40S", 48, cc=(8, 9))], X86_RAM, (65536, 4, "Q4_K_M")),
    ([_gpu("H100 80GB", 80, cc=(9, 0))], X86_RAM, (65536, 4, "Q8_0 or F16")),
    ([_gpu("A100 80GB", 80, cc=(8, 0))], X86_RAM, (65536, 4, "Q4_K_M")),
    ([_gpu("RTX PRO 6000", 96, cc=(10, 0))], X86_RAM, (131072, 4, "Q8_0 or F16")),
    (GB10(), SPARK_RAM, (131072, 4, "Q4_K_M")),              # unified: never the Q8 tier
    ([_gpu("NVIDIA GB10", 64, unified=True, cc=(12, 1))], SPARK_RAM, (65536, 4, "Q4_K_M")),   # 8 GB reserve -> 56
    ([_gpu("NVIDIA GB10", 32, unified=True, cc=(12, 1))], SPARK_RAM, (32768, 2, "Q4_K_M")),   # 4 GB reserve -> 28
    ([_gpu("NVIDIA GB10", 24, unified=True, cc=(12, 1))], SPARK_RAM, (16384, 1, "Q4_K_M")),   # 4 GB reserve -> 20
    ([_gpu("NVIDIA GB10", 16, unified=True, cc=(12, 1))], SPARK_RAM, (8192, 1, "Q4_K_M")),    # 4 GB reserve -> 12
    (A100x2(), X86_RAM, (131072, 4, "Q4_K_M")),
]


@pytest.mark.parametrize("gpus, sys_mem, expected", LADDER_CASES)
def test_ladder_matches_gpu_py_live(gpus, sys_mem, expected):
    budget = lm.tier_budget(gpus, sys_mem)
    ours = (lm.num_ctx_for(budget), lm.num_parallel_for(budget), lm.quant_for(budget))
    assert ours == expected
    theirs = gpu.get_ollama_optimizations(gpus)
    assert ours == (theirs.recommended_ctx, theirs.num_parallel, theirs.recommended_quant)


def test_quant_ladder_details():
    assert lm.quant_for(lm.tier_budget([_gpu("H100", 79.65, cc=(9, 0))], None)) == "Q8_0 or F16"  # snap
    assert lm.quant_for(lm.tier_budget([_gpu("H100", 48, cc=(9, 0))], None)) == "Q4_K_M"
    assert lm.quant_for(80.0) == "Q8_0 or F16"       # bare number: the tier's default
    assert lm.quant_for(24.0) == "Q4_K_M"
    assert [t.default_quant for t in lm.LOCAL_MODEL_TIERS[-2:]] == ["Q8_0 or F16"] * 2
    assert all(t.default_quant == "Q4_K_M" for t in lm.LOCAL_MODEL_TIERS[:-2])


# ---------------------------------------------------------------------------
# reason_for
# ---------------------------------------------------------------------------


def test_reason_for_every_pick_starts_with_its_tag():
    for tier in lm.LOCAL_MODEL_TIERS:
        for use_case, model in tier.picks.items():
            reason = lm.reason_for(tier.min_gb, model)
            assert reason.startswith(f"{model.tag} — "), (tier.label, use_case, reason)
            assert tier.label in reason and str(tier.num_ctx) in reason


def test_reason_for_unified_names_reserve_and_moe():
    budget = lm.tier_budget(GB10(), SPARK_RAM)
    reason = lm.reason_for(budget, lm.pick(budget, "chat"))
    assert reason.startswith("nemotron3:33b-q8 — max tier (112 GB budget): MoE Q8_0")
    assert "~112 GB of 128 GB usable after the 16 GB OS reserve" in reason
    assert "MoE preferred on a ~273 GB/s pool" in reason
    assert "vision-capable" in reason
    dense = lm.reason_for(budget, lm.pick(budget, "vision"))
    assert dense.startswith("llama3.2-vision — ") and "bandwidth-bound at ~273 GB/s" in dense
    assert "needs compute capability 8.0+" in dense


def test_reason_for_discrete_multi_gpu_and_cpu():
    r4090 = lm.tier_budget(RTX4090(), X86_RAM)
    reason = lm.reason_for(r4090, lm.pick(r4090, "chat"))
    assert reason.startswith("qwen3:30b-a3b — large tier (24 GB budget): MoE Q4_K_M, ~18.6 GB on disk, ~20.5 GB loaded at 32768 ctx")
    assert "16 GB of system RAM is available for CPU offload" in reason
    assert "OS reserve" not in reason and "MoE preferred" not in reason

    a100 = lm.tier_budget(A100x2(), X86_RAM)
    assert "Ollama will use all 2 GPUs automatically" in lm.reason_for(a100, lm.pick(a100, "chat"))

    cpu = lm.tier_budget([], None)
    reason = lm.reason_for(cpu, lm.pick(cpu, "chat"))
    assert reason.startswith("gemma3:1b — cpu tier (0 GB budget)") and "no GPU detected" in reason
    assert "CPU offload" not in reason


# ---------------------------------------------------------------------------
# Table invariants
# ---------------------------------------------------------------------------

TAG_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*(:[a-z0-9][a-z0-9._-]*)?$")

RETIRED_NAMES = {
    "nemotron", "nemotron-mini", "nemotron-omni", "nemotron-3-nano-omni", "nemotron-3-super",
    "qwen2.5", "qwen2.5-coder", "minicpm-v", "llava", "bakllava", "llama3.1", "llama3.2",
    "llama3.3", "deepseek-r1", "codellama", "gemma2", "phi4", "mistral",
}


def test_no_tag_appears_twice_with_different_sizes():
    seen: dict[str, set[tuple[float, float, str, str]]] = {}
    for tier in lm.LOCAL_MODEL_TIERS:
        for model in tier.picks.values():
            seen.setdefault(model.tag, set()).add(
                (model.weights_gb, model.runtime_gb, model.quant, model.catalog_id)
            )
    conflicts = {tag: rows for tag, rows in seen.items() if len(rows) > 1}
    assert conflicts == {}
    assert len(lm.all_tags()) == len(seen) == 16


def test_tags_are_lowercase_and_registry_shaped():
    for tag in lm.all_tags():
        assert tag == tag.lower(), tag
        assert TAG_RE.match(tag), tag
        assert "/" not in tag and " " not in tag
    assert lm.all_tags() == sorted(set(lm.all_tags()))


def test_no_retired_or_phantom_names_in_the_table():
    names = {p.name for p in lm.all_picks()}
    assert names.isdisjoint(RETIRED_NAMES), names & RETIRED_NAMES
    assert lm.pick_for_tag("nemotron-omni") is None and lm.pick_for_tag("nemotron:70b") is None


def test_every_pick_fits_at_the_bottom_of_its_tier():
    for tier in lm.LOCAL_MODEL_TIERS:
        # The CPU tier runs from system RAM; 4 GB is the ceiling there.
        ceiling = tier.min_gb if tier.min_gb > 0 else tier.max_gb
        for use_case, model in tier.picks.items():
            assert model.runtime_gb <= ceiling, (tier.label, use_case, model.tag, model.runtime_gb)


def test_runtime_estimate_rule():
    for model in lm.all_picks():
        factor = 1.1 if model.moe else 1.2
        assert model.runtime_gb == round(model.weights_gb * factor, 1), model.tag
        assert model.runtime_gb >= model.weights_gb > 0


def test_slot_semantics():
    for tier in lm.LOCAL_MODEL_TIERS:
        assert tier.picks["vision"].vision is True, tier.label
        assert tier.picks["embed"].tag == "nomic-embed-text"
        assert tier.picks["cpu_fallback"].runtime_gb <= 4.0, tier.label
        assert tier.notes, tier.label
    # MoE-first from 24 GB up: the chat pick is a MoE model on every tier that can hold one.
    assert all(t.picks["chat"].moe for t in lm.LOCAL_MODEL_TIERS if t.min_gb >= 24)
    assert not any(t.picks["chat"].moe for t in lm.LOCAL_MODEL_TIERS if t.min_gb < 16)
    # Nemotron 3 Nano Omni leads from 40 GB; the Q8 build from 48 GB.
    assert [t.picks["chat"].tag for t in lm.LOCAL_MODEL_TIERS[6:]] == [
        "nemotron3:33b", "nemotron3:33b-q8", "nemotron3:33b-q8", "nemotron3:33b-q8",
    ]
    assert lm.pick(112.0, "reasoning").tag == "gpt-oss:120b" and lm.pick(48.0, "reasoning").tag == "gpt-oss:20b"


def test_catalog_ids_unique_and_derived_from_tags():
    ids = [p.catalog_id for p in lm.all_picks()]
    assert len(ids) == len(set(ids))
    assert lm.pick_for_tag("qwen3:8b").catalog_id == "qwen3-8b"          # matches nvhive-catalog.json
    assert lm.pick_for_tag("gemma3:4b").catalog_id == "gemma3-4b"
    assert lm.pick_for_tag("llama3.2-vision").catalog_id == "llama32-vision"  # matches studio_packs
    for model in lm.all_picks():
        assert re.match(r"^[a-z0-9][a-z0-9.-]*$", model.catalog_id), model.catalog_id


def test_pick_for_tag_and_size_table():
    assert lm.pick_for_tag("moondream") is lm.MOONDREAM
    assert lm.pick_for_tag("moondream:latest") is lm.MOONDREAM
    assert lm.pick_for_tag("nomic-embed-text:latest").quant == "F16"
    assert lm.pick_for_tag("does-not-exist") is None
    sizes = lm.size_table()
    assert sizes["gpt-oss:20b"] == 15.2 and sizes["gemma3:4b"] == 4.0 and sizes["nemotron3:33b"] == 30.4
    assert set(sizes) == set(lm.all_tags())
    assert lm.QWEN3_8B.name == "qwen3" and lm.QWEN3_8B.version == "8b"
    assert lm.MOONDREAM.version == "latest"


def test_picks_are_frozen():
    with pytest.raises(AttributeError):
        lm.QWEN3_8B.tag = "qwen3:9b"  # type: ignore[misc]
    with pytest.raises(AttributeError):
        lm.LOCAL_MODEL_TIERS[0].num_ctx = 1  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Renderers (stable snapshots)
# ---------------------------------------------------------------------------

MARKDOWN_SNAPSHOT = """\
| Budget (GB) | Tier | num_ctx | parallel | quant | chat | code | vision | reasoning | embed | CPU fallback |
|---|---|---|---|---|---|---|---|---|---|---|
| 0-4 | cpu | 4096 | 1 | Q4_K_M | `gemma3:1b` | `qwen3:1.7b` | `moondream` | `qwen3:1.7b` | `nomic-embed-text` | `gemma3:1b` |
| 4-8 | mini | 4096 | 1 | Q4_K_M | `gemma3:4b` | `qwen3:4b` | `moondream` | `qwen3:4b` | `nomic-embed-text` | `gemma3:1b` |
| 8-12 | small | 4096 | 1 | Q4_K_M | `qwen3:8b` | `qwen3:8b` | `moondream` | `qwen3:8b` | `nomic-embed-text` | `gemma3:4b` |
| 12-16 | small-plus | 8192 | 1 | Q4_K_M | `qwen3:8b` | `qwen3:8b` | `qwen3-vl:8b` | `qwen3:8b` | `nomic-embed-text` | `gemma3:4b` |
| 16-24 | medium | 16384 | 1 | Q4_K_M | `qwen3:14b` | `qwen3:14b` | `llama3.2-vision` | `gpt-oss:20b` | `nomic-embed-text` | `gemma3:4b` |
| 24-40 | large | 32768 | 2 | Q4_K_M | `qwen3:30b-a3b` | `qwen3-coder:30b` | `llama3.2-vision` | `gpt-oss:20b` | `nomic-embed-text` | `gemma3:4b` |
| 40-48 | xl | 32768 | 2 | Q4_K_M | `nemotron3:33b` | `qwen3-coder:30b` | `llama3.2-vision` | `gpt-oss:20b` | `nomic-embed-text` | `gemma3:4b` |
| 48-80 | workstation | 65536 | 4 | Q4_K_M | `nemotron3:33b-q8` | `qwen3-coder:30b` | `llama3.2-vision` | `gpt-oss:20b` | `nomic-embed-text` | `gemma3:4b` |
| 80-96 | datacenter | 65536 | 4 | Q8_0 or F16 | `nemotron3:33b-q8` | `qwen3-coder:30b` | `llama3.2-vision` | `gpt-oss:120b` | `nomic-embed-text` | `gemma3:4b` |
| 96+ | max | 131072 | 4 | Q8_0 or F16 | `nemotron3:33b-q8` | `qwen3-coder:30b` | `llama3.2-vision` | `gpt-oss:120b` | `nomic-embed-text` | `gemma3:4b` |

| Tag | Catalog id | Quant | On disk (GB) | Loaded (GB) | MoE | Vision | Min CC |
|---|---|---|---|---|---|---|---|
| `gemma3:1b` | gemma3-1b | Q4_K_M | 0.8 | 1 |  |  |  |
| `gemma3:4b` | gemma3-4b | Q4_K_M | 3.3 | 4 |  | yes |  |
| `gpt-oss:120b` | gpt-oss-120b | MXFP4 | 65.4 | 71.9 | yes |  |  |
| `gpt-oss:20b` | gpt-oss-20b | MXFP4 | 13.8 | 15.2 | yes |  |  |
| `llama3.2-vision` | llama32-vision | Q4_K_M | 7.8 | 9.4 |  | yes | 8.0 |
| `moondream` | moondream | Q4_0 | 1.7 | 2 |  | yes |  |
| `nemotron3:33b` | nemotron3-33b | Q4_K_M | 27.6 | 30.4 | yes | yes |  |
| `nemotron3:33b-q8` | nemotron3-33b-q8 | Q8_0 | 36.5 | 40.2 | yes | yes |  |
| `nomic-embed-text` | nomic-embed-text | F16 | 0.3 | 0.4 |  |  |  |
| `qwen3-coder:30b` | qwen3-coder-30b | Q4_K_M | 18.6 | 20.5 | yes |  |  |
| `qwen3-vl:8b` | qwen3-vl-8b | Q4_K_M | 6.1 | 7.3 |  | yes |  |
| `qwen3:1.7b` | qwen3-1.7b | Q4_K_M | 1.4 | 1.7 |  |  |  |
| `qwen3:14b` | qwen3-14b | Q4_K_M | 9.3 | 11.2 |  |  |  |
| `qwen3:30b-a3b` | qwen3-30b-a3b | Q4_K_M | 18.6 | 20.5 | yes |  |  |
| `qwen3:4b` | qwen3-4b | Q4_K_M | 2.5 | 3 |  |  |  |
| `qwen3:8b` | qwen3-8b | Q4_K_M | 5.2 | 6.2 |  |  |  |
"""

SHELL_HEADER_LINES = 7  # comment, TIER_COUNT, RESERVE_GB, RESERVE_MIN_GB, RESERVE_MAX_GB, RESERVE_FRACTION, SNAP_MB

# install.sh's defensive check on the sourced snippet: integers or double-quoted tags only.
INSTALL_SH_LINE_RE = re.compile(r'^(#.*|NVH_[A-Z0-9_]+=([0-9]+|"[A-Za-z0-9._:-]*")|)$')

SHELL_SNAPSHOT_HEAD = """\
# Generated by nvh.core.local_models.tier_table_shell() - do not edit by hand.
NVH_TIER_COUNT=10
NVH_UNIFIED_OS_RESERVE_GB=16
NVH_UNIFIED_OS_RESERVE_MIN_GB=4
NVH_UNIFIED_OS_RESERVE_MAX_GB=16
NVH_UNIFIED_OS_RESERVE_FRACTION="0.125"
NVH_TIER_SNAP_MB=512
NVH_TIER_0_MIN=0
NVH_TIER_0_MAX=4
NVH_TIER_0_LABEL="cpu"
NVH_TIER_0_CTX=4096
NVH_TIER_0_PARALLEL=1
NVH_TIER_0_CHAT="gemma3:1b"
NVH_TIER_0_CODE="qwen3:1.7b"
NVH_TIER_0_VISION="moondream"
NVH_TIER_0_REASONING="qwen3:1.7b"
NVH_TIER_0_EMBED="nomic-embed-text"
NVH_TIER_0_CPU_FALLBACK="gemma3:1b"
"""

SHELL_SNAPSHOT_TAIL = """\
NVH_TIER_9_MIN=96
NVH_TIER_9_MAX=999999
NVH_TIER_9_LABEL="max"
NVH_TIER_9_CTX=131072
NVH_TIER_9_PARALLEL=4
NVH_TIER_9_CHAT="nemotron3:33b-q8"
NVH_TIER_9_CODE="qwen3-coder:30b"
NVH_TIER_9_VISION="llama3.2-vision"
NVH_TIER_9_REASONING="gpt-oss:120b"
NVH_TIER_9_EMBED="nomic-embed-text"
NVH_TIER_9_CPU_FALLBACK="gemma3:4b"
"""


def test_markdown_snapshot():
    assert lm.tier_table_markdown() == MARKDOWN_SNAPSHOT


def test_shell_snapshot():
    text = lm.tier_table_shell()
    assert text.startswith(SHELL_SNAPSHOT_HEAD)
    assert text.endswith(SHELL_SNAPSHOT_TAIL)
    lines = text.splitlines()
    assert len(lines) == SHELL_HEADER_LINES + 10 * (5 + len(lm.USE_CASES))
    assert text.isascii()
    for line in lines[1:]:
        assert re.match(r'^NVH_[A-Z0-9_]+=("[^"$`\\]*"|\d+)$', line), line
    for line in lines:
        assert INSTALL_SH_LINE_RE.match(line), f"install.sh would reject the snippet: {line}"
    # Every tier row is present and the middle of the ladder matches the table.
    assert 'NVH_TIER_5_MIN=24\nNVH_TIER_5_MAX=40\nNVH_TIER_5_LABEL="large"' in text
    assert 'NVH_TIER_5_CHAT="qwen3:30b-a3b"' in text and 'NVH_TIER_4_REASONING="gpt-oss:20b"' in text
    assert text.count("_MIN=") == text.count("_MAX=") == 10


def _shell_env(text: str) -> dict[str, str]:
    """The snippet as install.sh sees it after sourcing: name -> value, quotes stripped."""
    pairs = (line.split("=", 1) for line in text.splitlines() if line.startswith("NVH_"))
    return {name: value.strip('"') for name, value in pairs}


def test_shell_exports_the_reserve_curve_and_the_snap():
    """install.sh reproduces tier_budget's maths from these four values instead of typing them."""
    env = _shell_env(lm.tier_table_shell())
    assert env["NVH_UNIFIED_OS_RESERVE_GB"] == "16"                       # the GB10 figure, kept for older installers
    assert env["NVH_UNIFIED_OS_RESERVE_MIN_GB"] == str(int(lm.UNIFIED_OS_RESERVE_MIN_GB)) == "4"
    assert env["NVH_UNIFIED_OS_RESERVE_MAX_GB"] == str(int(lm.UNIFIED_OS_RESERVE_MAX_GB)) == "16"
    assert env["NVH_UNIFIED_OS_RESERVE_FRACTION"] == f"{lm.UNIFIED_OS_RESERVE_FRACTION:g}" == "0.125"
    assert env["NVH_TIER_SNAP_MB"] == str(int(lm.TIER_SNAP_GB * 1024)) == "512"   # install.sh used to hard-code +512
    lo, hi = int(env["NVH_UNIFIED_OS_RESERVE_MIN_GB"]), int(env["NVH_UNIFIED_OS_RESERVE_MAX_GB"])
    fraction = float(env["NVH_UNIFIED_OS_RESERVE_FRACTION"])
    for pool_gb in (8, 16, 24, 32, 48, 64, 96, 128, 192):
        assert min(hi, max(lo, round(pool_gb * fraction))) == lm.unified_os_reserve_gb(pool_gb), pool_gb


def test_renderers_are_deterministic():
    assert lm.tier_table_markdown() == lm.tier_table_markdown()
    assert lm.tier_table_shell() == lm.tier_table_shell()


# ---------------------------------------------------------------------------
# scripts/verify_local_model_tags.py (offline)
# ---------------------------------------------------------------------------


def test_verifier_tag_parsing(verifier):
    assert verifier.split_tag("qwen3:8b") == ("qwen3", "8b")
    assert verifier.split_tag("moondream") == ("moondream", "latest")
    assert verifier.manifest_url("nemotron3:33b-q8") == (
        "https://registry.ollama.ai/v2/library/nemotron3/manifests/33b-q8"
    )
    assert verifier.manifest_url("moondream").endswith("/moondream/manifests/latest")
    assert verifier.DEFAULT_TIMEOUT == 10.0
    assert verifier.manifest_size_gb({"layers": [{"size": 5_230_000_000}, {"size": 1_000}]}) == 5.23
    assert verifier.manifest_size_gb(None) is None and verifier.manifest_size_gb({"layers": []}) is None


def test_verifier_check_tag_verdicts(verifier, monkeypatch):
    def fake(tag, timeout=10.0):
        return {
            "ok:1": (200, {"layers": [{"size": 5_230_000_000}]}),
            "drift:1": (200, {"layers": [{"size": 9_000_000_000}]}),
            "gone:1": (404, None),
            "flaky:1": (503, None),
        }[tag]

    monkeypatch.setattr(verifier, "fetch_manifest", fake)
    ok = verifier.check_tag("ok:1", 5.2)
    assert (ok.status, ok.http_status, ok.registry_gb, ok.size_ok, ok.passed) == ("ok", 200, 5.23, True, True)
    drift = verifier.check_tag("drift:1", 5.2)
    assert (drift.status, drift.size_ok, drift.passed) == ("ok", False, False)
    gone = verifier.check_tag("gone:1", 5.2)
    assert (gone.status, gone.http_status, gone.passed) == ("missing", 404, False)
    flaky = verifier.check_tag("flaky:1", 5.2)
    assert (flaky.status, flaky.passed) == ("error", False) and "503" in flaky.detail

    def boom(tag, timeout=10.0):
        raise TimeoutError("timed out")

    monkeypatch.setattr(verifier, "fetch_manifest", boom)
    err = verifier.check_tag("x:1", 1.0)
    assert (err.status, err.http_status, err.passed) == ("error", None, False)
    assert err.detail.startswith("TimeoutError")
    assert err.as_dict()["passed"] is False and err.as_dict()["size_ok"] is None


def test_verifier_main_offline_pass_and_fail(verifier, monkeypatch, capsys):
    sizes = {p.tag: p.weights_gb for p in lm.all_picks()}

    def registry_ok(tag, timeout=10.0):
        return 200, {"layers": [{"size": int(sizes[tag] * 1e9)}]}

    monkeypatch.setattr(verifier, "fetch_manifest", registry_ok)
    assert verifier.main([]) == 0
    out = capsys.readouterr().out
    assert "16/16 tags verified" in out and "FAILED" not in out
    for tag in lm.all_tags():
        assert tag in out

    def registry_missing_one(tag, timeout=10.0):
        if tag == "nemotron3:33b":
            return 404, None
        return registry_ok(tag, timeout)

    monkeypatch.setattr(verifier, "fetch_manifest", registry_missing_one)
    assert verifier.main([]) == 1
    out = capsys.readouterr().out
    assert "15/16 tags verified; 1 FAILED: nemotron3:33b" in out and "MISSING" in out

    # --tag narrows the run: the phantom nemotron3:33b is never probed, so this passes.
    assert verifier.main(["--json", "--tag", "qwen3:8b"]) == 0
    payload = capsys.readouterr().out
    assert '"tag": "qwen3:8b"' in payload and '"passed": true' in payload
    assert "nemotron3" not in payload


def test_verifier_covers_every_table_tag(verifier, monkeypatch):
    seen: list[str] = []

    def record(tag, timeout=10.0):
        seen.append(tag)
        return 200, {"layers": [{"size": int(lm.pick_for_tag(tag).weights_gb * 1e9)}]}

    monkeypatch.setattr(verifier, "fetch_manifest", record)
    checks = verifier.verify()
    assert sorted(seen) == lm.all_tags()
    assert all(c.passed for c in checks)


# ---------------------------------------------------------------------------
# Live registry probe -- opt in with NVH_NETWORK_TESTS=1
# ---------------------------------------------------------------------------


@pytest.mark.network
@pytest.mark.skipif(
    os.environ.get("NVH_NETWORK_TESTS") != "1",
    reason="set NVH_NETWORK_TESTS=1 to probe registry.ollama.ai for every table tag",
)
def test_every_table_tag_is_published_on_the_registry(verifier):
    checks = verifier.verify(timeout=10.0)
    failed = [c for c in checks if not c.passed]
    assert not failed, "\n" + verifier.render_table(checks)


# ---------------------------------------------------------------------------
# Review 2026-09-02 (C4/C5): unreadable GPU memory vs no GPU; driver-reported sizes
# ---------------------------------------------------------------------------


def _ghost() -> GPUInfo:
    """A GPU NVML listed but could not size (a memory-unavailable 0 GB row)."""
    return _gpu("NVIDIA Ghost", 0, cc=(8, 9))


def test_total_gpus_counts_rows_seen_and_sized_gpus_rows_read():
    none = lm.tier_budget([], X86_RAM)
    assert (none.total_gpus, none.sized_gpus, none.gpu_memory_unreadable) == (0, 0, False)
    ghost = lm.tier_budget([_ghost()], X86_RAM)
    assert (ghost.total_gpus, ghost.sized_gpus, ghost.gpu_memory_unreadable) == (1, 0, True)
    mixed = lm.tier_budget([_ghost(), *GB10()], SPARK_RAM)
    assert (mixed.total_gpus, mixed.sized_gpus, mixed.gpu_memory_unreadable) == (2, 1, False)
    assert lm.tier_budget(A100x2(), X86_RAM).total_gpus == 2
    assert lm.tier_budget(None, None).total_gpus == 0


def test_unsized_gpu_is_not_cpu_only():
    """C4: a detected GPU whose memory could not be read is the 0-4 tier's
    4096, as gpu.get_ollama_optimizations sizes it; only an empty list is 2048."""
    ghost = lm.tier_budget([_ghost()], X86_RAM)
    assert ghost.budget_gb == 0.0 and lm.tier_for(ghost).label == "cpu"
    assert lm.num_ctx_for(ghost) == 4096 == lm.LOCAL_MODEL_TIERS[0].num_ctx
    assert lm.num_ctx_for(ghost) == gpu.get_ollama_optimizations([_ghost()]).recommended_ctx
    none = lm.tier_budget([], X86_RAM)
    assert lm.num_ctx_for(none) == lm.CPU_ONLY_NUM_CTX == 2048
    assert lm.num_ctx_for(none) == gpu.get_ollama_optimizations([]).recommended_ctx
    # The lone unreadable row still names the architecture (gpu._primary_row).
    assert ghost.compute_capability == (8, 9)


def test_reason_distinguishes_no_gpu_from_unreadable_gpu_memory():
    none = lm.tier_budget([], X86_RAM)
    reason = lm.reason_for(none, lm.pick(none, "chat"))
    assert "no GPU detected" in reason and "could not be read" not in reason
    ghost = lm.tier_budget([_ghost()], X86_RAM)
    reason = lm.reason_for(ghost, lm.pick(ghost, "chat"))
    assert "GPU detected but its memory could not be read" in reason
    assert "no GPU detected" not in reason and "4096 ctx" in reason
    # A sized card beside the ghost is neither.
    mixed = lm.tier_budget([_ghost(), *GB10()], SPARK_RAM)
    reason = lm.reason_for(mixed, lm.pick(mixed, "chat"))
    assert "no GPU detected" not in reason and "could not be read" not in reason


# Driver-reported totals sit a few hundred MiB under the nominal size. The
# table snaps (TIER_SNAP_GB) to the nominal tier and gpu.py reads its ladder
# through the same accessors, so the two agree on every row -- including the
# 24 and 80 GB steps where gpu.py's old raw compare used to land a tier low.
DRIVER_REPORTED = [
    # (row, nominal tier, (ctx, parallel, quant))
    (_gpu("NVIDIA GeForce RTX 4090", 23.99, cc=(8, 9)), "large", (32768, 2, "Q4_K_M")),
    (_gpu("NVIDIA H100 80GB HBM3", 79.65, cc=(9, 0)), "datacenter", (65536, 4, "Q8_0 or F16")),
    (_gpu("NVIDIA A100-SXM4-40GB", 39.59, cc=(8, 0)), "xl", (32768, 2, "Q4_K_M")),
]


@pytest.mark.parametrize("row, label, expected", DRIVER_REPORTED, ids=[r[0].name for r in DRIVER_REPORTED])
def test_driver_reported_sizes_land_in_the_nominal_tier(row, label, expected):
    """C5: the snap is applied in tier_for() and reaches every accessor -- gpu.py's included."""
    budget = lm.tier_budget([row], X86_RAM)
    tier = lm.tier_for(budget)
    assert tier.label == label
    assert budget.total_gb < tier.min_gb <= budget.total_gb + lm.TIER_SNAP_GB
    ours = (lm.num_ctx_for(budget), lm.num_parallel_for(budget), lm.quant_for(budget))
    assert ours == expected == (tier.num_ctx, tier.num_parallel, lm.quant_for(tier.min_gb))
    theirs = gpu.get_ollama_optimizations([row])
    assert ours == (theirs.recommended_ctx, theirs.num_parallel, theirs.recommended_quant)
    # The same card sized nominally is indistinguishable from the driver's figure.
    nominal = lm.tier_budget([_gpu(row.name, tier.min_gb, cc=row.compute_capability)], X86_RAM)
    assert lm.tier_for(nominal) is tier
    assert [p.tag for p in lm.recommended(nominal)] == [p.tag for p in lm.recommended(budget)]


def test_unknown_compute_capability_is_not_guessed_from_the_name():
    """C5: gpu.py parses "H100" out of the name when NVML gives no capability;
    this table never does, so the Hopper Q8 tier needs a reported capability."""
    h100 = _gpu("NVIDIA H100 80GB HBM3", 80, cc=(0, 0))
    budget = lm.tier_budget([h100], X86_RAM)
    assert budget.compute_capability == (0, 0) and not budget.compute_capability_known
    assert lm.quant_for(budget) == "Q4_K_M"
    theirs = gpu.get_ollama_optimizations([h100])
    assert theirs.compute_capability == (9, 0) and theirs.recommended_quant == "Q8_0 or F16"
    # With the capability reported the two agree again (see LADDER_CASES).
    reported = lm.tier_budget([_gpu("NVIDIA H100 80GB HBM3", 80, cc=(9, 0))], X86_RAM)
    assert lm.quant_for(reported) == "Q8_0 or F16"


def test_ladder_parity_claim_is_worded_honestly():
    text = MODULE.read_text(encoding="utf-8")
    assert "reproduce gpu.get_ollama_optimizations exactly" not in text
    # gpu.py snaps through tier_for() now: the "not yet" caveats are gone and the
    # one remaining difference -- the name-heuristic compute capability -- is named.
    assert "does not snap yet" not in text and "gaps close when gpu.py migrates" not in text
    assert "snaps by TIER_SNAP_GB" in text and "(0.5) for both" in text
    assert "reads one off the GPU name" in text and "module never does -- quant_for on a bare tier_budget()" in text


# ---------------------------------------------------------------------------
# Unified OS reserve curve (install-mac.sh regression: a 16 GB Mac planned against 0 GB)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "total_gb, reserve_gb",
    [
        (0, 4.0), (8, 4.0), (16, 4.0), (24, 4.0), (32, 4.0),   # the 4 GB floor: OS + browser
        (48, 6.0), (64, 8.0), (96, 12.0),                       # an eighth of the pool
        (128, 16.0), (192, 16.0), (1024, 16.0),                 # the GB10 figure is the ceiling
    ],
)
def test_unified_os_reserve_curve(total_gb, reserve_gb):
    assert lm.unified_os_reserve_gb(total_gb) == reserve_gb
    assert lm.unified_os_reserve_gb(total_gb) == min(
        lm.UNIFIED_OS_RESERVE_MAX_GB,
        max(lm.UNIFIED_OS_RESERVE_MIN_GB, round(total_gb * lm.UNIFIED_OS_RESERVE_FRACTION)),
    )


def test_unified_os_reserve_constants_and_shape():
    assert lm.UNIFIED_OS_RESERVE_MIN_GB == 4.0
    assert lm.UNIFIED_OS_RESERVE_MAX_GB == lm.UNIFIED_MEMORY_OS_RESERVE_GB == 16.0
    assert lm.UNIFIED_OS_RESERVE_FRACTION == 0.125
    assert lm.unified_os_reserve_gb(128) == lm.UNIFIED_MEMORY_OS_RESERVE_GB   # GB10 / DGX Spark: unchanged
    values = [lm.unified_os_reserve_gb(gb) for gb in range(0, 257)]
    assert values == sorted(values)                                          # never shrinks as the pool grows
    assert min(values) == 4.0 and max(values) == 16.0
    assert all(isinstance(v, float) for v in values)
    assert lm.unified_os_reserve_gb("garbage") == 4.0 and lm.unified_os_reserve_gb(-5) == 4.0


@pytest.mark.parametrize(
    "pool_gb, budget_gb, label, chat",
    [
        (8, 4.0, "mini", "gemma3:4b"),
        (16, 12.0, "small-plus", "qwen3:8b"),        # a 16 GB Apple Silicon Mac: was 0 GB / gemma3:1b
        (32, 28.0, "large", "qwen3:30b-a3b"),
        (64, 56.0, "workstation", "nemotron3:33b-q8"),
        (128, 112.0, "max", "nemotron3:33b-q8"),     # GB10: exactly as before
    ],
)
def test_unified_pool_budgets_follow_the_reserve_curve(pool_gb, budget_gb, label, chat):
    budget = lm.tier_budget([SimpleNamespace(vram_mb=pool_gb * 1024, unified_memory=True)], None)
    assert budget.unified and budget.offload_gb == 0.0 and budget.total_gb == pool_gb
    assert budget.os_reserve_gb == lm.unified_os_reserve_gb(pool_gb) == pool_gb - budget_gb
    assert budget.budget_gb == budget.combined_gb == budget_gb
    assert lm.tier_for(budget).label == label
    assert lm.pick(budget, "chat").tag == chat
    reason = lm.reason_for(budget, lm.pick(budget, "chat"))
    assert f"~{budget_gb:.0f} GB of {pool_gb} GB usable after the {budget.os_reserve_gb:.0f} GB OS reserve" in reason


def test_os_reserve_is_zero_off_a_unified_pool_and_never_negative():
    assert lm.tier_budget(RTX4090(), X86_RAM).os_reserve_gb == 0.0
    assert lm.tier_budget([], X86_RAM).os_reserve_gb == 0.0
    three = lm.tier_budget([SimpleNamespace(vram_mb=3 * 1024, unified_memory=True)], None)
    assert (three.os_reserve_gb, three.budget_gb, lm.tier_for(three).label) == (4.0, 0.0, "cpu")
    # The CLI's --unified-gb path (install-mac.sh) is this call: a 16 GB Mac gets an 8B model.
    mac = lm.tier_budget([SimpleNamespace(vram_mb=16.0 * 1024, unified_memory=True)], None)
    assert lm.pick(mac, "chat").tag == "qwen3:8b" and lm.num_ctx_for(mac) == 8192


# ---------------------------------------------------------------------------
# ordered_picks / vision_picks -- the consumers' hand-rolled derivations, in one place
# ---------------------------------------------------------------------------


def test_ordered_picks_whole_table_is_largest_first_and_deduped():
    # local_chat.PREFERRED_CHAT_MODELS and ollama_provider._fallback_model_preference derive this by hand.
    picks = lm.ordered_picks(None, "chat", "code")
    by_hand: dict[str, lm.LocalModelPick] = {}
    for tier in lm.LOCAL_MODEL_TIERS:
        for use_case in ("chat", "code"):
            by_hand.setdefault(tier.picks[use_case].tag, tier.picks[use_case])
    assert picks == sorted(by_hand.values(), key=lambda p: (-p.runtime_gb, p.tag))
    tags = [p.tag for p in picks]
    assert tags == [
        "nemotron3:33b-q8", "nemotron3:33b", "qwen3-coder:30b", "qwen3:30b-a3b", "qwen3:14b",
        "qwen3:8b", "gemma3:4b", "qwen3:4b", "qwen3:1.7b", "gemma3:1b",
    ]
    assert len(tags) == len(set(tags)) and "nomic-embed-text" not in tags
    sizes = [p.runtime_gb for p in picks]
    assert sizes == sorted(sizes, reverse=True)
    # No use case -> every column; equals all_picks() re-sorted.
    assert lm.ordered_picks(None) == sorted(lm.all_picks(), key=lambda p: (-p.runtime_gb, p.tag))
    with pytest.raises(ValueError):
        lm.ordered_picks(None, "music")


def test_ordered_picks_stops_at_the_budgets_tier():
    assert [p.tag for p in lm.ordered_picks(24.0, "chat", "code")] == [
        "qwen3-coder:30b", "qwen3:30b-a3b", "qwen3:14b", "qwen3:8b", "gemma3:4b", "qwen3:4b", "qwen3:1.7b", "gemma3:1b",
    ]
    assert lm.ordered_picks(0.0, "chat") == [lm.GEMMA3_1B]
    assert lm.ordered_picks(23.99, "chat") == lm.ordered_picks(24.0, "chat")    # snaps like every accessor
    budget = lm.tier_budget(RTX4090(), X86_RAM)
    assert all(p.runtime_gb <= budget.budget_gb for p in lm.ordered_picks(budget, "chat", "code", "reasoning"))
    assert lm.moe_first(budget) is False
    assert [p.tag for p in lm.ordered_picks(budget, "chat")][:2] == ["qwen3:30b-a3b", "qwen3:14b"]


def test_ordered_picks_moe_first_and_compute_floor():
    gb10 = lm.tier_budget(GB10(), SPARK_RAM)
    picks = lm.ordered_picks(gb10, "chat", "code", "reasoning")
    moe = [p.moe for p in picks]
    assert moe == sorted(moe, reverse=True) and moe[0] and not moe[-1]         # MoE block first, then dense
    assert [p.tag for p in picks][:3] == ["gpt-oss:120b", "nemotron3:33b-q8", "nemotron3:33b"]
    assert [p.tag for p in picks][-1] == "gemma3:1b"
    # A known Turing capability drops llama3.2-vision; an unknown one does not.
    turing = lm.tier_budget([_gpu("Quadro RTX 8000", 48, cc=(7, 5))], X86_RAM)
    assert [p.tag for p in lm.ordered_picks(turing, "vision")] == ["qwen3-vl:8b", "moondream"]
    assert [p.tag for p in lm.ordered_picks(48.0, "vision")] == ["llama3.2-vision", "qwen3-vl:8b", "moondream"]


def test_vision_picks_are_the_dedicated_vision_column_only():
    picks = lm.vision_picks()
    assert [p.tag for p in picks] == ["llama3.2-vision", "qwen3-vl:8b", "moondream"]
    assert picks == lm.ordered_picks(None, "vision")
    assert all(p.vision for p in picks)
    column = {tier.picks["vision"].tag for tier in lm.LOCAL_MODEL_TIERS}
    assert {p.tag for p in picks} == column
    # Vision-capable chat picks are not "vision models" here.
    capable = {p.tag for p in lm.all_picks() if p.vision}
    assert {"gemma3:4b", "nemotron3:33b", "nemotron3:33b-q8"} <= capable - column
