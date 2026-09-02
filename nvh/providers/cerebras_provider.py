"""Compat shim, removed in 0.43: ``CerebrasProvider`` is ``OpenAICompatibleProvider`` bound to ``PROVIDER_SPECS["cerebras"]``."""
from nvh.providers.openai_compatible import PROVIDER_SPECS, OpenAICompatibleProvider


class CerebrasProvider(OpenAICompatibleProvider):
    def __init__(self, *args, **kwargs):
        super().__init__(PROVIDER_SPECS["cerebras"], *args, **kwargs)
