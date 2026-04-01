"""Advisor profiles: strengths, weaknesses, weights, and routing intelligence.

Each advisor has a profile that tells the router:
- WHEN to use this advisor (strengths, best-for scenarios)
- WHEN NOT to use this advisor (weaknesses, negative prompts)
- HOW to weight this advisor in council mode
- COST tier for budget-aware routing

This data drives smart routing — the system picks the right advisor
for each question without the user needing to specify.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class AdvisorProfile:
    """Intelligence profile for an LLM advisor."""
    name: str
    display_name: str

    # Routing weights (0.0-1.0) — higher = more likely to be selected
    quality_weight: float       # overall response quality
    speed_weight: float         # inference speed
    cost_weight: float          # cost efficiency (higher = cheaper)
    reliability_weight: float   # uptime and consistency

    # What this advisor excels at
    strengths: list[str]
    best_for: list[str]         # specific use cases

    # When NOT to use this advisor (negative prompts for routing)
    weaknesses: list[str]
    avoid_for: list[str]        # specific scenarios where it's a bad choice

    # Cost tier: "free", "budget", "standard", "premium", "enterprise"
    cost_tier: str

    # Free tier info
    has_free_tier: bool = False
    free_tier_limits: str = ""

    # Special capabilities
    has_search: bool = False      # can search the web (Perplexity)
    has_code_exec: bool = False   # can run code
    is_local: bool = False        # runs on user's hardware
    is_fast: bool = False         # known for speed (Groq, Cerebras)
    is_reasoning: bool = False    # deep reasoning model (o3, DeepSeek-R1)
    long_context: bool = False    # 100K+ context window


# ---------------------------------------------------------------------------
# Advisor Profiles Database
# ---------------------------------------------------------------------------

ADVISOR_PROFILES: dict[str, AdvisorProfile] = {

    "openai": AdvisorProfile(
        name="openai",
        display_name="OpenAI",
        quality_weight=0.90,
        speed_weight=0.75,
        cost_weight=0.40,
        reliability_weight=0.95,
        strengths=[
            "Excellent instruction following",
            "Strong code generation across all languages",
            "Best-in-class multimodal (vision, audio)",
            "Consistent output formatting (JSON, structured)",
            "Largest ecosystem and tooling support",
        ],
        best_for=[
            "Code generation and debugging",
            "Structured data extraction (JSON, CSV)",
            "Multimodal tasks (image analysis, charts)",
            "API and tool use integration",
            "General-purpose tasks when reliability matters",
        ],
        weaknesses=[
            "Expensive for high-volume usage",
            "Creative writing can feel generic/safe",
            "Reasoning models (o3) are slow and costly",
            "Rate limits on free tier are strict",
        ],
        avoid_for=[
            "Budget-constrained batch processing",
            "Nuanced creative writing (Claude is better)",
            "Tasks requiring web search (use Perplexity)",
            "When privacy matters (use local Ollama instead)",
        ],
        cost_tier="standard",
    ),

    "anthropic": AdvisorProfile(
        name="anthropic",
        display_name="Anthropic Claude",
        quality_weight=0.92,
        speed_weight=0.70,
        cost_weight=0.35,
        reliability_weight=0.92,
        strengths=[
            "Best-in-class reasoning and analysis",
            "Superior creative and long-form writing",
            "Excellent code review and architecture advice",
            "Strong instruction following with nuance",
            "200K context window for large documents",
        ],
        best_for=[
            "Complex reasoning and analysis",
            "Creative writing (stories, essays, copy)",
            "Code review and architecture decisions",
            "Long document analysis (200K context)",
            "Nuanced questions requiring careful thought",
        ],
        weaknesses=[
            "Most expensive per-token for top models",
            "No free tier available",
            "Can be overly cautious / refuse edge cases",
            "Slower than speed-optimized providers",
        ],
        avoid_for=[
            "Simple factual queries (overkill, use cheaper model)",
            "Budget-constrained high-volume tasks",
            "Real-time / low-latency requirements (use Groq)",
            "Web search augmented queries (use Perplexity)",
        ],
        cost_tier="premium",
        long_context=True,
    ),

    "google": AdvisorProfile(
        name="google",
        display_name="Google Gemini",
        quality_weight=0.85,
        speed_weight=0.80,
        cost_weight=0.75,
        reliability_weight=0.85,
        strengths=[
            "1M token context window (largest available)",
            "Excellent multimodal (native image/video/audio)",
            "Strong math and scientific reasoning",
            "Very cost-effective (Flash model is extremely cheap)",
            "Free tier: 15 requests/minute",
        ],
        best_for=[
            "Very long document analysis (books, codebases)",
            "Multimodal tasks (images, diagrams, screenshots)",
            "Math and science questions",
            "Cost-sensitive bulk processing (use Flash)",
            "When you need free tier access",
        ],
        weaknesses=[
            "Creative writing less nuanced than Claude",
            "Safety filters can be overly aggressive",
            "Code generation slightly behind OpenAI/Anthropic",
            "API can be inconsistent on complex instructions",
        ],
        avoid_for=[
            "Sensitive/edgy creative content (safety filters)",
            "Production code generation for critical systems",
            "Tasks requiring precise instruction following",
        ],
        cost_tier="budget",
        has_free_tier=True,
        free_tier_limits="15 req/min free",
        long_context=True,
    ),

    "groq": AdvisorProfile(
        name="groq",
        display_name="Groq",
        quality_weight=0.75,
        speed_weight=0.99,
        cost_weight=0.85,
        reliability_weight=0.80,
        strengths=[
            "Fastest inference available (100-200ms latency)",
            "Free tier with generous limits",
            "Runs open-source models (Llama, Mixtral, Gemma)",
            "Extremely low cost per token",
            "Great for interactive/real-time use",
        ],
        best_for=[
            "Quick questions needing instant answers",
            "Interactive chat (REPL, conversational)",
            "High-volume batch processing (cheap + fast)",
            "When speed matters more than absolute quality",
            "Free tier usage for students",
        ],
        weaknesses=[
            "Quality limited by the underlying open-source models",
            "No proprietary model advantage (it's Llama/Mixtral)",
            "Short context on some models (8K for SpecDec)",
            "Rate limits can hit during heavy usage",
        ],
        avoid_for=[
            "Tasks requiring frontier model quality (use OpenAI/Anthropic)",
            "Very long context analysis (limited windows)",
            "Multimodal tasks (no vision support)",
            "Nuanced creative writing",
        ],
        cost_tier="free",
        has_free_tier=True,
        free_tier_limits="30 req/min, 14.4K tok/min",
        is_fast=True,
    ),

    "grok": AdvisorProfile(
        name="grok",
        display_name="Grok (xAI)",
        quality_weight=0.85,
        speed_weight=0.75,
        cost_weight=0.45,
        reliability_weight=0.80,
        strengths=[
            "Strong reasoning and analytical capabilities",
            "Good at conversational and witty responses",
            "Less restrictive content policies",
            "Competitive with GPT-4 class models",
        ],
        best_for=[
            "Analysis and reasoning tasks",
            "Conversational AI with personality",
            "Tasks where other models refuse (content policy)",
            "General knowledge questions",
        ],
        weaknesses=[
            "Newer provider, less battle-tested",
            "No free tier",
            "Smaller model ecosystem than OpenAI",
            "No vision/multimodal support yet",
        ],
        avoid_for=[
            "Budget-constrained usage (no free tier)",
            "Multimodal tasks (no vision)",
            "Enterprise use requiring long track record",
        ],
        cost_tier="standard",
    ),

    "mistral": AdvisorProfile(
        name="mistral",
        display_name="Mistral",
        quality_weight=0.80,
        speed_weight=0.82,
        cost_weight=0.70,
        reliability_weight=0.82,
        strengths=[
            "Strong multilingual capabilities (European languages)",
            "Good code generation",
            "Cost-effective for the quality level",
            "Fast inference on Small model",
            "Good at structured output",
            "Free Experiment plan available",
        ],
        best_for=[
            "Multilingual tasks (French, German, Spanish, etc.)",
            "Code generation at a good price point",
            "European data residency requirements",
            "Cost-effective general purpose tasks",
            "Free tier experimentation (2 RPM)",
        ],
        weaknesses=[
            "Not the best at English creative writing",
            "No vision/multimodal support",
            "Smaller model selection than OpenAI",
            "Free tier is very rate limited (2 RPM)",
        ],
        avoid_for=[
            "Multimodal tasks (no vision)",
            "English-only creative writing (Claude is better)",
            "Very long context analysis",
            "High-volume tasks on free tier",
        ],
        cost_tier="budget",
        has_free_tier=True,
        free_tier_limits="Free: 2 RPM, 1B tokens/month",
    ),

    "cohere": AdvisorProfile(
        name="cohere",
        display_name="Cohere",
        quality_weight=0.72,
        speed_weight=0.75,
        cost_weight=0.70,
        reliability_weight=0.78,
        strengths=[
            "Excellent RAG (Retrieval Augmented Generation)",
            "Strong summarization with citations",
            "Good multilingual support",
            "Trial API key on signup (free to try)",
        ],
        best_for=[
            "Summarization with source attribution",
            "Document search and retrieval tasks",
            "Multilingual content processing",
            "RAG pipeline integration",
        ],
        weaknesses=[
            "Code generation below average",
            "No vision/multimodal support",
            "Smaller community and ecosystem",
            "Limited structured output compared to OpenAI",
        ],
        avoid_for=[
            "Code generation and debugging",
            "Complex reasoning tasks",
            "Multimodal analysis",
            "High-precision structured data extraction",
        ],
        cost_tier="budget",
        has_free_tier=True,
        free_tier_limits="Trial API key on signup",
    ),

    "deepseek": AdvisorProfile(
        name="deepseek",
        display_name="DeepSeek",
        quality_weight=0.85,
        speed_weight=0.65,
        cost_weight=0.95,
        reliability_weight=0.72,
        strengths=[
            "Extremely cheap ($0.07/M input tokens)",
            "Strong code generation (competitive with GPT-4)",
            "Excellent math and reasoning (Reasoner model)",
            "Good value for the quality",
        ],
        best_for=[
            "Code generation on a budget",
            "Math and formal reasoning (use Reasoner)",
            "High-volume processing at lowest cost",
            "When you want near-frontier quality at budget prices",
        ],
        weaknesses=[
            "Based in China — data privacy concerns for some users",
            "Can be slow during peak times",
            "Less reliable uptime than major US providers",
            "Creative writing not as strong",
        ],
        avoid_for=[
            "Sensitive/confidential data (data privacy concerns)",
            "Low-latency real-time needs (use Groq)",
            "Creative writing and marketing copy",
            "When uptime SLA is critical",
        ],
        cost_tier="budget",
        is_reasoning=True,
    ),

    "ollama": AdvisorProfile(
        name="ollama",
        display_name="Ollama (Local)",
        quality_weight=0.75,
        speed_weight=0.70,
        cost_weight=1.00,
        reliability_weight=0.90,
        strengths=[
            "Completely free — unlimited usage",
            "100% private — no data leaves your machine",
            "No API key needed",
            "Works offline",
            "Runs NVIDIA Nemotron optimized for your GPU",
        ],
        best_for=[
            "Any task when privacy matters",
            "Unlimited free usage for students",
            "Offline work (no internet needed)",
            "Confidential/sensitive content",
            "Default fallback when cloud is down or budget is spent",
        ],
        weaknesses=[
            "Quality depends on GPU and model size",
            "Slower than cloud for large models",
            "Limited by your GPU VRAM",
            "No vision/multimodal on most local models",
        ],
        avoid_for=[
            "Tasks requiring absolute frontier quality",
            "Multimodal analysis (images, audio)",
            "When you need 1M+ token context (use Gemini)",
        ],
        cost_tier="free",
        has_free_tier=True,
        free_tier_limits="Unlimited (runs on your GPU)",
        is_local=True,
    ),

    "perplexity": AdvisorProfile(
        name="perplexity",
        display_name="Perplexity",
        quality_weight=0.78,
        speed_weight=0.70,
        cost_weight=0.50,
        reliability_weight=0.82,
        strengths=[
            "Web search augmented responses with citations",
            "Real-time information (not limited by training cutoff)",
            "Automatic source attribution",
            "Good for research and fact-checking",
        ],
        best_for=[
            "Questions requiring up-to-date information",
            "Research with source citations needed",
            "Fact-checking claims",
            "Current events and recent developments",
        ],
        weaknesses=[
            "Not the best for pure code generation",
            "More expensive than basic LLM calls",
            "Search augmentation adds latency",
            "Creative tasks don't benefit from search",
        ],
        avoid_for=[
            "Code generation (use OpenAI/Anthropic)",
            "Creative writing (search is irrelevant)",
            "Offline usage",
            "Simple questions that don't need web search",
        ],
        cost_tier="standard",
        has_search=True,
    ),

    "together": AdvisorProfile(
        name="together",
        display_name="Together AI",
        quality_weight=0.75,
        speed_weight=0.82,
        cost_weight=0.80,
        reliability_weight=0.80,
        strengths=[
            "Hosts many open-source models cheaply",
            "Fast inference on popular models",
            "Good API compatibility (OpenAI-compatible)",
        ],
        best_for=[
            "Running open-source models without own hardware",
            "Cost-effective Llama/Mixtral access",
            "When you want cloud speed without cloud prices",
            "Trying different open-source models",
        ],
        weaknesses=[
            "No free tier (eliminated July 2025)",
            "No proprietary models (only open-source)",
            "Quality limited by underlying models",
            "Less polished than major providers",
        ],
        avoid_for=[
            "Tasks requiring frontier proprietary models",
            "When you need guaranteed uptime SLA",
            "Multimodal tasks",
            "Budget-constrained users (no free tier)",
        ],
        cost_tier="budget",
        has_free_tier=False,
        free_tier_limits="Requires $5 minimum purchase",
    ),

    "fireworks": AdvisorProfile(
        name="fireworks",
        display_name="Fireworks AI",
        quality_weight=0.75,
        speed_weight=0.88,
        cost_weight=0.80,
        reliability_weight=0.78,
        strengths=[
            "Very fast open-source model inference",
            "Free tier available",
            "Good for prototyping and development",
            "Supports function calling on open models",
        ],
        best_for=[
            "Fast prototyping with open-source models",
            "Development and testing",
            "Speed-sensitive open-source model tasks",
        ],
        weaknesses=[
            "Smaller ecosystem than Together AI",
            "No proprietary models",
            "Less documentation",
        ],
        avoid_for=[
            "Production workloads requiring guaranteed SLA",
            "Frontier model quality tasks",
        ],
        cost_tier="budget",
        has_free_tier=True,
        free_tier_limits="Free tier available",
        is_fast=True,
    ),

    "openrouter": AdvisorProfile(
        name="openrouter",
        display_name="OpenRouter",
        quality_weight=0.85,
        speed_weight=0.75,
        cost_weight=0.60,
        reliability_weight=0.82,
        strengths=[
            "Access to 100+ models through one API",
            "Often cheaper than direct provider pricing",
            "Automatic fallback between providers",
            "One API key for everything",
        ],
        best_for=[
            "Accessing many different models without managing keys",
            "Cost optimization through provider comparison",
            "When you want one API key for multiple providers",
            "Trying models before committing to a provider",
        ],
        weaknesses=[
            "Adds a layer of indirection (slightly more latency)",
            "Pricing can change without notice",
            "You're trusting a middleman with your data",
        ],
        avoid_for=[
            "When you need direct provider relationship",
            "Ultra-low-latency requirements",
            "When data must not pass through third parties",
        ],
        cost_tier="standard",
    ),

    "cerebras": AdvisorProfile(
        name="cerebras",
        display_name="Cerebras",
        quality_weight=0.75,
        speed_weight=0.98,
        cost_weight=0.85,
        reliability_weight=0.75,
        strengths=[
            "Fastest inference available (wafer-scale chip)",
            "Free tier with 30 req/min",
            "Runs Llama models at extraordinary speed",
            "Great for interactive applications",
        ],
        best_for=[
            "When speed is the #1 priority",
            "Interactive/real-time AI applications",
            "High-volume fast processing",
            "Free tier access for experimentation",
        ],
        weaknesses=[
            "Limited model selection",
            "Newer provider, less proven at scale",
            "Quality limited by open-source models",
        ],
        avoid_for=[
            "Tasks requiring frontier quality",
            "Long context analysis (limited models)",
            "Multimodal tasks",
        ],
        cost_tier="free",
        has_free_tier=True,
        free_tier_limits="Free tier: 30 req/min",
        is_fast=True,
    ),

    "sambanova": AdvisorProfile(
        name="sambanova",
        display_name="SambaNova",
        quality_weight=0.75,
        speed_weight=0.90,
        cost_weight=0.80,
        reliability_weight=0.75,
        strengths=[
            "Very fast inference on custom hardware",
            "Free tier available",
            "Runs Llama models efficiently",
        ],
        best_for=[
            "Fast open-source model inference",
            "Free tier experimentation",
            "Speed-sensitive applications",
        ],
        weaknesses=[
            "Limited model selection",
            "Newer/less established provider",
            "Sparse documentation",
        ],
        avoid_for=[
            "Production workloads without SLA",
            "Tasks requiring frontier quality",
            "Multimodal or vision tasks",
        ],
        cost_tier="free",
        has_free_tier=True,
        free_tier_limits="Free tier available",
        is_fast=True,
    ),

    "huggingface": AdvisorProfile(
        name="huggingface",
        display_name="Hugging Face",
        quality_weight=0.65,
        speed_weight=0.60,
        cost_weight=0.90,
        reliability_weight=0.70,
        strengths=[
            "Free Inference API for many models",
            "Access to thousands of community models",
            "Good for experimentation and research",
            "Strong NLP pipeline support",
        ],
        best_for=[
            "Trying community/niche models",
            "NLP tasks (classification, NER, sentiment)",
            "Research and experimentation",
            "Free tier exploration",
        ],
        weaknesses=[
            "Inference API can be slow and unreliable",
            "Free tier has strict rate limits",
            "Models may not be chat-optimized",
            "Inconsistent quality across models",
        ],
        avoid_for=[
            "Production workloads (reliability issues)",
            "Real-time chat applications",
            "Tasks requiring consistent high quality",
            "When uptime matters",
        ],
        cost_tier="free",
        has_free_tier=True,
        free_tier_limits="Free Inference API (rate limited)",
    ),

    "ai21": AdvisorProfile(
        name="ai21",
        display_name="AI21 Labs",
        quality_weight=0.78,
        speed_weight=0.72,
        cost_weight=0.70,
        reliability_weight=0.78,
        strengths=[
            "Jamba model with 256K context window",
            "Good at document processing",
            "Free tier available",
            "Strong summarization capabilities",
        ],
        best_for=[
            "Long document analysis (256K context)",
            "Summarization tasks",
            "Document Q&A",
            "When you need more context than most models offer",
        ],
        weaknesses=[
            "Code generation below average",
            "Smaller community than major providers",
            "Less versatile than GPT-4 or Claude",
        ],
        avoid_for=[
            "Code generation and debugging",
            "Creative writing",
            "Tasks where the Jamba architecture doesn't help",
        ],
        cost_tier="budget",
        has_free_tier=True,
        free_tier_limits="Free tier available",
        long_context=True,
    ),

    "github": AdvisorProfile(
        name="github",
        display_name="GitHub Models",
        quality_weight=0.88,
        speed_weight=0.70,
        cost_weight=1.00,
        reliability_weight=0.85,
        strengths=[
            "Access to frontier models (GPT-4o, o3) completely free",
            "No credit card needed — just a GitHub account",
            "Students already have GitHub accounts",
            "Reliable Microsoft/Azure infrastructure",
        ],
        best_for=[
            "Students who need frontier quality for free",
            "Prototyping with GPT-4o without paying",
            "When you need better quality than local models",
        ],
        weaknesses=[
            "Very low rate limits (50 req/day on big models)",
            "8K context limit on some models",
            "Can't use for batch/volume processing",
        ],
        avoid_for=[
            "High-volume usage (50 req/day max)",
            "Long context tasks (8K limit)",
            "Production workloads",
        ],
        cost_tier="free",
        has_free_tier=True,
        free_tier_limits="Free: 50-150 req/day, all GitHub users",
    ),

    "nvidia": AdvisorProfile(
        name="nvidia",
        display_name="NVIDIA NIM",
        quality_weight=0.82,
        speed_weight=0.80,
        cost_weight=0.90,
        reliability_weight=0.80,
        strengths=[
            "Massive model catalog (100+ models including 405B)",
            "1000 free credits on signup",
            "Optimized for NVIDIA hardware",
            "Includes domain-specific models",
        ],
        best_for=[
            "Accessing large models (405B) for free",
            "NVIDIA GPU users (optimized inference)",
            "Trying many different models",
            "Domain-specific AI tasks",
        ],
        weaknesses=[
            "Requires phone SMS verification",
            "Credits can be exhausted on large models",
            "Can be slow during peak times",
        ],
        avoid_for=[
            "When you need guaranteed low latency",
            "High-volume production without paid tier",
        ],
        cost_tier="free",
        has_free_tier=True,
        free_tier_limits="1000+ free credits, 40 RPM",
    ),

    "siliconflow": AdvisorProfile(
        name="siliconflow",
        display_name="SiliconFlow",
        quality_weight=0.78,
        speed_weight=0.85,
        cost_weight=1.00,
        reliability_weight=0.80,
        strengths=[
            "Permanently free models at 1000 RPM — best rate limits of any free provider",
            "Large catalog of open-source models",
            "Very high throughput for a free tier",
            "OpenAI-compatible API",
        ],
        best_for=[
            "High-volume free tier usage",
            "Batch processing with open-source models",
            "When rate limits are a concern on other free providers",
            "Cost-free production workloads at moderate scale",
        ],
        weaknesses=[
            "China-based provider — data privacy considerations",
            "No frontier proprietary models",
            "English documentation can be sparse",
        ],
        avoid_for=[
            "Sensitive/confidential data (data privacy concerns)",
            "Tasks requiring frontier model quality",
            "When uptime SLA is critical",
        ],
        cost_tier="free",
        has_free_tier=True,
        free_tier_limits="Permanently free models at 1000 RPM",
    ),

    "llm7": AdvisorProfile(
        name="llm7",
        display_name="LLM7",
        quality_weight=0.75,
        speed_weight=0.78,
        cost_weight=1.00,
        reliability_weight=0.72,
        strengths=[
            "No signup required — anonymous API access works immediately",
            "Supports DeepSeek-R1 and other capable models for free",
            "30 RPM anonymous, 120 RPM with token",
            "Zero friction for first-time users",
        ],
        best_for=[
            "Zero-setup first use — no account needed",
            "Quick experiments without signing up anywhere",
            "Accessing DeepSeek-R1 reasoning for free",
            "Students who don't want to share email/payment info",
        ],
        weaknesses=[
            "Newer/smaller provider — less proven reliability",
            "Anonymous rate limits are moderate (30 RPM)",
            "Less documentation and community support",
        ],
        avoid_for=[
            "Production workloads requiring guaranteed uptime",
            "High-volume usage on anonymous tier",
            "When you need frontier model quality (GPT-4o level)",
        ],
        cost_tier="free",
        has_free_tier=True,
        free_tier_limits="Anonymous: 30 RPM (no signup). Token: 120 RPM",
    ),
}


def get_advisor_profile(name: str) -> AdvisorProfile | None:
    """Get the profile for an advisor by name."""
    return ADVISOR_PROFILES.get(name)


def get_best_advisor_for_task(
    task_description: str,
    available_advisors: list[str],
    prefer_free: bool = True,
    prefer_fast: bool = False,
    prefer_local: bool = False,
    needs_search: bool = False,
    needs_vision: bool = False,
    needs_long_context: bool = False,
) -> str | None:
    """Smart advisor selection based on task requirements and preferences.

    Args:
        task_description: What the user wants to do (used for keyword matching)
        available_advisors: List of advisor names that are configured and have credits
        prefer_free: Prioritize free-tier advisors
        prefer_fast: Prioritize speed
        prefer_local: Prioritize local/private models
        needs_search: Task needs web search (only Perplexity)
        needs_vision: Task needs image analysis
        needs_long_context: Task needs 100K+ context

    Returns:
        Best advisor name, or None if no suitable advisor.
    """
    if not available_advisors:
        return None

    # Hard requirements filter
    candidates = available_advisors.copy()

    if needs_search:
        candidates = [a for a in candidates if ADVISOR_PROFILES.get(a, AdvisorProfile(
            name=a, display_name=a, quality_weight=0.5, speed_weight=0.5,
            cost_weight=0.5, reliability_weight=0.5, strengths=[], best_for=[],
            weaknesses=[], avoid_for=[], cost_tier="standard",
        )).has_search]
        if not candidates:
            candidates = available_advisors  # fall back

    if needs_long_context:
        long_ctx = [a for a in candidates if ADVISOR_PROFILES.get(a) and ADVISOR_PROFILES[a].long_context]
        if long_ctx:
            candidates = long_ctx

    # Score each candidate
    scored: list[tuple[str, float]] = []
    for name in candidates:
        profile = ADVISOR_PROFILES.get(name)
        if not profile:
            scored.append((name, 0.5))
            continue

        score = profile.quality_weight * 0.4 + profile.reliability_weight * 0.2

        if prefer_free:
            score += profile.cost_weight * 0.3
        else:
            score += profile.cost_weight * 0.1

        if prefer_fast:
            score += profile.speed_weight * 0.3
        else:
            score += profile.speed_weight * 0.1

        if prefer_local and profile.is_local:
            score += 0.3

        # Check negative prompts — penalize if task matches avoid_for
        task_lower = task_description.lower()
        for avoid in profile.avoid_for:
            if any(word in task_lower for word in avoid.lower().split()[:3]):
                score -= 0.15

        scored.append((name, score))

    scored.sort(key=lambda x: x[1], reverse=True)
    return scored[0][0] if scored else None


def format_advisor_card(name: str) -> str:
    """Format an advisor profile as a rich text card for display."""
    profile = ADVISOR_PROFILES.get(name)
    if not profile:
        return f"Unknown advisor: {name}"

    lines = [
        f"[bold]{profile.display_name}[/bold]",
        f"Cost: {profile.cost_tier} | Quality: {profile.quality_weight:.0%} | Speed: {profile.speed_weight:.0%}",
        "",
        "[green]Best for:[/green]",
    ]
    for item in profile.best_for[:3]:
        lines.append(f"  + {item}")
    lines.append("")
    lines.append("[red]Avoid for:[/red]")
    for item in profile.avoid_for[:3]:
        lines.append(f"  - {item}")

    if profile.has_free_tier:
        lines.append(f"\n[cyan]Free: {profile.free_tier_limits}[/cyan]")

    return "\n".join(lines)
