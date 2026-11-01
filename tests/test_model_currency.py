"""Model currency: do the providers still serve the catalog's and the specs' model ids?

Network tier (``@pytest.mark.network``; opt in with ``NVH_NETWORK_TESTS=1``):
the weekly model-currency workflow runs it, a local run skips cleanly. Two
tiers inside: secret-free (llm7's anonymous ``GET /models``) and keyed
(``GROQ_API_KEY`` and ``NVIDIA_API_KEY`` only, decision 19), each keyed test
skipping when its variable is absent. No secret is ever printed: model ids
only.

The workflow's informational step is ``python scripts/gen_capabilities.py
--drift`` (``override_drift`` / ``print_override_drift`` live there): every
explicit ``source:`` override in capabilities.yaml beside what the installed
LiteLLM table says today, every model_overrides.json payload whose id LiteLLM
now carries, and every active row that no source prices any more (the
weekly counterpart of the spec-default release gate in
tests/test_capabilities_catalog.py, so a LiteLLM release that drops a row is
seen without turning CI red). It prints and never fails;
``test_override_drift_report_runs_offline`` pins its shape without the
network (the table is the one bundled with LiteLLM).
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import httpx
import pytest
import yaml

import nvh.providers.openai_compatible as oc
from nvh.providers import registry as registry_mod
from nvh.providers.specs import PROVIDER_FACTS, PROVIDER_SPECS

ROOT = Path(__file__).resolve().parents[1]

NETWORK = pytest.mark.skipif(
    os.environ.get("NVH_NETWORK_TESTS") != "1",
    reason="set NVH_NETWORK_TESTS=1 to probe the providers' live model lists",
)
#: The keyed tier's providers (decision 19): free, revocable keys only.
KEYED_PROVIDERS = ("groq", "nvidia")
PROBE_TIMEOUT = 30.0


def _catalog_ids(provider: str) -> set[str]:
    """The catalog's active rows for ``provider``."""
    return {m.model_id for m in oc._catalog().list_models(provider=provider) if m.status != "retired"}


def _spec_ids(provider: str) -> set[str]:
    spec = PROVIDER_SPECS[provider]
    return {m for m in (spec.default_model, spec.fallback_model) if m}


def _missing(model_ids: set[str], live: set[str]) -> set[str]:
    """The ids in ``model_ids`` the live list does not vouch for (exact or by tail)."""
    keys = oc.live_model_keys(live)
    return {m for m in model_ids if not oc.model_served(m, keys)}


async def _live(provider: str) -> set[str]:
    """The adapter's own live list (the ``/v1/models`` and ``nvh status --deep`` path)."""
    live = await oc.OpenAICompatibleProvider(PROVIDER_SPECS[provider]).live_model_ids()
    assert live, f"{provider}: no live model list (probe failed or empty)"
    return live


# -- secret-free tier ---------------------------------------------------------


@NETWORK
@pytest.mark.network
def test_llm7_anonymous_models_endpoint_serves_the_spec_ids():
    """The anonymous GET (no key at all) lists llm7's default and fallback."""
    spec = PROVIDER_SPECS["llm7"]
    assert spec.models_probe is not None
    url = spec.models_probe[0]
    resp = httpx.get(url, timeout=PROBE_TIMEOUT)
    assert resp.status_code == 200, f"{url}: HTTP {resp.status_code}"
    ids = oc.model_ids_from_payload(resp.json())
    assert ids, f"{url}: empty model list"
    assert _missing(_spec_ids("llm7"), set(ids)) == set()


@NETWORK
@pytest.mark.network
@pytest.mark.asyncio
async def test_llm7_catalog_rows_are_served():
    """Every active llm7 catalog row is in the live list the adapter fetches."""
    live = await _live("llm7")
    assert _missing(_catalog_ids("llm7"), live) == set()


# -- keyed tier ---------------------------------------------------------------


@NETWORK
@pytest.mark.network
@pytest.mark.asyncio
@pytest.mark.parametrize("provider", KEYED_PROVIDERS)
async def test_keyed_provider_serves_its_spec_and_catalog_ids(provider):
    """With the provider's key in the environment, its default, fallback and catalog rows are served."""
    spec = PROVIDER_SPECS[provider]
    if not os.environ.get(spec.key_env or ""):
        pytest.skip(f"{spec.key_env} not set")
    live = await _live(provider)
    assert _missing(_spec_ids(provider), live) == set(), "spec default/fallback not served"
    assert _missing(_catalog_ids(provider), live) == set(), "catalog rows not served"


# -- informational: the catalog's overrides against today's LiteLLM -----------
# The report is scripts/gen_capabilities.py's ``--drift`` (the workflow's
# step); it is loaded from the file the way tests/test_capabilities_catalog.py
# loads the generator, and these tests pin its shape offline.


def _generator():
    spec = importlib.util.spec_from_file_location("gen_capabilities", ROOT / "scripts" / "gen_capabilities.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_gen = _generator()
override_drift, print_override_drift = _gen.override_drift, _gen.print_override_drift


def test_drift_flag_prints_the_report_and_leaves_the_file_alone(capsys):
    """``python scripts/gen_capabilities.py --drift``: the header, then :func:`override_drift`'s lines, exit 0, no write."""
    before = _gen.CATALOG_PATH.read_bytes()
    assert _gen.main(["--drift"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0].startswith("# capabilities.yaml overrides vs LiteLLM ") and out[0].endswith("(informational)")
    assert out[1:] == override_drift()
    assert _gen.CATALOG_PATH.read_bytes() == before


def test_override_drift_report_runs_offline(capsys):
    """One line per override, per payload and per unpriced active row against the bundled table; no network."""
    lines = override_drift()
    rows = (yaml.safe_load(registry_mod.CATALOG_PATH.read_text(encoding="utf-8")) or {}).get("models") or {}
    expected = sum(1 for row in rows.values() for k in registry_mod.CATALOG_OVERRIDE_FIELDS if k in (row or {}))
    if registry_mod.MODEL_OVERRIDES_PATH.exists():
        expected += len(registry_mod.load_model_overrides())
    reg = registry_mod.ProviderRegistry()
    reg.load_capabilities()
    unpriced = [
        m.model_id for m in reg.list_models()
        if m.status != "retired" and not m.cost_known and not PROVIDER_FACTS[m.provider].metered_upstream
    ]
    assert len(lines) == expected + len(unpriced) > 0
    for model_id in unpriced:
        assert any(line.startswith(f"{model_id}: cost unknown") for line in lines), model_id
    verdicts = (": redundant (LiteLLM agrees)", ": differs (LiteLLM says ", ": no LiteLLM value", ": cost unknown", "model_overrides.json ")
    assert all(any(v in line for v in verdicts) for line in lines)
    assert all(line.endswith("]") for line in lines)  # every line names its source
    print_override_drift()
    assert "# capabilities.yaml overrides vs LiteLLM" in capsys.readouterr().out


def test_drift_report_lists_an_active_row_litellm_stopped_pricing(tmp_path, monkeypatch):
    """A row LiteLLM no longer carries is reported (not a CI failure) unless retired or metered upstream."""
    rows = (yaml.safe_load(registry_mod.CATALOG_PATH.read_text(encoding="utf-8")) or {}).get("models") or {}
    groq_default = PROVIDER_SPECS["groq"].default_model
    hf_default = PROVIDER_SPECS["huggingface"].default_model
    sample = {
        "groq/gone-from-litellm": {k: v for k, v in rows[groq_default].items() if k not in ("status", "successor")},
        "groq/retired-row": {**rows[groq_default], "status": "retired", "successor": groq_default},
        hf_default: rows[hf_default],
    }
    path = tmp_path / "capabilities.yaml"
    path.write_text(yaml.safe_dump({"models": sample}, sort_keys=False), encoding="utf-8")
    monkeypatch.setattr(registry_mod, "CATALOG_PATH", path)
    monkeypatch.setattr(registry_mod, "MODEL_OVERRIDES_PATH", tmp_path / "absent.json")
    registry_mod.reset_catalog_state()
    lines = override_drift()
    unknown = [line for line in lines if ": cost unknown" in line]
    assert len(unknown) == 1 and unknown[0].startswith("groq/gone-from-litellm: cost unknown")
