"""Compat shim, removed in 0.43: ``NvidiaProvider`` is ``OpenAICompatibleProvider`` bound to ``PROVIDER_SPECS["nvidia"]``."""
from nvh.providers.openai_compatible import PROVIDER_SPECS, OpenAICompatibleProvider


class NvidiaProvider(OpenAICompatibleProvider):
    def __init__(self, *args, **kwargs):
        super().__init__(PROVIDER_SPECS["nvidia"], *args, **kwargs)
