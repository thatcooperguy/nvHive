"""Compat shim, removed in 0.43: ``LLM7Provider`` is ``OpenAICompatibleProvider`` bound to ``PROVIDER_SPECS["llm7"]``."""
from nvh.providers.openai_compatible import PROVIDER_SPECS, OpenAICompatibleProvider


class LLM7Provider(OpenAICompatibleProvider):
    def __init__(self, *args, **kwargs):
        super().__init__(PROVIDER_SPECS["llm7"], *args, **kwargs)
