"""Compat shim, removed in 0.43: ``AI21Provider`` is ``OpenAICompatibleProvider`` bound to ``PROVIDER_SPECS["ai21"]``."""
from nvh.providers.openai_compatible import PROVIDER_SPECS, OpenAICompatibleProvider


class AI21Provider(OpenAICompatibleProvider):
    def __init__(self, *args, **kwargs):
        super().__init__(PROVIDER_SPECS["ai21"], *args, **kwargs)
