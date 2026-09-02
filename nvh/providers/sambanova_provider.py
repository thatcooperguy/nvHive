"""Compat shim, removed in 0.43: ``SambaNovProvider`` is ``OpenAICompatibleProvider`` bound to ``PROVIDER_SPECS["sambanova"]``."""
from nvh.providers.openai_compatible import PROVIDER_SPECS, OpenAICompatibleProvider


class SambaNovProvider(OpenAICompatibleProvider):
    def __init__(self, *args, **kwargs):
        super().__init__(PROVIDER_SPECS["sambanova"], *args, **kwargs)
