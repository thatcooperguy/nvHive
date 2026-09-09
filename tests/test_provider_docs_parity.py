"""docs/PROVIDERS.md's provider table and free-tier ladder are generated from nvh.providers.specs (derive, don't type).

scripts/gen_providers_doc.py fills two marker-delimited blocks from the spec
table; the committed file must match byte-for-byte so a new spec row, a
renamed key variable or a changed free-tier note fails CI instead of drifting
in the doc. The same spec rows derive the free-tier ladder, the quota rows,
the advisor cards and the proxy's model map, so this file also pins those
derivations to the table.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest

import nvh.api.proxy as proxy
from nvh.core.advisor_profiles import ADVISOR_PROFILES, AdvisorProfile
from nvh.core.free_tier import FREE_TIER_ADVISORS
from nvh.providers.quota_info import PROVIDER_QUOTAS, format_rate_limit_message
from nvh.providers.registry import BESPOKE_ADAPTERS, RETIRED_PROVIDERS
from nvh.providers.specs import BESPOKE_SPECS, PROVIDER_FACTS, PROVIDER_SPECS, free_tier_ladder

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "gen_providers_doc.py"
DOC = ROOT / "docs" / "PROVIDERS.md"


@pytest.fixture(scope="module")
def generator():
    spec = importlib.util.spec_from_file_location("gen_providers_doc", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def doc() -> str:
    return DOC.read_text(encoding="utf-8").replace("\r\n", "\n")


def _rows(table: str) -> list[str]:
    """The body rows of a markdown table block (header and rule dropped)."""
    lines = [line for line in table.strip().splitlines() if line.startswith("|")]
    return lines[2:]


# --- the generated doc ----------------------------------------------------------


def test_check_mode_reports_current(generator, capsys):
    assert generator.main(["--check"]) == 0
    assert "is current" in capsys.readouterr().out


def test_committed_doc_matches_generator(generator, doc: str):
    assert generator.render(doc) == doc, "docs/PROVIDERS.md is stale — run: python scripts/gen_providers_doc.py"


def test_every_block_has_one_marker_pair(generator, doc: str):
    assert set(generator.BLOCKS) == {"providers", "free-tier-ladder"}
    for name in generator.BLOCKS:
        assert doc.count(generator.BEGIN.format(name=name)) == 1, name
        assert doc.count(generator.END.format(name=name)) == 1, name
        assert generator.block_text(doc, name).strip(), name


def test_stale_doc_fails_check_and_is_repaired(generator, tmp_path, monkeypatch, doc: str):
    end = generator.END.format(name="providers")
    stale = tmp_path / "PROVIDERS.md"
    stale.write_text(doc.replace(end, "| stale | `row` | — | — | — |\n" + end), encoding="utf-8")
    monkeypatch.setattr(generator, "DOC_PATH", stale)
    assert generator.main(["--check"]) == 1
    # And the non-check form repairs it.
    assert generator.main([]) == 0
    assert stale.read_text(encoding="utf-8") == doc


def test_doubled_marker_is_refused(generator, doc: str):
    begin = generator.BEGIN.format(name="providers")
    with pytest.raises(SystemExit, match="exactly one"):
        generator.render(doc.replace(begin, begin + "\n" + begin))


def test_every_provider_appears_exactly_once(generator, doc: str):
    table = generator.block_text(doc, "providers")
    for name in set(PROVIDER_SPECS) | set(BESPOKE_ADAPTERS):
        assert table.count(f"| `{name}` |") == 1, name
    assert len(_rows(table)) == len(PROVIDER_FACTS)


def test_table_rows_carry_the_spec_facts(generator, doc: str):
    table = generator.block_text(doc, "providers")
    for row in _rows(table):
        name = re.search(r"\| `([a-z0-9_]+)` \|", row).group(1)
        spec = PROVIDER_FACTS[name]
        assert row.startswith(f"| {spec.display_name} |"), name
        if spec.key_env:
            assert f"`{spec.key_env}`" in row, name
            for alt in spec.alt_key_envs:
                assert f"`{alt}`" in row, (name, alt)
        else:
            assert "| — |" in row, name
        if spec.free_tier:
            assert spec.free_info in row, name
        else:
            assert "| paid" in row, name
        if spec.default_model:
            assert f"`{spec.default_model}`" in row, name


def test_table_order_no_key_then_free_then_paid_then_bespoke(generator):
    names = [spec.name for spec in generator.doc_order()]
    assert names[:2] == ["ollama", "llm7"]
    keyed_free = [a.name for a in FREE_TIER_ADVISORS if a.check_fn == "env"]
    assert names[2:2 + len(keyed_free)] == keyed_free
    assert names[-2:] == ["triton", "mock"]
    paid = names[2 + len(keyed_free):-2]
    assert paid == [n for n, s in PROVIDER_SPECS.items() if not s.free_tier]


def test_retired_providers_are_absent_from_the_table(generator, doc: str):
    table = generator.block_text(doc, "providers")
    for name in RETIRED_PROVIDERS:
        assert name not in table, name
    assert "GITHUB_TOKEN" not in doc


def test_ladder_block_lists_the_free_tier_advisors_in_order(generator, doc: str):
    block = generator.block_text(doc, "free-tier-ladder")
    lines = [line for line in block.strip().splitlines() if line.strip()]
    assert len(lines) == len(FREE_TIER_ADVISORS)
    for line, advisor in zip(lines, FREE_TIER_ADVISORS, strict=True):
        spec = PROVIDER_FACTS[advisor.name]
        assert line.startswith(f"{advisor.priority}. **{spec.display_name}** (`{advisor.name}`)"), advisor.name
        assert advisor.daily_limit in line, advisor.name
        if advisor.check_fn == "env":
            assert f"`{advisor.env_var}`" in line, advisor.name


def test_doc_no_longer_claims_hand_maintenance_or_the_legacy_env(doc: str):
    assert "hand-maintained" not in doc
    assert "~/.hive/.env" not in doc
    assert "$NVH_HOME/config/.env" in doc


# --- the spec table itself ----------------------------------------------------


def test_bespoke_rows_match_the_registry_adapters():
    assert set(BESPOKE_SPECS) == set(BESPOKE_ADAPTERS)
    assert not set(BESPOKE_SPECS) & set(PROVIDER_SPECS)
    assert list(PROVIDER_FACTS) == list(PROVIDER_SPECS) + list(BESPOKE_SPECS)


def test_every_spec_has_the_descriptive_facts():
    for name, spec in PROVIDER_FACTS.items():
        assert spec.display_name, name
        assert spec.cost_tier in {"free", "budget", "standard", "premium"}, name
        if name in PROVIDER_SPECS:
            assert spec.key_env.endswith("_API_KEY"), name
            assert spec.signup_url.startswith("https://"), name
            assert spec.strengths and spec.weaknesses and spec.best_for and spec.avoid_for, name
            for weight in (spec.quality_weight, spec.speed_weight, spec.cost_weight, spec.reliability_weight):
                assert 0.0 <= weight <= 1.0, name
        if spec.free_tier and name in PROVIDER_SPECS:
            assert spec.free_info, name
            assert spec.cost_tier in {"free", "budget"}, name


def test_key_env_derives_from_the_name_unless_overridden():
    for name, spec in PROVIDER_SPECS.items():
        assert spec.key_env == ("XAI_API_KEY" if name == "grok" else f"{name.upper()}_API_KEY"), name
    assert BESPOKE_SPECS["ollama"].key_env == "" and BESPOKE_SPECS["mock"].key_env == ""
    assert BESPOKE_SPECS["triton"].key_env == "TRITON_URL"
    assert "HIVE_HUGGINGFACE_API_KEY" not in PROVIDER_SPECS["huggingface"].alt_key_envs
    assert PROVIDER_SPECS["huggingface"].alt_key_envs == ("HF_TOKEN",)


# --- the derived tables -------------------------------------------------------


def test_free_tier_ladder_ranks_are_unique_and_cover_every_free_cloud_spec():
    ladder = free_tier_ladder()
    ranks = [spec.free_tier_rank for spec in ladder]
    assert ranks == sorted(ranks) and len(set(ranks)) == len(ranks)
    assert ladder[0].name == "ollama"
    for name, spec in PROVIDER_SPECS.items():
        assert (spec.free_tier_rank > 0) is spec.free_tier, name
    assert [a.name for a in FREE_TIER_ADVISORS] == [spec.name for spec in ladder]
    for advisor, spec in zip(FREE_TIER_ADVISORS, ladder, strict=True):
        assert advisor.priority == spec.free_tier_rank
        assert advisor.daily_limit == spec.free_info
        assert advisor.env_var == (spec.key_env or "")
        assert advisor.check_fn == ("ollama" if spec.name == "ollama" else "anonymous" if spec.anonymous_key else "env")


def test_quota_rows_cover_every_spec_and_follow_its_tier():
    assert set(PROVIDER_QUOTAS) == set(PROVIDER_FACTS)
    for name, quota in PROVIDER_QUOTAS.items():
        spec = PROVIDER_FACTS[name]
        assert quota.provider == name
        assert quota.tier == ("free" if spec.is_local else spec.quota_tier), name
        assert quota.upgrade_url == spec.upgrade_url, name
        if spec.free_tier and not spec.is_local:
            assert quota.limit_description == spec.free_info, name
        # A row's own reset / upgrade sentence wins over the tier's; no URL, no upgrade sentence.
        if spec.reset_hint:
            assert quota.reset_hint == spec.reset_hint, name
        if spec.upgrade_hint:
            assert quota.upgrade_hint == spec.upgrade_hint, name
        elif not spec.upgrade_url:
            assert quota.upgrade_hint == "", name
        assert "  " not in quota.upgrade_hint and not quota.upgrade_hint.endswith(" "), name
    assert PROVIDER_QUOTAS["llm7"].tier == "anonymous"
    assert PROVIDER_QUOTAS["openai"].tier == "paid"
    assert PROVIDER_QUOTAS["openai"].upgrade_url == PROVIDER_SPECS["openai"].billing_url
    # The provider-specific facts the tier templates cannot express survive.
    assert "midnight PT" in PROVIDER_QUOTAS["google"].reset_hint and "tokens/day" in PROVIDER_QUOTAS["google"].limit_description
    assert "don't expire" in PROVIDER_QUOTAS["nvidia"].reset_hint
    assert "input tokens" in PROVIDER_QUOTAS["deepseek"].limit_description
    assert PROVIDER_QUOTAS["ollama"].upgrade_hint.startswith("Pull more models: nvh models pull")
    assert "nvh models pull" not in PROVIDER_QUOTAS["triton"].upgrade_hint
    assert PROVIDER_QUOTAS["mock"].upgrade_hint == "" and "Upgrade" not in format_rate_limit_message("mock", "429")


def test_advisor_profiles_are_the_spec_rows_with_card_prose():
    """ADVISOR_PROFILES holds the ProviderSpec rows themselves — no second card dataclass."""
    assert set(ADVISOR_PROFILES) == {name for name, spec in PROVIDER_FACTS.items() if spec.strengths}
    assert set(PROVIDER_SPECS) | {"ollama"} <= set(ADVISOR_PROFILES)
    for name, profile in ADVISOR_PROFILES.items():
        spec = PROVIDER_FACTS[name]
        assert profile is spec, name
        assert isinstance(profile, AdvisorProfile)
        # The card-side spellings are properties on the spec.
        assert profile.has_free_tier is spec.free_tier
        assert profile.free_tier_limits == spec.free_info
    # A paid provider's access note reaches the card (``nvh advisor info together``).
    assert ADVISOR_PROFILES["together"].free_tier_limits == "Requires $5 minimum purchase"
    assert ADVISOR_PROFILES["together"].has_free_tier is False


def test_proxy_routes_every_spec_default_to_its_provider():
    for name, spec in PROVIDER_SPECS.items():
        assert proxy.resolve_provider_from_model(spec.default_model) == (name, spec.default_model), name
        for family in spec.model_prefixes:
            assert proxy.resolve_provider_from_model(f"{family}-x")[0] == name, (name, family)


def test_proxy_leaves_shared_ids_and_unknowns_to_the_router():
    # "openai/gpt-oss-120b" is the bare form of four specs' defaults and
    # LiteLLM's generic client id: nobody's exact match, never OpenAI's.
    assert "openai/gpt-oss-120b" not in proxy._MODEL_TO_PROVIDER
    assert proxy.resolve_provider_from_model("openai/gpt-oss-120b") == (None, "openai/gpt-oss-120b")
    assert proxy.resolve_provider_from_model("gpt-oss-120b") == (None, "gpt-oss-120b")
    proxy._litellm_provider.cache_clear()  # the one process cache this module keeps
    assert proxy.resolve_provider_from_model("mystery-model") == (None, "mystery-model")
    assert proxy._litellm_provider.cache_info().currsize == 1
