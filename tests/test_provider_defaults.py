"""Shipped provider defaults: the settings template and the server's copy vs PROVIDER_SPECS.

The 0.41.1 hotfix replaced every retired model ID and removed the GitHub
Models provider (service retired 2026-07-30). Two hand-typed copies of the
defaults still exist — nvh.config.settings.generate_default_config and
nvh.api.server._PROVIDER_DEFAULT_CONFIG — so these tests pin both to
nvh.providers.specs, the one source since 0.44, rather than to a third copy
of the IDs typed here.
"""

from __future__ import annotations

import pytest
import yaml

import nvh.api.server as server_module
from nvh.cli.setup import RETIRED_MODEL_RENAMES
from nvh.config.settings import generate_default_config
from nvh.providers.registry import BESPOKE_ADAPTERS
from nvh.providers.specs import BESPOKE_SPECS, PROVIDER_SPECS

# Every ID the migrate table retires, plus the GitHub Models fallback that
# left with its provider (no rename target — the provider is gone).
RETIRED_MODEL_IDS = {
    old for table in RETIRED_MODEL_RENAMES.values() for old in table
} | {"meta-llama-3.1-8b-instruct"}


def _template_advisors() -> dict[str, dict]:
    return yaml.safe_load(generate_default_config())["advisors"]


def test_template_has_a_stanza_per_provider_and_nothing_else() -> None:
    assert set(_template_advisors()) == set(PROVIDER_SPECS) | set(BESPOKE_ADAPTERS)


@pytest.mark.parametrize("name", sorted(PROVIDER_SPECS))
def test_template_stanza_matches_provider_spec(name: str) -> None:
    """The settings template is a hand copy of PROVIDER_SPECS until the template derives from it."""
    block = _template_advisors()[name]
    spec = PROVIDER_SPECS[name]
    assert block.get("default_model") == spec.default_model, f"{name}.default_model"
    # A blank template fallback inherits the spec's; anything else must match it.
    assert block.get("fallback_model", "") in ("", spec.fallback_model), f"{name}.fallback_model"
    if spec.base_url:
        assert block.get("base_url") == spec.base_url, f"{name}.base_url"
    # The api_key reference names the spec's primary key variable first.
    assert str(block.get("api_key", "")).startswith("${" + spec.key_env), f"{name}.api_key"


def test_template_mock_defaults_match_the_bespoke_spec() -> None:
    mock = _template_advisors()["mock"]
    assert (mock["default_model"], mock["fallback_model"]) == (
        BESPOKE_SPECS["mock"].default_model, BESPOKE_SPECS["mock"].fallback_model,
    )


def test_template_has_no_retired_models_or_github() -> None:
    advisors = _template_advisors()
    assert "github" not in advisors
    assert "GITHUB_TOKEN" not in generate_default_config()
    for name, block in advisors.items():
        for field in ("default_model", "fallback_model"):
            assert block.get(field, "") not in RETIRED_MODEL_IDS, f"{name}.{field}"


def test_llm7_default_is_a_served_free_tier_model() -> None:
    llm7 = _template_advisors()["llm7"]
    assert llm7["enabled"] is True
    assert llm7["default_model"] == PROVIDER_SPECS["llm7"].default_model == "gpt-oss"


def test_server_defaults_match_settings_template() -> None:
    advisors = _template_advisors()
    server_defaults = server_module._PROVIDER_DEFAULT_CONFIG
    assert "github" not in server_defaults
    for name, defaults in server_defaults.items():
        template = advisors[name]
        for field, value in defaults.items():
            assert template.get(field) == value, (
                f"{name}.{field}: server={value!r} template={template.get(field)!r}"
            )


def test_server_key_env_map_matches_provider_specs() -> None:
    """The server's env-var copy names exactly the specs' primary key variables."""
    env_map = server_module._PROVIDER_ENV_VAR_MAP
    assert set(env_map) == set(PROVIDER_SPECS)
    for name, env in env_map.items():
        assert env == PROVIDER_SPECS[name].key_env, name


def test_server_provider_maps_dropped_github() -> None:
    for mapping in (
        server_module._PROVIDER_ENV_VAR_MAP,
        server_module._PROVIDER_KEY_URLS,
        server_module._PROVIDER_DOC_URLS,
        server_module._PROVIDER_LOGO_SLUGS,
        server_module._PROVIDER_DEFAULT_CONFIG,
    ):
        assert "github" not in mapping
    assert "github" not in server_module._ALLOWED_PROVIDERS
    accepted = server_module.SaveKeyRequest(provider="groq", api_key="gsk_0123456789")
    assert accepted.provider == "groq"


def test_save_key_rejects_github() -> None:
    with pytest.raises(ValueError, match="Unknown provider 'github'"):
        server_module.SaveKeyRequest(provider="github", api_key="ghp_0123456789")
