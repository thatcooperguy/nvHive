"""Per-provider facts for the LiteLLM-backed cloud adapters.

One :class:`ProviderSpec` row per provider; the behaviour lives once in
:class:`nvh.providers.openai_compatible.OpenAICompatibleProvider`. Ollama,
Triton and Mock have bespoke adapters and no row here. This module imports
nothing heavy so config, CLI and API code can read the table at startup.

Default and fallback IDs were verified against LiteLLM 1.99.0's model DB and
each provider's model docs for 0.41.1; the prefixes against
``litellm.get_llm_provider`` on the bare IDs for 0.42.1.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ProviderSpec:
    name: str
    default_model: str
    fallback_model: str
    # Route LiteLLM needs in front of every model ID sent to this provider;
    # :meth:`route` prepends it to IDs that lack it, so ``-m gpt-oss-120b``
    # works on a routed provider. Empty only where LiteLLM already infers the
    # provider from the bare ID (openai, anthropic, cohere).
    litellm_prefix: str = ""
    base_url: str | None = None
    # Extra env vars the key may live under, consulted after
    # COUNCIL_{NAME}_API_KEY and {NAME}_API_KEY by
    # :func:`nvh.providers.registry.resolve_provider_key`: LiteLLM's own
    # spelling where it differs, then the historical HIVE_{NAME}_API_KEY.
    env_keys: tuple[str, ...] = ()
    zero_cost: bool = False
    # Key sent when none is configured (providers with an anonymous tier).
    anonymous_key: str = ""
    # ISO date the provider, or the API surface this adapter uses, stops serving.
    sunset_date: str | None = None
    # What ``sunset_date`` retires, for the diagnostics row.
    sunset_note: str = ""
    # Request timeout in seconds; slow free tiers and NIM cold starts need more.
    timeout: int = 120
    # Model the one-token health ping uses when the provider has no free
    # /models endpoint; empty means ``default_model``.
    health_model: str = ""
    # LiteLLM surface the adapter calls: "chat" (``litellm.acompletion``) or
    # "responses" (``litellm.aresponses``, the OpenAI Responses API shape).
    api_surface: str = "chat"

    def route(self, model: str) -> str:
        """``model`` with :attr:`litellm_prefix` applied exactly once."""
        prefix = self.litellm_prefix
        if not prefix or model.startswith(prefix):
            return model
        # A partial route ("accounts/fireworks/models/x" under
        # "fireworks_ai/accounts/fireworks/models/") only gets the missing lead.
        parts = prefix.rstrip("/").split("/")
        for i in range(1, len(parts)):
            tail = "/".join(parts[i:]) + "/"
            if model.startswith(tail):
                return "/".join(parts[:i]) + "/" + model
        return prefix + model


_SPECS: tuple[ProviderSpec, ...] = (
    ProviderSpec("openai", "gpt-5.6-terra", "gpt-5.6-luna", env_keys=("HIVE_OPENAI_API_KEY",)),
    ProviderSpec(
        "anthropic",
        "claude-sonnet-5",
        "claude-haiku-4-5-20251001",
        env_keys=("HIVE_ANTHROPIC_API_KEY",),
    ),
    # Bare "gemini-*" IDs route to vertex_ai without the prefix.
    ProviderSpec(
        "google",
        "gemini/gemini-3.7-flash",
        "gemini/gemini-3.5-flash-lite",
        litellm_prefix="gemini/",
        env_keys=("GEMINI_API_KEY", "HIVE_GOOGLE_API_KEY"),
    ),
    ProviderSpec(
        "groq",
        "groq/openai/gpt-oss-120b",
        "groq/openai/gpt-oss-20b",
        litellm_prefix="groq/",
        env_keys=("HIVE_GROQ_API_KEY",),
    ),
    ProviderSpec(
        "grok",
        "xai/grok-4.6",
        "xai/grok-4.3",
        litellm_prefix="xai/",
        base_url="https://api.x.ai/v1",
        env_keys=("XAI_API_KEY", "HIVE_GROK_API_KEY"),
    ),
    ProviderSpec(
        "mistral",
        "mistral/mistral-large-latest",
        "mistral/mistral-small-latest",
        litellm_prefix="mistral/",
        env_keys=("HIVE_MISTRAL_API_KEY",),
    ),
    ProviderSpec(
        "cohere",
        "command-a-03-2025",
        "command-r-08-2024",
        env_keys=("CO_API_KEY", "HIVE_COHERE_API_KEY"),
    ),
    ProviderSpec(
        "deepseek",
        "deepseek/deepseek-v4-pro",
        "deepseek/deepseek-v4-flash",
        litellm_prefix="deepseek/",
        base_url="https://api.deepseek.com",
        env_keys=("HIVE_DEEPSEEK_API_KEY",),
    ),
    # Sonar Chat Completions retires 2026-09-27; the Agent API (Responses
    # shape, POST /v1/responses) replaces it. LiteLLM sends "preset/<name>" as
    # {"preset": name}; Perplexity maps Sonar Pro -> "low" and Sonar -> "fast"
    # (docs.perplexity.ai/docs/agent-api/migrate-from-sonar/overview).
    ProviderSpec(
        "perplexity",
        "perplexity/preset/low",
        "perplexity/preset/fast",
        litellm_prefix="perplexity/",
        env_keys=("PERPLEXITYAI_API_KEY", "HIVE_PERPLEXITY_API_KEY"),
        timeout=600,
        api_surface="responses",
    ),
    ProviderSpec(
        "together",
        "together_ai/openai/gpt-oss-120b",
        "together_ai/openai/gpt-oss-20b",
        litellm_prefix="together_ai/",
        env_keys=("TOGETHERAI_API_KEY", "HIVE_TOGETHER_API_KEY"),
    ),
    ProviderSpec(
        "fireworks",
        "fireworks_ai/accounts/fireworks/models/gpt-oss-120b",
        "fireworks_ai/accounts/fireworks/models/nemotron-lightning-3p5-30b-a3b",
        litellm_prefix="fireworks_ai/accounts/fireworks/models/",
        env_keys=("FIREWORKS_AI_API_KEY", "HIVE_FIREWORKS_API_KEY"),
    ),
    ProviderSpec(
        "openrouter",
        "openrouter/openai/gpt-oss-120b",
        "openrouter/openai/gpt-oss-20b",
        litellm_prefix="openrouter/",
        env_keys=("HIVE_OPENROUTER_API_KEY",),
    ),
    ProviderSpec(
        "cerebras",
        "cerebras/gpt-oss-120b",
        "cerebras/gpt-oss-120b",
        litellm_prefix="cerebras/",
        env_keys=("HIVE_CEREBRAS_API_KEY",),
    ),
    ProviderSpec(
        "sambanova",
        "sambanova/Meta-Llama-3.3-70B-Instruct",
        "sambanova/gpt-oss-120b",
        litellm_prefix="sambanova/",
        env_keys=("HIVE_SAMBANOVA_API_KEY",),
    ),
    ProviderSpec(
        "huggingface",
        "huggingface/openai/gpt-oss-120b",
        "huggingface/openai/gpt-oss-20b",
        litellm_prefix="huggingface/",
        env_keys=("HF_TOKEN", "HUGGINGFACE_API_KEY", "HIVE_HUGGINGFACE_API_KEY"),
    ),
    ProviderSpec(
        "ai21",
        "ai21_chat/jamba-large-1.7",
        "ai21_chat/jamba-mini-2",
        litellm_prefix="ai21_chat/",
        env_keys=("HIVE_AI21_API_KEY",),
    ),
    # Without the prefix LiteLLM parses "meta/..." as its own Llama-API provider.
    # NIM cold starts take minutes, and the 8B fallback answers the health ping
    # far quicker than the 70B default.
    ProviderSpec(
        "nvidia",
        "nvidia_nim/meta/llama-3.3-70b-instruct",
        "nvidia_nim/meta/llama-3.1-8b-instruct",
        litellm_prefix="nvidia_nim/",
        base_url="https://integrate.api.nvidia.com/v1",
        env_keys=("NIM_API_KEY", "HIVE_NVIDIA_API_KEY"),
        timeout=600,
        health_model="nvidia_nim/meta/llama-3.1-8b-instruct",
    ),
    # LiteLLM has no siliconflow/llm7 route; "openai/" selects its generic
    # OpenAI-compatible client and api_base points it at the host.
    ProviderSpec(
        "siliconflow",
        "Qwen/Qwen2.5-7B-Instruct",
        "Qwen/Qwen2.5-7B-Instruct",
        litellm_prefix="openai/",
        base_url="https://api.siliconflow.cn/v1",
        env_keys=("HIVE_SILICONFLOW_API_KEY",),
        zero_cost=True,
        timeout=600,
    ),
    ProviderSpec(
        "llm7",
        "gpt-oss",
        "minimax-m2.7",
        litellm_prefix="openai/",
        base_url="https://api.llm7.io/v1",
        env_keys=("HIVE_LLM7_API_KEY",),
        zero_cost=True,
        anonymous_key="anonymous",
        timeout=600,
    ),
)

PROVIDER_SPECS: dict[str, ProviderSpec] = {spec.name: spec for spec in _SPECS}
