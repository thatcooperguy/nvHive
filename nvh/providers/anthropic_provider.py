"""Compat shim, removed in 0.43: ``AnthropicProvider`` is ``OpenAICompatibleProvider`` bound to ``PROVIDER_SPECS["anthropic"]``."""
from nvh.providers.openai_compatible import PROVIDER_SPECS, OpenAICompatibleProvider


class AnthropicProvider(OpenAICompatibleProvider):
    def __init__(self, *args, **kwargs):
        super().__init__(PROVIDER_SPECS["anthropic"], *args, **kwargs)
