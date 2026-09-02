"""Compat shim, removed in 0.43: ``FireworksProvider`` is ``OpenAICompatibleProvider`` bound to ``PROVIDER_SPECS["fireworks"]``."""
from nvh.providers.openai_compatible import PROVIDER_SPECS, OpenAICompatibleProvider


class FireworksProvider(OpenAICompatibleProvider):
    def __init__(self, *args, **kwargs):
        super().__init__(PROVIDER_SPECS["fireworks"], *args, **kwargs)
