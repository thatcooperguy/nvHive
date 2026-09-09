"""Per-provider facts: one :class:`ProviderSpec` row per provider.

The LiteLLM-backed cloud adapters share one behaviour class,
:class:`nvh.providers.openai_compatible.OpenAICompatibleProvider`, and read
their routing facts (models, prefix, base URL, env vars) from
:data:`PROVIDER_SPECS`. Ollama, Triton and Mock have bespoke adapters
(``nvh.providers.registry.BESPOKE_ADAPTERS``) and no row there; their
*descriptive* facts sit in :data:`BESPOKE_SPECS` so every table that
describes providers has exactly one source. :data:`PROVIDER_FACTS` is the
union.

Since 0.44 (design D4) everything the CLI, API, docs and router used to
hand-type per provider is a field here: :mod:`nvh.core.free_tier`,
:mod:`nvh.providers.quota_info`, :mod:`nvh.core.advisor_profiles` and the
proxy's model->provider map derive their tables from these rows, and
``scripts/gen_providers_doc.py`` renders the table in docs/PROVIDERS.md.
Change a fact here and every copy follows; ``tests/test_provider_docs_parity.py``
fails when the committed doc lags.

This module imports nothing heavy so config, CLI and API code can read the
table at startup.

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

    # --- Descriptive facts (0.44, D4) -------------------------------------
    # Name shown in docs, ``nvh keys``, advisor cards and the dashboard.
    display_name: str = ""
    # The ``<NAME>_API_KEY`` variable docs and setup name first. ``None``
    # derives ``{NAME}_API_KEY`` from ``name``; ``""`` means no key is taken.
    key_env: str | None = None
    # Where the key is issued.
    signup_url: str = ""
    # Where credits or a higher tier are bought; empty falls back to
    # ``signup_url`` (:attr:`upgrade_url`).
    billing_url: str = ""
    free_tier: bool = False
    # One-line quota or pricing note: the free tier's limits, or for a paid
    # provider the access note (``nvh keys``, the docs table, the advisor
    # card, rate-limit text).
    free_info: str = ""
    # Rate-limit text (:mod:`nvh.providers.quota_info`) where the per-tier
    # wording is wrong for this provider: when its limits reset, and what to
    # do for more. Empty means the tier's default sentence.
    reset_hint: str = ""
    upgrade_hint: str = ""
    # Position on the free-tier ladder :mod:`nvh.core.free_tier` walks
    # (1 = tried first); 0 keeps a free provider off the ladder (mock).
    free_tier_rank: int = 0
    # "free" | "budget" | "standard" | "premium" (advisor cards, budget routing).
    cost_tier: str = "standard"
    # Routing weights, 0-1, higher preferred (the router's profile bonus).
    quality_weight: float = 0.75
    speed_weight: float = 0.75
    cost_weight: float = 0.75
    reliability_weight: float = 0.75
    # Advisor-card prose: when to use this provider and when not to.
    strengths: tuple[str, ...] = ()
    best_for: tuple[str, ...] = ()
    weaknesses: tuple[str, ...] = ()
    avoid_for: tuple[str, ...] = ()
    # Capability flags the router and ``nvh advisor info`` read.
    has_search: bool = False
    is_local: bool = False
    is_fast: bool = False
    is_reasoning: bool = False
    long_context: bool = False
    # Bare model-ID families the OpenAI-compatible proxy routes here
    # ("gpt-4o-2024-11-20" -> openai) before asking LiteLLM.
    model_prefixes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.key_env is None:
            object.__setattr__(self, "key_env", f"{self.name.upper()}_API_KEY")
        if not self.display_name:
            object.__setattr__(self, "display_name", self.name)

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

    @property
    def alt_key_envs(self) -> tuple[str, ...]:
        """The provider's own key variables besides :attr:`key_env` (the legacy ``HIVE_*`` spellings excluded)."""
        return tuple(k for k in self.env_keys if k != self.key_env and not k.startswith("HIVE_"))

    @property
    def upgrade_url(self) -> str:
        """Where to buy credits or raise the tier: :attr:`billing_url`, else :attr:`signup_url`."""
        return self.billing_url or self.signup_url

    @property
    def quota_tier(self) -> str:
        """``"anonymous"`` (works without a key), ``"free"`` or ``"paid"``."""
        if self.anonymous_key:
            return "anonymous"
        return "free" if self.free_tier else "paid"

    # The advisor-card spellings (:mod:`nvh.core.advisor_profiles`).

    @property
    def has_free_tier(self) -> bool:
        """:attr:`free_tier` under the card's name."""
        return self.free_tier

    @property
    def free_tier_limits(self) -> str:
        """:attr:`free_info` under the card's name — the free tier's limits, or a paid provider's access note."""
        return self.free_info


_SPECS: tuple[ProviderSpec, ...] = (
    ProviderSpec(
        "openai",
        "gpt-5.6-terra",
        "gpt-5.6-luna",
        env_keys=("HIVE_OPENAI_API_KEY",),
        display_name="OpenAI",
        signup_url="https://platform.openai.com/api-keys",
        billing_url="https://platform.openai.com/settings/organization/billing",
        cost_tier="standard",
        quality_weight=0.90,
        speed_weight=0.75,
        cost_weight=0.40,
        reliability_weight=0.95,
        strengths=(
            "Excellent instruction following",
            "Strong code generation across all languages",
            "Best-in-class multimodal (vision, audio)",
            "Consistent output formatting (JSON, structured)",
            "Largest ecosystem and tooling support",
        ),
        best_for=(
            "Code generation and debugging",
            "Structured data extraction (JSON, CSV)",
            "Multimodal tasks (image analysis, charts)",
            "API and tool use integration",
            "General-purpose tasks when reliability matters",
        ),
        weaknesses=(
            "Expensive for high-volume usage",
            "Creative writing can feel generic/safe",
            "Reasoning models (o3) are slow and costly",
            "Rate limits on free tier are strict",
        ),
        avoid_for=(
            "Budget-constrained batch processing",
            "Nuanced creative writing (Claude is better)",
            "Tasks requiring web search (use Perplexity)",
            "When privacy matters (use local Ollama instead)",
        ),
        # The families the proxy table carried, collapsed to the shortest
        # prefixes with the same startswith() matches.
        model_prefixes=("gpt-5", "gpt-4", "gpt-3.5-turbo", "o1", "o3"),
    ),
    ProviderSpec(
        "anthropic",
        "claude-sonnet-5",
        "claude-haiku-4-5-20251001",
        env_keys=("HIVE_ANTHROPIC_API_KEY",),
        display_name="Anthropic",
        signup_url="https://console.anthropic.com/settings/keys",
        billing_url="https://console.anthropic.com/settings/billing",
        cost_tier="premium",
        quality_weight=0.92,
        speed_weight=0.70,
        cost_weight=0.35,
        reliability_weight=0.92,
        strengths=(
            "Best-in-class reasoning and analysis",
            "Superior creative and long-form writing",
            "Excellent code review and architecture advice",
            "Strong instruction following with nuance",
            "200K context window for large documents",
        ),
        best_for=(
            "Complex reasoning and analysis",
            "Creative writing (stories, essays, copy)",
            "Code review and architecture decisions",
            "Long document analysis (200K context)",
            "Nuanced questions requiring careful thought",
        ),
        weaknesses=(
            "Most expensive per-token for top models",
            "No free tier available",
            "Can be overly cautious / refuse edge cases",
            "Slower than speed-optimized providers",
        ),
        avoid_for=(
            "Simple factual queries (overkill, use cheaper model)",
            "Budget-constrained high-volume tasks",
            "Real-time / low-latency requirements (use Groq)",
            "Web search augmented queries (use Perplexity)",
        ),
        long_context=True,
        model_prefixes=("claude-3", "claude-sonnet-5", "claude-sonnet-4", "claude-opus-4", "claude-haiku-4"),
    ),
    # Bare "gemini-*" IDs route to vertex_ai without the prefix.
    ProviderSpec(
        "google",
        "gemini/gemini-3.7-flash",
        "gemini/gemini-3.5-flash-lite",
        litellm_prefix="gemini/",
        env_keys=("GEMINI_API_KEY", "HIVE_GOOGLE_API_KEY"),
        display_name="Google Gemini",
        signup_url="https://aistudio.google.com/apikey",
        free_tier=True,
        free_info="Free tier: 15 req/min, 1M tokens/day",
        reset_hint="Per-minute limits reset every 60 seconds; daily limits reset at midnight PT",
        upgrade_hint="Create a new API key in a fresh project, or enable billing",
        free_tier_rank=3,
        cost_tier="budget",
        quality_weight=0.85,
        speed_weight=0.80,
        cost_weight=0.75,
        reliability_weight=0.85,
        strengths=(
            "1M token context window (largest available)",
            "Excellent multimodal (native image/video/audio)",
            "Strong math and scientific reasoning",
            "Very cost-effective (Flash model is extremely cheap)",
            "Free tier: 15 requests/minute",
        ),
        best_for=(
            "Very long document analysis (books, codebases)",
            "Multimodal tasks (images, diagrams, screenshots)",
            "Math and science questions",
            "Cost-sensitive bulk processing (use Flash)",
            "When you need free tier access",
        ),
        weaknesses=(
            "Creative writing less nuanced than Claude",
            "Safety filters can be overly aggressive",
            "Code generation slightly behind OpenAI/Anthropic",
            "API can be inconsistent on complex instructions",
        ),
        avoid_for=(
            "Sensitive/edgy creative content (safety filters)",
            "Production code generation for critical systems",
            "Tasks requiring precise instruction following",
        ),
        long_context=True,
        model_prefixes=("gemini-3", "gemini-2.5", "gemini-2.0", "gemini-1.5-pro", "gemini-1.5-flash", "gemini-pro"),
    ),
    ProviderSpec(
        "groq",
        "groq/openai/gpt-oss-120b",
        "groq/openai/gpt-oss-20b",
        litellm_prefix="groq/",
        env_keys=("HIVE_GROQ_API_KEY",),
        display_name="Groq",
        signup_url="https://console.groq.com/keys",
        billing_url="https://console.groq.com/settings/billing",
        free_tier=True,
        free_info="Free tier: 30 req/min, 14.4K tok/min",
        free_tier_rank=2,
        cost_tier="free",
        quality_weight=0.75,
        speed_weight=0.99,
        cost_weight=0.85,
        reliability_weight=0.80,
        strengths=(
            "Fastest inference available (100-200ms latency)",
            "Free tier with generous limits",
            "Runs open-source models (Llama, Mixtral, Gemma)",
            "Extremely low cost per token",
            "Great for interactive/real-time use",
        ),
        best_for=(
            "Quick questions needing instant answers",
            "Interactive chat (REPL, conversational)",
            "High-volume batch processing (cheap + fast)",
            "When speed matters more than absolute quality",
            "Free tier usage for students",
        ),
        weaknesses=(
            "Quality limited by the underlying open-source models",
            "No proprietary model advantage (it's Llama/Mixtral)",
            "Short context on some models (8K for SpecDec)",
            "Rate limits can hit during heavy usage",
        ),
        avoid_for=(
            "Tasks requiring frontier model quality (use OpenAI/Anthropic)",
            "Very long context analysis (limited windows)",
            "Multimodal tasks (no vision support)",
            "Nuanced creative writing",
        ),
        is_fast=True,
        model_prefixes=("llama-3", "mixtral-8x7b", "mixtral-8x22b"),
    ),
    ProviderSpec(
        "grok",
        "xai/grok-4.6",
        "xai/grok-4.3",
        litellm_prefix="xai/",
        base_url="https://api.x.ai/v1",
        env_keys=("XAI_API_KEY", "HIVE_GROK_API_KEY"),
        display_name="Grok (xAI)",
        key_env="XAI_API_KEY",
        signup_url="https://console.x.ai",
        cost_tier="standard",
        quality_weight=0.85,
        speed_weight=0.75,
        cost_weight=0.45,
        reliability_weight=0.80,
        strengths=(
            "Strong reasoning and analytical capabilities",
            "Good at conversational and witty responses",
            "Less restrictive content policies",
            "Competitive with GPT-4 class models",
        ),
        best_for=(
            "Analysis and reasoning tasks",
            "Conversational AI with personality",
            "Tasks where other models refuse (content policy)",
            "General knowledge questions",
        ),
        weaknesses=(
            "Newer provider, less battle-tested",
            "No free tier",
            "Smaller model ecosystem than OpenAI",
            "No vision/multimodal support yet",
        ),
        avoid_for=(
            "Budget-constrained usage (no free tier)",
            "Multimodal tasks (no vision)",
            "Enterprise use requiring long track record",
        ),
        model_prefixes=("grok-4", "grok-3"),
    ),
    ProviderSpec(
        "mistral",
        "mistral/mistral-large-latest",
        "mistral/mistral-small-latest",
        litellm_prefix="mistral/",
        env_keys=("HIVE_MISTRAL_API_KEY",),
        display_name="Mistral",
        signup_url="https://console.mistral.ai/api-keys",
        billing_url="https://console.mistral.ai/billing",
        free_tier=True,
        free_info="Free Experiment plan: 2 RPM, 1B tokens/month",
        free_tier_rank=4,
        cost_tier="budget",
        quality_weight=0.80,
        speed_weight=0.82,
        cost_weight=0.70,
        reliability_weight=0.82,
        strengths=(
            "Strong multilingual capabilities (European languages)",
            "Good code generation",
            "Cost-effective for the quality level",
            "Fast inference on Small model",
            "Good at structured output",
            "Free Experiment plan available",
        ),
        best_for=(
            "Multilingual tasks (French, German, Spanish, etc.)",
            "Code generation at a good price point",
            "European data residency requirements",
            "Cost-effective general purpose tasks",
            "Free tier experimentation (2 RPM)",
        ),
        weaknesses=(
            "Not the best at English creative writing",
            "No vision/multimodal support",
            "Smaller model selection than OpenAI",
            "Free tier is very rate limited (2 RPM)",
        ),
        avoid_for=(
            "Multimodal tasks (no vision)",
            "English-only creative writing (Claude is better)",
            "Very long context analysis",
            "High-volume tasks on free tier",
        ),
        model_prefixes=("mistral-large", "mistral-medium", "mistral-small"),
    ),
    ProviderSpec(
        "cohere",
        "command-a-03-2025",
        "command-r-08-2024",
        env_keys=("CO_API_KEY", "HIVE_COHERE_API_KEY"),
        display_name="Cohere",
        signup_url="https://dashboard.cohere.com/api-keys",
        billing_url="https://dashboard.cohere.com/billing",
        free_tier=True,
        free_info="Trial API key included on signup",
        free_tier_rank=5,
        cost_tier="budget",
        quality_weight=0.72,
        speed_weight=0.75,
        cost_weight=0.70,
        reliability_weight=0.78,
        strengths=(
            "Excellent RAG (Retrieval Augmented Generation)",
            "Strong summarization with citations",
            "Good multilingual support",
            "Trial API key on signup (free to try)",
        ),
        best_for=(
            "Summarization with source attribution",
            "Document search and retrieval tasks",
            "Multilingual content processing",
            "RAG pipeline integration",
        ),
        weaknesses=(
            "Code generation below average",
            "No vision/multimodal support",
            "Smaller community and ecosystem",
            "Limited structured output compared to OpenAI",
        ),
        avoid_for=(
            "Code generation and debugging",
            "Complex reasoning tasks",
            "Multimodal analysis",
            "High-precision structured data extraction",
        ),
    ),
    ProviderSpec(
        "deepseek",
        "deepseek/deepseek-v4-pro",
        "deepseek/deepseek-v4-flash",
        litellm_prefix="deepseek/",
        base_url="https://api.deepseek.com",
        env_keys=("HIVE_DEEPSEEK_API_KEY",),
        display_name="DeepSeek",
        signup_url="https://platform.deepseek.com/api_keys",
        billing_url="https://platform.deepseek.com/top_up",
        free_info="Very cheap: $0.07/M input tokens",
        cost_tier="budget",
        quality_weight=0.85,
        speed_weight=0.65,
        cost_weight=0.95,
        reliability_weight=0.72,
        strengths=(
            "Extremely cheap ($0.07/M input tokens)",
            "Strong code generation (competitive with GPT-4)",
            "Excellent math and reasoning (Reasoner model)",
            "Good value for the quality",
        ),
        best_for=(
            "Code generation on a budget",
            "Math and formal reasoning (use Reasoner)",
            "High-volume processing at lowest cost",
            "When you want near-frontier quality at budget prices",
        ),
        weaknesses=(
            "Based in China — data privacy concerns for some users",
            "Can be slow during peak times",
            "Less reliable uptime than major US providers",
            "Creative writing not as strong",
        ),
        avoid_for=(
            "Sensitive/confidential data (data privacy concerns)",
            "Low-latency real-time needs (use Groq)",
            "Creative writing and marketing copy",
            "When uptime SLA is critical",
        ),
        is_reasoning=True,
        model_prefixes=("deepseek-v4", "deepseek-chat", "deepseek-coder"),
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
        display_name="Perplexity",
        signup_url="https://www.perplexity.ai/settings/api",
        free_info="Search-augmented responses with citations",
        cost_tier="standard",
        quality_weight=0.78,
        speed_weight=0.70,
        cost_weight=0.50,
        reliability_weight=0.82,
        strengths=(
            "Web search augmented responses with citations",
            "Real-time information (not limited by training cutoff)",
            "Automatic source attribution",
            "Good for research and fact-checking",
        ),
        best_for=(
            "Questions requiring up-to-date information",
            "Research with source citations needed",
            "Fact-checking claims",
            "Current events and recent developments",
        ),
        weaknesses=(
            "Not the best for pure code generation",
            "More expensive than basic LLM calls",
            "Search augmentation adds latency",
            "Creative tasks don't benefit from search",
        ),
        avoid_for=(
            "Code generation (use OpenAI/Anthropic)",
            "Creative writing (search is irrelevant)",
            "Offline usage",
            "Simple questions that don't need web search",
        ),
        has_search=True,
    ),
    ProviderSpec(
        "together",
        "together_ai/openai/gpt-oss-120b",
        "together_ai/openai/gpt-oss-20b",
        litellm_prefix="together_ai/",
        env_keys=("TOGETHERAI_API_KEY", "HIVE_TOGETHER_API_KEY"),
        display_name="Together AI",
        signup_url="https://api.together.ai/settings/api-keys",
        free_info="Requires $5 minimum purchase",
        cost_tier="budget",
        quality_weight=0.75,
        speed_weight=0.82,
        cost_weight=0.80,
        reliability_weight=0.80,
        strengths=(
            "Hosts many open-source models cheaply",
            "Fast inference on popular models",
            "Good API compatibility (OpenAI-compatible)",
        ),
        best_for=(
            "Running open-source models without own hardware",
            "Cost-effective Llama/Mixtral access",
            "When you want cloud speed without cloud prices",
            "Trying different open-source models",
        ),
        weaknesses=(
            "No free tier (eliminated July 2025)",
            "No proprietary models (only open-source)",
            "Quality limited by underlying models",
            "Less polished than major providers",
        ),
        avoid_for=(
            "Tasks requiring frontier proprietary models",
            "When you need guaranteed uptime SLA",
            "Multimodal tasks",
            "Budget-constrained users (no free tier)",
        ),
    ),
    ProviderSpec(
        "fireworks",
        "fireworks_ai/accounts/fireworks/models/gpt-oss-120b",
        "fireworks_ai/accounts/fireworks/models/nemotron-lightning-3p5-30b-a3b",
        litellm_prefix="fireworks_ai/accounts/fireworks/models/",
        env_keys=("FIREWORKS_AI_API_KEY", "HIVE_FIREWORKS_API_KEY"),
        display_name="Fireworks AI",
        signup_url="https://fireworks.ai/account/api-keys",
        billing_url="https://fireworks.ai/account/billing",
        free_tier=True,
        free_info="Free tier available",
        free_tier_rank=9,
        cost_tier="budget",
        quality_weight=0.75,
        speed_weight=0.88,
        cost_weight=0.80,
        reliability_weight=0.78,
        strengths=(
            "Very fast open-source model inference",
            "Free tier available",
            "Good for prototyping and development",
            "Supports function calling on open models",
        ),
        best_for=(
            "Fast prototyping with open-source models",
            "Development and testing",
            "Speed-sensitive open-source model tasks",
        ),
        weaknesses=(
            "Smaller ecosystem than Together AI",
            "No proprietary models",
            "Less documentation",
        ),
        avoid_for=(
            "Production workloads requiring guaranteed SLA",
            "Frontier model quality tasks",
        ),
        is_fast=True,
    ),
    ProviderSpec(
        "openrouter",
        "openrouter/openai/gpt-oss-120b",
        "openrouter/openai/gpt-oss-20b",
        litellm_prefix="openrouter/",
        env_keys=("HIVE_OPENROUTER_API_KEY",),
        display_name="OpenRouter",
        signup_url="https://openrouter.ai/settings/keys",
        free_info="Routes to best available provider",
        cost_tier="standard",
        quality_weight=0.85,
        speed_weight=0.75,
        cost_weight=0.60,
        reliability_weight=0.82,
        strengths=(
            "Access to 100+ models through one API",
            "Often cheaper than direct provider pricing",
            "Automatic fallback between providers",
            "One API key for everything",
        ),
        best_for=(
            "Accessing many different models without managing keys",
            "Cost optimization through provider comparison",
            "When you want one API key for multiple providers",
            "Trying models before committing to a provider",
        ),
        weaknesses=(
            "Adds a layer of indirection (slightly more latency)",
            "Pricing can change without notice",
            "You're trusting a middleman with your data",
        ),
        avoid_for=(
            "When you need direct provider relationship",
            "Ultra-low-latency requirements",
            "When data must not pass through third parties",
        ),
    ),
    ProviderSpec(
        "cerebras",
        "cerebras/gpt-oss-120b",
        "cerebras/gpt-oss-120b",
        litellm_prefix="cerebras/",
        env_keys=("HIVE_CEREBRAS_API_KEY",),
        display_name="Cerebras",
        signup_url="https://cloud.cerebras.ai",
        free_tier=True,
        free_info="Free tier: 30 req/min",
        free_tier_rank=10,
        cost_tier="free",
        quality_weight=0.75,
        speed_weight=0.98,
        cost_weight=0.85,
        reliability_weight=0.75,
        strengths=(
            "Fastest inference available (wafer-scale chip)",
            "Free tier with 30 req/min",
            "Runs Llama models at extraordinary speed",
            "Great for interactive applications",
        ),
        best_for=(
            "When speed is the #1 priority",
            "Interactive/real-time AI applications",
            "High-volume fast processing",
            "Free tier access for experimentation",
        ),
        weaknesses=(
            "Limited model selection",
            "Newer provider, less proven at scale",
            "Quality limited by open-source models",
        ),
        avoid_for=(
            "Tasks requiring frontier quality",
            "Long context analysis (limited models)",
            "Multimodal tasks",
        ),
        is_fast=True,
    ),
    ProviderSpec(
        "sambanova",
        "sambanova/Meta-Llama-3.3-70B-Instruct",
        "sambanova/gpt-oss-120b",
        litellm_prefix="sambanova/",
        env_keys=("HIVE_SAMBANOVA_API_KEY",),
        display_name="SambaNova",
        signup_url="https://cloud.sambanova.ai",
        free_tier=True,
        free_info="Free tier available",
        free_tier_rank=11,
        cost_tier="free",
        quality_weight=0.75,
        speed_weight=0.90,
        cost_weight=0.80,
        reliability_weight=0.75,
        strengths=(
            "Very fast inference on custom hardware",
            "Free tier available",
            "Runs Llama models efficiently",
        ),
        best_for=(
            "Fast open-source model inference",
            "Free tier experimentation",
            "Speed-sensitive applications",
        ),
        weaknesses=(
            "Limited model selection",
            "Newer/less established provider",
            "Sparse documentation",
        ),
        avoid_for=(
            "Production workloads without SLA",
            "Tasks requiring frontier quality",
            "Multimodal or vision tasks",
        ),
        is_fast=True,
    ),
    ProviderSpec(
        "huggingface",
        "huggingface/openai/gpt-oss-120b",
        "huggingface/openai/gpt-oss-20b",
        litellm_prefix="huggingface/",
        env_keys=("HF_TOKEN", "HUGGINGFACE_API_KEY", "HIVE_HUGGINGFACE_API_KEY"),
        display_name="Hugging Face",
        signup_url="https://huggingface.co/settings/tokens",
        free_tier=True,
        free_info="Free Inference API",
        free_tier_rank=12,
        cost_tier="free",
        quality_weight=0.65,
        speed_weight=0.60,
        cost_weight=0.90,
        reliability_weight=0.70,
        strengths=(
            "Free Inference API for many models",
            "Access to thousands of community models",
            "Good for experimentation and research",
            "Strong NLP pipeline support",
        ),
        best_for=(
            "Trying community/niche models",
            "NLP tasks (classification, NER, sentiment)",
            "Research and experimentation",
            "Free tier exploration",
        ),
        weaknesses=(
            "Inference API can be slow and unreliable",
            "Free tier has strict rate limits",
            "Models may not be chat-optimized",
            "Inconsistent quality across models",
        ),
        avoid_for=(
            "Production workloads (reliability issues)",
            "Real-time chat applications",
            "Tasks requiring consistent high quality",
            "When uptime matters",
        ),
    ),
    ProviderSpec(
        "ai21",
        "ai21_chat/jamba-large-1.7",
        "ai21_chat/jamba-mini-2",
        litellm_prefix="ai21_chat/",
        env_keys=("HIVE_AI21_API_KEY",),
        display_name="AI21 Labs",
        signup_url="https://studio.ai21.com/account/api-key",
        free_tier=True,
        free_info="Free tier available",
        free_tier_rank=13,
        cost_tier="budget",
        quality_weight=0.78,
        speed_weight=0.72,
        cost_weight=0.70,
        reliability_weight=0.78,
        strengths=(
            "Jamba model with 256K context window",
            "Good at document processing",
            "Free tier available",
            "Strong summarization capabilities",
        ),
        best_for=(
            "Long document analysis (256K context)",
            "Summarization tasks",
            "Document Q&A",
            "When you need more context than most models offer",
        ),
        weaknesses=(
            "Code generation below average",
            "Smaller community than major providers",
            "Less versatile than GPT-4 or Claude",
        ),
        avoid_for=(
            "Code generation and debugging",
            "Creative writing",
            "Tasks where the Jamba architecture doesn't help",
        ),
        long_context=True,
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
        display_name="NVIDIA NIM",
        signup_url="https://build.nvidia.com",
        free_tier=True,
        free_info="1000+ free API credits, 40 RPM, NVIDIA Developer Program",
        reset_hint="Credits don't expire; rate limits reset every minute",
        free_tier_rank=6,
        cost_tier="free",
        quality_weight=0.82,
        speed_weight=0.80,
        cost_weight=0.90,
        reliability_weight=0.80,
        strengths=(
            "Massive model catalog (100+ models including 405B)",
            "1000 free credits on signup",
            "Optimized for NVIDIA hardware",
            "Includes domain-specific models",
        ),
        best_for=(
            "Accessing large models (405B) for free",
            "NVIDIA GPU users (optimized inference)",
            "Trying many different models",
            "Domain-specific AI tasks",
        ),
        weaknesses=(
            "Requires phone SMS verification",
            "Credits can be exhausted on large models",
            "Can be slow during peak times",
        ),
        avoid_for=(
            "When you need guaranteed low latency",
            "High-volume production without paid tier",
        ),
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
        display_name="SiliconFlow",
        signup_url="https://cloud.siliconflow.cn",
        free_tier=True,
        # Unverified marketing copy carried over from 0.41; see docs/PROVIDERS.md.
        free_info="Permanently free models at 1000 RPM",
        free_tier_rank=7,
        cost_tier="free",
        quality_weight=0.78,
        speed_weight=0.85,
        cost_weight=1.00,
        reliability_weight=0.80,
        strengths=(
            "Permanently free models at 1000 RPM — best rate limits of any free provider",
            "Large catalog of open-source models",
            "Very high throughput for a free tier",
            "OpenAI-compatible API",
        ),
        best_for=(
            "High-volume free tier usage",
            "Batch processing with open-source models",
            "When rate limits are a concern on other free providers",
            "Cost-free production workloads at moderate scale",
        ),
        weaknesses=(
            "China-based provider — data privacy considerations",
            "No frontier proprietary models",
            "English documentation can be sparse",
        ),
        avoid_for=(
            "Sensitive/confidential data (data privacy concerns)",
            "Tasks requiring frontier model quality",
            "When uptime SLA is critical",
        ),
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
        display_name="LLM7",
        signup_url="https://token.llm7.io/",
        billing_url="https://llm7.io",
        free_tier=True,
        free_info="Anonymous access: 30 RPM, no signup. Token: 120 RPM",
        free_tier_rank=8,
        cost_tier="free",
        quality_weight=0.75,
        speed_weight=0.78,
        cost_weight=1.00,
        reliability_weight=0.72,
        strengths=(
            "No signup required — anonymous API access works immediately",
            "Supports DeepSeek-R1 and other capable models for free",
            "30 RPM anonymous, 120 RPM with token",
            "Zero friction for first-time users",
        ),
        best_for=(
            "Zero-setup first use — no account needed",
            "Quick experiments without signing up anywhere",
            "Accessing DeepSeek-R1 reasoning for free",
            "Students who don't want to share email/payment info",
        ),
        weaknesses=(
            "Newer/smaller provider — less proven reliability",
            "Anonymous rate limits are moderate (30 RPM)",
            "Less documentation and community support",
        ),
        avoid_for=(
            "Production workloads requiring guaranteed uptime",
            "High-volume usage on anonymous tier",
            "When you need frontier model quality (GPT-4o level)",
        ),
    ),
)

PROVIDER_SPECS: dict[str, ProviderSpec] = {spec.name: spec for spec in _SPECS}

# Descriptive rows for the adapters in ``registry.BESPOKE_ADAPTERS``. The
# registry never builds these (their transport is bespoke) and the routing
# fields are unused: Ollama's default model is picked per machine from the
# VRAM tier table (nvh.core.local_models), Triton's comes from config.
_BESPOKE: tuple[ProviderSpec, ...] = (
    ProviderSpec(
        "ollama",
        "",
        "",
        zero_cost=True,
        display_name="Ollama (local)",
        key_env="",
        signup_url="https://ollama.com/download",
        billing_url="https://ollama.com/library",
        free_tier=True,
        free_info="Unlimited, free, runs on your GPU",
        upgrade_hint="Pull more models: nvh models pull <tag>",
        free_tier_rank=1,
        cost_tier="free",
        quality_weight=0.75,
        speed_weight=0.70,
        cost_weight=1.00,
        reliability_weight=0.90,
        strengths=(
            "Completely free — unlimited usage",
            "100% private — no data leaves your machine",
            "No API key needed",
            "Works offline",
            "Runs NVIDIA Nemotron optimized for your GPU",
        ),
        best_for=(
            "Any task when privacy matters",
            "Unlimited free usage for students",
            "Offline work (no internet needed)",
            "Confidential/sensitive content",
            "Default fallback when cloud is down or budget is spent",
        ),
        weaknesses=(
            "Quality depends on GPU and model size",
            "Slower than cloud for large models",
            "Limited by your GPU VRAM",
            "No vision/multimodal on most local models",
        ),
        avoid_for=(
            "Tasks requiring absolute frontier quality",
            "Multimodal analysis (images, audio)",
            "When you need 1M+ token context (use Gemini)",
        ),
        is_local=True,
    ),
    ProviderSpec(
        "triton",
        "",
        "",
        zero_cost=True,
        display_name="Triton",
        # The endpoint variable; Triton takes no key.
        key_env="TRITON_URL",
        free_tier=True,
        free_info="your own inference server",
        upgrade_hint="Load more models on the Triton server (model repository)",
        cost_tier="free",
        is_local=True,
    ),
    ProviderSpec(
        "mock",
        "mock/default",
        "mock/fast",
        zero_cost=True,
        display_name="Mock",
        key_env="",
        free_tier=True,
        free_info="tests only, no network",
        cost_tier="free",
    ),
)

BESPOKE_SPECS: dict[str, ProviderSpec] = {spec.name: spec for spec in _BESPOKE}

# Every provider nvHive knows, cloud adapters first: the one table the
# derived docs, ladders and cards iterate.
PROVIDER_FACTS: dict[str, ProviderSpec] = {**PROVIDER_SPECS, **BESPOKE_SPECS}


def free_tier_ladder() -> list[ProviderSpec]:
    """The free-tier providers in the order the router tries them (rank 1 first)."""
    ranked = [spec for spec in PROVIDER_FACTS.values() if spec.free_tier and spec.free_tier_rank > 0]
    return sorted(ranked, key=lambda spec: spec.free_tier_rank)
