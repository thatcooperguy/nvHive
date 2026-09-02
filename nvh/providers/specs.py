"""Per-provider facts for the LiteLLM-backed cloud adapters.

One :class:`ProviderSpec` row per provider; the behaviour lives once in
:class:`nvh.providers.openai_compatible.OpenAICompatibleProvider`. Ollama,
Triton and Mock have bespoke adapters and no row here. This module imports
nothing heavy so config, CLI and API code can read the table at startup.

Default and fallback IDs were verified against LiteLLM 1.99.0's model DB and
each provider's model docs for 0.41.1.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ProviderSpec:
    name: str
    default_model: str
    fallback_model: str
    # Route prefix LiteLLM needs on every model ID sent to this provider; it is
    # prepended to any ID that lacks it. Empty when the shipped IDs already
    # carry their route ("groq/...") or LiteLLM infers it from the bare name.
    litellm_prefix: str = ""
    base_url: str | None = None
    # Env vars the adapter consults itself when no key is passed. The registry
    # already checks {NAME}_API_KEY / COUNCIL_{NAME}_API_KEY and LiteLLM reads
    # its own conventional variables, so only extra names belong here.
    env_keys: tuple[str, ...] = ()
    zero_cost: bool = False
    # Key sent when none is configured (providers with an anonymous tier).
    anonymous_key: str = ""
    # ISO date the provider, or the API surface this adapter uses, stops serving.
    sunset_date: str | None = None


_SPECS: tuple[ProviderSpec, ...] = (
    ProviderSpec("openai", "gpt-5.6-terra", "gpt-5.6-luna"),
    ProviderSpec("anthropic", "claude-sonnet-5", "claude-haiku-4-5-20251001"),
    ProviderSpec("google", "gemini/gemini-3.7-flash", "gemini/gemini-3.5-flash-lite"),
    ProviderSpec("groq", "groq/openai/gpt-oss-120b", "groq/openai/gpt-oss-20b"),
    ProviderSpec("grok", "xai/grok-4.6", "xai/grok-4.3", base_url="https://api.x.ai/v1"),
    ProviderSpec("mistral", "mistral/mistral-large-latest", "mistral/mistral-small-latest"),
    ProviderSpec("cohere", "command-a-03-2025", "command-r-08-2024"),
    ProviderSpec(
        "deepseek",
        "deepseek/deepseek-v4-pro",
        "deepseek/deepseek-v4-flash",
        base_url="https://api.deepseek.com",
    ),
    # Sonar Chat Completions is retired on this date (docs.perplexity.ai/getting-started/models).
    ProviderSpec("perplexity", "perplexity/sonar-pro", "perplexity/sonar", sunset_date="2026-09-27"),
    ProviderSpec("together", "together_ai/openai/gpt-oss-120b", "together_ai/openai/gpt-oss-20b"),
    ProviderSpec(
        "fireworks",
        "fireworks_ai/accounts/fireworks/models/gpt-oss-120b",
        "fireworks_ai/accounts/fireworks/models/nemotron-lightning-3p5-30b-a3b",
    ),
    ProviderSpec("openrouter", "openrouter/openai/gpt-oss-120b", "openrouter/openai/gpt-oss-20b"),
    ProviderSpec("cerebras", "cerebras/gpt-oss-120b", "cerebras/gpt-oss-120b"),
    ProviderSpec("sambanova", "sambanova/Meta-Llama-3.3-70B-Instruct", "sambanova/gpt-oss-120b"),
    ProviderSpec("huggingface", "huggingface/openai/gpt-oss-120b", "huggingface/openai/gpt-oss-20b"),
    ProviderSpec("ai21", "ai21_chat/jamba-large-1.7", "ai21_chat/jamba-mini-2"),
    # Without the prefix LiteLLM parses "meta/..." as its own Llama-API provider.
    ProviderSpec(
        "nvidia",
        "nvidia_nim/meta/llama-3.3-70b-instruct",
        "nvidia_nim/meta/llama-3.1-8b-instruct",
        litellm_prefix="nvidia_nim/",
        base_url="https://integrate.api.nvidia.com/v1",
        env_keys=("NVIDIA_API_KEY", "NIM_API_KEY", "HIVE_NVIDIA_API_KEY"),
    ),
    # LiteLLM has no siliconflow/llm7 route; "openai/" selects its generic
    # OpenAI-compatible client and api_base points it at the host.
    ProviderSpec(
        "siliconflow",
        "Qwen/Qwen2.5-7B-Instruct",
        "Qwen/Qwen2.5-7B-Instruct",
        litellm_prefix="openai/",
        base_url="https://api.siliconflow.cn/v1",
        env_keys=("SILICONFLOW_API_KEY", "HIVE_SILICONFLOW_API_KEY"),
        zero_cost=True,
    ),
    ProviderSpec(
        "llm7",
        "gpt-oss",
        "minimax-m2.7",
        litellm_prefix="openai/",
        base_url="https://api.llm7.io/v1",
        env_keys=("LLM7_API_KEY", "HIVE_LLM7_API_KEY"),
        zero_cost=True,
        anonymous_key="anonymous",
    ),
)

PROVIDER_SPECS: dict[str, ProviderSpec] = {spec.name: spec for spec in _SPECS}
