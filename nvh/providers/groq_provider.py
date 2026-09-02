"""Compat shim, removed in 0.43: ``GroqProvider`` is ``OpenAICompatibleProvider`` bound to ``PROVIDER_SPECS["groq"]``."""
from nvh.providers.openai_compatible import PROVIDER_SPECS, OpenAICompatibleProvider


class GroqProvider(OpenAICompatibleProvider):
    def __init__(self, *args, **kwargs):
        super().__init__(PROVIDER_SPECS["groq"], *args, **kwargs)
