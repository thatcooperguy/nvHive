"""Compat shim, removed in 0.43: ``CohereProvider`` is ``OpenAICompatibleProvider`` bound to ``PROVIDER_SPECS["cohere"]``."""
from nvh.providers.openai_compatible import PROVIDER_SPECS, OpenAICompatibleProvider


class CohereProvider(OpenAICompatibleProvider):
    def __init__(self, *args, **kwargs):
        super().__init__(PROVIDER_SPECS["cohere"], *args, **kwargs)
