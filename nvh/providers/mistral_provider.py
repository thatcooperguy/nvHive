"""Compat shim, removed in 0.43: ``MistralProvider`` is ``OpenAICompatibleProvider`` bound to ``PROVIDER_SPECS["mistral"]``."""
from nvh.providers.openai_compatible import PROVIDER_SPECS, OpenAICompatibleProvider


class MistralProvider(OpenAICompatibleProvider):
    def __init__(self, *args, **kwargs):
        super().__init__(PROVIDER_SPECS["mistral"], *args, **kwargs)
