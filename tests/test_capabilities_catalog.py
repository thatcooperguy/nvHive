"""capabilities.yaml is the hand-maintained half of the model catalog; LiteLLM holds the rest.

Since 0.45 (refresh wave 2, F2) ``ProviderRegistry.load_capabilities`` reads
price, context and the tool / vision / JSON flags from LiteLLM's model table
(``litellm.model_cost``, with ``nvh/config/model_overrides.json`` registered
into it for the spec defaults LiteLLM does not carry) and takes only the
hand-maintained fields and explicit, ``source:``-annotated overrides from
``nvh/config/capabilities.yaml``. These tests pin:

* every ``ProviderSpec`` default and fallback prices on whatever LiteLLM is
  installed -- in the bundled table, through model_overrides.json, by the
  provider's ``zero_cost`` flag, or as a sourced YAML row with explicit
  rates -- no skip; a ``metered_upstream`` provider (Hugging Face) is the
  one exemption and must load as 'cost unknown' instead of a known $0;
* every YAML row that hand-types an override of a LiteLLM-read fact carries
  a ``source:`` (the generator's derived fields need none), and every
  model_overrides.json payload does too and names a current spec id;
* the retired rows are exactly the ``RETIRED_MODEL_RENAMES`` sources present
  in the file, each with its successor;
* ``scripts/gen_capabilities.py --check`` round-trips the committed file,
  ignores the LiteLLM version its header records, and repairs a stale copy;
* the loader's precedence (YAML override > LiteLLM > defaults), zero-cost
  pricing, ``cost_known`` plus the once-per-model log for unpriced rows, the
  retired status, and that the Perplexity presets are never registered with
  LiteLLM (``litellm.cost_per_token`` must keep raising for them).

Hermetic: ``NVH_HOME`` is ``tmp_path``; the table is the one bundled with the
installed LiteLLM (conftest sets ``LITELLM_LOCAL_MODEL_COST_MAP``); a catalog
load opens no socket. Reset hooks: ``registry.reset_registry()``,
``registry.reset_catalog_state()``, ``openai_compatible._catalog.cache_clear()``.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import socket
from datetime import date
from decimal import Decimal
from pathlib import Path

import httpx
import litellm
import pytest
import yaml

import nvh.core.local_models as lm
from nvh.cli.setup import RETIRED_MODEL_RENAMES
from nvh.providers import openai_compatible as oc
from nvh.providers import registry
from nvh.providers.base import ModelInfo
from nvh.providers.registry import ProviderRegistry
from nvh.providers.specs import PROVIDER_FACTS, PROVIDER_SPECS

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "gen_capabilities.py"
CATALOG = ROOT / "nvh" / "config" / "capabilities.yaml"
OVERRIDES = ROOT / "nvh" / "config" / "model_overrides.json"


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch, tmp_path):
    monkeypatch.setenv("NVH_HOME", str(tmp_path))
    registry.reset_registry()
    registry.reset_catalog_state()
    oc._catalog.cache_clear()
    yield
    registry.reset_registry()
    registry.reset_catalog_state()
    oc._catalog.cache_clear()


@pytest.fixture(scope="module")
def generator():
    spec = importlib.util.spec_from_file_location("gen_capabilities", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def catalog_text() -> str:
    return CATALOG.read_text(encoding="utf-8").replace("\r\n", "\n")


@pytest.fixture(scope="module")
def rows(catalog_text) -> dict[str, dict]:
    return yaml.safe_load(catalog_text)["models"]


@pytest.fixture(scope="module")
def overrides() -> dict[str, dict]:
    return json.loads(OVERRIDES.read_text(encoding="utf-8"))["models"]


@pytest.fixture
def bundled_table(monkeypatch) -> dict:
    """LiteLLM's table as shipped in the installed wheel, before anything nvHive registered."""
    monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "True")
    return litellm.get_model_cost_map(litellm.model_cost_map_url)


def _spec_ids() -> dict[str, str]:
    ids: dict[str, str] = {}
    for name, spec in sorted(PROVIDER_SPECS.items()):
        for field in ("default_model", "fallback_model"):
            ids.setdefault(getattr(spec, field), f"{name}.{field}")
    return ids


def _loaded() -> ProviderRegistry:
    reg = ProviderRegistry()
    reg.load_capabilities()
    return reg


def _write_catalog(tmp_path: Path, models: dict) -> Path:
    path = tmp_path / "catalog.yaml"
    path.write_text(yaml.safe_dump({"models": models}, sort_keys=False), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Every spec default and fallback prices somewhere, on whatever LiteLLM is installed
# ---------------------------------------------------------------------------


def test_every_spec_default_and_fallback_has_a_price_source(bundled_table, rows, overrides):
    unpriced = []
    for model_id, where in _spec_ids().items():
        row = rows.get(model_id)
        spec = PROVIDER_FACTS[where.split(".")[0]]
        if spec.metered_upstream:
            continue  # priced on the provider's own account: 'cost unknown' by design (below)
        priced_row = row is not None and (
            spec.zero_cost or (bool(row.get("source")) and "input_cost_per_1m_tokens" in row)
        )
        if not (model_id in bundled_table or model_id in overrides or priced_row):
            unpriced.append(f"{where} = {model_id}")
    assert not unpriced, (
        "spec defaults/fallbacks no source prices (add a model_overrides.json payload or a "
        f"sourced capabilities.yaml row): {unpriced}"
    )


def test_loaded_catalog_prices_every_spec_default_and_fallback():
    reg = _loaded()
    for model_id, where in _spec_ids().items():
        info = reg.get_model_info(model_id)
        assert info is not None, f"{where}: {model_id!r} has no catalog row"
        assert info.provider == where.split(".")[0], (model_id, info.provider)
        if PROVIDER_FACTS[info.provider].metered_upstream:
            # Known only once LiteLLM itself carries a row (then LiteLLM wins).
            assert info.cost_known is (model_id in litellm.model_cost), (model_id, info.cost_known)
        else:
            assert info.cost_known, f"{where}: {model_id!r} loaded as 'cost unknown'"
        assert info.status == "active", (model_id, info.status)


def test_metered_upstream_rows_load_cost_unknown_with_their_sourced_facts(rows, overrides, caplog):
    metered = [name for name, spec in PROVIDER_FACTS.items() if spec.metered_upstream]
    assert metered == ["huggingface"]
    with caplog.at_level(logging.INFO, logger="nvh.providers.registry"):
        reg = _loaded()
    for name in metered:
        spec = PROVIDER_FACTS[name]
        assert not spec.zero_cost
        for model_id in (spec.default_model, spec.fallback_model):
            # Never registered with LiteLLM: a huggingface/ payload would make
            # litellm.register_model fetch huggingface.co/<id>/config.json at load.
            assert model_id not in overrides, model_id
            row = rows[model_id]
            assert row.get("source") and "input_cost_per_1m_tokens" not in row, model_id
            info = reg.get_model_info(model_id)
            assert info.cost_known is False and info.input_cost_per_1m_tokens == 0, model_id
            assert info.context_window == row["context_window"] > 0
            assert info.supports_function_calling is True is info.supports_tools
            assert oc.model_supports_function_calling(model_id) is True
        # Expected state, so no warning: the once-per-model line is debug level.
        assert not [r for r in caplog.records if name in r.getMessage() and r.levelno >= logging.INFO]


def test_active_non_spec_rows_that_no_source_prices_are_only_reported_by_the_weekly_job(rows):
    """The one release gate is the spec defaults (above); other rows LiteLLM stops pricing are
    informational (``tests/test_model_currency.py`` lists them) so a LiteLLM release cannot fail CI."""
    reg = _loaded()
    unpriced_active = sorted(
        m.model_id for m in reg.list_models()
        if m.status != "retired" and not m.cost_known and not PROVIDER_FACTS[m.provider].metered_upstream
    )
    # Pinned on both LiteLLM versions this release was verified against (1.99.0, 1.106.6):
    # the three rows neither prices were retired in this wave, so none remain.
    assert unpriced_active == [], unpriced_active
    for model_id in ("gemini/gemini-2.0-pro", "groq/mixtral-8x7b-32768", "xai/grok-3"):
        assert rows[model_id]["status"] == "retired" and rows[model_id]["successor"], model_id


def test_loaded_catalog_prices_zero_cost_providers_at_zero_without_a_litellm_row():
    reg = _loaded()
    for name, spec in PROVIDER_FACTS.items():
        if not spec.zero_cost:
            continue
        for info in reg.get_models_for_provider(name):
            assert info.cost_known and info.input_cost_per_1m_tokens == 0 == info.output_cost_per_1m_tokens, (
                info.model_id
            )


# ---------------------------------------------------------------------------
# Provenance: every override has a source
# ---------------------------------------------------------------------------


def test_every_override_row_has_a_source_and_every_source_has_overrides(rows, generator):
    overridable = set(registry.CATALOG_OVERRIDE_FIELDS)
    for model_id, row in rows.items():
        derived = set(generator.DERIVED_OVERRIDES.get(row["provider"], ()))
        typed = (overridable & set(row)) - derived
        source = row.get("source", "")
        if typed:
            assert isinstance(source, str) and source.strip(), f"{model_id}: overrides {sorted(typed)} without source:"
        else:
            assert not source, f"{model_id}: source: without any hand-typed override field"
        if PROVIDER_FACTS[row["provider"]].zero_cost:
            assert not set(generator.PRICE_FIELDS) & set(row), f"{model_id}: a zero-cost row carries a price"
        unknown = set(row) - overridable - set(registry.CATALOG_OWN_FIELDS)
        assert not unknown, f"{model_id}: unknown fields {sorted(unknown)}"
        assert row["provider"] in PROVIDER_FACTS, (model_id, row["provider"])


def test_catalog_field_sets_cover_model_info():
    derived = {"model_id", "cost_known"}
    catalog_fields = (set(registry.CATALOG_OWN_FIELDS) - {"source"}) | set(registry.CATALOG_OVERRIDE_FIELDS)
    assert catalog_fields == set(ModelInfo.model_fields) - derived
    assert not set(registry.CATALOG_OWN_FIELDS) & set(registry.CATALOG_OVERRIDE_FIELDS)


def test_model_overrides_payloads_name_current_spec_ids_and_carry_sources(rows, overrides):
    spec_ids = set(_spec_ids())
    for model_id, payload in overrides.items():
        assert model_id in spec_ids, f"model_overrides.json row {model_id!r} is not a current spec default/fallback"
        assert payload.get("source", "").strip(), model_id
        for key in ("litellm_provider", "mode", "input_cost_per_token", "output_cost_per_token"):
            assert key in payload, (model_id, key)
        provider = rows[model_id]["provider"]
        spec = PROVIDER_FACTS[provider]
        # Zero-cost providers on the openai/ route, the Responses-surface
        # presets and metered-upstream providers (LiteLLM bills them at $0
        # regardless, and registering a huggingface/ id makes it fetch the
        # model's config.json) are priced by the catalog, never registered.
        assert not spec.zero_cost and not spec.metered_upstream and spec.api_surface == "chat", (
            f"{model_id}: {provider} rows belong in capabilities.yaml, not model_overrides.json"
        )


def test_perplexity_presets_stay_unknown_to_litellm_after_a_load():
    _loaded()
    spec = PROVIDER_SPECS["perplexity"]
    for model_id in (spec.default_model, spec.fallback_model):
        with pytest.raises(Exception):
            litellm.cost_per_token(model=model_id, prompt_tokens=1, completion_tokens=1)
        info = registry.get_registry().get_model_info(model_id) or _loaded().get_model_info(model_id)
        assert info is not None and info.cost_known and info.input_cost_per_1m_tokens > 0


# ---------------------------------------------------------------------------
# Retired rows
# ---------------------------------------------------------------------------


def test_retired_rows_are_exactly_the_rename_sources_present(rows):
    expected = {
        model_id: RETIRED_MODEL_RENAMES[row["provider"]][model_id]
        for model_id, row in rows.items()
        if model_id in RETIRED_MODEL_RENAMES.get(row["provider"], {})
    }
    actual = {model_id: row.get("successor") for model_id, row in rows.items() if row.get("status") == "retired"}
    assert actual == expected
    assert expected, "the renames table names catalog rows; none found"
    for model_id, row in rows.items():
        if row.get("status") != "retired":
            assert "successor" not in row and "status" not in row, model_id
    for successor in expected.values():
        assert successor in rows and rows[successor].get("status") != "retired", successor


def test_loaded_rows_carry_status_and_successor(rows):
    reg = _loaded()
    for model_id, row in rows.items():
        info = reg.get_model_info(model_id)
        assert info is not None, model_id
        assert info.status == row.get("status", "active"), model_id
        assert info.successor == row.get("successor"), model_id


# ---------------------------------------------------------------------------
# The generator
# ---------------------------------------------------------------------------


def test_generator_check_reports_current(generator, capsys):
    assert generator.main(["--check"]) == 0
    assert "is current" in capsys.readouterr().out


def test_generator_check_ignores_the_recorded_litellm_version(generator, tmp_path, catalog_text):
    recorded = generator.recorded_litellm_version(catalog_text)
    assert recorded, "the header must record the LiteLLM version the file was written against"
    copy = tmp_path / "capabilities.yaml"
    copy.write_text(catalog_text.replace(f"litellm {recorded} ", "litellm 0.0.0-other "), encoding="utf-8")
    assert generator.main(["--check"], catalog_path=copy) == 0


def test_generator_check_fails_on_a_stale_file_and_the_rewrite_repairs_it(generator, tmp_path, catalog_text):
    retired = next(line for line in catalog_text.splitlines() if line == "    status: retired")
    stale = tmp_path / "capabilities.yaml"
    stale.write_text(catalog_text.replace(retired, "    status: active", 1), encoding="utf-8")
    assert generator.main(["--check"], catalog_path=stale) == 1
    assert generator.main([], catalog_path=stale) == 0
    repaired = stale.read_text(encoding="utf-8")
    expected = catalog_text.replace(
        f"litellm {generator.recorded_litellm_version(catalog_text)} ",
        f"litellm {generator.installed_litellm_version()} ",
    )
    assert repaired == expected


def test_generator_refuses_an_override_without_a_source(generator, tmp_path):
    row = {"provider": "openai", "display_name": "X", "context_window": 10, "typical_latency_ms": 1, "capability_scores": {}}
    path = _write_catalog(tmp_path, {"gpt-5.6-terra": row})
    with pytest.raises(SystemExit, match="need a `source:`"):
        generator.main(["--check"], catalog_path=path)


def _ollama_complete(sample: dict, rows: dict) -> dict:
    """``sample`` plus every tier-table tag's row: the ollama rows must be exactly the table's tags."""
    for tag in lm.all_tags():
        sample.setdefault(f"ollama/{tag}", rows[f"ollama/{tag}"])
    return sample


def test_generator_derives_vision_and_retirement_and_drops_zero_cost_prices(generator, rows):
    bare_ollama = {k: v for k, v in rows["ollama/gemma3:1b"].items() if k in registry.CATALOG_OWN_FIELDS and k != "source"}
    zero_cost = PROVIDER_SPECS["llm7"].default_model  # an active row of a zero_cost provider
    sample = _ollama_complete({
        "ollama/gemma3:4b": {**rows["ollama/gemma3:4b"], "supports_vision": False},
        # A derived-only row needs no source; a typed price on a zero-cost row is dropped, not kept.
        "ollama/gemma3:1b": bare_ollama,
        zero_cost: {**rows[zero_cost], "input_cost_per_1m_tokens": 7, "output_cost_per_1m_tokens": 7},
        "gpt-4o": {k: v for k, v in rows["gpt-4o"].items() if k not in ("status", "successor")},
    }, rows)
    out = generator.normalise(sample)
    assert out["ollama/gemma3:4b"]["supports_vision"] is lm.pick_for_tag("gemma3:4b").vision is True
    assert out["ollama/gemma3:1b"]["supports_vision"] is False and "source" not in out["ollama/gemma3:1b"]
    assert not set(generator.PRICE_FIELDS) & set(out[zero_cost]) and out[zero_cost]["source"]
    assert out["gpt-4o"]["status"] == "retired"
    assert out["gpt-4o"]["successor"] == RETIRED_MODEL_RENAMES["openai"]["gpt-4o"]
    assert list(out["gpt-4o"])[:4] == ["provider", "display_name", "status", "successor"]


def test_generator_refuses_a_source_that_backs_only_derived_fields(generator, rows):
    bare = {k: v for k, v in rows["ollama/gemma3:1b"].items() if k in registry.CATALOG_OWN_FIELDS and k != "source"}
    sample = _ollama_complete({"ollama/gemma3:1b": {**bare, "source": "nothing typed here"}}, rows)
    with pytest.raises(SystemExit, match="without any hand-typed override"):
        generator.normalise(sample)


# ---------------------------------------------------------------------------
# The loader
# ---------------------------------------------------------------------------


def _own(provider: str, **extra) -> dict:
    return {"provider": provider, "display_name": "T", "typical_latency_ms": 7, "capability_scores": {"conversation": 0.5}, **extra}


def test_loader_reads_price_context_and_flags_from_litellm(tmp_path):
    spec = PROVIDER_SPECS["groq"]
    model_id = spec.default_model
    expected = litellm.model_cost[model_id]
    reg = ProviderRegistry()
    reg.load_capabilities(_write_catalog(tmp_path, {model_id: _own("groq")}))
    info = reg.get_model_info(model_id)
    assert info is not None and info.cost_known
    assert info.context_window == expected["max_input_tokens"]
    assert info.max_output_tokens == expected["max_output_tokens"]
    assert info.input_cost_per_1m_tokens == Decimal(str(expected["input_cost_per_token"])) * 1_000_000
    assert info.output_cost_per_1m_tokens == Decimal(str(expected["output_cost_per_token"])) * 1_000_000
    assert info.supports_function_calling is bool(expected["supports_function_calling"]) is info.supports_tools
    assert info.supports_vision is bool(expected.get("supports_vision"))
    assert info.supports_json_mode is bool(expected.get("supports_response_schema"))
    assert info.typical_latency_ms == 7 and info.capability_scores == {"conversation": 0.5}


def test_loader_lets_a_sourced_yaml_override_beat_litellm(tmp_path):
    model_id = PROVIDER_SPECS["groq"].default_model
    row = _own("groq", context_window=1234, supports_function_calling=False, deprecation_date=date(2026, 6, 1), source="test")
    reg = ProviderRegistry()
    reg.load_capabilities(_write_catalog(tmp_path, {model_id: row}))
    info = reg.get_model_info(model_id)
    assert info.context_window == 1234
    assert info.supports_function_calling is False is info.supports_tools
    # YAML reads a bare ISO date as datetime.date; the row keeps the string.
    assert info.deprecation_date == "2026-06-01"
    # Fields the row does not override still come from LiteLLM.
    assert info.input_cost_per_1m_tokens == Decimal(str(litellm.model_cost[model_id]["input_cost_per_token"])) * 1_000_000
    assert info.cost_known


def test_generator_and_loader_agree_on_a_bare_deprecation_date(generator, tmp_path, rows):
    """A ``deprecation_date: 2026-06-01`` row passes ``--check`` and loads (it used to pass the check and fail the load)."""
    model_id = PROVIDER_SPECS["groq"].default_model
    row = {**rows[model_id], "deprecation_date": date(2026, 6, 1), "source": "test"}
    path = _write_catalog(tmp_path, _ollama_complete({model_id: row}, rows))
    assert generator.main([], catalog_path=path) == 0
    assert "deprecation_date: 2026-06-01\n" in path.read_text(encoding="utf-8")
    assert generator.main(["--check"], catalog_path=path) == 0
    reg = ProviderRegistry()
    reg.load_capabilities(path)
    assert reg.get_model_info(model_id).deprecation_date == "2026-06-01"


def test_loader_prices_a_zero_cost_provider_row_litellm_does_not_know(tmp_path, caplog):
    reg = ProviderRegistry()
    with caplog.at_level(logging.INFO, logger="nvh.providers.registry"):
        reg.load_capabilities(_write_catalog(tmp_path, {"llm7-only/never-mapped": _own("llm7", context_window=9, source="t")}))
    info = reg.get_model_info("llm7-only/never-mapped")
    assert info.cost_known and info.input_cost_per_1m_tokens == 0 == info.output_cost_per_1m_tokens
    assert info.context_window == 9 and info.supports_function_calling is None
    assert "cost unknown" not in caplog.text


def test_loader_marks_an_unpriced_row_and_logs_it_once(tmp_path, caplog):
    path = _write_catalog(tmp_path, {"nowhere/unmapped": _own("openai")})
    with caplog.at_level(logging.INFO, logger="nvh.providers.registry"):
        reg = ProviderRegistry()
        reg.load_capabilities(path)
        ProviderRegistry().load_capabilities(path)
    info = reg.get_model_info("nowhere/unmapped")
    assert info is not None and info.cost_known is False
    assert info.input_cost_per_1m_tokens == 0 == info.output_cost_per_1m_tokens
    hits = [r for r in caplog.records if "nowhere/unmapped" in r.getMessage()]
    assert len(hits) == 1 and hits[0].levelno == logging.INFO and "cost unknown" in hits[0].getMessage()


def test_loader_warns_when_a_spec_default_is_unpriced(tmp_path, caplog, monkeypatch):
    monkeypatch.setattr(registry, "_SPEC_IDS", frozenset({"nowhere/unmapped"}))
    with caplog.at_level(logging.INFO, logger="nvh.providers.registry"):
        ProviderRegistry().load_capabilities(_write_catalog(tmp_path, {"nowhere/unmapped": _own("openai")}))
    hits = [r for r in caplog.records if "nowhere/unmapped" in r.getMessage()]
    assert len(hits) == 1 and hits[0].levelno == logging.WARNING


def test_loader_registers_override_payloads_only_for_ids_litellm_lacks(tmp_path):
    fake = "nvhive-test/never-in-litellm"
    known = PROVIDER_SPECS["openai"].default_model
    payloads = {
        "models": {
            fake: {"source": "t", "litellm_provider": "openai", "mode": "chat", "max_input_tokens": 4321,
                   "max_output_tokens": 21, "input_cost_per_token": 1e-06, "output_cost_per_token": 2e-06,
                   "supports_function_calling": True},
            known: {"source": "t", "litellm_provider": "openai", "mode": "chat", "input_cost_per_token": 99.0,
                    "output_cost_per_token": 99.0},
        }
    }
    overrides_path = tmp_path / "overrides.json"
    overrides_path.write_text(json.dumps(payloads), encoding="utf-8")
    catalog = _write_catalog(tmp_path, {fake: _own("openai"), known: _own("openai")})
    try:
        reg = ProviderRegistry()
        reg.load_capabilities(catalog, overrides_path=overrides_path)
        info = reg.get_model_info(fake)
        assert info.cost_known and info.context_window == 4321 and info.max_output_tokens == 21
        assert info.input_cost_per_1m_tokens == Decimal(1) and info.output_cost_per_1m_tokens == Decimal(2)
        assert info.supports_function_calling is True
        assert fake in litellm.model_cost and "source" not in litellm.model_cost[fake]
        # LiteLLM already prices the known id, so its payload is ignored.
        assert reg.get_model_info(known).input_cost_per_1m_tokens != Decimal(99_000_000)
        assert litellm.model_cost[known]["input_cost_per_token"] != 99.0
    finally:
        _forget_litellm_registration(fake)
    assert fake not in litellm.open_ai_chat_completion_models
    with pytest.raises(Exception):
        litellm.get_llm_provider(fake)  # the fake id resolves to no provider once more


def _forget_litellm_registration(model_id: str) -> None:
    """Undo every side effect ``litellm.register_model`` has on the process (the table, the
    provider model list, and the registry it replays into the table on a cost-map reload)."""
    def drop(models) -> None:
        if isinstance(models, set):
            models.discard(model_id)
        elif isinstance(models, list):
            while model_id in models:
                models.remove(model_id)

    litellm.model_cost.pop(model_id, None)
    for attr in ("open_ai_chat_completion_models", "model_list"):
        drop(getattr(litellm, attr, None))
    replay = getattr(litellm.utils, "_runtime_registered_model_cost", None)
    if isinstance(replay, dict):
        replay.pop(model_id, None)
    by_provider = getattr(litellm, "models_by_provider", None)
    if isinstance(by_provider, dict):
        for models in by_provider.values():
            drop(models)


def test_loader_reads_retired_status_and_successor_from_the_row(tmp_path):
    row = _own("openai", status="retired", successor="gpt-5.6-terra")
    reg = ProviderRegistry()
    reg.load_capabilities(_write_catalog(tmp_path, {"gpt-4o": row}))
    info = reg.get_model_info("gpt-4o")
    assert info.status == "retired" and info.successor == "gpt-5.6-terra"


def test_loader_tolerates_a_minimal_row_and_a_missing_file(tmp_path):
    reg = ProviderRegistry()
    reg.load_capabilities(_write_catalog(tmp_path, {"synthetic-model": {"provider": "spark"}}))
    assert reg.get_model_info("synthetic-model").provider == "spark"
    reg.load_capabilities(tmp_path / "absent.yaml")
    assert reg.list_models() and len(reg.list_models()) == 1


def test_loading_the_shipped_catalog_opens_no_socket(monkeypatch):
    """No socket and, since LiteLLM swallows its own fetch failures, no httpx request attempted either
    (``litellm.register_model`` of a ``huggingface/`` id fetched the model's config.json until 0.45)."""
    attempted: list[str] = []

    def boom(*args, **kwargs):
        raise AssertionError("catalog load tried to open a socket")

    def record(self, request, *args, **kwargs):
        attempted.append(str(request.url))
        raise httpx.ConnectError("blocked by the test", request=request)

    async def arecord(self, request, *args, **kwargs):
        record(self, request)

    monkeypatch.setattr(socket, "socket", boom)
    monkeypatch.setattr(socket, "create_connection", boom)
    monkeypatch.setattr(httpx.Client, "send", record)
    monkeypatch.setattr(httpx.AsyncClient, "send", arecord)
    # Register the payloads afresh in this process so the registration path itself is exercised.
    for model_id in registry.load_model_overrides():
        _forget_litellm_registration(model_id)
    reg = _loaded()
    assert len(reg.list_models()) > 50
    assert attempted == [], attempted


def test_function_calling_gate_follows_the_loaded_catalog():
    assert oc.model_supports_function_calling(PROVIDER_SPECS["groq"].default_model) is True
    assert oc.model_supports_function_calling(PROVIDER_SPECS["perplexity"].default_model) is False
    assert oc.model_supports_function_calling(PROVIDER_SPECS["llm7"].default_model) is True


def test_generator_shares_the_check_or_write_contract(generator, tmp_path, catalog_text, capsys):
    """scripts/_gendoc.check_or_write: a CRLF checkout of a current file passes ``--check``, the rewrite
    prints ``wrote <path> (<rows> rows, ...)`` and lands LF-only against the installed LiteLLM."""
    copy = tmp_path / "capabilities.yaml"
    copy.write_bytes(catalog_text.replace("\n", "\r\n").encode("utf-8"))
    assert generator.main(["--check"], catalog_path=copy) == 0
    assert capsys.readouterr().out.strip() == f"{copy} is current"
    assert generator.main([], catalog_path=copy) == 0
    out = capsys.readouterr().out.rstrip()
    assert out.startswith(f"wrote {copy} (") and out.endswith(" with explicit overrides)")
    written = copy.read_bytes()
    assert b"\r\n" not in written
    assert written.decode("utf-8") == catalog_text.replace(
        f"litellm {generator.recorded_litellm_version(catalog_text)} ",
        f"litellm {generator.installed_litellm_version()} ",
    )
