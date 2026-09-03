"""Tests for nvh.utils.gpu — detection status, model recommendations, Ollama tuning.

recommend_models / get_ollama_optimizations read nvh.core.local_models; the
recommendation tests derive every expected tag, size, reason and ladder value
from that table so they cannot drift from it.
"""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from nvh.core import local_models as lm
from nvh.utils import gpu


@pytest.fixture(autouse=True)
def _fresh_smi_memo():
    """detect_gpu_status memoises the nvidia-smi fallback for SMI_FALLBACK_TTL_S; each test starts cold."""
    gpu.clear_gpu_detection_cache()
    yield
    gpu.clear_gpu_detection_cache()


def _row(name: str, vram_mb: int, *, unified: bool = False, index: int = 0) -> gpu.GPUInfo:
    return gpu.GPUInfo(
        name=name, vram_mb=vram_mb, vram_gb=round(vram_mb / 1024, 1), driver_version="580.65",
        cuda_version="13.0", utilization_pct=0, memory_used_mb=0, memory_free_mb=vram_mb, index=index,
        unified_memory=unified,
    )


def test_detect_gpu_status_distinguishes_blocked_devices(monkeypatch) -> None:
    monkeypatch.setattr(gpu, "_detect_gpus_pynvml", lambda *, issues=None: [])
    monkeypatch.setattr(gpu, "_detect_gpus_smi", lambda *, issues=None: [])
    monkeypatch.setattr(gpu, "_nvidia_device_files_present", lambda: True)
    monkeypatch.setattr(gpu.shutil, "which", lambda command: "/usr/bin/nvidia-smi" if command == "nvidia-smi" else None)

    status = gpu.detect_gpu_status()

    assert status["status"] == "blocked"
    assert status["device_files_present"] is True
    assert any(issue["code"] == "devices-present-no-query" for issue in status["issues"])


def test_gpu_architecture_info_marks_name_heuristic() -> None:
    info = gpu.GPUInfo(
        name="NVIDIA RTX 4090",
        vram_mb=24576,
        vram_gb=24.0,
        driver_version="570.00",
        cuda_version="12.4",
        utilization_pct=0,
        memory_used_mb=0,
        memory_free_mb=24576,
        index=0,
    )

    arch = gpu.gpu_architecture_info(info)

    assert arch["architecture"] == "Ada Lovelace"
    assert arch["heuristic"] is True


RAM_16 = gpu.SystemMemoryInfo(16.0, 12.0, 8.0)
RAM_32 = gpu.SystemMemoryInfo(32.0, 24.0, 16.0)
RAM_64 = gpu.SystemMemoryInfo(64.0, 48.0, 32.0)
RAM_128 = gpu.SystemMemoryInfo(128.0, 100.0, 70.0)

# Tags that used to be pull targets somewhere in the six ladders and are gone
# from the registry (or never existed). None may come back out of gpu.py.
RETIRED_TAG_PREFIXES = (
    "nemotron-omni", "nemotron-3-nano-omni", "nemotron:70b", "nemotron-3-super", "nemotron-mini",
    "qwen2.5-coder", "minicpm-v", "llava", "bakllava", "llama3.3:70b", "deepseek-r1:8b",
    "codellama", "gemma2", "phi4", "qwen2.5", "mistral:7b", "llama3.2:3b", "llama3.1:8b",
)
LEGACY_BANDS = ("mini", "small", "medium", "full")


def _card(name: str, vram_gb: float, *, cc: tuple[int, int] = (0, 0), unified: bool = False, index: int = 0) -> gpu.GPUInfo:
    vram_mb = int(vram_gb * 1024)
    return gpu.GPUInfo(
        name=name, vram_mb=vram_mb, vram_gb=round(vram_mb / 1024, 2), driver_version="580.65",
        cuda_version="13.0", utilization_pct=0, memory_used_mb=0, memory_free_mb=vram_mb, index=index,
        compute_capability=cc, unified_memory=unified,
    )


def _recommend(gpus: list[gpu.GPUInfo], sys_mem: gpu.SystemMemoryInfo) -> tuple[gpu.MemoryBudget, list[gpu.ModelRecommendation]]:
    with patch.object(gpu, "detect_system_memory", return_value=sys_mem):
        return gpu._memory_budget(gpus, sys_mem), gpu.recommend_models(gpus=gpus)


def _optimize(gpus: list[gpu.GPUInfo], sys_mem: gpu.SystemMemoryInfo) -> gpu.OllamaOptimization:
    with patch.object(gpu, "detect_system_memory", return_value=sys_mem):
        return gpu.get_ollama_optimizations(gpus=gpus)


def _table_order(budget: lm.TierBudget) -> list[str]:
    """recommend_models' order minus the hybrid pick: the table's text picks, then vision, then embed."""
    picks = [p.tag for p in lm.recommended(budget)]
    tail = [p.tag for u in ("vision", "embed") if (p := lm.pick(budget, u)) is not None and p.tag in picks]
    return [t for t in picks if t not in tail] + tail


def _expected_hybrid(budget: lm.TierBudget) -> lm.LocalModelPick | None:
    """The rule recommend_models applies, restated from the table: a card of HYBRID_MIN_BUDGET_GB or
    more, the tier combined_gb reaches, its chat (else code) pick, when that pick needs more than
    VRAM, fits VRAM + RAM bonus and leaves at most HYBRID_MAX_RAM_SHARE of itself in RAM."""
    if budget.unified or budget.offload_gb <= 0 or budget.sized_gpus == 0:
        return None
    home = lm.tier_for(budget)
    reach = replace(budget, budget_gb=budget.combined_gb, offload_gb=0.0)
    if home.min_gb < gpu.HYBRID_MIN_BUDGET_GB or lm.tier_for(reach) is home:
        return None
    ceiling = min(budget.combined_gb, budget.budget_gb / (1 - gpu.HYBRID_MAX_RAM_SHARE))
    listed = set(_table_order(budget))
    for use_case in ("chat", "code"):
        pick = lm.pick(reach, use_case)
        if pick is not None and pick.tag not in listed and budget.budget_gb < pick.runtime_gb <= ceiling:
            return pick
    return None


class TestTierVocabulary:
    """gpu.py's one contribution the table does not make: the tier words consumers read."""

    def test_tier_labels_cover_every_table_tier(self) -> None:
        assert set(gpu.TIER_LABELS) == {t.label for t in lm.LOCAL_MODEL_TIERS}
        assert set(gpu.TIER_LABELS.values()) <= set(LEGACY_BANDS)
        # Bands never go down as the table goes up.
        ranks = [LEGACY_BANDS.index(gpu.TIER_LABELS[t.label]) for t in lm.LOCAL_MODEL_TIERS]
        assert ranks == sorted(ranks)
        assert gpu.TIER_LABELS[lm.LOCAL_MODEL_TIERS[0].label] == "mini"
        assert gpu.TIER_LABELS[lm.LOCAL_MODEL_TIERS[-1].label] == "full"

    def test_recommendation_tier_is_the_home_tier_band(self) -> None:
        for pick in lm.all_picks():
            home = next(t for t in lm.LOCAL_MODEL_TIERS if pick.tag in {p.tag for p in t.picks.values()})
            assert gpu.recommendation_tier(pick) == gpu.TIER_LABELS[home.label]
        with pytest.raises(KeyError):
            gpu.recommendation_tier(replace(lm.all_picks()[0], tag="not-in-the-table"))

    def test_shared_constants_are_the_tables(self) -> None:
        assert gpu.UNIFIED_MEMORY_OS_RESERVE_GB == lm.UNIFIED_MEMORY_OS_RESERVE_GB
        assert gpu.UNIFIED_MEMORY_BANDWIDTH_GBPS == lm.UNIFIED_MEMORY_BANDWIDTH_GBPS


class TestMemoryBudget:
    def test_memory_budget_is_a_tier_budget_view(self) -> None:
        rtx = _card("NVIDIA GeForce RTX 4090", 24)
        budget = gpu._memory_budget([rtx], RAM_64)
        table = lm.tier_budget([rtx], RAM_64)

        assert isinstance(budget, lm.TierBudget)
        assert (budget.model_budget_gb, budget.cpu_offload_gb, budget.unified_memory, budget.total_vram_gb) == (
            budget.budget_gb, budget.offload_gb, budget.unified, budget.total_gb,
        )
        assert (budget.budget_gb, budget.offload_gb, budget.unified, budget.total_gb, budget.combined_gb) == (
            table.budget_gb, table.offload_gb, table.unified, table.total_gb, table.combined_gb,
        )
        assert budget.combined_gb == budget.budget_gb + budget.offload_gb

    def test_memory_budget_reads_compute_capability_off_the_name_when_unreported(self) -> None:
        """The table never guesses; gpu.py's name heuristic fills the gap so the Turing swap and Hopper quant work."""
        assert gpu._memory_budget([_card("NVIDIA GeForce RTX 4090", 24)], RAM_64).compute_capability == (8, 9)
        assert gpu._memory_budget([_card("RTX 2080 Ti", 24)], RAM_64).compute_capability == (7, 5)
        assert gpu._memory_budget([_card("NVIDIA H100 80GB HBM3", 80)], RAM_64).compute_capability == (9, 0)
        # A reported capability wins over the name.
        assert gpu._memory_budget([_card("NVIDIA GeForce RTX 4090", 24, cc=(8, 6))], RAM_64).compute_capability == (8, 6)
        assert gpu._memory_budget([], RAM_64).compute_capability == (0, 0)

    def test_memory_budget_accepts_no_system_memory(self) -> None:
        budget = gpu._memory_budget([_card("NVIDIA GeForce RTX 4090", 24)])
        assert (budget.budget_gb, budget.offload_gb) == (24.0, 0.0)


class TestGPURecommendations:
    """recommend_models / get_ollama_optimizations read nvh.core.local_models.

    Every expectation here is derived from the table (picks, runtime sizes,
    reason_for, num_ctx / num_parallel / quant) so a table edit cannot desync
    it; what the tests pin is gpu.py's own contribution -- the tier words, the
    ordering, the hybrid / multi-GPU / unified decorations, the snap and the
    name-heuristic compute capability.
    """

    def test_recommend_models_no_gpu(self) -> None:
        budget, recs = _recommend([], RAM_16)

        assert (budget.total_gpus, budget.sized_gpus) == (0, 0)
        assert budget.offload_gb > 0, "RAM bonus is computed but there is no GPU to be hybrid with"
        assert [r.model for r in recs] == _table_order(budget)
        assert not any(r.tier.endswith("-hybrid") for r in recs)
        assert recs[0].tier == gpu.recommendation_tier(lm.recommended(budget)[0]) == "mini"
        assert "no GPU detected" in recs[0].reason
        assert all(r.note == "" for r in recs)

    @pytest.mark.parametrize(
        "gpus, sys_mem",
        [
            ([], RAM_16),
            ([_card("RTX 4060", 8)], RAM_32),
            ([_card("RTX 4070 Ti", 16)], RAM_32),
            ([_card("NVIDIA GeForce RTX 4090", 24)], RAM_64),
            ([_card("RTX 2080 Ti", 24)], RAM_64),
            ([_card("RTX 6000 Ada", 48)], RAM_64),
            ([_card("H100", 80)], RAM_128),
            ([_card("NVIDIA GB10", 128, unified=True, cc=(12, 1))], RAM_128),
            ([_card("RTX 3090", 24), _card("RTX 3090", 24, index=1)], RAM_64),
        ],
        ids=["cpu", "8gb", "16gb", "4090", "turing-24gb", "48gb", "h100", "gb10", "2x3090"],
    )
    def test_every_rec_is_a_table_row(self, gpus, sys_mem) -> None:
        budget, recs = _recommend(gpus, sys_mem)

        assert recs and len({r.model for r in recs}) == len(recs), "unique tags"
        text = [r for r in recs if not r.tier.endswith("-hybrid")]
        assert [r.model for r in text] == _table_order(budget)
        vision, embed = lm.pick(budget, "vision"), lm.pick(budget, "embed")
        for i, rec in enumerate(recs):
            pick = lm.pick_for_tag(rec.model)
            assert pick is not None and rec.model in lm.all_tags()
            assert rec.vram_required_gb == pick.runtime_gb
            assert rec.use_case in lm.USE_CASES
            if rec.tier.endswith("-hybrid"):
                assert rec.tier == gpu.recommendation_tier(pick) + "-hybrid"
                assert rec.reason.startswith(lm.reason_for(budget, pick))
                continue
            assert rec.reason == lm.reason_for(budget, pick)
            if rec.model == vision.tag:
                assert (rec.tier, rec.use_case) == ("vision", "vision")
            elif rec.model == embed.tag:
                assert (rec.tier, rec.use_case) == ("embed", "embed")
            elif i == 0 and budget.sized_gpus > 1:
                assert rec.tier == "multi-gpu"
            else:
                assert rec.tier == gpu.recommendation_tier(pick)
        assert recs[0].model == lm.recommended(budget)[0].tag
        # The embedding pick is last: callers take recs[1] as the chat fallback.
        assert recs[-1].model == embed.tag and recs[-1].tier == "embed"
        assert [r for r in recs if r.tier.startswith("vision")] == [r for r in recs if r.model == vision.tag]

    def test_recommend_models_8gb(self) -> None:
        budget, recs = _recommend([_card("RTX 4060", 8)], RAM_32)
        assert recs[0].model == lm.pick(budget, "chat").tag
        assert recs[0].tier == gpu.recommendation_tier(lm.pick(budget, "chat")) == "small"
        assert {r.tier.removesuffix("-hybrid") for r in recs} <= {*LEGACY_BANDS, "vision", "embed"}

    def test_recommend_models_24gb(self) -> None:
        budget, recs = _recommend([_card("NVIDIA GeForce RTX 4090", 24)], RAM_64)
        assert lm.tier_for(budget) is lm.tier_for(24.0)
        assert recs[0].model == lm.recommended(budget)[0].tag
        assert recs[0].tier == "full"

    # ---- hybrid (CPU offload) pick ----

    def test_hybrid_pick_needs_offload_and_fits_combined(self) -> None:
        budget, recs = _recommend([_card("NVIDIA GeForce RTX 4090", 24)], RAM_64)
        expected = _expected_hybrid(budget)
        assert expected is not None and budget.budget_gb < expected.runtime_gb <= budget.combined_gb

        hybrids = [r for r in recs if r.tier.endswith("-hybrid")]
        assert [(r.model, r.tier, r.vram_required_gb) for r in hybrids] == [
            (expected.tag, gpu.recommendation_tier(expected) + "-hybrid", expected.runtime_gb),
        ]
        reason = hybrids[0].reason
        assert reason.startswith(lm.reason_for(budget, expected))
        assert (
            f"partial CPU offload: {budget.budget_gb:.0f} GB VRAM + {budget.offload_gb:.0f} GB RAM = "
            f"{budget.combined_gb:.0f} GB combined"
        ) in reason
        # It sits after the text picks and before the vision / embed picks.
        text_end = max(i for i, r in enumerate(recs) if r.tier in LEGACY_BANDS)
        assert recs.index(hybrids[0]) == text_end + 1
        assert recs.index(hybrids[0]) < min(i for i, r in enumerate(recs) if r.tier in ("vision", "embed"))

    def test_hybrid_follows_the_table_rule_across_budgets(self) -> None:
        for vram_gb in (6, 8, 12, 16, 20, 24, 32, 40, 48, 64, 80, 96):
            budget, recs = _recommend([_card("NVIDIA GPU", vram_gb, cc=(8, 9))], RAM_64)
            expected = _expected_hybrid(budget)
            hybrids = [r for r in recs if r.tier.endswith("-hybrid")]
            assert [r.model for r in hybrids] == ([expected.tag] if expected else []), f"{vram_gb} GB"
            for rec in hybrids:
                spill = rec.vram_required_gb - budget.budget_gb
                assert 0 < spill <= gpu.HYBRID_MAX_RAM_SHARE * rec.vram_required_gb, f"{vram_gb} GB"
                assert budget.budget_gb >= gpu.HYBRID_MIN_BUDGET_GB, f"{vram_gb} GB"

    def test_no_hybrid_when_combined_stays_in_the_tier(self) -> None:
        budget, recs = _recommend([_card("RTX 6000 Ada", 48)], RAM_64)
        assert lm.tier_for(budget.combined_gb) is lm.tier_for(budget)
        assert not any(r.tier.endswith("-hybrid") for r in recs)

    def test_no_hybrid_from_the_cpu_tier(self) -> None:
        """A 2 GB card runs the CPU tier's models from RAM already: nothing to offload *from*."""
        budget, recs = _recommend([_card("GTX 1050", 2)], RAM_64)
        assert lm.tier_for(budget) is lm.LOCAL_MODEL_TIERS[0] and budget.offload_gb > 0
        assert not any(r.tier.endswith("-hybrid") for r in recs)

    def test_no_hybrid_on_unified_memory(self) -> None:
        budget, recs = _recommend([_card("NVIDIA GB10", 128, unified=True, cc=(12, 1))], RAM_128)
        assert budget.offload_gb == 0.0
        assert not any(r.tier.endswith("-hybrid") for r in recs)

    def test_hybrid_limits_are_read_off_the_table(self) -> None:
        """12 GB is the small-plus floor: the first tier whose num_ctx rises above the entry tiers'."""
        small_plus = next(t for t in lm.LOCAL_MODEL_TIERS if t.label == "small-plus")
        assert gpu.HYBRID_MIN_BUDGET_GB == small_plus.min_gb == 12.0
        entry_ctx = lm.LOCAL_MODEL_TIERS[0].num_ctx
        assert all(t.num_ctx == entry_ctx for t in lm.LOCAL_MODEL_TIERS if t.min_gb < gpu.HYBRID_MIN_BUDGET_GB)
        assert small_plus.num_ctx > entry_ctx
        assert gpu.HYBRID_MAX_RAM_SHARE == 0.4

    @pytest.mark.parametrize(
        "vram_gb, expected",
        [
            (6, None),               # below the floor: the old rule offered qwen3:14b with 5.2 of its 11.2 GB in RAM
            (8, None),               # below the floor: the old rule offered qwen3:30b-a3b with 12.5 of 20.5 GB (61%) in RAM
            (12, None),              # on the floor, but the 30B MoE the reached tier names would spill 8.5 of 20.5 GB (41%)
            (16, "qwen3:30b-a3b"),   # 4.5 of 20.5 GB (22%) in RAM
            (24, "nemotron3:33b"),   # 6.4 of 30.4 GB (21%) in RAM
        ],
        ids=["6gb", "8gb", "12gb", "16gb", "24gb"],
    )
    def test_hybrid_needs_a_card_with_headroom(self, vram_gb, expected) -> None:
        """A hybrid pick must sit mostly in VRAM: no card under 12 GB, no pick over 40% in RAM."""
        budget, recs = _recommend([_card("NVIDIA GPU", vram_gb, cc=(8, 9))], RAM_64)
        assert budget.offload_gb == 16.0 and budget.combined_gb == vram_gb + 16
        hybrids = [r for r in recs if r.tier.endswith("-hybrid")]
        assert [r.model for r in hybrids] == ([expected] if expected else [])
        reach = replace(budget, budget_gb=budget.combined_gb, offload_gb=0.0)
        assert lm.tier_for(reach) is not lm.tier_for(budget)          # RAM reaches a higher tier every time...
        assert lm.pick(reach, "chat").runtime_gb > budget.budget_gb   # ...whose chat pick does not fit VRAM,
        if expected is None:                                          # so only the guards said no.
            return
        pick = lm.pick_for_tag(expected)
        spill = pick.runtime_gb - budget.budget_gb
        assert 0 < spill <= gpu.HYBRID_MAX_RAM_SHARE * pick.runtime_gb
        assert hybrids[0].reason.startswith(lm.reason_for(budget, pick))
        assert f"~{round(spill, 1):g} GB ({spill / pick.runtime_gb:.0%}) in RAM" in hybrids[0].reason
        assert hybrids[0].tier == gpu.recommendation_tier(pick) + "-hybrid" == "full-hybrid"

    # ---- unified pools: the OS reserve follows the pool ----

    @pytest.mark.parametrize("pool_gb, reserve_gb", [(8, 4), (16, 4), (32, 4), (64, 8), (128, 16)])
    def test_unified_budget_reserve_follows_the_pool(self, pool_gb, reserve_gb) -> None:
        """A 16 GB unified pool loses 4 GB, not the GB10's 16: the budget, reason, note and Ollama notes agree."""
        pool = [_card("NVIDIA GB10", pool_gb, unified=True, cc=(12, 1))]
        budget, recs = _recommend(pool, RAM_128)
        assert budget.os_reserve_gb == gpu.unified_os_reserve_gb(pool_gb) == reserve_gb
        assert (budget.model_budget_gb, budget.cpu_offload_gb) == (pool_gb - reserve_gb, 0.0)
        assert f"~{pool_gb - reserve_gb} GB of {pool_gb} GB usable after the {reserve_gb} GB OS reserve" in recs[0].reason
        assert f"~{reserve_gb} GB is reserved for the OS" in recs[0].note
        assert f"leaving ~{pool_gb - reserve_gb} GB for models" in recs[0].note
        assert not any(r.tier.endswith("-hybrid") for r in recs)
        opt = _optimize(pool, RAM_128)
        assert any(f"after the {reserve_gb} GB OS reserve" in n for n in opt.notes)
        assert any(f"keep ~{reserve_gb} GB free for the OS" in n for n in opt.notes)
        assert (opt.recommended_ctx, opt.num_parallel) == (lm.num_ctx_for(budget), lm.num_parallel_for(budget))

    # ---- multi-GPU ----

    def test_recommend_models_multi_gpu(self) -> None:
        pair = [_card("RTX 3090", 24), _card("RTX 3090", 24, index=1)]
        budget, recs = _recommend(pair, RAM_64)
        _, single = _recommend([_card("RTX 3090", 48)], RAM_64)

        assert budget.sized_gpus == 2 and budget.budget_gb == 48.0
        assert recs[0].tier == "multi-gpu"
        assert recs[0].model == lm.recommended(budget)[0].tag
        assert all("Ollama will use all 2 GPUs automatically" in r.reason for r in recs)
        assert [r.model for r in recs] == [r.model for r in single]
        assert [r.tier for r in recs[1:]] == [r.tier for r in single[1:]]

    def test_recommend_models_250gb_vram(self) -> None:
        trio = [_card("A100 80GB", 80, index=i) for i in range(3)]
        budget, recs = _recommend(trio, gpu.SystemMemoryInfo(256.0, 200.0, 140.0))
        assert lm.tier_for(budget) is lm.LOCAL_MODEL_TIERS[-1]
        assert recs[0].tier == "multi-gpu"
        assert "Ollama will use all 3 GPUs automatically" in recs[0].reason

    def test_recommend_models_large_vram(self) -> None:
        budget, recs = _recommend([_card("H100", 80)], RAM_128)
        assert budget.compute_capability == (9, 0)
        assert recs[0].model == lm.recommended(budget)[0].tag
        assert recs[0].tier == "full"

    # ---- vision pick ----

    @pytest.mark.parametrize(
        "gpus, sys_mem",
        [
            ([], RAM_16),
            ([_card("GTX 1050", 2)], gpu.SystemMemoryInfo(8.0, 4.0, 2.0)),
            ([_card("RTX 4060", 8)], RAM_32),
            ([_card("RTX 4070 Ti", 16)], RAM_32),
            ([_card("RTX 2080 Ti", 24)], RAM_64),
            ([_card("RTX 6000 Ada", 48)], RAM_64),
        ],
        ids=["cpu", "2gb", "8gb", "16gb", "turing-24gb", "48gb"],
    )
    def test_vision_rec_is_the_tables_vision_pick(self, gpus, sys_mem) -> None:
        budget, recs = _recommend(gpus, sys_mem)
        vision = lm.pick(budget, "vision")
        assert [(r.model, r.tier) for r in recs if r.tier.startswith("vision")] == [(vision.tag, "vision")]
        assert lm.pick_for_tag(vision.tag).vision is True

    def test_vision_model_turing_swap(self) -> None:
        """On Turing (CC 7.5, no BF16) the table's compute floor sends the vision pick a tier down."""
        floors = [p for p in lm.all_picks() if p.vision and p.min_compute_capability is not None]
        assert floors, "the table carries a vision pick with a compute floor"
        turing, turing_recs = _recommend([_card("RTX 2080 Ti", 24)], RAM_64)
        ada, ada_recs = _recommend([_card("NVIDIA GeForce RTX 4090", 24)], RAM_64)
        assert turing.compute_capability == (7, 5) and ada.compute_capability == (8, 9)

        turing_vision = [r.model for r in turing_recs if r.tier.startswith("vision")]
        ada_vision = [r.model for r in ada_recs if r.tier.startswith("vision")]
        assert turing_vision == [lm.pick(turing, "vision").tag]
        assert ada_vision == [lm.pick(ada, "vision").tag]
        assert turing_vision != ada_vision
        for pick in floors:
            if pick.min_compute_capability > (7, 5):
                assert pick.tag not in [r.model for r in turing_recs]

    # ---- Ollama optimizations ----

    def test_get_ollama_optimizations_no_gpu(self) -> None:
        opt = _optimize([], RAM_16)
        budget = gpu._memory_budget([], None)
        assert opt.architecture == "CPU"
        assert opt.flash_attention is False
        assert (opt.num_parallel, opt.recommended_ctx, opt.recommended_quant) == (
            lm.num_parallel_for(budget), lm.num_ctx_for(budget), lm.quant_for(budget),
        )
        assert opt.recommended_ctx == lm.CPU_ONLY_NUM_CTX

    def test_get_ollama_optimizations_rtx4090(self) -> None:
        rtx = _card("NVIDIA GeForce RTX 4090", 24)
        opt = _optimize([rtx], RAM_32)
        budget = gpu._memory_budget([rtx], RAM_32)
        assert opt.flash_attention is True
        assert opt.architecture == "Ada Lovelace"
        assert (opt.recommended_ctx, opt.num_parallel, opt.recommended_quant) == (
            lm.num_ctx_for(budget), lm.num_parallel_for(budget), lm.quant_for(budget),
        )
        tier = lm.tier_for(budget)
        assert (opt.recommended_ctx, opt.num_parallel) == (tier.num_ctx, tier.num_parallel)

    def test_get_ollama_optimizations_hopper_gets_the_tables_hbm_quant(self) -> None:
        h100 = _card("NVIDIA H100 80GB HBM3", 80)
        opt = _optimize([h100], RAM_128)
        budget = gpu._memory_budget([h100], RAM_128)
        assert budget.compute_capability == (9, 0), "read off the name"
        assert opt.recommended_quant == lm.quant_for(budget) == lm.tier_for(budget).default_quant
        assert opt.recommended_quant != "Q4_K_M"
        assert any("Q8_0 or F16 recommended" in n for n in opt.notes)

    # ---- the tier snap ----

    @pytest.mark.parametrize(
        "name, reported_mib, nominal_gb, cc",
        [
            ("NVIDIA GeForce RTX 4090", 24564, 24, (8, 9)),
            ("NVIDIA H100 80GB HBM3", 81559, 80, (9, 0)),
            ("NVIDIA A100-SXM4-40GB", 40536, 40, (8, 0)),
        ],
        ids=["4090-23.99", "h100-79.65", "a100-39.59"],
    )
    def test_driver_reported_sizes_land_in_the_nominal_tier(self, name, reported_mib, nominal_gb, cc) -> None:
        """A 23.99 GB card is the 24 GB tier now: recommendations and optimizations both snap."""
        reported = _card(name, reported_mib / 1024, cc=cc)
        nominal = _card(name, nominal_gb, cc=cc)
        budget = gpu._memory_budget([reported], RAM_64)
        assert budget.total_gb < nominal_gb <= budget.total_gb + lm.TIER_SNAP_GB
        assert lm.tier_for(budget) is lm.tier_for(float(nominal_gb))

        _, recs_reported = _recommend([reported], RAM_64)
        _, recs_nominal = _recommend([nominal], RAM_64)
        assert [(r.model, r.tier) for r in recs_reported] == [(r.model, r.tier) for r in recs_nominal]

        opt_reported, opt_nominal = _optimize([reported], RAM_64), _optimize([nominal], RAM_64)
        assert (opt_reported.recommended_ctx, opt_reported.num_parallel, opt_reported.recommended_quant) == (
            opt_nominal.recommended_ctx, opt_nominal.num_parallel, opt_nominal.recommended_quant,
        ) == (lm.num_ctx_for(budget), lm.num_parallel_for(budget), lm.quant_for(budget))

    # ---- every tag gpu.py can emit ----

    def test_every_emitted_tag_is_in_the_table(self) -> None:
        """Sweep budgets x pool types x architectures x RAM x GPU counts: every tag recommend_models
        can emit is a registry-verified table tag, no retired tag survives, and the tier words stay
        in the vocabulary consumers read; get_ollama_optimizations agrees with the table throughout."""
        budgets = (0, 2, 3.9, 4, 6, 8, 11, 12, 15, 16, 20, 23.99, 24, 32, 39.59, 40, 47, 48, 64, 79.65, 80, 95, 96, 112, 128, 192)
        capabilities = ((0, 0), (7, 5), (8, 0), (8, 6), (8, 9), (9, 0), (10, 0), (12, 1))
        emitted_tags: set[str] = set()
        emitted_tiers: set[str] = set()
        for vram_gb in budgets:
            for cc in capabilities:
                for unified in (False, True):
                    for sys_mem in (RAM_16, RAM_64):
                        for count in (1, 2):
                            name = "NVIDIA GB10" if unified else "NVIDIA GPU"
                            gpus = [_card(name, vram_gb, cc=cc, unified=unified, index=i) for i in range(count)] if vram_gb else []
                            budget, recs = _recommend(gpus, sys_mem)
                            emitted_tags |= {r.model for r in recs}
                            emitted_tiers |= {r.tier for r in recs}
                            for rec in recs:
                                assert rec.vram_required_gb == lm.pick_for_tag(rec.model).runtime_gb
                            opt = _optimize(gpus, sys_mem)
                            assert (opt.recommended_ctx, opt.num_parallel, opt.recommended_quant) == (
                                lm.num_ctx_for(budget), lm.num_parallel_for(budget), lm.quant_for(budget),
                            ), (vram_gb, cc, unified, sys_mem, count)

        assert emitted_tags <= set(lm.all_tags()), emitted_tags - set(lm.all_tags())
        retired = {t for t in emitted_tags if t.startswith(RETIRED_TAG_PREFIXES)}
        assert not retired, retired
        assert emitted_tiers <= {*LEGACY_BANDS, *(f"{b}-hybrid" for b in LEGACY_BANDS), "vision", "embed", "multi-gpu"}
        assert {"mini", "small", "medium", "full", "vision", "embed", "multi-gpu"} <= emitted_tiers
        assert any(t.endswith("-hybrid") for t in emitted_tiers)

    def test_unified_memory_note_names_the_tables_moe_picks(self) -> None:
        budget = gpu._memory_budget([_card("NVIDIA GB10", 128, unified=True, cc=(12, 1))], RAM_128)
        note = gpu.unified_memory_note(budget)
        moe = [p.tag for p in lm.recommended(budget) if p.moe]
        assert moe and all(tag in note for tag in moe)
        assert f"{budget.total_gb:.0f} GB LPDDR5x" in note
        assert f"~{gpu.UNIFIED_MEMORY_BANDWIDTH_GBPS} GB/s" in note
        assert f"leaving ~{budget.budget_gb:.0f} GB for models" in note
        assert "not an extra CPU-offload pool" in note

        # A pool too small for any MoE pick says so instead of naming one -- and it loses what a
        # 16 GB pool actually reserves (4 GB), not the GB10's 16.
        tiny = gpu._memory_budget([_card("NVIDIA GB10", 16, unified=True, cc=(12, 1))], RAM_16)
        assert (tiny.os_reserve_gb, tiny.budget_gb) == (4.0, 12.0)
        assert not any(p.moe for p in lm.recommended(tiny))
        tiny_note = gpu.unified_memory_note(tiny)
        assert "no MoE model fits this budget yet" in tiny_note
        assert "~4 GB is reserved for the OS" in tiny_note and "leaving ~12 GB for models" in tiny_note


class TestGPUDetection:
    def test_detect_system_memory_fields(self):
        from nvh.utils.gpu import detect_system_memory
        mem = detect_system_memory()
        assert hasattr(mem, "total_ram_gb")
        assert hasattr(mem, "available_ram_gb")
        assert mem.total_ram_gb >= 0

    def test_recommend_models_no_gpu(self):
        from nvh.utils.gpu import recommend_models
        recs = recommend_models(gpus=None)
        assert isinstance(recs, list)

    def test_recommend_models_empty_list(self):
        from nvh.utils.gpu import recommend_models
        recs = recommend_models(gpus=[])
        assert isinstance(recs, list)

    def test_detect_gpus_returns_list(self):
        from nvh.utils.gpu import detect_gpus
        result = detect_gpus()
        assert isinstance(result, list)

    def test_detect_system_memory(self):
        from nvh.utils.gpu import detect_system_memory
        mem = detect_system_memory()
        assert mem is not None
        assert hasattr(mem, "total_ram_gb") or hasattr(mem, "total_gb") or isinstance(mem, dict)

    def test_recommend_models(self):
        import pytest

        try:
            from nvh.utils.gpu import recommend_models
            recs = recommend_models(vram_gb=24)
            assert isinstance(recs, (list, dict))
        except (ImportError, TypeError):
            pytest.skip("recommend_models not available or different signature")


class TestCheckOomRisk:
    """check_oom_risk: a unified pool is ONE pool; discrete GPUs keep the pre-Spark numbers."""

    @staticmethod
    def _gpu(name: str, vram_mb: int, free_mb: int, *, unified: bool = False):
        return gpu.GPUInfo(
            name=name,
            vram_mb=vram_mb,
            vram_gb=round(vram_mb / 1024, 1),
            driver_version="580.65",
            cuda_version="13.0",
            utilization_pct=0,
            memory_used_mb=vram_mb - free_mb,
            memory_free_mb=free_mb,
            index=0,
            unified_memory=unified,
        )

    def test_unified_pool_is_not_counted_twice(self, monkeypatch):
        """40 GB model, GB10 with 30 GB MemAvailable: not hybrid, not safe, RAM not re-reported."""
        # memory_free_mb already IS MemAvailable; detect_system_memory must not be consulted.
        def boom():
            raise AssertionError("system RAM must not be read on a unified pool")

        monkeypatch.setattr(gpu, "detect_system_memory", boom)
        spark = self._gpu("NVIDIA GB10", 128 * 1024, 30 * 1024, unified=True)

        result = gpu.check_oom_risk(40.0, [spark])

        assert result["unified_memory"] is True
        assert result["gpu_free_gb"] == 30.0
        assert result["ram_free_gb"] == 0.0
        assert result["safe"] is False
        assert result["fits_gpu"] is False
        assert result["fits_hybrid"] is False
        assert "hybrid" not in result["recommendation"].lower()
        assert "unified memory pool" in result["recommendation"]
        assert "Short by 14 GB" in result["recommendation"]   # 40 - 30 * 0.85 = 14.5 → "14"

    def test_unified_pool_fit_is_still_reported(self, monkeypatch):
        monkeypatch.setattr(gpu, "detect_system_memory", lambda: gpu.SystemMemoryInfo(128.0, 90.0, 63.0))
        spark = self._gpu("NVIDIA GB10", 128 * 1024, 90 * 1024, unified=True)

        result = gpu.check_oom_risk(40.0, [spark])

        assert result["safe"] is True and result["fits_gpu"] is True and result["fits_hybrid"] is False
        assert result["ram_free_gb"] == 0.0
        assert result["recommendation"] == (
            "Model fits in unified memory (40 GB needed, 90 GB free) — full GPU acceleration"
        )

    def test_discrete_gpu_numbers_and_messages_unchanged(self, monkeypatch):
        """RTX 4090 with 24 GB free + 64 GB RAM (48 avail, 33.6 effective): byte-identical to HEAD."""
        monkeypatch.setattr(gpu, "detect_system_memory", lambda: gpu.SystemMemoryInfo(64.0, 48.0, 33.6))
        rtx = self._gpu("NVIDIA GeForce RTX 4090", 24576, 24576)

        fits = gpu.check_oom_risk(15.0, [rtx])
        hybrid = gpu.check_oom_risk(40.0, [rtx])
        oom = gpu.check_oom_risk(60.0, [rtx])

        for r in (fits, hybrid, oom):
            assert r["unified_memory"] is False
            assert r["gpu_free_gb"] == 24.0
            assert r["ram_free_gb"] == 48.0

        assert (fits["safe"], fits["fits_gpu"], fits["fits_hybrid"]) == (True, True, False)
        assert fits["recommendation"] == "Model fits in GPU VRAM (15 GB needed, 24 GB free) — full GPU acceleration"

        assert (hybrid["safe"], hybrid["fits_gpu"], hybrid["fits_hybrid"]) == (True, False, True)
        assert hybrid["recommendation"] == (
            "Model needs hybrid mode: 20 GB on GPU + 20 GB on CPU RAM. Expect 30-50% slower than full GPU"
        )

        assert (oom["safe"], oom["fits_gpu"], oom["fits_hybrid"]) == (False, False, False)
        assert oom["recommendation"] == (
            "OOM RISK: Model needs 60 GB but only 20 GB GPU + 34 GB RAM available. "
            "Short by 6 GB. Use a smaller model or lower quantization"
        )

    def test_no_gpu_still_uses_system_ram(self, monkeypatch):
        monkeypatch.setattr(gpu, "detect_system_memory", lambda: gpu.SystemMemoryInfo(16.0, 12.0, 8.4))

        result = gpu.check_oom_risk(5.0, [])

        assert result["unified_memory"] is False
        assert result["gpu_free_gb"] == 0.0 and result["ram_free_gb"] == 12.0
        assert result["fits_hybrid"] is True


class TestSmiFallbackMemo:
    """D3: when NVML enumerates the GPU but cannot size it (or is absent), detect_gpu_status runs
    the nvidia-smi fallback — the row query, bare ``nvidia-smi`` for the CUDA header and
    ``--query-gpu=compute_cap``: three subprocesses — and used to run them on *every* call.
    Status is polled in bursts; within SMI_FALLBACK_TTL_S the fallback now runs once."""

    _ROW = "0, NVIDIA GeForce RTX 4090, 24564, 1000, 23564, 5 %, 580.65\n"
    _NA_ROW = "0, NVIDIA GeForce RTX 4090, [N/A], [N/A], [N/A], 0 %, 580.65\n"

    @staticmethod
    def _fake_smi(monkeypatch, stdout: str) -> list[list[str]]:
        """A fake nvidia-smi binary answering all three queries; returns the spawn log."""
        spawned: list[list[str]] = []

        def run(argv, *args, **kwargs):
            spawned.append(list(argv))
            if "--query-gpu=compute_cap" in argv:
                return SimpleNamespace(returncode=0, stdout="8.9\n", stderr="")
            if len(argv) == 1:  # bare nvidia-smi: the human-readable header
                return SimpleNamespace(returncode=0, stdout="| NVIDIA-SMI 580.65  Driver Version: 580.65  CUDA Version: 13.0 |\n", stderr="")
            return SimpleNamespace(returncode=0, stdout=stdout, stderr="")

        monkeypatch.setattr(gpu.subprocess, "run", run)
        monkeypatch.setattr(gpu.shutil, "which", lambda command: "/usr/bin/nvidia-smi")
        monkeypatch.setattr(gpu, "_nvidia_device_files_present", lambda: True)
        monkeypatch.setattr(gpu, "detect_system_memory", lambda: gpu.SystemMemoryInfo(64.0, 48.0, 33.6))
        return spawned

    def test_burst_of_status_calls_spawns_the_fallback_once(self, monkeypatch):
        monkeypatch.setattr(gpu, "_detect_gpus_pynvml", lambda *, issues=None: [_row("NVIDIA GeForce RTX 4090", 0)])
        spawned = self._fake_smi(monkeypatch, self._ROW)

        first = gpu.detect_gpu_status()
        assert first["status"] == "ready" and first["source"] == "nvidia-smi"
        assert len(spawned) == 3  # the first call is exactly the old call: query, header, compute_cap

        second, third = gpu.detect_gpu_status(), gpu.detect_gpu_status()
        assert len(spawned) == 3  # ...and the rest of the burst respawns nothing
        for status in (second, third):
            assert status["status"] == "ready" and status["source"] == "nvidia-smi"
            assert [(g.name, g.vram_mb, g.memory_free_mb, g.compute_capability, g.cuda_version) for g in status["gpus"]] == [
                ("NVIDIA GeForce RTX 4090", 24564, 23564, (8, 9), "13.0"),
            ]
            assert status["summary"] == "1 GPU ready: NVIDIA GeForce RTX 4090, 24 GB VRAM"
        assert second["gpus"][0] is not first["gpus"][0]  # callers never share the memo's row objects

    def test_memo_expires_after_the_ttl(self, monkeypatch):
        monkeypatch.setattr(gpu, "_detect_gpus_pynvml", lambda *, issues=None: [])
        spawned = self._fake_smi(monkeypatch, self._ROW)
        clock = [1000.0]
        monkeypatch.setattr(gpu, "time", SimpleNamespace(monotonic=lambda: clock[0]))

        gpu.detect_gpu_status()
        clock[0] += gpu.SMI_FALLBACK_TTL_S - 0.5
        gpu.detect_gpu_status()
        assert len(spawned) == 3
        clock[0] += 1.0  # past the TTL: a fresh fallback
        assert gpu.detect_gpu_status()["status"] == "ready"
        assert len(spawned) == 6

    def test_memo_carries_the_fallback_issues(self, monkeypatch):
        """A memoised fallback must explain itself exactly like a fresh one: the row-scoped
        memory-unavailable issue (and its index) survives, as separate dicts per caller."""
        monkeypatch.setattr(gpu, "_detect_gpus_pynvml", lambda *, issues=None: [])
        spawned = self._fake_smi(monkeypatch, self._NA_ROW)

        first, second = gpu.detect_gpu_status(), gpu.detect_gpu_status()

        assert len(spawned) == 3
        for status in (first, second):
            assert status["status"] == "blocked" and status["source"] == "nvidia-smi"
            assert [(g.name, g.vram_mb) for g in status["gpus"]] == [("NVIDIA GeForce RTX 4090", 0)]
            assert [(i["source"], i["code"], i["index"]) for i in status["issues"]] == [
                ("nvidia-smi", "memory-unavailable", 0),
            ]
        assert second["issues"][0] is not first["issues"][0]

    def test_memo_carries_a_failed_fallback_too(self, monkeypatch):
        """nvidia-smi missing: the binary-missing note is still reported on the memoised call."""
        monkeypatch.setattr(gpu, "_detect_gpus_pynvml", lambda *, issues=None: [])
        monkeypatch.setattr(gpu, "_nvidia_device_files_present", lambda: False)
        monkeypatch.setattr(gpu.shutil, "which", lambda command: None)
        which_calls: list[int] = []
        real_smi = gpu._detect_gpus_smi
        monkeypatch.setattr(gpu, "_detect_gpus_smi", lambda *, issues=None: which_calls.append(1) or real_smi(issues=issues))

        first, second = gpu.detect_gpu_status(), gpu.detect_gpu_status()

        assert which_calls == [1]
        for status in (first, second):
            assert status["status"] == "not-detected"
            assert [i["code"] for i in status["issues"]] == ["binary-missing"]

    def test_clear_forces_a_fresh_fallback(self, monkeypatch):
        monkeypatch.setattr(gpu, "_detect_gpus_pynvml", lambda *, issues=None: [])
        spawned = self._fake_smi(monkeypatch, self._ROW)

        gpu.detect_gpu_status()
        gpu.clear_gpu_detection_cache()
        gpu.detect_gpu_status()
        assert len(spawned) == 6

    def test_direct_fallback_calls_are_not_memoised(self, monkeypatch):
        """Only detect_gpu_status coalesces; _detect_gpus_smi itself still answers fresh."""
        spawned = self._fake_smi(monkeypatch, self._ROW)
        gpu._detect_gpus_smi(issues=[])
        gpu._detect_gpus_smi(issues=[])
        assert len(spawned) == 6


class TestFormatGpuMemory:
    """D4: one spelling of a row's memory for every CLI/UI label — never '0 GB VRAM' for a
    GPU whose pool could not be read."""

    def test_discrete_row(self):
        assert gpu.format_gpu_memory(_row("NVIDIA GeForce RTX 4090", 24576)) == "24 GB VRAM"

    def test_unified_row(self):
        assert gpu.format_gpu_memory(_row("NVIDIA GB10", 131072, unified=True)) == "128 GB unified"

    def test_unreadable_row_is_never_zero_gb(self):
        text = gpu.format_gpu_memory(_row("NVIDIA GeForce RTX 4090", 0))
        assert text == "memory unreadable"
        assert "0" not in text and "GB" not in text

    def test_precision_and_compact_spellings(self):
        row = _row("NVIDIA GeForce RTX 4090", 24576)
        assert gpu.format_gpu_memory(row, precision=1) == "24.0 GB VRAM"
        assert gpu.format_gpu_memory(row, compact=True) == "24GB VRAM"
        assert gpu.format_gpu_memory(_row("NVIDIA GB10", 131072, unified=True), compact=True) == "128GB unified"
        assert gpu.format_gpu_memory(_row("x", 0), compact=True, precision=1) == "memory unreadable"

    def test_rounding_matches_the_old_f_strings(self):
        """The CLI sites printed f'{vram_gb:.0f} GB VRAM'; readable rows must render byte-identically."""
        for vram_mb in (10240 + 512, 12288, 24564, 81920):
            row = _row("NVIDIA GPU", vram_mb)
            assert gpu.format_gpu_memory(row) == f"{row.vram_gb:.0f} GB VRAM"
            assert gpu.format_gpu_memory(row, compact=True) == f"{row.vram_gb:.0f}GB VRAM"
