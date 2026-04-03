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
        from nvh.providers.ai21_provider import AI21Provider
        from nvh.providers.anthropic_provider import AnthropicProvider
        from nvh.providers.cerebras_provider import CerebrasProvider
        from nvh.providers.cohere_provider import CohereProvider
        from nvh.providers.deepseek_provider import DeepSeekProvider
        from nvh.providers.fireworks_provider import FireworksProvider
        from nvh.providers.github_provider import GitHubProvider
        from nvh.providers.google_provider import GoogleProvider
        from nvh.providers.grok_provider import GrokProvider
        from nvh.providers.groq_provider import GroqProvider
        from nvh.providers.huggingface_provider import HuggingFaceProvider
        from nvh.providers.llm7_provider import LLM7Provider
        from nvh.providers.mistral_provider import MistralProvider
        from nvh.providers.mock_provider import MockProvider
        from nvh.providers.nvidia_provider import NvidiaProvider
        from nvh.providers.ollama_provider import OllamaProvider
        from nvh.providers.openai_provider import OpenAIProvider
        from nvh.providers.openrouter_provider import OpenRouterProvider
        from nvh.providers.perplexity_provider import PerplexityProvider
        from nvh.providers.sambanova_provider import SambaNovProvider
        from nvh.providers.siliconflow_provider import SiliconFlowProvider
        from nvh.providers.together_provider import TogetherProvider
        from nvh.providers.triton_provider import TritonProvider

        enabled = []

        provider_classes: dict[str, type] = {
            "openai": OpenAIProvider,
            "anthropic": AnthropicProvider,
            "google": GoogleProvider,
            "ollama": OllamaProvider,
            "groq": GroqProvider,
            "grok": GrokProvider,
            "mistral": MistralProvider,
            "cohere": CohereProvider,
            "deepseek": DeepSeekProvider,
            "mock": MockProvider,
            "perplexity": PerplexityProvider,
            "together": TogetherProvider,
            "fireworks": FireworksProvider,
            "openrouter": OpenRouterProvider,
            "cerebras": CerebrasProvider,
            "sambanova": SambaNovProvider,
            "huggingface": HuggingFaceProvider,
            "ai21": AI21Provider,
            "github": GitHubProvider,
            "nvidia": NvidiaProvider,
            "siliconflow": SiliconFlowProvider,
            "llm7": LLM7Provider,
            "triton": TritonProvider,
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

            # Try keyring as fallback
            if not api_key:
                try:
                    import keyring
                    api_key = keyring.get_password("nvhive", f"{name}_api_key") or ""
                except Exception:
                    pass

            # Determine provider class
            ptype = pconfig.type or name
            if ptype == "openai_compatible":
                ptype = "openai"

            # Mock provider: construct directly without API key forwarding
            if ptype == "mock":
                provider = MockProvider(
                    default_model=pconfig.default_model or "mock/default",
                    fallback_model=pconfig.fallback_model or "mock/fast",
                    provider_name=name,
                )
                self.register(name, provider)
                enabled.append(name)
                continue

            cls = provider_classes.get(ptype)
            if cls is None:
                # Try openai_compatible for unknown types
                cls = provider_classes.get("openai")
                if cls is None:
                    continue

            provider = cls(
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
