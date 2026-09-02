"""Compat shim, removed in 0.43: ``SiliconFlowProvider`` is ``OpenAICompatibleProvider`` bound to ``PROVIDER_SPECS["siliconflow"]``."""
from nvh.providers.openai_compatible import PROVIDER_SPECS, OpenAICompatibleProvider


class SiliconFlowProvider(OpenAICompatibleProvider):
    def __init__(self, *args, **kwargs):
        super().__init__(PROVIDER_SPECS["siliconflow"], *args, **kwargs)
