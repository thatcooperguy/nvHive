"""Main orchestration engine.

Ties together routing, providers, council, fallback, caching,
and budget.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from collections import OrderedDict
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from nvh.config.settings import CouncilConfig, load_config
from nvh.core.context import ConversationManager
from nvh.core.council import CouncilOrchestrator, CouncilResponse
from nvh.core.rate_limiter import ProviderRateManager
from nvh.core.router import RoutingDecision, RoutingEngine
from nvh.core.webhooks import (
    WebhookEvent,
    WebhookManager,
    format_budget_alert,
    format_provider_alert,
    format_query_complete,
)
from nvh.providers.base import (
    CompletionResponse,
    FinishReason,
    Message,
    ProviderError,
    ProviderUnavailableError,
    RateLimitError,
    Usage,
)
from nvh.providers.registry import ProviderRegistry, get_registry
from nvh.storage import repository as repo

# ---------------------------------------------------------------------------
# In-Memory LRU Cache with TTL
# ---------------------------------------------------------------------------

@dataclass
class CacheEntry:
    response: CompletionResponse
    timestamp: float


class ResponseCache:
    """Simple in-memory LRU cache with TTL."""

    def __init__(self, max_size: int = 1000, ttl_seconds: int = 86400):
        self.max_size = max_size
        self.ttl = ttl_seconds
        self._store: OrderedDict[str, CacheEntry] = OrderedDict()
        self._lock = asyncio.Lock()

    def _make_key(
        self,
        provider: str,
        model: str,
        messages: list[Message],
        temperature: float,
        max_tokens: int,
    ) -> str:
        key_data = json.dumps({
            "provider": provider,
            "model": model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }, sort_keys=True)
        return hashlib.sha256(key_data.encode()).hexdigest()

    async def get(
        self,
        provider: str,
        model: str,
        messages: list[Message],
        temperature: float,
        max_tokens: int,
    ) -> CompletionResponse | None:
        async with self._lock:
            key = self._make_key(provider, model, messages, temperature, max_tokens)
            entry = self._store.get(key)
            if entry is None:
                return None
            # Check TTL
            if time.time() - entry.timestamp > self.ttl:
                del self._store[key]
                return None
            # Move to end (most recently used)
            self._store.move_to_end(key)
            # Return a copy with cache_hit flag
            resp = entry.response.model_copy()
            resp.cache_hit = True
            resp.cost_usd = Decimal("0")
            return resp

    async def put(
        self,
        provider: str,
        model: str,
        messages: list[Message],
        temperature: float,
        max_tokens: int,
        response: CompletionResponse,
    ) -> None:
        async with self._lock:
            key = self._make_key(provider, model, messages, temperature, max_tokens)
            self._store[key] = CacheEntry(response=response, timestamp=time.time())
            self._store.move_to_end(key)
            # Evict if over capacity
            while len(self._store) > self.max_size:
                self._store.popitem(last=False)

    async def clear(self, provider: str | None = None) -> int:
        async with self._lock:
            if provider is None:
                count = len(self._store)
                self._store.clear()
                return count
            keys_to_remove = [
                k for k, v in self._store.items()
                if v.response.provider == provider
            ]
            for k in keys_to_remove:
                del self._store[k]
            return len(keys_to_remove)

    @property
    def stats(self) -> dict[str, Any]:
        return {
            "entries": len(self._store),
            "max_size": self.max_size,
            "ttl_seconds": self.ttl,
        }


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

logger = logging.getLogger(__name__)


class Engine:
    """Main orchestration engine."""

    def __init__(
        self,
        config: CouncilConfig | None = None,
        registry: ProviderRegistry | None = None,
    ):
        self.config = config or load_config()
        self.registry = registry or get_registry()
        self.rate_manager = ProviderRateManager()
        self.router = RoutingEngine(self.config, self.registry, self.rate_manager)
        self.council = CouncilOrchestrator(self.config, self.registry, self.rate_manager)
        self.context = ConversationManager()
        self.cache = ResponseCache(
            max_size=self.config.cache.max_size,
            ttl_seconds=self.config.cache.ttl_seconds,
        )
        self.webhooks = WebhookManager()
        if self.config.webhooks:
            self.webhooks.load_from_config(
                [wh.model_dump() for wh in self.config.webhooks]
            )
        self._initialized = False
        self._budget_lock = asyncio.Lock()
        self.learning = None  # Initialized in initialize()

        # Drift detection state
        self.score_history: dict[tuple[str, str], list[float]] = {}
        self.provider_weights: dict[str, float] = {}

        # Local LLM orchestrator
        from nvh.core.orchestrator import LocalOrchestrator, OrchestrationConfig, OrchestrationMode
        orch_mode = OrchestrationMode(self.config.defaults.orchestration_mode)
        self.orchestrator = LocalOrchestrator(OrchestrationConfig(mode=orch_mode))

        # Load COUNCIL.md context files for injection into all prompts
        from nvh.core.context_files import find_context_files
        self._context_files = find_context_files()
        if self._context_files:
            names = [f.name for f in self._context_files]
            logger.info(f"Loaded context files: {names}")

    def _build_system_prompt(self, user_prompt: str | None = None) -> str | None:
        """Build system prompt with COUNCIL.md context injected.

        Combines context files (COUNCIL.md, .council/context/*.md, etc.)
        with the user's explicit system prompt.
        """
        from nvh.core.context_files import build_context_prompt
        combined = build_context_prompt(
            context_files=self._context_files,
            user_system_prompt=user_prompt or "",
        )
        return combined if combined else None

    async def initialize(self) -> list[str]:
        """Initialize the engine: setup DB, register providers.

        If no providers are configured, auto-detects zero-signup providers
        (Ollama, LLM7) so the product works out of the box.

        Returns list of enabled provider names.
        """
        if not self._initialized:
            await repo.init_db()
            enabled = self.registry.setup_from_config(self.config)

            # Auto-detect zero-signup providers if nothing is configured
            if not enabled:
                enabled = self._auto_detect_providers()

            # Initialize local orchestrator
            gpu_vram = 0
            try:
                from nvh.utils.gpu import get_total_vram_mb
                gpu_vram = get_total_vram_mb() / 1024
            except Exception:
                pass
            await self.orchestrator.initialize(self.registry, gpu_vram)

            await self.webhooks.start()

            # Initialize adaptive learning engine
            try:
                from nvh.core.learning import LearningEngine
                self.learning = LearningEngine()
                await self.learning.load_scores()
                self.router.set_learned_scores(
                    self.learning.get_score_map(),
                )
                logger.info(
                    "Learning engine initialized (%d learned scores)",
                    len(self.learning.get_score_map()),
                )
            except Exception as e:
                logger.warning("Learning engine init failed: %s", e)
                self.learning = None

            # Seed provider_weights so drift rerouting has entries to adjust
            for name in enabled:
                self.provider_weights.setdefault(name, 1.0)

            self._initialized = True
            return enabled
        return self.registry.list_enabled()

    def _auto_detect_providers(self) -> list[str]:
        """Fallback: auto-detect providers that need no configuration."""
        detected = []

        # LLM7 — always available (anonymous API, no signup)
        try:
            from nvh.providers.llm7_provider import LLM7Provider
            provider = LLM7Provider()
            self.registry.register("llm7", provider)
            detected.append("llm7")
            logger.info("Auto-detected: LLM7 (anonymous, no signup needed)")
        except Exception:
            pass

        # Ollama — check if running locally (supports OLLAMA_BASE_URL override)
        import os
        try:
            import httpx
            ollama_url = os.environ.get(
                "OLLAMA_BASE_URL", "http://localhost:11434",
            )
            resp = httpx.get(f"{ollama_url}/api/tags", timeout=2)
            if resp.status_code == 200:
                from nvh.providers.ollama_provider import OllamaProvider
                provider = OllamaProvider(base_url=ollama_url)
                self.registry.register("ollama", provider)
                detected.append("ollama")
                logger.info("Auto-detected: Ollama (local, running on %s)", ollama_url)
        except Exception:
            pass

        # Check for API keys in environment AND keyring
        env_providers = {
            "GROQ_API_KEY": ("groq", "nvh.providers.groq_provider", "GroqProvider"),
            "GITHUB_TOKEN": ("github", "nvh.providers.github_provider", "GitHubProvider"),
            "GOOGLE_API_KEY": ("google", "nvh.providers.google_provider", "GoogleProvider"),
            "OPENAI_API_KEY": ("openai", "nvh.providers.openai_provider", "OpenAIProvider"),
            "ANTHROPIC_API_KEY": (
                "anthropic",
                "nvh.providers.anthropic_provider",
                "AnthropicProvider",
            ),
        }

        # Also check keyring for keys saved by nvh setup
        def _get_key(env_var: str, provider_name: str) -> str:
            # 1. Environment variable (highest priority)
            key = os.environ.get(env_var, "")
            if key:
                return key
            # 2. Keyring (saved by nvh setup)
            try:
                import keyring
                key = keyring.get_password(
                    "nvhive", f"{provider_name}_api_key",
                )
                if key:
                    # Also set the env var so providers can find it
                    os.environ[env_var] = key
                    return key
            except Exception:
                pass
            return ""

        for env_var, (name, module_path, class_name) in env_providers.items():
            key = _get_key(env_var, name)
            if key and name not in detected:
                try:
                    import importlib
                    mod = importlib.import_module(module_path)
                    cls = getattr(mod, class_name)
                    provider = cls(api_key=key)
                    self.registry.register(name, provider)
                    detected.append(name)
                    logger.info("Auto-detected: %s (API key found)", name)
                except Exception:
                    pass

        return detected

    # -----------------------------------------------------------------------
    # Connectivity Check
    # -----------------------------------------------------------------------

    async def check_connectivity(self) -> bool:
        """Check whether any cloud advisor is reachable.

        Returns True if connectivity is available, False if offline.
        Falls back to True on unexpected errors so queries are not blocked.
        """
        return await self._check_connectivity()

    async def _check_connectivity(self) -> bool:
        """Quick connectivity check — can we reach any cloud provider?

        Tests multiple endpoints in parallel and returns True if any respond.
        """
        import httpx

        endpoints = [
            "https://api.groq.com",
            "https://api.openai.com",
            "https://api.anthropic.com",
            "https://generativelanguage.googleapis.com",
        ]

        async def _ping(url: str) -> bool:
            try:
                async with httpx.AsyncClient() as client:
                    resp = await client.head(url, timeout=3)
                    return resp.status_code < 500
            except Exception:
                return False

        results = await asyncio.gather(*[_ping(url) for url in endpoints])
        return any(results)

    # -----------------------------------------------------------------------
    # Simple Query
    # -----------------------------------------------------------------------

    async def query(
        self,
        prompt: str,
        provider: str | None = None,
        model: str | None = None,
        system_prompt: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        stream: bool = False,
        use_cache: bool = True,
        strategy: str = "best",
        conversation_id: str | None = None,
        continue_last: bool = False,
        privacy: bool = False,
        escalate: bool = False,
        verify: bool = False,
    ) -> CompletionResponse:
        """Execute a single query with routing, fallback, caching, and budget enforcement.

        When *privacy* is ``True``, cache reads/writes, query logging, and
        conversation persistence are all skipped so no data is stored.

        When *escalate* is ``True`` and no explicit provider is given, the
        engine tries the cheapest strategy first and automatically escalates
        to the best strategy if the response confidence is too low.

        When *verify* is ``True``, a different provider cross-checks the
        response for hallucinations and logical errors.
        """
        # --- Confidence-gated escalation (delegates the whole query) ---
        if escalate and not provider:
            from nvh.core.smart_query import query_with_escalation

            resp, meta = await query_with_escalation(
                self,
                prompt,
                provider=provider,
                model=model,
                system_prompt=system_prompt,
                temperature=temperature,
                max_tokens=max_tokens,
                stream=stream,
                use_cache=use_cache,
                conversation_id=conversation_id,
                continue_last=continue_last,
                privacy=privacy,
            )
            # Attach escalation metadata
            resp.metadata.update(meta)

            # Optional verification on the (possibly escalated) response
            if verify and not privacy:
                from nvh.core.smart_query import verify_response

                verification = await verify_response(
                    self, prompt, resp.content, resp.provider,
                )
                resp.metadata["verification"] = {
                    "verdict": verification.verdict,
                    "confidence": verification.confidence,
                    "issues": verification.issues,
                    "correction": verification.correction,
                    "verifier": verification.verifier_provider,
                }
            return resp

        await self.initialize()

        temp = temperature if temperature is not None else self.config.defaults.temperature
        max_tok = max_tokens or self.config.defaults.max_tokens
        sys_prompt = self._build_system_prompt(system_prompt or self.config.defaults.system_prompt)

        # Budget check
        await self._check_budget()

        # Offline detection: if no provider is pinned and cloud is unreachable,
        # auto-route to a local provider (Ollama, then LLM7 as anonymous fallback).
        _effective_provider = provider
        if not _effective_provider:
            is_online = await self._check_connectivity()
            if not is_online:
                enabled = self.registry.list_enabled()
                for _local in ("ollama", "llm7"):
                    if _local in enabled:
                        _effective_provider = _local
                        logger.info(
                            "Offline detected — auto-routing to local provider: %s",
                            _effective_provider,
                        )
                        break
                if not _effective_provider:
                    raise ProviderUnavailableError(
                        "You appear to be offline and no local providers are available.\n"
                        "Options:\n"
                        "  Start Ollama:   ollama serve\n"
                        "  Install Ollama: curl -fsSL https://ollama.com/install.sh | sh\n"
                        "  Check network:  ping api.groq.com"
                    )

        # Smart routing via local orchestrator (LIGHT+ mode), falls back to keywords
        _orchestrated_provider = _effective_provider
        if not _orchestrated_provider and self.orchestrator.is_active:
            available = self.registry.list_enabled()
            orch_result = await self.orchestrator.smart_route(prompt, available)
            if orch_result and orch_result.get("advisor") in available:
                _orchestrated_provider = orch_result["advisor"]
                logger.debug(
                    "Orchestrator routed to %s: %s",
                    _orchestrated_provider,
                    orch_result.get("reason", ""),
                )

        # Route
        decision = self.router.route(
            query=prompt,
            provider_override=_orchestrated_provider,
            model_override=model,
            strategy=strategy,
        )

        # Save routing context for `nvh why`
        self._last_decision = decision
        self._last_prompt = prompt

        # Prompt optimization via local orchestrator (LIGHT+ mode)
        _optimized_prompt = prompt
        if self.orchestrator.is_active:
            _optimized_prompt = await self.orchestrator.optimize_prompt(prompt, decision.provider)

        # Build messages (with conversation context if continuing, unless privacy mode)
        if privacy:
            messages: list[Message] = []
            if sys_prompt:
                messages.append(Message(role="system", content=sys_prompt))
            messages.append(Message(role="user", content=_optimized_prompt))
        else:
            messages = await self._build_messages(
                prompt=_optimized_prompt,
                conversation_id=conversation_id,
                continue_last=continue_last,
                system_prompt=sys_prompt,
                provider=decision.provider,
                model=decision.model,
            )

        # Cache check (skipped in privacy mode)
        if not privacy and use_cache and self.config.cache.enabled and temp == 0:
            cached = await self.cache.get(
                decision.provider, decision.model,
                messages, temp, max_tok,
            )
            if cached:
                await self._log_query(
                    cached, "simple",
                    cache_hit=True,
                    conversation_id=conversation_id,
                )
                return cached

        # Execute with fallback chain
        response = await self._execute_with_fallback(
            messages=messages,
            decision=decision,
            temperature=temp,
            max_tokens=max_tok,
            system_prompt=sys_prompt,
            stream=stream,
        )

        # Response evaluation via local orchestrator (FULL mode only)
        eval_result: dict | None = None
        from nvh.core.orchestrator import OrchestrationMode as _OMode
        if self.orchestrator.mode == _OMode.FULL and not privacy:
            eval_result = await self.orchestrator.evaluate_response(
                query=prompt,
                response_text=response.content,
                advisor=response.provider,
            )
            if eval_result.get("should_retry") and not response.fallback_from:
                retry_provider = eval_result.get("retry_with")
                if retry_provider and retry_provider != response.provider:
                    logger.info(
                        "Orchestrator: retrying with %s (quality=%s, reason=%s)",
                        retry_provider,
                        eval_result.get("quality"),
                        eval_result.get("reason", "low quality"),
                    )
                    # Actually retry with the suggested provider
                    if self.registry.has(retry_provider):
                        retry_decision = self.router.route(
                            query=prompt,
                            provider_override=retry_provider,
                        )
                        try:
                            retry_response = await self._execute_with_fallback(
                                messages=messages,
                                decision=retry_decision,
                                temperature=temp,
                                max_tokens=max_tok,
                                system_prompt=sys_prompt,
                                stream=stream,
                            )
                            response = retry_response
                        except Exception:
                            logger.debug(
                                "Retry with %s failed, keeping original",
                                retry_provider,
                            )

        if privacy:
            # Skip all storage in privacy mode
            return response

        # Cache the response
        if use_cache and self.config.cache.enabled and temp == 0:
            await self.cache.put(
                decision.provider, decision.model,
                messages, temp, max_tok, response,
            )

        # Log and persist
        conv_id = conversation_id
        if conversation_id or continue_last:
            conv_id = await self.context.get_or_create_conversation(
                conversation_id=conversation_id,
                continue_last=continue_last,
                provider=response.provider,
                model=response.model,
            )
            await self.context.add_user_message(conv_id, prompt)
            await self.context.add_assistant_message(conv_id, response)

        await self._log_query(response, "simple", conversation_id=conv_id)

        # Emit webhook event
        await self.webhooks.emit(
            WebhookEvent.QUERY_COMPLETE,
            format_query_complete(
                provider=response.provider,
                model=response.model,
                tokens=response.usage.total_tokens,
                cost=float(response.cost_usd or 0),
                latency_ms=response.latency_ms or 0,
                mode="simple",
            ),
        )

        # Adaptive learning: record outcome (fire-and-forget)
        if self.learning and not privacy:
            _eval_quality = (
                eval_result.get("quality")
                if eval_result else None
            )
            asyncio.create_task(self._record_learning(
                decision=decision,
                response=response,
                quality_score=_eval_quality,
            ))

        # --- Response verification (cross-model check) ---
        if verify and not privacy:
            from nvh.core.smart_query import verify_response

            verification = await verify_response(
                self, prompt, response.content, response.provider,
            )
            response.metadata["verification"] = {
                "verdict": verification.verdict,
                "confidence": verification.confidence,
                "issues": verification.issues,
                "correction": verification.correction,
                "verifier": verification.verifier_provider,
            }

        # Save routing context for `nvh why`
        if not privacy:
            self._save_last_query_context(
                decision, response, prompt,
            )

        return response

    async def query_stream(
        self,
        prompt: str,
        provider: str | None = None,
        model: str | None = None,
        system_prompt: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        on_token: Any = None,
    ) -> CompletionResponse:
        """Stream a query, calling on_token(delta) for each chunk.

        Returns the final collected CompletionResponse. This is a
        thin wrapper that sets up routing/messages like query() but
        uses stream_to_callback instead of collecting silently.
        """
        await self.initialize()

        temp = temperature if temperature is not None else self.config.defaults.temperature
        max_tok = max_tokens or self.config.defaults.max_tokens
        sys_prompt = self._build_system_prompt(system_prompt or self.config.defaults.system_prompt)

        await self._check_budget()

        # Route
        decision = self.router.route(
            query=prompt,
            provider_override=provider,
            model_override=model,
            strategy="best",
        )

        # Build messages
        messages: list[Message] = []
        if sys_prompt:
            messages.append(Message(role="system", content=sys_prompt))
        messages.append(Message(role="user", content=prompt))

        # Get the provider and stream
        fallback_chain = self._get_fallback_chain(decision.provider)
        for i, provider_name in enumerate(fallback_chain):
            if not self.registry.has(provider_name):
                continue
            try:
                self.rate_manager.check_available(provider_name)
            except Exception:
                continue

            prov = self.registry.get(provider_name)
            pconfig = self.config.providers.get(provider_name)
            pmodel = decision.model if i == 0 else (pconfig.default_model if pconfig else "")

            try:
                from nvh.utils.streaming import stream_to_callback
                stream_iter = prov.stream(
                    messages=messages,
                    model=pmodel or None,
                    temperature=temp,
                    max_tokens=max_tok,
                    system_prompt=sys_prompt,
                )
                response = await stream_to_callback(stream_iter, on_token=on_token)
                self.rate_manager.record_success(provider_name)
                return response
            except Exception as exc:
                logger.warning("Stream failed on %s: %s", provider_name, exc)
                continue

        raise ProviderError("All providers failed during streaming")

    def _save_last_query_context(
        self,
        decision: RoutingDecision,
        response: CompletionResponse,
        prompt: str,
    ) -> None:
        """Save routing explanation to disk for `nvh why`."""
        import json as _json
        from pathlib import Path as _Path

        context = {
            "prompt": prompt[:200],
            "task_type": decision.task_type.value,
            "classification_confidence": decision.confidence,
            "provider": response.provider,
            "model": response.model,
            "strategy": "best",
            "scores": decision.scores,
            "reason": decision.reason,
            "latency_ms": response.latency_ms,
            "cost_usd": str(response.cost_usd),
            "tokens": {
                "input": response.usage.input_tokens,
                "output": response.usage.output_tokens,
            },
            "fallback_from": response.fallback_from,
            "cache_hit": response.cache_hit,
            "verification": response.metadata.get(
                "verification",
            ),
            "escalated": response.metadata.get("escalated"),
            "timestamp": (
                __import__("datetime")
                .datetime.now(__import__("datetime").UTC)
                .isoformat()
            ),
        }

        try:
            why_path = _Path.home() / ".hive" / "last_query.json"
            why_path.parent.mkdir(parents=True, exist_ok=True)
            why_path.write_text(_json.dumps(context, indent=2))
        except Exception:
            pass  # non-critical

    async def _record_learning(
        self,
        decision: RoutingDecision,
        response: CompletionResponse,
        quality_score: float | None = None,
        user_feedback: int | None = None,
    ) -> None:
        """Record routing outcome for adaptive learning (non-blocking)."""
        # Append to score_history for drift detection
        task_key = (response.provider, decision.task_type.value)
        score = quality_score if quality_score is not None else (
            1.0 if response.finish_reason != FinishReason.ERROR else 0.0
        )
        self.score_history.setdefault(task_key, []).append(score)

        try:
            await self.learning.record_outcome(
                provider=response.provider,
                model=response.model,
                task_type=decision.task_type.value,
                classification_confidence=decision.confidence,
                routing_strategy="best",
                composite_score=decision.scores.get(
                    "composite", 0.0,
                ),
                capability_score_used=decision.scores.get(
                    "capability", 0.5,
                ),
                quality_score=quality_score,
                user_feedback=user_feedback,
                latency_ms=response.latency_ms or 0,
                cost_usd=response.cost_usd,
                status=(
                    "error"
                    if response.finish_reason == FinishReason.ERROR
                    else "success"
                ),
                was_fallback=bool(response.fallback_from),
                was_retry=False,
            )
            # Refresh router's learned scores
            self.router.set_learned_scores(
                self.learning.get_score_map(),
            )
        except Exception as e:
            logger.debug("Learning record failed: %s", e)

    # -----------------------------------------------------------------------
    # Drift Detection
    # -----------------------------------------------------------------------

    def check_drift(self) -> list:
        """Check for provider quality drift based on accumulated score history.

        Returns a list of :class:`~nvh.core.drift_detector.DriftAlert` objects.
        """
        from nvh.core.drift_detector import check_for_drift

        alerts = check_for_drift(self)
        if alerts:
            logger.warning(
                "Drift detected for %d provider/task pairs",
                len(alerts),
            )
        return alerts

    def auto_reroute(self) -> list[str]:
        """Detect drift and automatically deprioritize degraded providers.

        Returns a list of human-readable action strings describing what changed.
        """
        from nvh.core.drift_detector import auto_reroute

        alerts = self.check_drift()
        if not alerts:
            return []
        actions = auto_reroute(self, alerts)
        for action in actions:
            logger.info("Drift reroute: %s", action)
        return actions

    # -----------------------------------------------------------------------
    # Council Mode
    # -----------------------------------------------------------------------

    async def run_council(
        self,
        prompt: str,
        members: list[str] | None = None,
        weights: dict[str, float] | None = None,
        strategy: str | None = None,
        system_prompt: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        synthesize: bool = True,
        conversation_id: str | None = None,
        auto_agents: bool = False,
        agent_preset: str | None = None,
        num_agents: int | None = None,
        privacy: bool = False,
    ) -> CouncilResponse:
        """Run a council session.

        Args:
            auto_agents: Auto-generate expert personas based on query content.
            agent_preset: Use a named preset ("executive", "engineering", etc.).
            num_agents: Number of agent personas to generate.
            privacy: When ``True``, skip all query logging and conversation
                     persistence so no data is stored.
        """
        await self.initialize()
        await self._check_budget()

        temp = temperature if temperature is not None else self.config.defaults.temperature
        max_tok = max_tokens or self.config.defaults.max_tokens
        sys_prompt = self._build_system_prompt(system_prompt or self.config.defaults.system_prompt)

        # Build messages with conversation context (skipped in privacy mode)
        msgs: list[Message] | None = None
        if conversation_id and not privacy:
            msgs = await self.context.get_context_messages(conversation_id, sys_prompt)
            msgs.append(Message(role="user", content=prompt))

        result = await self.council.run_council(
            query=prompt,
            members_override=members,
            weights_override=weights,
            strategy=strategy,
            system_prompt=sys_prompt,
            temperature=temp,
            max_tokens=max_tok,
            messages=msgs,
            synthesize=synthesize,
            auto_agents=auto_agents,
            agent_preset=agent_preset,
            num_agents=num_agents,
        )

        if privacy:
            # Skip all logging and persistence in privacy mode
            return result

        # Log each member response
        for pname, resp in result.member_responses.items():
            await self._log_query(resp, "council", conversation_id=conversation_id)

        if result.synthesis:
            await self._log_query(result.synthesis, "council", conversation_id=conversation_id)

        # Emit webhook event
        total_tokens = sum(
            r.usage.total_tokens for r in result.member_responses.values()
        )
        total_cost = float(result.total_cost_usd or 0)
        await self.webhooks.emit(
            WebhookEvent.COUNCIL_COMPLETE,
            {
                "members": list(result.member_responses.keys()),
                "strategy": result.strategy,
                "total_tokens": total_tokens,
                "total_cost_usd": round(total_cost, 6),
                "total_latency_ms": result.total_latency_ms,
                "quorum_met": result.quorum_met,
                "failed_members": result.failed_members,
                "synthesized": result.synthesis is not None,
            },
        )

        return result

    # -----------------------------------------------------------------------
    # Compare Mode
    # -----------------------------------------------------------------------

    async def compare(
        self,
        prompt: str,
        providers: list[str] | None = None,
        system_prompt: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> dict[str, CompletionResponse]:
        """Query multiple providers and return all responses for comparison."""
        await self.initialize()
        await self._check_budget()

        temp = temperature if temperature is not None else self.config.defaults.temperature
        max_tok = max_tokens or self.config.defaults.max_tokens
        sys_prompt = self._build_system_prompt(system_prompt or self.config.defaults.system_prompt)

        target_providers = providers or self.registry.list_enabled()
        messages = [Message(role="user", content=prompt)]

        tasks = {}
        for pname in target_providers:
            if not self.registry.has(pname):
                continue
            p = self.registry.get(pname)
            pconfig = self.config.providers.get(pname)
            pmodel = pconfig.default_model if pconfig else ""
            tasks[pname] = asyncio.create_task(
                p.complete(
                    messages=messages,
                    model=pmodel or None,
                    temperature=temp,
                    max_tokens=max_tok,
                    system_prompt=sys_prompt,
                )
            )

        results: dict[str, CompletionResponse] = {}
        for pname, task in tasks.items():
            try:
                results[pname] = await task
            except Exception as e:
                results[pname] = CompletionResponse(
                    content=f"Error: {e}",
                    model="",
                    provider=pname,
                    usage=Usage(),
                    finish_reason=FinishReason.ERROR,
                )

        for pname, resp in results.items():
            await self._log_query(resp, "compare")

        return results

    # -----------------------------------------------------------------------
    # Internals
    # -----------------------------------------------------------------------

    async def _build_messages(
        self,
        prompt: str,
        conversation_id: str | None,
        continue_last: bool,
        system_prompt: str | None,
        provider: str,
        model: str,
    ) -> list[Message]:
        """Build the message list, including conversation history if applicable."""
        if conversation_id or continue_last:
            conv_id = await self.context.get_or_create_conversation(
                conversation_id=conversation_id,
                continue_last=continue_last,
                provider=provider,
                model=model,
            )
            messages = await self.context.get_context_messages(conv_id, system_prompt)
            messages.append(Message(role="user", content=prompt))
            return messages

        messages: list[Message] = []
        if system_prompt:
            messages.append(Message(role="system", content=system_prompt))
        messages.append(Message(role="user", content=prompt))
        return messages

    async def _execute_with_fallback(
        self,
        messages: list[Message],
        decision: RoutingDecision,
        temperature: float,
        max_tokens: int,
        system_prompt: str | None,
        stream: bool,
    ) -> CompletionResponse:
        """Execute query with automatic fallback chain on failure."""
        fallback_chain = self._get_fallback_chain(decision.provider)
        failure_log: list[str] = []  # track why each provider failed

        for i, provider_name in enumerate(fallback_chain):
            if not self.registry.has(provider_name):
                failure_log.append(f"{provider_name}: not registered")
                continue

            # Check circuit breaker / rate limiter
            try:
                self.rate_manager.check_available(provider_name)
            except ProviderUnavailableError:
                failure_log.append(
                    f"{provider_name}: circuit breaker open",
                )
                continue
            except RateLimitError:
                failure_log.append(f"{provider_name}: rate limited")
                continue
            except Exception as e:
                failure_log.append(f"{provider_name}: health check error ({e})")
                continue

            provider = self.registry.get(provider_name)
            pconfig = self.config.providers.get(provider_name)
            model = decision.model if i == 0 else (pconfig.default_model if pconfig else "")

            try:
                if stream:
                    from nvh.utils.streaming import collect_stream
                    stream_iter = await provider.stream(
                        messages=messages,
                        model=model or None,
                        temperature=temperature,
                        max_tokens=max_tokens,
                        system_prompt=system_prompt,
                    ).__aiter__()
                    # For now, collect the stream (CLI will handle real streaming)
                    response = await collect_stream(stream_iter)
                else:
                    response = await provider.complete(
                        messages=messages,
                        model=model or None,
                        temperature=temperature,
                        max_tokens=max_tokens,
                        system_prompt=system_prompt,
                    )

                self.rate_manager.record_success(provider_name)

                if i > 0:
                    response.fallback_from = decision.provider
                    logger.info(
                        "Fallback succeeded: %s → %s (after %d failures)",
                        decision.provider, provider_name, i,
                    )
                    # Emit recovery event
                    await self.webhooks.emit(
                        WebhookEvent.PROVIDER_RECOVERED,
                        format_provider_alert(
                            provider=provider_name,
                            event_type=WebhookEvent.PROVIDER_RECOVERED,
                        ),
                    )

                return response

            except RateLimitError as e:
                self.rate_manager.record_failure(provider_name, e)
                failure_log.append(f"{provider_name}: rate limited ({e})")
                await self.webhooks.emit(
                    WebhookEvent.PROVIDER_DOWN,
                    format_provider_alert(
                        provider=provider_name,
                        event_type=WebhookEvent.PROVIDER_DOWN,
                        error=str(e),
                    ),
                )
                continue
            except ProviderError as e:
                self.rate_manager.record_failure(provider_name, e)
                failure_log.append(
                    f"{provider_name}: {type(e).__name__} — {e}",
                )
                await self.webhooks.emit(
                    WebhookEvent.PROVIDER_ERROR,
                    format_provider_alert(
                        provider=provider_name,
                        event_type=WebhookEvent.PROVIDER_ERROR,
                        error=str(e),
                    ),
                )
                continue

        # All providers failed — build a detailed, actionable error
        failure_summary = "\n  ".join(failure_log[:8]) if failure_log else "No providers attempted"
        raise ProviderError(
            f"All advisors failed — could not complete your request.\n\n"
            f"What was tried:\n  {failure_summary}\n\n"
            f"What to do:\n"
            f"  1. Check provider status:  nvh status\n"
            f"  2. Reconfigure providers:  nvh setup\n"
            f"  3. Try a local model:      nvh safe \"your question\"\n"
            f"  4. Try a specific provider: nvh ask --advisor groq \"your question\"",
            provider=decision.provider,
        )

    def _get_fallback_chain(self, primary: str) -> list[str]:
        """Build fallback chain: primary first, then configured fallback order."""
        chain = [primary]
        for p in self.config.council.fallback_order:
            if p not in chain:
                chain.append(p)
        # Add any remaining enabled providers
        for p in self.registry.list_enabled():
            if p not in chain:
                chain.append(p)
        return chain

    async def _check_budget(self) -> None:
        """Check if budget limits have been reached (serialised via lock)."""
        async with self._budget_lock:
            await self._check_budget_inner()

    async def _check_budget_inner(self) -> None:
        """Inner budget check — must only be called while holding _budget_lock."""
        budget = self.config.budget

        daily_spend = Decimal("0")
        monthly_spend = Decimal("0")

        if budget.daily_limit_usd > 0:
            daily_spend = await repo.get_spend("daily")
            if daily_spend >= budget.daily_limit_usd:
                if budget.hard_stop:
                    raise BudgetExceededError(
                        f"Daily budget limit reached"
                        f" (${daily_spend:.4f}"
                        f" / ${budget.daily_limit_usd:.2f}).\n"
                        f"Options:\n"
                        f"  Raise the limit:    nvh config set budget.daily_limit_usd 10\n"
                        f"  Disable hard stop:  nvh config set budget.hard_stop false\n"
                        f"  Use a free model:   nvh safe \"question\"  (local/free only)"
                    )
            elif (
                budget.alert_threshold > 0
                and float(daily_spend) / float(budget.daily_limit_usd)
                >= budget.alert_threshold
            ):
                await self.webhooks.emit(
                    WebhookEvent.BUDGET_THRESHOLD,
                    format_budget_alert(
                        daily_spend=float(daily_spend),
                        daily_limit=float(budget.daily_limit_usd),
                        monthly_spend=float(monthly_spend),
                        monthly_limit=float(budget.monthly_limit_usd),
                        threshold_pct=budget.alert_threshold,
                    ),
                )

        if budget.monthly_limit_usd > 0:
            monthly_spend = await repo.get_spend("monthly")
            if monthly_spend >= budget.monthly_limit_usd:
                if budget.hard_stop:
                    raise BudgetExceededError(
                        f"Monthly budget limit reached"
                        f" (${monthly_spend:.4f}"
                        f" / ${budget.monthly_limit_usd:.2f}).\n"
                        f"Options:\n"
                        f"  Raise the limit:    nvh config set budget.monthly_limit_usd 50\n"
                        f"  Disable hard stop:  nvh config set budget.hard_stop false\n"
                        f"  Use a free model:   nvh safe \"question\"  (local/free only)"
                    )
            elif (
                budget.alert_threshold > 0
                and float(monthly_spend) / float(budget.monthly_limit_usd)
                >= budget.alert_threshold
            ):
                if budget.daily_limit_usd <= 0:
                    # Only emit monthly alert if we haven't already emitted daily above
                    daily_spend = await repo.get_spend("daily")
                await self.webhooks.emit(
                    WebhookEvent.BUDGET_THRESHOLD,
                    format_budget_alert(
                        daily_spend=float(daily_spend),
                        daily_limit=float(budget.daily_limit_usd),
                        monthly_spend=float(monthly_spend),
                        monthly_limit=float(budget.monthly_limit_usd),
                        threshold_pct=budget.alert_threshold,
                    ),
                )

    async def _log_query(
        self,
        response: CompletionResponse,
        mode: str,
        cache_hit: bool = False,
        conversation_id: str | None = None,
    ) -> None:
        """Log a query to the database."""
        try:
            await repo.log_query(
                mode=mode,
                provider=response.provider,
                model=response.model,
                input_tokens=response.usage.input_tokens,
                output_tokens=response.usage.output_tokens,
                cost_usd=response.cost_usd,
                latency_ms=response.latency_ms,
                status="success" if response.finish_reason != FinishReason.ERROR else "error",
                cache_hit=cache_hit,
                fallback_from=response.fallback_from or "",
                conversation_id=conversation_id,
            )
        except Exception as e:
            logger.warning(f"Failed to log query: {e}")

    async def get_budget_status(self) -> dict[str, Any]:
        """Get current budget status."""
        daily = await repo.get_spend("daily")
        monthly = await repo.get_spend("monthly")
        daily_by_provider = await repo.get_spend_by_provider("daily")
        daily_queries = await repo.get_query_count("daily")
        monthly_queries = await repo.get_query_count("monthly")

        return {
            "daily_spend": daily,
            "daily_limit": self.config.budget.daily_limit_usd,
            "monthly_spend": monthly,
            "monthly_limit": self.config.budget.monthly_limit_usd,
            "daily_queries": daily_queries,
            "monthly_queries": monthly_queries,
            "by_provider": daily_by_provider,
        }


class BudgetExceededError(Exception):
    pass
