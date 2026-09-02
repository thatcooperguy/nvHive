"""Leftover stanzas for retired providers must not become live adapters.

Before 0.41.1 an unknown provider name fell through to OpenAIProvider, so a
0.41.0 ``github:`` block built a LazyProvider at the dead endpoint and the
fallback chain kept hitting it on every query.
"""

from __future__ import annotations

import logging

from nvh.config.settings import CouncilConfig, ProviderConfig
from nvh.providers.registry import PROVIDER_SPECS, RETIRED_PROVIDERS, ProviderRegistry


def test_retired_provider_stanza_is_skipped_with_a_warning(caplog) -> None:
    config = CouncilConfig(
        providers={
            "github": ProviderConfig(api_key="ghp_x", default_model="gpt-4o-mini", enabled=True),
            "mock": ProviderConfig(default_model="mock/default", enabled=True),
        }
    )
    registry = ProviderRegistry()
    with caplog.at_level(logging.WARNING, logger="nvh.providers.registry"):
        enabled = registry.setup_from_config(config)

    assert enabled == ["mock"]
    assert not registry.has("github")
    assert "provider github was retired on 2026-07-30" in caplog.text
    assert "nvh config migrate" in caplog.text


def test_disabled_retired_stanza_is_silent(caplog) -> None:
    config = CouncilConfig(providers={"github": ProviderConfig(enabled=False)})
    with caplog.at_level(logging.WARNING, logger="nvh.providers.registry"):
        assert ProviderRegistry().setup_from_config(config) == []
    assert caplog.text == ""


def test_retired_providers_have_no_adapter_spec() -> None:
    assert not set(RETIRED_PROVIDERS) & set(PROVIDER_SPECS)
