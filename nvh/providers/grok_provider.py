"""Compat shim, removed in 0.43: ``GrokProvider`` is ``OpenAICompatibleProvider`` bound to ``PROVIDER_SPECS["grok"]``."""
from nvh.providers.openai_compatible import PROVIDER_SPECS, OpenAICompatibleProvider


class GrokProvider(OpenAICompatibleProvider):
    def __init__(self, *args, **kwargs):
        super().__init__(PROVIDER_SPECS["grok"], *args, **kwargs)
