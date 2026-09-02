"""Compat shim, removed in 0.43: ``DeepSeekProvider`` is ``OpenAICompatibleProvider`` bound to ``PROVIDER_SPECS["deepseek"]``."""
from nvh.providers.openai_compatible import PROVIDER_SPECS, OpenAICompatibleProvider


class DeepSeekProvider(OpenAICompatibleProvider):
    def __init__(self, *args, **kwargs):
        super().__init__(PROVIDER_SPECS["deepseek"], *args, **kwargs)
