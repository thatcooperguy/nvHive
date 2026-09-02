"""Provider registry: discovery, registration, and lookup."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

import yaml

from nvh.config.settings import CouncilConfig
from nvh.providers.base import ModelInfo, Provider
from nvh.providers.lazy_provider import LazyProvider
from nvh.providers.specs import PROVIDER_SPECS

logger = logging.getLogger(__name__)

# Adapters with their own transport or discovery code. Every other provider is
# a PROVIDER_SPECS row served by OpenAICompatibleProvider.
BESPOKE_ADAPTERS: dict[str, tuple[str, str]] = {
    "ollama": ("nvh.providers.ollama_provider", "OllamaProvider"),
    "triton": ("nvh.providers.triton_provider", "TritonProvider"),
    "mock": ("nvh.providers.mock_provider", "MockProvider"),
}

# provider name -> date its service shut down. Stanzas left in a user's
# config are skipped here and removed by `nvh config migrate`.
RETIRED_PROVIDERS: dict[str, str] = {
    "github": "2026-07-30",
}


def lazy_adapter(name: str, ptype: str = "", **kwargs: Any) -> LazyProvider:
    """Wrap the adapter for ``name`` so it is imported on first use.

    Blank kwargs are dropped so the adapter's own defaults apply. Unknown
    types (including ``openai_compatible``) take the generic OpenAI route with
    the caller's ``base_url``.
    """
    kwargs = {k: v for k, v in kwargs.items() if v not in ("", None)}
    ptype = ptype or name
    if ptype in BESPOKE_ADAPTERS:
        module_path, class_name = BESPOKE_ADAPTERS[ptype]
        return LazyProvider(name, module_path, class_name, provider_name=name, **kwargs)
    spec = PROVIDER_SPECS.get(ptype, PROVIDER_SPECS["openai"])
    return LazyProvider(
        name,
        "nvh.providers.openai_compatible",
        "OpenAICompatibleProvider",
        spec=spec,
        provider_name=name,
        **kwargs,
    )


class ProviderRegistry:
    """Central registry for all LLM provider adapters."""

    def __init__(self) -> None:
        self._providers: dict[str, Provider] = {}
        self._model_catalog: dict[str, ModelInfo] = {}

    def register(self, name: str, provider: Provider) -> None:
        self._providers[name] = provider

    def get(self, name: str) -> Provider:
        if name not in self._providers:
            raise KeyError(f"Provider '{name}' is not registered. Available: {list(self._providers.keys())}")
        return self._providers[name]

    def list_providers(self) -> list[str]:
        return list(self._providers.keys())

    def list_enabled(self) -> list[str]:
        return list(self._providers.keys())

    def has(self, name: str) -> bool:
        return name in self._providers

    # -----------------------------------------------------------------------
    # Model Catalog
    # -----------------------------------------------------------------------

    def load_capabilities(self, path: Path | None = None) -> None:
        """Load the model capability catalog from YAML."""
        if path is None:
            path = Path(__file__).parent.parent / "config" / "capabilities.yaml"
        if not path.exists():
            return
        with open(path) as f:
            data = yaml.safe_load(f)
        models = data.get("models", {})
        for model_id, info in models.items():
            self._model_catalog[model_id] = ModelInfo(
                model_id=model_id,
                **info,
            )

    def get_model_info(self, model_id: str) -> ModelInfo | None:
        return self._model_catalog.get(model_id)

    def list_models(self, provider: str | None = None) -> list[ModelInfo]:
        models = list(self._model_catalog.values())
        if provider:
            models = [m for m in models if m.provider == provider]
        return models

    def get_models_for_provider(self, provider_name: str) -> list[ModelInfo]:
        return [m for m in self._model_catalog.values() if m.provider == provider_name]

    # -----------------------------------------------------------------------
    # Auto-setup from config
    # -----------------------------------------------------------------------

    def setup_from_config(self, config: CouncilConfig) -> list[str]:
        """Initialize provider adapters from config. Returns list of enabled provider names."""
        enabled = []

        for name, pconfig in config.providers.items():
            if not pconfig.enabled:
                continue
            if name in RETIRED_PROVIDERS:
                logger.warning(
                    "provider %s was retired on %s — remove it or run `nvh config migrate`",
                    name, RETIRED_PROVIDERS[name],
                )
                continue

            # Resolve API key: config value, then env var fallback
            api_key = pconfig.api_key
            if not api_key or api_key.startswith("${"):
                env_names = [
                    f"COUNCIL_{name.upper()}_API_KEY",
                    f"{name.upper()}_API_KEY",
                ]
                for env_name in env_names:
                    val = os.environ.get(env_name)
                    if val:
                        api_key = val
                        break

            # Try keyring as an opt-in fallback. Headless/rootless cloud desktops
            # often have a slow or unavailable keyring service, so startup should
            # not block on it by default.
            use_keyring = os.environ.get("NVH_USE_KEYRING", "0").lower() in {"1", "true", "yes"}
            if not api_key and use_keyring:
                try:
                    import keyring
                    api_key = keyring.get_password("nvhive", f"{name}_api_key") or ""
                except Exception:
                    pass

            ptype = pconfig.type or name
            if ptype == "mock":
                # Mock provider: construct directly without API key forwarding
                from nvh.providers.mock_provider import MockProvider

                provider: Provider = MockProvider(
                    default_model=pconfig.default_model or "mock/default",
                    fallback_model=pconfig.fallback_model or "mock/fast",
                    provider_name=name,
                )
            else:
                provider = lazy_adapter(
                    name,
                    ptype,
                    api_key=api_key,
                    default_model=pconfig.default_model,
                    fallback_model=pconfig.fallback_model,
                    base_url=pconfig.base_url,
                )
            self.register(name, provider)
            enabled.append(name)

        self.load_capabilities()
        return enabled


# Module-level singleton
_registry: ProviderRegistry | None = None


def get_registry() -> ProviderRegistry:
    global _registry
    if _registry is None:
        _registry = ProviderRegistry()
    return _registry


def reset_registry() -> None:
    global _registry
    _registry = None
