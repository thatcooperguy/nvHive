"""Compat shim, removed in 0.43: ``TogetherProvider`` is ``OpenAICompatibleProvider`` bound to ``PROVIDER_SPECS["together"]``."""
from nvh.providers.openai_compatible import PROVIDER_SPECS, OpenAICompatibleProvider


class TogetherProvider(OpenAICompatibleProvider):
    def __init__(self, *args, **kwargs):
        super().__init__(PROVIDER_SPECS["together"], *args, **kwargs)
