"""Compat shim, removed in 0.43: ``PerplexityProvider`` is ``OpenAICompatibleProvider`` bound to ``PROVIDER_SPECS["perplexity"]``."""
from nvh.providers.openai_compatible import PROVIDER_SPECS, OpenAICompatibleProvider


class PerplexityProvider(OpenAICompatibleProvider):
    def __init__(self, *args, **kwargs):
        super().__init__(PROVIDER_SPECS["perplexity"], *args, **kwargs)
