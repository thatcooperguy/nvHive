"""Shipped provider defaults derive from nvh.providers.specs: the settings template and the server's maps.

Until 0.45 the ``advisors:`` template in nvh.config.settings and the key /
URL / default-model maps in nvh.api.server were hand copies of the spec table
that these tests cross-checked. They are derivations now, so the tests pin
the *derivation*: every stanza, map entry and ``/v1/setup/free-providers``
row is rebuilt here from the same spec row and compared, with no provider
fact typed in this file.
"""

from __future__ import annotations

import pytest
import yaml

import nvh.api.server as server_module
import nvh.config.settings as settings_module
from nvh.cli.setup import RETIRED_MODEL_RENAMES
from nvh.config.settings import _DEFAULT_LOCAL_BUDGET_GB, generate_default_config
from nvh.core import local_models
from nvh.providers.quota_info import PROVIDER_QUOTAS
from nvh.providers.registry import BESPOKE_ADAPTERS
from nvh.providers.specs import BESPOKE_SPECS, PROVIDER_FACTS, PROVIDER_SPECS
from nvh.providers.triton_provider import DEFAULT_TRITON_URL
from nvh.utils.ollama import DEFAULT_OLLAMA_URL

# Every ID the migrate table retires, plus the GitHub Models fallback that
# left with its provider (no rename target — the provider is gone).
RETIRED_MODEL_IDS = {
    old for table in RETIRED_MODEL_RENAMES.values() for old in table
} | {"meta-llama-3.1-8b-instruct"}


def _template_advisors() -> dict[str, dict]:
    return yaml.safe_load(generate_default_config())["advisors"]


# --- the settings template ------------------------------------------------------


def test_template_has_a_stanza_per_provider_and_nothing_else() -> None:
    assert list(_template_advisors()) == list(PROVIDER_FACTS)
    assert set(PROVIDER_FACTS) == set(PROVIDER_SPECS) | set(BESPOKE_ADAPTERS)


@pytest.mark.parametrize("name", sorted(PROVIDER_SPECS))
def test_cloud_stanza_is_the_spec_row(name: str) -> None:
    block = _template_advisors()[name]
    spec = PROVIDER_SPECS[name]
    assert block["default_model"] == spec.default_model
    assert block["fallback_model"] == spec.fallback_model
    assert block.get("base_url") == spec.base_url
    assert "type" not in block  # LiteLLM-backed rows share the default adapter
    # The key reference names the primary variable first, the spec's own
    # alternates as nested defaults, and the anonymous key last.
    assert block["api_key"] == settings_module._key_reference(spec, default=spec.anonymous_key)
    assert block["api_key"].startswith("${" + spec.key_env)
    for alt in spec.alt_key_envs:
        assert f"${{{alt}" in block["api_key"], alt
    # Only the anonymous tier works before any key is pasted.
    assert block["enabled"] is bool(spec.anonymous_key)


def test_key_reference_nests_the_alternates_and_the_anonymous_key() -> None:
    hf = PROVIDER_SPECS["huggingface"]
    assert settings_module._key_reference(hf) == "${" + hf.key_env + ":-${" + hf.alt_key_envs[0] + "}}"
    llm7 = PROVIDER_SPECS["llm7"]
    assert settings_module._key_reference(llm7, default=llm7.anonymous_key) == "${" + llm7.key_env + ":-" + llm7.anonymous_key + "}"
    openai = PROVIDER_SPECS["openai"]
    assert openai.alt_key_envs == () and settings_module._key_reference(openai) == "${" + openai.key_env + "}"


def test_bespoke_stanzas_name_their_adapter_type() -> None:
    advisors = _template_advisors()
    for name in BESPOKE_SPECS:
        assert advisors[name]["type"] == name
        assert "api_key" not in advisors[name]
    ollama = advisors["ollama"]
    assert ollama["base_url"] == DEFAULT_OLLAMA_URL
    assert ollama["default_model"] == f"ollama/{local_models.pick(_DEFAULT_LOCAL_BUDGET_GB, 'chat').tag}"
    triton = BESPOKE_SPECS["triton"]
    assert advisors["triton"]["base_url"] == settings_module._key_reference(
        triton, default=DEFAULT_TRITON_URL,
    )
    assert advisors["triton"]["default_model"] == ""
    mock = advisors["mock"]
    assert (mock["default_model"], mock["fallback_model"]) == (
        BESPOKE_SPECS["mock"].default_model, BESPOKE_SPECS["mock"].fallback_model,
    )


def _clear_key_env(monkeypatch) -> None:
    for spec in PROVIDER_FACTS.values():
        for var in (f"COUNCIL_{spec.name.upper()}_API_KEY", spec.key_env or "", *spec.env_keys):
            if var:
                monkeypatch.delenv(var, raising=False)
    for var in ("TRITON_URL", "TRITON_ENDPOINT", "NVH_USE_KEYRING"):
        monkeypatch.delenv(var, raising=False)


def _load_template(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    path.write_text(generate_default_config(), encoding="utf-8")
    monkeypatch.setattr(settings_module, "_find_project_config", lambda: None)
    return settings_module.load_config(config_path=path)


@pytest.mark.parametrize("name", sorted(name for name, spec in PROVIDER_SPECS.items() if spec.alt_key_envs))
def test_template_key_reference_round_trips_through_load_config(name: str, tmp_path, monkeypatch) -> None:
    """``${PRIMARY:-${ALT}}`` resolves to whichever variable is set: the primary, else the alternate."""
    from nvh.providers.registry import resolve_provider_key

    spec = PROVIDER_SPECS[name]
    alt = spec.alt_key_envs[0]
    _clear_key_env(monkeypatch)

    monkeypatch.setenv(spec.key_env, "primary-value")
    monkeypatch.setenv(alt, "alternate-value")
    pconfig = _load_template(tmp_path, monkeypatch).providers[name]
    assert pconfig.api_key == "primary-value"  # no stray brace from the nested default
    assert resolve_provider_key(name, pconfig) == ("primary-value", "config")

    monkeypatch.delenv(spec.key_env)
    pconfig = _load_template(tmp_path, monkeypatch).providers[name]
    assert pconfig.api_key == "alternate-value"
    assert resolve_provider_key(name, pconfig) == ("alternate-value", "config")

    monkeypatch.delenv(alt)
    pconfig = _load_template(tmp_path, monkeypatch).providers[name]
    assert pconfig.api_key == (spec.anonymous_key or "")
    assert resolve_provider_key(name, pconfig)[0] == (spec.anonymous_key or None)


def test_template_triton_endpoint_chain_round_trips(tmp_path, monkeypatch) -> None:
    _clear_key_env(monkeypatch)
    assert _load_template(tmp_path, monkeypatch).providers["triton"].base_url == DEFAULT_TRITON_URL
    monkeypatch.setenv("TRITON_ENDPOINT", "http://spark:8001")
    assert _load_template(tmp_path, monkeypatch).providers["triton"].base_url == "http://spark:8001"
    monkeypatch.setenv("TRITON_URL", "http://spark:9001")
    assert _load_template(tmp_path, monkeypatch).providers["triton"].base_url == "http://spark:9001"


def test_template_has_no_retired_models_or_github() -> None:
    text = generate_default_config()
    advisors = _template_advisors()
    assert "github" not in advisors
    assert "GITHUB_TOKEN" not in text
    assert "@ADVISOR_STANZAS@" not in text and "@OLLAMA_DEFAULT_MODEL@" not in text
    for name, block in advisors.items():
        for field in ("default_model", "fallback_model"):
            assert block.get(field, "") not in RETIRED_MODEL_IDS, f"{name}.{field}"


def test_llm7_default_is_a_served_free_tier_model() -> None:
    llm7 = _template_advisors()["llm7"]
    assert llm7["enabled"] is True
    assert llm7["default_model"] == PROVIDER_SPECS["llm7"].default_model == "gpt-oss:20b"


# --- the server's maps ------------------------------------------------------------


def test_server_defaults_match_settings_template() -> None:
    advisors = _template_advisors()
    server_defaults = server_module._PROVIDER_DEFAULT_CONFIG
    assert set(server_defaults) == set(PROVIDER_SPECS)
    for name, defaults in server_defaults.items():
        template = advisors[name]
        for field, value in defaults.items():
            assert template.get(field) == value, (
                f"{name}.{field}: server={value!r} template={template.get(field)!r}"
            )
        assert set(defaults) == {"default_model", "fallback_model"} | ({"base_url"} if PROVIDER_SPECS[name].base_url else set())


def test_server_maps_are_the_spec_rows() -> None:
    assert server_module._PROVIDER_ENV_VAR_MAP == {name: spec.key_env for name, spec in PROVIDER_SPECS.items()}
    for name, spec in PROVIDER_SPECS.items():
        assert server_module._provider_env_var(name) == spec.key_env, name
    # The setup rows read signup_url / docs_url / logo_slug straight from the
    # spec (test_setup_row_derives_from_the_spec_row); no module-level copy remains.
    for stale in ("_PROVIDER_KEY_URLS", "_PROVIDER_DOC_URLS", "_PROVIDER_LOGO_SLUGS", "_get_signup_url", "_provider_docs_url"):
        assert not hasattr(server_module, stale), stale
    # Every cloud row carries the setup facts the maps are built from.
    for name, spec in PROVIDER_SPECS.items():
        assert spec.signup_url and spec.docs_url, name
        assert spec.key_placeholder.endswith("..."), name
        if spec.key_prefixes:
            assert spec.key_placeholder == spec.key_prefixes[0] + "...", name


def test_allowed_providers_are_every_keyable_spec_row() -> None:
    assert server_module._ALLOWED_PROVIDERS == set(PROVIDER_FACTS) - {"mock", "triton"}
    assert "llm7" in server_module._ALLOWED_PROVIDERS  # the optional token
    accepted = server_module.SaveKeyRequest(provider="groq", api_key="gsk_0123456789")
    assert accepted.provider == "groq"
    for name in ("github", "triton", "mock"):
        with pytest.raises(ValueError, match=f"Unknown provider '{name}'"):
            server_module.SaveKeyRequest(provider=name, api_key="ghp_0123456789")


# --- the free-providers rows --------------------------------------------------------


@pytest.mark.parametrize("name", sorted(set(PROVIDER_FACTS) - {"mock", "triton"}))
def test_setup_row_derives_from_the_spec_row(name: str, monkeypatch) -> None:
    spec = PROVIDER_FACTS[name]
    for env in (spec.key_env, *spec.env_keys):
        if env:
            monkeypatch.delenv(env, raising=False)
    row = server_module._setup_provider_row(spec, available_names=set())
    takes_key = spec.takes_api_key
    assert row["id"] == name
    assert row["name"] == row["display_name"] == spec.display_name
    assert row["signup_url"] == spec.signup_url
    assert row["key_url"] == (spec.signup_url if takes_key else "")
    assert row["docs_url"] == spec.docs_url
    assert row["env_var"] == row["env_key"] == (spec.key_env if takes_key else "")
    assert row["placeholder"] == spec.key_placeholder
    assert row["requires_key"] is row["requires_signup"] is (takes_key and not spec.anonymous_key)
    assert row["signup_tier"] == spec.signup_tier
    assert row["signup_tier"] in {"none", "email", "account"}
    assert row["free_tier_limits"] == row["free_info"] == spec.free_info
    assert row["daily_limit"] == PROVIDER_QUOTAS[name].limit_description
    assert row["strengths"] == list(spec.strengths[:3])
    assert row["default_model"] == spec.default_model and row["fallback_model"] == spec.fallback_model
    assert row["logo_slug"] == (spec.logo_slug or None)
    assert row["priority"] == (spec.free_tier_rank if spec.free_tier else server_module._PAID_PRIORITY_BASE + list(PROVIDER_FACTS).index(name))
    assert row["configured"] is False  # no key in the environment and nothing detected


def test_setup_rows_signup_groups_follow_the_spec(monkeypatch) -> None:
    for spec in PROVIDER_FACTS.values():
        if not spec.takes_api_key or spec.anonymous_key:
            assert spec.signup_tier == "none", spec.name
        elif not spec.free_tier:
            assert spec.signup_tier == "account", spec.name
    # A key in the environment configures the row; an anonymous tier is configured by detection.
    groq = PROVIDER_SPECS["groq"]
    monkeypatch.setenv(groq.key_env, "gsk_test")
    assert server_module._setup_provider_row(groq, set())["configured"] is True
    monkeypatch.delenv(groq.key_env)
    assert server_module._setup_provider_row(groq, set())["configured"] is False
    assert server_module._setup_provider_row(groq, {"groq"})["configured"] is True
