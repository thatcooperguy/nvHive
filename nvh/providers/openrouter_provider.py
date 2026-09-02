"""Compat shim, removed in 0.43: ``OpenRouterProvider`` is ``OpenAICompatibleProvider`` bound to ``PROVIDER_SPECS["openrouter"]``."""
from nvh.providers.openai_compatible import PROVIDER_SPECS, OpenAICompatibleProvider


class OpenRouterProvider(OpenAICompatibleProvider):
    def __init__(self, *args, **kwargs):
        super().__init__(PROVIDER_SPECS["openrouter"], *args, **kwargs)
