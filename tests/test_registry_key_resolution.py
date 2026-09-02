"""One key-resolution order for the registry, the adapters and the diagnostics.

Before 0.42.1 the registry read COUNCIL_/<NAME>_API_KEY, the adapter read the
spec's env_keys and `nvh status --deep` read <NAME>_/HIVE_<NAME>_API_KEY, so
NIM_API_KEY-only boxes were "missing" in the check while the adapter worked.
"""

from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from nvh.config.settings import CouncilConfig, ProviderConfig
from nvh.providers.registry import ProviderRegistry, resolve_provider_key
from nvh.providers.specs import PROVIDER_SPECS


@pytest.fixture
def clean_env(monkeypatch):
    monkeypatch.delenv("NVH_USE_KEYRING", raising=False)
    for name, spec in PROVIDER_SPECS.items():
        for var in (f"COUNCIL_{name.upper()}_API_KEY", f"{name.upper()}_API_KEY", *spec.env_keys):
            monkeypatch.delenv(var, raising=False)
    monkeypatch.delenv("MYPROXY_API_KEY", raising=False)
    monkeypatch.delenv("COUNCIL_MYPROXY_API_KEY", raising=False)


def test_resolution_order(clean_env, monkeypatch):
    assert resolve_provider_key("groq") == (None, "none")
    monkeypatch.setenv("HIVE_GROQ_API_KEY", "hive")
    assert resolve_provider_key("groq") == ("hive", "env:HIVE_GROQ_API_KEY")
    monkeypatch.setenv("GROQ_API_KEY", "plain")
    assert resolve_provider_key("groq") == ("plain", "env:GROQ_API_KEY")
    monkeypatch.setenv("COUNCIL_GROQ_API_KEY", "council")
    assert resolve_provider_key("groq") == ("council", "env:COUNCIL_GROQ_API_KEY")
    pconfig = SimpleNamespace(api_key="configured")
    assert resolve_provider_key("groq", pconfig) == ("configured", "config")


def test_unexpanded_placeholder_falls_through(clean_env, monkeypatch):
    monkeypatch.setenv("NIM_API_KEY", "nim")
    pconfig = SimpleNamespace(api_key="${NVIDIA_API_KEY}")
    assert resolve_provider_key("nvidia", pconfig) == ("nim", "env:NIM_API_KEY")


def test_anonymous_tier_is_last(clean_env):
    assert resolve_provider_key("llm7") == ("anonymous", "anonymous")
    assert resolve_provider_key("openai") == (None, "none")


def test_keyring_only_when_opted_in(clean_env, monkeypatch):
    fake = SimpleNamespace(get_password=lambda service, item: "from-keyring" if item == "groq_api_key" else None)
    monkeypatch.setitem(sys.modules, "keyring", fake)
    assert resolve_provider_key("groq") == (None, "none")
    monkeypatch.setenv("NVH_USE_KEYRING", "1")
    assert resolve_provider_key("groq") == ("from-keyring", "keyring")


def test_ptype_selects_the_spec_for_a_renamed_stanza(clean_env, monkeypatch):
    monkeypatch.setenv("MYPROXY_API_KEY", "mine")
    assert resolve_provider_key("myproxy", ptype="openai") == ("mine", "env:MYPROXY_API_KEY")
    monkeypatch.delenv("MYPROXY_API_KEY")
    monkeypatch.setenv("HIVE_OPENAI_API_KEY", "shared")
    assert resolve_provider_key("myproxy", ptype="openai") == ("shared", "env:HIVE_OPENAI_API_KEY")


def test_registry_hands_the_resolved_key_to_the_adapter(clean_env, monkeypatch):
    monkeypatch.setenv("NIM_API_KEY", "nim")
    config = CouncilConfig(providers={"nvidia": ProviderConfig(enabled=True, api_key="${NVIDIA_API_KEY}")})
    registry = ProviderRegistry()
    assert registry.setup_from_config(config) == ["nvidia"]
    assert registry.get("nvidia")._load()._api_key == "nim"
