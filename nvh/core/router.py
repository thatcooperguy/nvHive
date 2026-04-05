"""Query routing engine: task classification and provider selection."""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass
from typing import Any

from nvh.config.settings import CouncilConfig, RoutingRule
from nvh.core.rate_limiter import ProviderRateManager
from nvh.providers.base import ModelInfo, TaskType
from nvh.providers.registry import ProviderRegistry

# Providers that run on NVIDIA hardware / NVIDIA-optimized inference
_NVIDIA_PROVIDERS = frozenset({"nvidia", "ollama", "triton"})

# ---------------------------------------------------------------------------
# Task Classifier — TF-IDF cosine similarity with regex fallback
# ---------------------------------------------------------------------------

_STOPWORDS = frozenset({
    "a", "an", "the", "is", "it", "of", "in", "to", "and", "or", "for",
    "on", "at", "by", "with", "as", "be", "was", "are", "been", "from",
    "has", "have", "had", "do", "does", "did", "but", "not", "this",
    "that", "these", "those", "i", "me", "my", "you", "your", "we",
    "our", "he", "she", "they", "them", "its", "can", "will", "would",
    "could", "should", "just", "about", "so", "if", "then", "than",
    "also", "into", "up", "out", "some", "all", "no", "more",
})

_TASK_CORPUS: dict[TaskType, list[str]] = {
    TaskType.CODE_GENERATION: [
        "Write a Python function that sorts a list",
        "Create a REST API endpoint for user registration",
        "Implement a binary search tree in Java",
        "Build a React component for a login form",
        "Write a SQL query to find duplicate records",
        "Create a Dockerfile for a Node.js application",
        "Implement rate limiting middleware in Express",
        "Code a recursive Fibonacci function",
        "Write a class that implements the observer pattern",
        "Build a CLI tool in Go that processes CSV files",
    ],
    TaskType.CODE_REVIEW: [
        "Review this code for security vulnerabilities",
        "What's wrong with this function and how can I improve it",
        "Critique this implementation and suggest optimizations",
        "Is this code production-ready and what needs to change",
        "Review this pull request for best practices",
        "Check this module for code smells and anti-patterns",
        "Evaluate the test coverage of this codebase",
    ],
    TaskType.CODE_DEBUG: [
        "I'm getting a TypeError exception can you fix this bug",
        "Debug this function it returns the wrong result",
        "Why does this code throw a segmentation fault",
        "Fix this stack trace error in my Python script",
        "This program crashes when I pass null values",
        "Help me find the bug in this recursive function",
        "My application doesn't work after the latest update",
    ],
    TaskType.REASONING: [
        "Explain how neural networks learn through backpropagation",
        "Why does quicksort have O(n log n) average complexity",
        "Analyze the trade-offs between SQL and NoSQL databases",
        "Compare microservices versus monolithic architecture",
        "Evaluate the pros and cons of functional programming",
        "Think through this logic puzzle step by step",
        "How does garbage collection work in modern languages",
    ],
    TaskType.MATH: [
        "Calculate the integral of x squared from 0 to 5",
        "Solve this equation 2x plus 5 equals 15",
        "Compute the derivative of sin x times cos x",
        "Prove that the square root of 2 is irrational",
        "What is the probability of rolling two sixes",
        "Find the eigenvalues of this matrix",
        "Solve this system of linear algebra equations",
    ],
    TaskType.CREATIVE_WRITING: [
        "Write a short story about a dragon who learns to fly",
        "Compose a poem about the ocean at sunset",
        "Draft a blog post about sustainable living",
        "Create a fictional dialogue between two scientists",
        "Write a narrative essay about overcoming adversity",
        "Compose song lyrics about finding your path",
        "Write a creative fiction piece set in space",
    ],
    TaskType.SUMMARIZATION: [
        "Summarize this article for me in three sentences",
        "Give me a brief summary of this research paper",
        "TLDR of this long email thread",
        "Condense this report into key bullet points",
        "Recap the main arguments from this document",
        "Provide a summary overview of this chapter",
    ],
    TaskType.TRANSLATION: [
        "Translate this paragraph to Spanish",
        "Convert this English text into French",
        "How do you say this phrase in German",
        "Translate this document from Japanese to English",
        "Give me the Chinese translation of this sentence",
        "Translate this technical manual to Portuguese",
    ],
    TaskType.CONVERSATION: [
        "Hello how are you doing today",
        "Hi there thanks for your help",
        "Hey what's up",
        "Thank you that was very helpful",
        "Good morning nice to meet you",
        "How are you",
    ],
    TaskType.QUESTION_ANSWERING: [
        "What is the capital of France",
        "Who invented the telephone",
        "When did World War II end",
        "Where is the Great Barrier Reef located",
        "How many planets are in our solar system",
        "Define photosynthesis",
        "What is the meaning of entropy in physics",
    ],
    TaskType.STRUCTURED_EXTRACTION: [
        "Extract all email addresses from this text",
        "Parse this HTML into a JSON structure",
        "Convert this data into a CSV table",
        "Extract the key-value pairs from this log file",
        "Format this information as a structured schema",
        "Pull out all the dates and names from this document",
    ],
    TaskType.MULTIMODAL: [
        "Look at this image and describe what you see",
        "Analyze this screenshot and identify the UI elements",
        "What's in this photo",
        "Describe the diagram in this picture",
        "Process this image and extract the text from it",
        "What does this visual chart show",
    ],
    TaskType.LONG_CONTEXT_ANALYSIS: [
        "Analyze this entire 50-page document and find key themes",
        "Read this full research paper and identify methodology flaws",
        "Review this long report and extract actionable insights",
        "Summarize this entire book chapter by chapter",
        "Process this lengthy legal document for compliance issues",
        "Analyze the full text of this academic article",
    ],
}

_TASK_PATTERNS: dict[TaskType, list[re.Pattern[str]]] = {
    TaskType.CODE_GENERATION: [
        re.compile(r"\b(write|create|implement|build|code|function|class|program|script)\b", re.I),
        re.compile(r"\b(def |class |import |from |func |fn |const |let |var )\b"),
        re.compile(r"```"),
    ],
    TaskType.CODE_REVIEW: [
        re.compile(
            r"\b(review|critique|improve|refactor|optimize|clean up)\b"
            r".*\b(code|function|class)\b", re.I,
        ),
    ],
    TaskType.CODE_DEBUG: [
        re.compile(
            r"\b(debug|fix|error|bug|traceback|exception"
            r"|stack trace|doesn.t work|broken)\b", re.I,
        ),
    ],
    TaskType.REASONING: [
        re.compile(r"\b(explain|why|how does|analyze|compare|evaluate|think|reason|logic)\b", re.I),
    ],
    TaskType.MATH: [
        re.compile(
            r"\b(calculate|compute|solve|equation"
            r"|integral|derivative|proof|theorem)\b", re.I,
        ),
        re.compile(r"[0-9]+\s*[\+\-\*\/\^]\s*[0-9]+"),
        re.compile(r"\b(algebra|calculus|statistics|probability|matrix|vector)\b", re.I),
    ],
    TaskType.CREATIVE_WRITING: [
        re.compile(
            r"\b(write|compose|draft|create)\b"
            r".*\b(story|poem|essay|blog|article"
            r"|letter|song|script)\b", re.I,
        ),
        re.compile(r"\b(creative|fiction|narrative|prose)\b", re.I),
    ],
    TaskType.SUMMARIZATION: [
        re.compile(r"\b(summarize|summary|tldr|tl;dr|brief|condense|recap)\b", re.I),
    ],
    TaskType.TRANSLATION: [
        re.compile(
            r"\b(translate|translation|in (spanish|french"
            r"|german|chinese|japanese|korean|arabic"
            r"|portuguese|italian|russian|hindi))\b", re.I,
        ),
    ],
    TaskType.CONVERSATION: [
        re.compile(r"\b(hello|hi|hey|thanks|thank you|how are you|what.s up)\b", re.I),
    ],
    TaskType.QUESTION_ANSWERING: [
        re.compile(
            r"\b(what is|who is|when did|where is"
            r"|how many|how much|define|meaning of)\b",
            re.I,
        ),
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


def _tokenize(text: str) -> list[str]:
    """Lowercase, split on non-alphanumeric, filter stopwords."""
    tokens = re.findall(r"[a-z0-9]+", text.lower())
    return [t for t in tokens if t not in _STOPWORDS and len(t) > 1]


def _compute_tf(tokens: list[str]) -> dict[str, float]:
    """Term frequency: count / total_tokens."""
    if not tokens:
        return {}
    counts = Counter(tokens)
    total = len(tokens)
    return {term: count / total for term, count in counts.items()}


def _compute_idf(corpus_docs: list[list[str]]) -> dict[str, float]:
    """IDF: log(N / df) for each term."""
    n = len(corpus_docs)
    if n == 0:
        return {}
    df: dict[str, int] = {}
    for doc in corpus_docs:
        for term in set(doc):
            df[term] = df.get(term, 0) + 1
    return {term: math.log(n / count) for term, count in df.items()}


def _tfidf_vector(
    tokens: list[str], idf: dict[str, float],
) -> dict[str, float]:
    """TF * IDF for each term — sparse vector as dict."""
    tf = _compute_tf(tokens)
    return {term: freq * idf.get(term, 0.0) for term, freq in tf.items()}


def _cosine_similarity(
    a: dict[str, float], b: dict[str, float],
) -> float:
    """Cosine similarity between two sparse vectors."""
    if not a or not b:
        return 0.0
    dot = sum(a[k] * b[k] for k in a if k in b)
    mag_a = math.sqrt(sum(v * v for v in a.values()))
    mag_b = math.sqrt(sum(v * v for v in b.values()))
    if mag_a == 0.0 or mag_b == 0.0:
        return 0.0
    return dot / (mag_a * mag_b)


@dataclass
class ClassificationResult:
    task_type: TaskType
    confidence: float
    all_scores: dict[TaskType, float]


class TaskClassifier:
    """TF-IDF based task classification with regex fallback."""

    _TFIDF_FALLBACK_THRESHOLD = 0.15

    def __init__(self) -> None:
        self._corpus: dict[TaskType, list[str]] = _TASK_CORPUS
        self._idf: dict[str, float] = {}
        self._centroids: dict[TaskType, dict[str, float]] = {}
        self._initialized = False

    def _initialize(self) -> None:
        """Build TF-IDF vectors from training corpus (called once)."""
        # Tokenize every document across all task types
        all_docs: list[list[str]] = []
        doc_map: list[tuple[TaskType, list[str]]] = []
        for task_type, examples in self._corpus.items():
            for text in examples:
                tokens = _tokenize(text)
                all_docs.append(tokens)
                doc_map.append((task_type, tokens))

        # Compute IDF across entire corpus
        self._idf = _compute_idf(all_docs)

        # Compute centroid TF-IDF vector per task type
        for task_type in self._corpus:
            vectors: list[dict[str, float]] = []
            for tt, tokens in doc_map:
                if tt == task_type:
                    vectors.append(_tfidf_vector(tokens, self._idf))

            # Average the vectors to get centroid
            if vectors:
                centroid: dict[str, float] = {}
                for vec in vectors:
                    for term, val in vec.items():
                        centroid[term] = centroid.get(term, 0.0) + val
                n = len(vectors)
                self._centroids[task_type] = {
                    t: v / n for t, v in centroid.items()
                }

        self._initialized = True

    def _classify_regex(self, query: str) -> ClassificationResult:
        """Regex fallback classifier (original implementation)."""
        scores: dict[TaskType, float] = {}

        for task_type, patterns in _TASK_PATTERNS.items():
            match_count = sum(1 for p in patterns if p.search(query))
            if match_count > 0:
                scores[task_type] = min(
                    1.0, match_count / len(patterns) * 0.8 + 0.2,
                )

        if not scores:
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

    def classify(self, query: str) -> ClassificationResult:
        """Classify a query using TF-IDF similarity, regex fallback."""
        if not self._initialized:
            self._initialize()

        tokens = _tokenize(query)
        query_vec = _tfidf_vector(tokens, self._idf)

        # Cosine similarity against each centroid
        scores: dict[TaskType, float] = {}
        for task_type, centroid in self._centroids.items():
            sim = _cosine_similarity(query_vec, centroid)
            if sim > 0.0:
                scores[task_type] = sim

        if scores:
            best_type = max(scores, key=scores.get)  # type: ignore[arg-type]
            best_score = scores[best_type]

            if best_score >= self._TFIDF_FALLBACK_THRESHOLD:
                return ClassificationResult(
                    task_type=best_type,
                    confidence=best_score,
                    all_scores=scores,
                )

        # Fall back to regex when TF-IDF confidence is too low
        return self._classify_regex(query)


# Module-level singleton — initialized lazily on first classify() call
_classifier = TaskClassifier()


def classify_task(query: str) -> ClassificationResult:
    """Classify query into a task type. Backward-compatible wrapper."""
    return _classifier.classify(query)


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
        # Learned scores overlay — populated by LearningEngine
        self._learned_scores: dict[tuple[str, str, str], Any] = {}

    def set_learned_scores(
        self,
        scores: dict[tuple[str, str, str], Any],
    ) -> None:
        """Update the in-memory learned scores from the learning engine."""
        self._learned_scores = scores

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
        import logging
        _log = logging.getLogger(__name__)

        try:
            classification = classify_task(query)
        except Exception as e:
            _log.warning("Task classification failed (%s), defaulting to CONVERSATION", e)
            classification = ClassificationResult(
                task_type=TaskType.CONVERSATION,
                confidence=0.3,
                all_scores={TaskType.CONVERSATION: 0.3},
            )

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
        skipped_reasons: dict[str, str] = {}

        # Import advisor profiles once outside the loop
        try:
            from nvh.core.advisor_profiles import (
                ADVISOR_PROFILES as advisor_profiles,  # noqa: N811
            )
        except ImportError:
            advisor_profiles = {}

        for pname in available:
            try:
                # Skip unhealthy providers
                try:
                    health = self.rate_manager.get_health_score(pname)
                except Exception:
                    health = 0.5  # assume moderate health on check failure
                if health < 0.1:
                    skipped_reasons[pname] = "unhealthy (health < 0.1)"
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
                    skipped_reasons[pname] = "no models available"
                    continue

                # Pick the best model from this provider
                best_model = models[0]
                best_cap = 0.0
                for m in models:
                    task_key = classification.task_type.value
                    static_cap = m.capability_scores.get(
                        task_key, 0.5,
                    )
                    # Blend with learned score if available
                    ls_key = (pname, m.model_id, task_key)
                    learned = self._learned_scores.get(ls_key)
                    if learned and learned.sample_count > 0:
                        from nvh.core.learning import blend_score
                        cap = blend_score(
                            static_cap,
                            learned.learned_capability,
                            learned.sample_count,
                        )
                    else:
                        cap = static_cap
                    if cap > best_cap:
                        best_cap = cap
                        best_model = m

                # Filter by context window
                if input_tokens > 0 and best_model.context_window > 0:
                    if input_tokens > best_model.context_window * 0.9:
                        skipped_reasons[pname] = (
                            f"context too small ({best_model.context_window})"
                        )
                        continue

                # Calculate composite score with advisor profile intelligence
                cap_score = best_cap
                cost_score = self._cost_score(best_model)
                lat_score = self._latency_score(best_model)

                # Apply advisor profile adjustments (strengths/weaknesses)
                profile_bonus = 0.0
                profile = advisor_profiles.get(pname)
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
                    _search_words = [
                        "search", "latest", "current",
                        "recent", "news", "today",
                    ]
                    _fast_words = [
                        "quick", "fast", "brief", "short",
                    ]
                    _local_words = [
                        "private", "confidential",
                        "secret", "sensitive",
                    ]
                    _reasoning_words = [
                        "prove", "derive", "proof",
                        "theorem", "reason", "logic",
                    ]
                    if profile.has_search and any(
                        w in task_desc for w in _search_words
                    ):
                        profile_bonus += 0.15
                    if profile.is_fast and any(
                        w in task_desc for w in _fast_words
                    ):
                        profile_bonus += 0.10
                    if profile.is_local and any(
                        w in task_desc for w in _local_words
                    ):
                        profile_bonus += 0.15
                    if profile.is_reasoning and any(
                        w in task_desc
                        for w in _reasoning_words
                    ):
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

                # Apply --prefer-nvidia bonus (1.3x) when quality is comparable
                if self.config.defaults.prefer_nvidia and pname in _NVIDIA_PROVIDERS:
                    composite *= 1.3

                provider_scores[pname] = {
                    "composite": composite,
                    "capability": cap_score,
                    "cost": cost_score,
                    "latency": lat_score,
                    "health": health,
                    "model": 0,  # reserved for future model-level scoring
                }
            except Exception as e:
                _log.warning("Scoring failed for provider %s: %s", pname, e)
                skipped_reasons[pname] = f"scoring error: {e}"
                continue

        if not provider_scores:
            default = self.config.defaults.provider or available[0]
            skip_summary = "; ".join(f"{p}: {r}" for p, r in list(skipped_reasons.items())[:5])
            _log.warning(
                "All providers filtered out during routing. Skipped: %s. Falling back to %s.",
                skip_summary or "(none)", default,
            )
            return RoutingDecision(
                provider=default,
                model=model_override or "",
                task_type=classification.task_type,
                confidence=classification.confidence,
                scores={},
                reason=(
                    f"All providers filtered out"
                    f" ({skip_summary or 'unknown'}),"
                    f" using default: {default}"
                ),
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
        local_strong_tasks = {
            TaskType.CONVERSATION,
            TaskType.QUESTION_ANSWERING,
            TaskType.SUMMARIZATION,
            TaskType.TRANSLATION,
            TaskType.CODE_DEBUG,          # simple debugging
        }

        # Task types that should escalate to cloud for better quality
        escalate_tasks = {
            TaskType.MULTIMODAL,           # needs vision models
            TaskType.LONG_CONTEXT_ANALYSIS, # may exceed local context window
        }

        # Keywords that suggest the user wants cloud quality
        escalate_keywords = [
            "best possible", "highest quality", "professional",
            "production", "enterprise", "critical",
        ]

        # Always escalate multimodal and long-context
        if classification.task_type in escalate_tasks:
            return None

        # Check for escalation keywords
        query_lower = query.lower()
        if any(kw in query_lower for kw in escalate_keywords):
            return None

        # Check if query is simple enough for local
        estimated_tokens = input_tokens or len(query) // 4
        is_short = estimated_tokens < 500
        is_simple_task = classification.task_type in local_strong_tasks

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
            reason = (
                f"Local-first: {classification.task_type.value}"
                f" handled well by local model"
                f" (capability: {local_capability:.0%})"
            )
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
