"""Query routing engine: task classification and provider selection."""

from __future__ import annotations

import re
from dataclasses import dataclass

from nvh.config.settings import CouncilConfig, RoutingRule
from nvh.core.rate_limiter import ProviderRateManager
from nvh.providers.base import ModelInfo, TaskType
from nvh.providers.registry import ProviderRegistry

# ---------------------------------------------------------------------------
# Task Classifier (keyword/regex for MVP)
# ---------------------------------------------------------------------------

_TASK_PATTERNS: dict[TaskType, list[re.Pattern[str]]] = {
    TaskType.CODE_GENERATION: [
        re.compile(r"\b(write|create|implement|build|code|function|class|program|script)\b", re.I),
        re.compile(r"\b(def |class |import |from |func |fn |const |let |var )\b"),
        re.compile(r"```"),
    ],
    TaskType.CODE_REVIEW: [
        re.compile(r"\b(review|critique|improve|refactor|optimize|clean up)\b.*\b(code|function|class)\b", re.I),
    ],
    TaskType.CODE_DEBUG: [
        re.compile(r"\b(debug|fix|error|bug|traceback|exception|stack trace|doesn.t work|broken)\b", re.I),
    ],
    TaskType.REASONING: [
        re.compile(r"\b(explain|why|how does|analyze|compare|evaluate|think|reason|logic)\b", re.I),
    ],
    TaskType.MATH: [
        re.compile(r"\b(calculate|compute|solve|equation|integral|derivative|proof|theorem)\b", re.I),
        re.compile(r"[0-9]+\s*[\+\-\*\/\^]\s*[0-9]+"),
        re.compile(r"\b(algebra|calculus|statistics|probability|matrix|vector)\b", re.I),
    ],
    TaskType.CREATIVE_WRITING: [
        re.compile(r"\b(write|compose|draft|create)\b.*\b(story|poem|essay|blog|article|letter|song|script)\b", re.I),
        re.compile(r"\b(creative|fiction|narrative|prose)\b", re.I),
    ],
    TaskType.SUMMARIZATION: [
        re.compile(r"\b(summarize|summary|tldr|tl;dr|brief|condense|recap)\b", re.I),
    ],
    TaskType.TRANSLATION: [
        re.compile(r"\b(translate|translation|in (spanish|french|german|chinese|japanese|korean|arabic|portuguese|italian|russian|hindi))\b", re.I),
    ],
    TaskType.CONVERSATION: [
        re.compile(r"\b(hello|hi|hey|thanks|thank you|how are you|what.s up)\b", re.I),
    ],
    TaskType.QUESTION_ANSWERING: [
        re.compile(r"\b(what is|who is|when did|where is|how many|how much|define|meaning of)\b", re.I),
        re.compile(r"\?$"),
    ],
    TaskType.STRUCTURED_EXTRACTION: [
        re.compile(r"\b(extract|parse|json|csv|table|structured|schema|format as)\b", re.I),
    ],
    TaskType.MULTIMODAL: [
        re.compile(r"\b(image|photo|picture|screenshot|diagram|video|audio|visual)\b", re.I),
    ],
    TaskType.LONG_CONTEXT_ANALYSIS: [
        re.compile(r"\b(document|paper|article|book|report|long|entire|full text)\b", re.I),
    ],
}


@dataclass
class ClassificationResult:
    task_type: TaskType
    confidence: float
    all_scores: dict[TaskType, float]


def classify_task(query: str) -> ClassificationResult:
    """Classify query into a task type using keyword/regex matching."""
    scores: dict[TaskType, float] = {}

    for task_type, patterns in _TASK_PATTERNS.items():
        match_count = sum(1 for p in patterns if p.search(query))
        if match_count > 0:
            scores[task_type] = min(1.0, match_count / len(patterns) * 0.8 + 0.2)

    if not scores:
        # Default to conversation for short queries, QA for questions
        if "?" in query:
            scores[TaskType.QUESTION_ANSWERING] = 0.5
        else:
            scores[TaskType.CONVERSATION] = 0.4

    best_type = max(scores, key=scores.get)  # type: ignore[arg-type]
    best_score = scores[best_type]

    return ClassificationResult(
        task_type=best_type,
        confidence=best_score,
        all_scores=scores,
    )


# ---------------------------------------------------------------------------
# Routing Engine
# ---------------------------------------------------------------------------

@dataclass
class RoutingDecision:
    provider: str
    model: str
    task_type: TaskType
    confidence: float
    scores: dict[str, float]
    reason: str


class RoutingEngine:
    """Selects the optimal provider/model for a given query."""

    def __init__(
        self,
        config: CouncilConfig,
        registry: ProviderRegistry,
        rate_manager: ProviderRateManager,
    ):
        self.config = config
        self.registry = registry
        self.rate_manager = rate_manager

    def route(
        self,
        query: str,
        provider_override: str | None = None,
        model_override: str | None = None,
        strategy: str = "best",
        input_tokens: int = 0,
    ) -> RoutingDecision:
        """Route a query to the best provider.

        Args:
            query: The user's query text
            provider_override: Explicit provider choice (bypasses routing)
            model_override: Explicit model choice
            strategy: 'best', 'cheapest', 'fastest', 'best-for-task'
            input_tokens: Estimated input token count
        """
        classification = classify_task(query)

        # Direct override — bypass routing
        if provider_override:
            model = model_override or ""
            if not model:
                pconfig = self.config.providers.get(provider_override)
                if pconfig:
                    model = pconfig.default_model
            return RoutingDecision(
                provider=provider_override,
                model=model,
                task_type=classification.task_type,
                confidence=classification.confidence,
                scores={},
                reason=f"User override: --provider {provider_override}",
            )

        # Check custom routing rules first
        for rule in self.config.routing.rules:
            if self._rule_matches(rule, classification, input_tokens):
                model = model_override or rule.model
                if not model:
                    pconfig = self.config.providers.get(rule.provider)
                    if pconfig:
                        model = pconfig.default_model
                return RoutingDecision(
                    provider=rule.provider,
                    model=model,
                    task_type=classification.task_type,
                    confidence=classification.confidence,
                    scores={},
                    reason=f"Matched routing rule: {rule.match}",
                )

        # --- Local-first routing: Nemotron broker ---
        # If Ollama is available with a Nemotron model, check if the query
        # is simple enough to handle locally. This saves money, keeps data
        # private, and only escalates to cloud when quality requires it.
        if strategy in ("best", "cheapest") and "ollama" in self.registry.list_enabled():
            local_decision = self._try_local_first(
                query, classification, model_override, input_tokens,
            )
            if local_decision:
                return local_decision

        # Score all available providers
        available = self.registry.list_enabled()
        if not available:
            default = self.config.defaults.provider
            return RoutingDecision(
                provider=default or "openai",
                model=model_override or self.config.defaults.model,
                task_type=classification.task_type,
                confidence=classification.confidence,
                scores={},
                reason="No providers available, using default",
            )

        provider_scores: dict[str, dict[str, float]] = {}
        for pname in available:
            # Skip unhealthy providers
            health = self.rate_manager.get_health_score(pname)
            if health < 0.1:
                continue

            models = self.registry.get_models_for_provider(pname)
            if not models:
                # Use provider config default model
                pconfig = self.config.providers.get(pname)
                if pconfig and pconfig.default_model:
                    model_info = self.registry.get_model_info(pconfig.default_model)
                    if model_info:
                        models = [model_info]

            if not models:
                continue

            # Pick the best model from this provider
            best_model = models[0]
            best_cap = 0.0
            for m in models:
                task_key = classification.task_type.value
                cap = m.capability_scores.get(task_key, 0.5)
                if cap > best_cap:
                    best_cap = cap
                    best_model = m

            # Filter by context window
            if input_tokens > 0 and best_model.context_window > 0:
                if input_tokens > best_model.context_window * 0.9:
                    continue

            # Calculate composite score with advisor profile intelligence
            cap_score = best_cap
            cost_score = self._cost_score(best_model)
            lat_score = self._latency_score(best_model)

            # Apply advisor profile adjustments (strengths/weaknesses)
            profile_bonus = 0.0
            from nvh.core.advisor_profiles import ADVISOR_PROFILES
            profile = ADVISOR_PROFILES.get(pname)
            if profile:
                # Bonus for advisor's inherent quality/reliability
                profile_bonus += profile.quality_weight * 0.05
                # Penalty if task matches advisor's "avoid_for" list
                task_desc = query.lower()
                for avoid in profile.avoid_for:
                    avoid_words = [w.lower() for w in avoid.split()[:4]]
                    if any(w in task_desc for w in avoid_words if len(w) > 3):
                        profile_bonus -= 0.10
                        break
                # Bonus for special capabilities matching the task
                if profile.has_search and any(w in task_desc for w in ["search", "latest", "current", "recent", "news", "today"]):
                    profile_bonus += 0.15
                if profile.is_fast and any(w in task_desc for w in ["quick", "fast", "brief", "short"]):
                    profile_bonus += 0.10
                if profile.is_local and any(w in task_desc for w in ["private", "confidential", "secret", "sensitive"]):
                    profile_bonus += 0.15
                if profile.is_reasoning and any(w in task_desc for w in ["prove", "derive", "proof", "theorem", "reason", "logic"]):
                    profile_bonus += 0.12

            weights = self.config.routing.weights

            if strategy == "cheapest":
                composite = cost_score
            elif strategy == "fastest":
                composite = lat_score
            elif strategy == "best-for-task":
                composite = cap_score + profile_bonus
            else:  # "best"
                composite = (
                    cap_score * weights.get("capability", 0.4)
                    + cost_score * weights.get("cost", 0.3)
                    + lat_score * weights.get("latency", 0.2)
                    + health * weights.get("health", 0.1)
                    + profile_bonus
                )

            provider_scores[pname] = {
                "composite": composite,
                "capability": cap_score,
                "cost": cost_score,
                "latency": lat_score,
                "health": health,
                "model": 0,  # placeholder
            }

        if not provider_scores:
            default = self.config.defaults.provider or available[0]
            return RoutingDecision(
                provider=default,
                model=model_override or "",
                task_type=classification.task_type,
                confidence=classification.confidence,
                scores={},
                reason="All providers filtered out, using default",
            )

        # Select the best
        best_provider = max(provider_scores, key=lambda p: provider_scores[p]["composite"])
        best_scores = provider_scores[best_provider]

        # Determine model
        if model_override:
            selected_model = model_override
        else:
            pconfig = self.config.providers.get(best_provider)
            selected_model = pconfig.default_model if pconfig else ""

        return RoutingDecision(
            provider=best_provider,
            model=selected_model,
            task_type=classification.task_type,
            confidence=classification.confidence,
            scores=best_scores,
            reason=f"Best composite score ({best_scores['composite']:.3f})",
        )

    def _try_local_first(
        self,
        query: str,
        classification: ClassificationResult,
        model_override: str | None,
        input_tokens: int,
    ) -> RoutingDecision | None:
        """Local-first broker: decide if Nemotron can handle this query.

        The local model handles a query when:
        1. It's a simple task (conversation, Q&A, summarization, translation)
        2. The query is short (< 500 tokens estimated)
        3. The local model has adequate capability scores for the task type
        4. The query doesn't explicitly need web search or multimodal

        If the query needs frontier quality (complex reasoning, advanced code,
        multimodal), this returns None and the router escalates to cloud.
        """
        # Task types the local model handles well
        LOCAL_STRONG_TASKS = {
            TaskType.CONVERSATION,
            TaskType.QUESTION_ANSWERING,
            TaskType.SUMMARIZATION,
            TaskType.TRANSLATION,
            TaskType.CODE_DEBUG,          # simple debugging
        }

        # Task types that should escalate to cloud for better quality
        ESCALATE_TASKS = {
            TaskType.MULTIMODAL,           # needs vision models
            TaskType.LONG_CONTEXT_ANALYSIS, # may exceed local context window
        }

        # Keywords that suggest the user wants cloud quality
        ESCALATE_KEYWORDS = [
            "best possible", "highest quality", "professional",
            "production", "enterprise", "critical",
        ]

        # Always escalate multimodal and long-context
        if classification.task_type in ESCALATE_TASKS:
            return None

        # Check for escalation keywords
        query_lower = query.lower()
        if any(kw in query_lower for kw in ESCALATE_KEYWORDS):
            return None

        # Check if query is simple enough for local
        estimated_tokens = input_tokens or len(query) // 4
        is_short = estimated_tokens < 500
        is_simple_task = classification.task_type in LOCAL_STRONG_TASKS

        # Get local model capability for this task
        local_models = self.registry.get_models_for_provider("ollama")
        if not local_models:
            pconfig = self.config.providers.get("ollama")
            if pconfig and pconfig.default_model:
                local_info = self.registry.get_model_info(pconfig.default_model)
                if local_info:
                    local_models = [local_info]

        if not local_models:
            return None

        best_local = local_models[0]
        task_key = classification.task_type.value
        local_capability = best_local.capability_scores.get(task_key, 0.5)

        # Decision: use local if simple task OR short query with decent capability
        use_local = False
        reason = ""

        if is_simple_task and local_capability >= 0.65:
            use_local = True
            reason = f"Local-first: {classification.task_type.value} handled well by local model (capability: {local_capability:.0%})"
        elif is_short and local_capability >= 0.70:
            use_local = True
            reason = f"Local-first: short query, local model capable ({local_capability:.0%})"
        elif classification.confidence < 0.5 and is_short:
            # Ambiguous short query — keep it local (cheap to try)
            use_local = True
            reason = "Local-first: short/ambiguous query — trying local before cloud"

        if use_local:
            pconfig = self.config.providers.get("ollama")
            model = model_override or (pconfig.default_model if pconfig else best_local.model_id)
            return RoutingDecision(
                provider="ollama",
                model=model,
                task_type=classification.task_type,
                confidence=classification.confidence,
                scores={"local_capability": local_capability},
                reason=reason,
            )

        return None  # escalate to cloud scoring

    def _rule_matches(
        self,
        rule: RoutingRule,
        classification: ClassificationResult,
        input_tokens: int,
    ) -> bool:
        for key, value in rule.match.items():
            if key == "task_type":
                if classification.task_type.value != value:
                    return False
            elif key == "input_tokens":
                # Parse comparison like "> 100000"
                if value.startswith(">"):
                    threshold = int(value.strip("> "))
                    if input_tokens <= threshold:
                        return False
                elif value.startswith("<"):
                    threshold = int(value.strip("< "))
                    if input_tokens >= threshold:
                        return False
        # Check provider is available
        if rule.provider and not self.registry.has(rule.provider):
            return False
        return True

    def _cost_score(self, model: ModelInfo) -> float:
        """Inverse cost score: cheaper = higher score. Normalized 0-1."""
        cost = float(model.input_cost_per_1m_tokens + model.output_cost_per_1m_tokens)
        if cost <= 0:
            return 1.0  # Free models get max score
        # Normalize: $0.10/M = 1.0, $100/M = ~0.0
        return max(0.0, min(1.0, 1.0 - (cost / 100.0)))

    def _latency_score(self, model: ModelInfo) -> float:
        """Inverse latency score: faster = higher score. Normalized 0-1."""
        lat = model.typical_latency_ms
        if lat <= 0:
            return 1.0
        # Normalize: 100ms = 1.0, 5000ms = 0.0
        return max(0.0, min(1.0, 1.0 - (lat / 5000.0)))
