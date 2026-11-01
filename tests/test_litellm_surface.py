"""The LiteLLM surface nvHive relies on, pinned without network.

The cloud adapters sit on a handful of LiteLLM entry points: ``acompletion``
and ``aresponses`` (openai_compatible.py), ``cost_per_token`` and the
``model_cost`` DB it reads, ``get_llm_provider`` (proxy.py unpacks its
4-tuple; openai_compatible.py takes element 3 as the base URL),
``supports_function_calling`` (the native tool-calling gate), plus
``register_model`` and ``supports_vision`` for the litellm-derived catalog
work. These tests pin that each still exists with the shape nvHive uses and
that the model DB knows every ``ProviderSpec`` default/fallback ID except a
documented allowlist.

Verified against LiteLLM 1.99.0 (the ``pyproject.toml`` floor) and 1.106.6
on 2026-10-31. The allowlist is the union of what both releases lack, so it
may name an ID a newer DB does know; it may never name an ID the spec table
no longer uses. Hermetic: the DB check runs against the JSON bundled with the
installed LiteLLM (``LITELLM_LOCAL_MODEL_COST_MAP``), which is what
``litellm.model_cost`` holds whenever the live map cannot be fetched. The
``[loaded]`` variant reads ``litellm.model_cost`` itself, which ``import
litellm`` fills from the live GitHub map when the network allows, so it is
``network``-marked and runs only under ``NVH_NETWORK_TESTS=1`` (offline it
would merely duplicate the bundled check, and an upstream row removal must
never turn main red).
"""

from __future__ import annotations

import inspect
import os

import litellm
import pytest

from nvh.providers.specs import PROVIDER_SPECS

# Default/fallback IDs LiteLLM's model DB does not carry, with the reason, as
# computed on 2026-10-31 against the maps bundled with 1.99.0 and 1.106.6 and
# the live map. ``litellm.cost_per_token`` raises for these and the adapter
# prices them from capabilities.yaml instead (test_perplexity_surface.py pins
# that path). Keep every entry a current spec ID; drop one when the DB learns it.
UNKNOWN_TO_LITELLM: dict[str, str] = {
    # AI21: the DB keys Jamba bare ("jamba-large-1.7", "jamba-mini-1.7") and
    # under bedrock/vertex/snowflake/azure; it has no ai21_chat/ rows.
    "ai21_chat/jamba-large-1.7": "DB keys Jamba bare (jamba-large-1.7); no ai21_chat/ rows",
    "ai21_chat/jamba-mini-2": "no row under any key; the DB stops at jamba-mini-1.7",
    # Hugging Face Inference Providers: the DB has no huggingface/ rows at all.
    "huggingface/openai/gpt-oss-120b": "DB has no huggingface/ rows; billed by the upstream provider",
    "huggingface/openai/gpt-oss-20b": "DB has no huggingface/ rows; billed by the upstream provider",
    # LLM7: an OpenAI-compatible endpoint LiteLLM does not know (openai/ route,
    # base_url, zero_cost); its bare IDs appear under no key.
    "gpt-oss": "llm7 bare ID on the openai/ route; LiteLLM has no llm7 provider (zero_cost)",
    "minimax-m2.7": "llm7 bare ID on the openai/ route; LiteLLM has no llm7 provider (zero_cost)",
    # NVIDIA NIM: the DB's only nvidia_nim/ rows are rerankers.
    "nvidia_nim/meta/llama-3.3-70b-instruct": "DB's only nvidia_nim/ rows are rerankers",
    "nvidia_nim/meta/llama-3.1-8b-instruct": "DB's only nvidia_nim/ rows are rerankers",
    # OpenRouter gpt-oss: priced in 1.99.0's bundled DB, absent from 1.106.6's
    # bundled DB and from the live map on 2026-10-31.
    "openrouter/openai/gpt-oss-120b": "in 1.99.0's bundled DB; dropped by 1.106.6 and the live map",
    "openrouter/openai/gpt-oss-20b": "in 1.99.0's bundled DB; dropped by 1.106.6 and the live map",
    # Perplexity Agent API presets: priced from usage.cost in the response,
    # with the capabilities.yaml rates as the fallback.
    "perplexity/preset/low": "Agent API preset; priced from usage.cost, catalog fallback",
    "perplexity/preset/fast": "Agent API preset; priced from usage.cost, catalog fallback",
    # SiliconFlow: not a LiteLLM provider (openai/ route, base_url, zero_cost);
    # the DB knows the model only under deepinfra/, together_ai/ and novita/.
    "Qwen/Qwen2.5-7B-Instruct": "siliconflow bare ID on the openai/ route; DB has it only under other providers",
}


def _spec_ids() -> dict[str, str]:
    """Every default/fallback ID in the spec table -> ``provider.field`` it comes from."""
    ids: dict[str, str] = {}
    for name, spec in sorted(PROVIDER_SPECS.items()):
        for field in ("default_model", "fallback_model"):
            ids.setdefault(getattr(spec, field), f"{name}.{field}")
    return ids


def _missing(cost_map: dict) -> dict[str, str]:
    return {mid: where for mid, where in _spec_ids().items() if mid not in cost_map}


@pytest.fixture
def bundled_model_cost(monkeypatch) -> dict:
    """The model DB shipped inside the installed LiteLLM wheel, read without network."""
    monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "True")
    return litellm.get_model_cost_map(litellm.model_cost_map_url)


_LIVE_MAP = pytest.param(
    "loaded",
    marks=[
        pytest.mark.network,
        pytest.mark.skipif(
            os.environ.get("NVH_NETWORK_TESTS") != "1",
            reason="litellm.model_cost is the live GitHub map when network is available; "
            "set NVH_NETWORK_TESTS=1 to check it",
        ),
    ],
)


@pytest.fixture(params=[_LIVE_MAP, "bundled"])
def model_cost(request, bundled_model_cost) -> dict:
    """The bundled DB nvHive falls back to, and (opt-in) ``litellm.model_cost`` as loaded."""
    return litellm.model_cost if request.param == "loaded" else bundled_model_cost


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------


def test_aresponses_is_a_coroutine_function():
    assert inspect.iscoroutinefunction(litellm.aresponses)
    assert inspect.iscoroutinefunction(litellm.acompletion)


def test_get_llm_provider_returns_a_4_tuple_naming_the_provider():
    spec = PROVIDER_SPECS["groq"]
    result = litellm.get_llm_provider(spec.default_model)
    assert isinstance(result, tuple) and len(result) == 4
    bare, provider, _key, api_base = result  # the unpacking proxy.py does
    assert provider == "groq"
    assert bare == spec.default_model.removeprefix(spec.litellm_prefix)
    assert isinstance(api_base, str) and api_base.startswith("https://")  # openai_compatible.py:[3]


def test_register_model_and_capability_probes_exist():
    assert callable(litellm.register_model)
    assert callable(litellm.supports_function_calling)
    assert callable(litellm.supports_vision)
    assert callable(litellm.cost_per_token)


# ---------------------------------------------------------------------------
# Model DB
# ---------------------------------------------------------------------------


def test_model_cost_is_a_large_dict(model_cost):
    assert isinstance(model_cost, dict)
    assert len(model_cost) > 1000


def test_model_cost_knows_every_default_and_fallback_id_except_the_allowlist(model_cost):
    missing = _missing(model_cost)
    unexpected = {mid: where for mid, where in missing.items() if mid not in UNKNOWN_TO_LITELLM}
    assert not unexpected, (
        "ProviderSpec IDs newly unknown to LiteLLM's model DB (fix the spec or extend "
        f"UNKNOWN_TO_LITELLM with a reason): {unexpected}"
    )


def test_allowlist_names_only_current_spec_ids():
    stale = set(UNKNOWN_TO_LITELLM) - set(_spec_ids())
    assert not stale, f"UNKNOWN_TO_LITELLM entries no spec uses any more: {sorted(stale)}"
    assert all(reason.strip() for reason in UNKNOWN_TO_LITELLM.values())


def test_perplexity_presets_are_on_the_allowlist():
    # test_perplexity_surface.py prices them from usage.cost / capabilities.yaml.
    spec = PROVIDER_SPECS["perplexity"]
    assert {spec.default_model, spec.fallback_model} <= set(UNKNOWN_TO_LITELLM)
