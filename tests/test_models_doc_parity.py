"""docs/MODELS.md's tables are generated from nvh.core.local_models (derive, don't type).

scripts/gen_models_doc.py fills five marker-delimited blocks from the tier
table (and, for the DGX Spark baselines, from gpu_emulation); the committed
file must match byte-for-byte so a table edit, a moved boundary or a retired
tag fails CI instead of drifting in the doc.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest

import nvh.core.local_models as lm

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "gen_models_doc.py"
DOC = ROOT / "docs" / "MODELS.md"

# Names that left the registry or the table; none may come back as a pick.
RETIRED = (
    "nemotron-omni", "nemotron-3-nano-omni", "nemotron:70b", "nemotron-3-super", "nemotron-mini",
    "qwen2.5-coder", "qwen2.5", "minicpm-v", "llava", "bakllava", "llama3.3:70b", "llama3.1",
    "deepseek-r1", "codellama", "gemma2", "phi4", "mistral:7b", "llama3.2:3b",
)


def _mentions(tag: str, text: str) -> bool:
    return re.search(rf"(?<![A-Za-z0-9_.-]){re.escape(tag)}(?![A-Za-z0-9_.-])", text) is not None


@pytest.fixture(scope="module")
def generator():
    spec = importlib.util.spec_from_file_location("gen_models_doc", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def doc() -> str:
    return DOC.read_text(encoding="utf-8").replace("\r\n", "\n")


def test_check_mode_reports_current(generator, capsys):
    assert generator.main(["--check"]) == 0
    assert "is current" in capsys.readouterr().out


def test_committed_doc_matches_generator(generator, doc: str):
    assert generator.render(doc) == doc, "docs/MODELS.md is stale — run: python scripts/gen_models_doc.py"


def test_every_block_has_one_marker_pair(generator, doc: str):
    assert set(generator.BLOCKS) == {
        "local-model-tiers", "unified-os-reserve", "wizard-chat-matrix", "installer-pull-ladder",
        "gb10-baselines",
    }
    for name in generator.BLOCKS:
        assert doc.count(generator.BEGIN.format(name=name)) == 1, name
        assert doc.count(generator.END.format(name=name)) == 1, name
        assert generator.block_text(doc, name).strip(), name


def test_tier_block_is_the_table_renderer(generator, doc: str):
    block = generator.block_text(doc, "local-model-tiers")
    assert block.strip() == lm.tier_table_markdown().strip()
    for tag in lm.all_tags():
        assert f"`{tag}`" in block, tag
    for tier in lm.LOCAL_MODEL_TIERS:
        assert f"| {tier.range_label} | {tier.label} |" in block, tier.label


def test_no_retired_tag_in_the_ladders(generator, doc: str):
    # gb10-baselines is measured history and may name models that have since
    # left the table; the three ladders may not.
    for name in ("local-model-tiers", "wizard-chat-matrix", "installer-pull-ladder"):
        block = generator.block_text(doc, name)
        for retired in RETIRED:
            assert not _mentions(retired, block), (name, retired)


def test_install_chain_walks_chat_picks_down_to_the_cpu_fallback(generator):
    for n, tier in enumerate(lm.LOCAL_MODEL_TIERS):
        chain = generator.install_sh_chain(n)
        assert chain[0] == tier.picks["chat"].tag
        assert chain[-1] in {tier.picks["cpu_fallback"].tag, lm.LOCAL_MODEL_TIERS[0].picks["chat"].tag}
        assert len(chain) == len(set(chain))
        for tag in chain:
            assert lm.pick_for_tag(tag) is not None, tag
    # Every chain is the tier's chat pick followed by a strictly lower tier's.
    top = generator.install_sh_chain(len(lm.LOCAL_MODEL_TIERS) - 1)
    weights = [lm.pick_for_tag(t).weights_gb for t in top]
    assert weights == sorted(weights, reverse=True)


@pytest.mark.parametrize("tier", lm.LOCAL_MODEL_TIERS, ids=lambda t: t.label)
def test_unified_pool_lands_exactly_on_the_tier_floor(generator, tier):
    """The unified column is computed at the tier floor, not ``floor + 16`` (the flat GB10 reserve).

    The OS reserve scales with the pool (unified_os_reserve_gb: an eighth of
    it, 4-16 GB), so ``floor + 16`` overshoots every floor below 128 GB and
    landed the column one or two tiers high. The search must stop on the
    smallest half-gigabyte pool whose budget *is* the floor.
    """
    pool = generator.unified_pool_gb(tier.min_gb)
    budget = generator._unified_budget(tier)
    assert budget.unified and budget.offload_gb == 0.0
    assert budget.total_gb == pool
    assert budget.os_reserve_gb == lm.unified_os_reserve_gb(pool)
    assert budget.budget_gb == tier.min_gb, (tier.label, pool, budget.budget_gb)
    assert lm.tier_for(budget) is tier
    assert pool >= lm.UNIFIED_OS_RESERVE_MIN_GB and (pool * 2) == int(pool * 2)  # 0.5 GB grid
    if tier.min_gb > 0:
        # Smallest: half a gigabyte less does not reach the floor.
        assert generator.unified_budget_for_pool(pool - 0.5).budget_gb < tier.min_gb
        # And never above the flat-reserve pool the old renderer used.
        assert pool <= tier.min_gb + lm.UNIFIED_MEMORY_OS_RESERVE_GB


def test_installer_ladder_names_the_unified_pool_per_tier(generator, doc: str):
    block = generator.block_text(doc, "installer-pull-ladder")
    for tier in lm.LOCAL_MODEL_TIERS:
        pool = generator.unified_pool_gb(tier.min_gb)
        first = lm.recommended(generator._unified_budget(tier))[0].tag
        assert f"| {pool:g} GB pool: `{first}`" in block, (tier.label, pool, first)
    # A 16 GB pool plans against 12 GB (small-plus), not 0 (cpu) as the flat reserve had it.
    assert "| 12-16 | small-plus |" in block
    assert "| 16 GB pool: `qwen3:8b`" in block


def test_unified_reserve_block_follows_the_curve(generator, doc: str):
    block = generator.block_text(doc, "unified-os-reserve")
    for pool in generator.UNIFIED_POOLS_GB:
        reserve = lm.unified_os_reserve_gb(pool)
        tier = lm.tier_for(pool - reserve)
        assert f"| {pool:g} | {reserve:g} | {pool - reserve:g} | {tier.range_label} ({tier.label}) |" in block, pool
    # The two ends of the curve: a 16 GB laptop keeps 4, a 128 GB GB10 keeps 16.
    assert "| 16 | 4 | 12 |" in block
    assert f"| 128 | {lm.UNIFIED_MEMORY_OS_RESERVE_GB:g} | {128 - lm.UNIFIED_MEMORY_OS_RESERVE_GB:g} |" in block


def test_chat_matrix_covers_every_chat_and_vision_pick(generator, doc: str):
    block = generator.block_text(doc, "wizard-chat-matrix")
    for tier in lm.LOCAL_MODEL_TIERS:
        for use_case in ("chat", "vision"):
            assert f"`{tier.picks[use_case].tag}`" in block, (tier.label, use_case)
    # Vision-only picks are not pulled by install.sh and say how to get them.
    vision_only = {t.picks["vision"].tag for t in lm.LOCAL_MODEL_TIERS} - {
        t.picks["chat"].tag for t in lm.LOCAL_MODEL_TIERS
    }
    for tag in vision_only:
        assert f"`nvh models pull {tag}`" in block, tag


def test_gb10_block_uses_the_measured_baselines(generator, doc: str):
    from nvh.utils.gpu_emulation import _MEASURED_BASELINES

    block = generator.block_text(doc, "gb10-baselines")
    for (gpu, model), toks in _MEASURED_BASELINES.items():
        if gpu == "gb10":
            assert f"| `{model}` |" in block, model
            assert f"| {toks:g} |" in block, model


def test_stale_doc_fails_check(generator, tmp_path, monkeypatch, doc: str):
    end = generator.END.format(name="local-model-tiers")
    stale = tmp_path / "MODELS.md"
    stale.write_text(doc.replace(end, "| stale row |\n" + end), encoding="utf-8")
    monkeypatch.setattr(generator, "DOC_PATH", stale)
    assert generator.main(["--check"]) == 1
    # And the non-check form repairs it.
    assert generator.main([]) == 0
    assert stale.read_text(encoding="utf-8") == doc
