"""Compat shim, removed in 0.43: ``HuggingFaceProvider`` is ``OpenAICompatibleProvider`` bound to ``PROVIDER_SPECS["huggingface"]``."""
from nvh.providers.openai_compatible import PROVIDER_SPECS, OpenAICompatibleProvider


class HuggingFaceProvider(OpenAICompatibleProvider):
    def __init__(self, *args, **kwargs):
        super().__init__(PROVIDER_SPECS["huggingface"], *args, **kwargs)
