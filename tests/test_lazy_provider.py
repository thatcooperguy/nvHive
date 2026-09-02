"""Tests for lazy provider startup behavior."""

from __future__ import annotations

import sys
import types

import pytest

from nvh.config.settings import CouncilConfig, ProviderConfig
from nvh.providers.base import HealthStatus
from nvh.providers.lazy_provider import LazyProvider
from nvh.providers.registry import ProviderRegistry


@pytest.mark.asyncio
async def test_lazy_provider_imports_only_when_used(monkeypatch) -> None:
    constructed: list[str] = []
    module = types.ModuleType("tests.fake_lazy_provider")

    class FakeProvider:
        def __init__(self, marker: str) -> None:
            constructed.append(marker)

        async def health_check(self) -> HealthStatus:
            return HealthStatus(provider="fake", healthy=True)

        def estimate_tokens(self, text: str) -> int:
            return len(text.split())

    module.FakeProvider = FakeProvider
    monkeypatch.setitem(sys.modules, "tests.fake_lazy_provider", module)

    provider = LazyProvider(
        "fake",
        "tests.fake_lazy_provider",
        "FakeProvider",
        marker="loaded",
    )

    assert provider.name == "fake"
    assert constructed == []
    assert provider.estimate_tokens("hello local model") == 3
    assert constructed == ["loaded"]
    assert (await provider.health_check()).healthy is True


def test_registry_keeps_configured_providers_lazy(monkeypatch) -> None:
    class ExplodingLoader:
        def import_module(self, module_name: str):
            if module_name == "nvh.providers.openai_compatible":
                raise AssertionError("configured providers should not import during registry setup")
            return __import__(module_name, fromlist=["*"])

    monkeypatch.setattr("importlib.import_module", ExplodingLoader().import_module)

    config = CouncilConfig(
        providers={
            "openai": ProviderConfig(
                type="openai",
                enabled=True,
                api_key="test",
                default_model="gpt-test",
            )
        }
    )

    registry = ProviderRegistry()
    enabled = registry.setup_from_config(config)

    assert enabled == ["openai"]
    assert isinstance(registry.get("openai"), LazyProvider)
