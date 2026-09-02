"""Compat shim, removed in 0.43: ``OpenAIProvider`` is ``OpenAICompatibleProvider`` bound to ``PROVIDER_SPECS["openai"]``."""
from nvh.providers.openai_compatible import PROVIDER_SPECS, OpenAICompatibleProvider


class OpenAIProvider(OpenAICompatibleProvider):
    def __init__(self, *args, **kwargs):
        super().__init__(PROVIDER_SPECS["openai"], *args, **kwargs)
