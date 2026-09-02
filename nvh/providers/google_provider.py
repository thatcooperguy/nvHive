"""Compat shim, removed in 0.43: ``GoogleProvider`` is ``OpenAICompatibleProvider`` bound to ``PROVIDER_SPECS["google"]``."""
from nvh.providers.openai_compatible import PROVIDER_SPECS, OpenAICompatibleProvider


class GoogleProvider(OpenAICompatibleProvider):
    def __init__(self, *args, **kwargs):
        super().__init__(PROVIDER_SPECS["google"], *args, **kwargs)
