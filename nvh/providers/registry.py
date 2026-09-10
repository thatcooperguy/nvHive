"""Provider registry: discovery, registration, and lookup."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

import yaml

from nvh.config.settings import CouncilConfig
from nvh.core.atohi import AdmissionBroker, AdmittedProvider, AtohiAdmission
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


def _keyring_enabled() -> bool:
    # Headless/rootless cloud desktops often have a slow or unavailable
    # keyring service, so startup never blocks on it unless asked to.
    return os.environ.get("NVH_USE_KEYRING", "0").lower() in {"1", "true", "yes"}


def resolve_provider_key(
    name: str, pconfig: Any = None, *, ptype: str | None = None,
) -> tuple[str | None, str]:
    """``(key, source)`` for a provider, or ``(None, "none")``.

    One resolution order for the registry, the adapters and the diagnostics:
    the config value (unless it is an unexpanded ``${VAR}`` placeholder),
    ``COUNCIL_<NAME>_API_KEY``, ``<NAME>_API_KEY``, the spec's ``env_keys``,
    the keyring when ``NVH_USE_KEYRING`` is set, then the spec's anonymous
    key. ``source`` is ``config``, ``env:<VAR>``, ``keyring`` or ``anonymous``.
    """
    configured = str(getattr(pconfig, "api_key", "") or "")
    if configured and not configured.startswith("${"):
        return configured, "config"
    spec = PROVIDER_SPECS.get(ptype or name)
    upper = name.upper()
    env_names = [f"COUNCIL_{upper}_API_KEY", f"{upper}_API_KEY"]
    if spec is not None:
        env_names.extend(v for v in spec.env_keys if v not in env_names)
    for env_name in env_names:
        value = os.environ.get(env_name, "")
        if value:
            return value, f"env:{env_name}"
    if _keyring_enabled():
        try:
            import keyring

            value = keyring.get_password("nvhive", f"{name}_api_key") or ""
        except Exception:
            value = ""
        if value:
            return value, "keyring"
    if spec is not None and spec.anonymous_key:
        return spec.anonymous_key, "anonymous"
    return None, "none"


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

    def __init__(self, *, atohi_broker: AdmissionBroker | None = None) -> None:
        self._providers: dict[str, Provider] = {}
        self._model_catalog: dict[str, ModelInfo] = {}
        self._atohi_broker = atohi_broker
        self._admission: AtohiAdmission | None = None
        self._provider_types: dict[str, str] = {}
        self._bound_admission = False
        self._admitted: dict[str, AdmittedProvider] = {}

    def _matches_admission(self, config: CouncilConfig) -> bool:
        return self._admission is not None and (
            self._admission.enabled == config.atohi.enabled
            and self._admission.managed_providers == frozenset(config.atohi.managed_providers)
            and self._provider_types == {
                name: value.type or name for name, value in config.providers.items()
            }
        )

    def scoped(self, config: CouncilConfig) -> ProviderRegistry:
        """Bind one caller's policy while preserving shared adapter registration.

        Engine and its Council reuse a matching bound view. Other callers share
        raw adapters/catalog, never policy or wrappers; later registrations from
        either the original registry or the engine remain immediately visible.
        """
        if self._bound_admission and self._matches_admission(config):
            return self
        view = ProviderRegistry(atohi_broker=self._atohi_broker)
        view._providers = self._providers
        view._model_catalog = self._model_catalog
        view.configure_admission(config)
        view._bound_admission = True
        return view

    def configure_admission(self, config: CouncilConfig) -> None:
        if self._bound_admission:
            if not self._matches_admission(config):
                raise ValueError("Bound admission policy cannot change; create a new scoped registry")
            return
        self._admission = AtohiAdmission(config.atohi, broker=self._atohi_broker)
        self._provider_types = {name: value.type or name for name, value in config.providers.items()}
        self._admitted.clear()

    def check_admission_available(self) -> None:
        """Fail before fan-out or initialization can contact any provider."""
        if self._admission is not None:
            self._admission.require_broker()

    @property
    def admission(self) -> AtohiAdmission:
        """The configured policy for trusted auxiliary calls owned by this view."""
        if self._admission is None:
            raise ValueError("Registry admission policy is not configured")
        return self._admission

    def register(self, name: str, provider: Provider) -> None:
        if isinstance(provider, AdmittedProvider):
            provider = provider.provider
        self._providers[name] = provider
        self._admitted.pop(name, None)

    def get(self, name: str) -> Provider:
        if name not in self._providers:
            raise KeyError(f"Provider '{name}' is not registered. Available: {list(self._providers.keys())}")
        provider = self._providers[name]
        if self._admission is not None and self._admission.manages(
            name, self._provider_types.get(name, name),
        ):
            wrapped = self._admitted.get(name)
            if wrapped is None or wrapped.provider is not provider:
                wrapped = AdmittedProvider(provider, self._admission, name)
                self._admitted[name] = wrapped
            return wrapped
        return provider

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
        self.configure_admission(config)
        self.check_admission_available()
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

            ptype = pconfig.type or name
            api_key = resolve_provider_key(name, pconfig, ptype=ptype)[0] or ""
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
