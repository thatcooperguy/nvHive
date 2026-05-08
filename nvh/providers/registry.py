"""Provider registry: discovery, registration, and lookup."""

from __future__ import annotations

import os
from pathlib import Path

import yaml

from nvh.config.settings import CouncilConfig
from nvh.providers.base import ModelInfo, Provider


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
        import importlib

        from nvh.providers.lazy_provider import LazyProvider

        enabled = []

        provider_specs: dict[str, tuple[str, str]] = {
            "openai": ("nvh.providers.openai_provider", "OpenAIProvider"),
            "anthropic": ("nvh.providers.anthropic_provider", "AnthropicProvider"),
            "google": ("nvh.providers.google_provider", "GoogleProvider"),
            "ollama": ("nvh.providers.ollama_provider", "OllamaProvider"),
            "groq": ("nvh.providers.groq_provider", "GroqProvider"),
            "grok": ("nvh.providers.grok_provider", "GrokProvider"),
            "mistral": ("nvh.providers.mistral_provider", "MistralProvider"),
            "cohere": ("nvh.providers.cohere_provider", "CohereProvider"),
            "deepseek": ("nvh.providers.deepseek_provider", "DeepSeekProvider"),
            "mock": ("nvh.providers.mock_provider", "MockProvider"),
            "perplexity": ("nvh.providers.perplexity_provider", "PerplexityProvider"),
            "together": ("nvh.providers.together_provider", "TogetherProvider"),
            "fireworks": ("nvh.providers.fireworks_provider", "FireworksProvider"),
            "openrouter": ("nvh.providers.openrouter_provider", "OpenRouterProvider"),
            "cerebras": ("nvh.providers.cerebras_provider", "CerebrasProvider"),
            "sambanova": ("nvh.providers.sambanova_provider", "SambaNovProvider"),
            "huggingface": ("nvh.providers.huggingface_provider", "HuggingFaceProvider"),
            "ai21": ("nvh.providers.ai21_provider", "AI21Provider"),
            "github": ("nvh.providers.github_provider", "GitHubProvider"),
            "nvidia": ("nvh.providers.nvidia_provider", "NvidiaProvider"),
            "siliconflow": ("nvh.providers.siliconflow_provider", "SiliconFlowProvider"),
            "llm7": ("nvh.providers.llm7_provider", "LLM7Provider"),
            "triton": ("nvh.providers.triton_provider", "TritonProvider"),
        }

        for name, pconfig in config.providers.items():
            if not pconfig.enabled:
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

            # Determine provider class
            ptype = pconfig.type or name
            if ptype == "openai_compatible":
                ptype = "openai"

            spec = provider_specs.get(ptype)
            if spec is None:
                spec = provider_specs.get("openai")
                if spec is None:
                    continue
            module_path, class_name = spec

            # Mock provider: construct directly without API key forwarding
            if ptype == "mock":
                mod = importlib.import_module(module_path)
                cls = getattr(mod, class_name)
                provider = cls(
                    default_model=pconfig.default_model or "mock/default",
                    fallback_model=pconfig.fallback_model or "mock/fast",
                    provider_name=name,
                )
                self.register(name, provider)
                enabled.append(name)
                continue

            provider = LazyProvider(
                name,
                module_path,
                class_name,
                api_key=api_key,
                default_model=pconfig.default_model,
                fallback_model=pconfig.fallback_model,
                base_url=pconfig.base_url or None,
                provider_name=name,
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
