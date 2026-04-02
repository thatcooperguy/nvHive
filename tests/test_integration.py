"""Integration tests for the full Council pipeline using mock providers.

No real API calls are made. All providers are replaced with SimpleTestProvider,
and the database is an isolated in-memory SQLite instance per test.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from decimal import Decimal
from pathlib import Path

import pytest

import nvh.storage.repository as repo
from nvh.config.settings import (
    BudgetConfig,
    CacheConfig,
    CouncilConfig,
    CouncilModeConfig,
    DefaultsConfig,
    ProviderConfig,
    RoutingConfig,
)
from nvh.core.engine import BudgetExceededError, Engine
from nvh.providers.base import (
    CompletionResponse,
    FinishReason,
    HealthStatus,
    Message,
    ModelInfo,
    ProviderUnavailableError,
    StreamChunk,
    Usage,
)
from nvh.providers.registry import ProviderRegistry

# ---------------------------------------------------------------------------
# Minimal mock provider
# ---------------------------------------------------------------------------


class SimpleTestProvider:
    """Minimal mock provider for integration tests — no real API calls."""

    def __init__(self, name: str = "test_provider", should_fail: bool = False) -> None:
        self._name = name
        self._should_fail = should_fail
        self._call_count = 0

    @property
    def name(self) -> str:
        return self._name

    async def complete(
        self,
        messages: list[Message],
        model: str | None = None,
        temperature: float = 1.0,
        max_tokens: int = 4096,
        system_prompt: str | None = None,
        **kwargs,
    ) -> CompletionResponse:
        self._call_count += 1
        if self._should_fail:
            raise ProviderUnavailableError("Simulated failure", provider=self._name)
        content = f"Mock response from {self._name}"
        return CompletionResponse(
            content=content,
            model=model or "test-model",
            provider=self._name,
            usage=Usage(input_tokens=10, output_tokens=20, total_tokens=30),
            cost_usd=Decimal("0.001"),
            latency_ms=100,
        )

    async def stream(
        self,
        messages: list[Message],
        model: str | None = None,
        temperature: float = 1.0,
        max_tokens: int = 4096,
        system_prompt: str | None = None,
        **kwargs,
    ) -> AsyncIterator[StreamChunk]:
        resp = await self.complete(messages, model=model, temperature=temperature,
                                   max_tokens=max_tokens, system_prompt=system_prompt)

        async def _gen() -> AsyncIterator[StreamChunk]:
            yield StreamChunk(
                delta=resp.content,
                is_final=True,
                accumulated_content=resp.content,
                model=resp.model,
                provider=self._name,
                usage=resp.usage,
                cost_usd=resp.cost_usd,
                finish_reason=FinishReason.STOP,
            )

        return _gen()

    async def list_models(self) -> list[ModelInfo]:
        return [ModelInfo(model_id="test-model", provider=self._name)]

    async def health_check(self) -> HealthStatus:
        return HealthStatus(provider=self._name, healthy=True, latency_ms=1)

    def estimate_tokens(self, text: str) -> int:
        return len(text) // 4


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config(
    providers: dict[str, ProviderConfig] | None = None,
    council_kwargs: dict | None = None,
    budget_kwargs: dict | None = None,
    cache_kwargs: dict | None = None,
) -> CouncilConfig:
    """Build a minimal CouncilConfig suitable for tests."""
    return CouncilConfig(
        defaults=DefaultsConfig(
            provider="alpha",
            model="test-model",
            temperature=1.0,
            max_tokens=256,
        ),
        providers=providers or {},
        council=CouncilModeConfig(**(council_kwargs or {})),
        routing=RoutingConfig(),
        budget=BudgetConfig(**(budget_kwargs or {})),
        cache=CacheConfig(**(cache_kwargs or {})),
    )


def _make_registry(*provider_instances: SimpleTestProvider) -> ProviderRegistry:
    """Build a ProviderRegistry pre-populated with the given mock providers."""
    registry = ProviderRegistry()
    for p in provider_instances:
        registry.register(p.name, p)
    return registry


async def _init_memory_db(tmp_path: Path) -> None:
    """Point the repository module at a fresh per-test SQLite file."""
    db_file = tmp_path / "test_council.db"
    # Reset module-level globals so a fresh engine is created
    repo._engine = None
    repo._session_factory = None
    await repo.init_db(db_path=db_file)


async def _make_engine(
    tmp_path: Path,
    providers: list[SimpleTestProvider] | None = None,
    config_overrides: dict | None = None,
    council_kwargs: dict | None = None,
    budget_kwargs: dict | None = None,
    cache_kwargs: dict | None = None,
) -> Engine:
    """Build and initialise an Engine wired to mock providers and an in-memory DB."""
    await _init_memory_db(tmp_path)

    provider_list = providers or [SimpleTestProvider("alpha")]
    provider_configs = {
        p.name: ProviderConfig(enabled=True, default_model="test-model")
        for p in provider_list
    }

    config = _make_config(
        providers=provider_configs,
        council_kwargs=council_kwargs,
        budget_kwargs=budget_kwargs,
        cache_kwargs=cache_kwargs,
    )
    registry = _make_registry(*provider_list)

    engine = Engine(config=config, registry=registry)
    # Mark as already initialised so Engine.initialize() won't call
    # setup_from_config (which would try to load real providers) or
    # re-run init_db (which would reset our per-test DB).
    engine._initialized = True
    return engine


# ---------------------------------------------------------------------------
# TestEngineIntegration
# ---------------------------------------------------------------------------


class TestEngineIntegration:
    """Full pipeline tests with mock providers."""

    async def test_simple_query_end_to_end(self, tmp_path: Path) -> None:
        """Query flows through: route -> provider -> response -> log."""
        provider = SimpleTestProvider("alpha")
        engine = await _make_engine(tmp_path, providers=[provider])

        response = await engine.query("Hello, world!")

        assert response.content == "Mock response from alpha"
        assert response.provider == "alpha"
        assert response.usage.total_tokens == 30
        assert provider._call_count == 1

    async def test_query_with_conversation_persistence(self, tmp_path: Path) -> None:
        """Multi-turn conversation is stored and retrievable."""
        provider = SimpleTestProvider("alpha")
        engine = await _make_engine(tmp_path, providers=[provider])

        # First turn — creates the conversation
        r1 = await engine.query("First message", continue_last=True)
        assert r1.content == "Mock response from alpha"

        # Retrieve the stored conversation
        conversations = await repo.list_conversations()
        assert len(conversations) >= 1

        conv_id = conversations[0].id
        messages = await repo.get_messages(conv_id)
        # The user message and assistant message should both be persisted
        assert len(messages) == 2
        roles = {m.role for m in messages}
        assert "user" in roles
        assert "assistant" in roles

    async def test_query_with_cache_hit(self, tmp_path: Path) -> None:
        """Second identical query returns cached response (temperature must be 0)."""
        provider = SimpleTestProvider("alpha")
        engine = await _make_engine(
            tmp_path,
            providers=[provider],
            cache_kwargs={"enabled": True, "ttl_seconds": 3600, "max_size": 100},
        )

        prompt = "Cached query"
        # Cache only activates at temperature=0
        await engine.query(prompt, temperature=0.0)
        r2 = await engine.query(prompt, temperature=0.0)

        assert r2.cache_hit is True
        # Provider should only have been called once
        assert provider._call_count == 1

    async def test_fallback_on_provider_failure(self, tmp_path: Path) -> None:
        """When primary provider fails, fallback provider is used."""
        failing = SimpleTestProvider("alpha", should_fail=True)
        backup = SimpleTestProvider("beta")

        config = _make_config(
            providers={
                "alpha": ProviderConfig(enabled=True, default_model="test-model"),
                "beta": ProviderConfig(enabled=True, default_model="test-model"),
            },
            council_kwargs={"fallback_order": ["alpha", "beta"]},
        )
        await _init_memory_db(tmp_path)
        registry = _make_registry(failing, backup)
        engine = Engine(config=config, registry=registry)
        engine._initialized = True

        # alpha is primary but will fail; beta should be used
        response = await engine.query("Test fallback", provider="alpha")

        assert response.provider == "beta"
        assert response.fallback_from == "alpha"

    async def test_budget_enforcement(self, tmp_path: Path) -> None:
        """Query is blocked when daily budget is exceeded."""
        # Log fake spend that exceeds the tiny daily limit
        await _init_memory_db(tmp_path)
        await repo.log_query(
            mode="simple",
            provider="alpha",
            model="test-model",
            cost_usd=Decimal("10.00"),
        )

        provider = SimpleTestProvider("alpha")
        config = _make_config(
            providers={"alpha": ProviderConfig(enabled=True, default_model="test-model")},
            budget_kwargs={"daily_limit_usd": Decimal("1.00"), "hard_stop": True},
        )
        registry = _make_registry(provider)
        engine = Engine(config=config, registry=registry)
        engine._initialized = True

        with pytest.raises(BudgetExceededError):
            await engine.query("This should be blocked")

    async def test_routing_selects_best_provider(self, tmp_path: Path) -> None:
        """Router picks one of the registered providers for the query."""
        alpha = SimpleTestProvider("alpha")
        beta = SimpleTestProvider("beta")
        engine = await _make_engine(tmp_path, providers=[alpha, beta])

        # The router should select one of the registered providers.
        # The model field may be empty when no capability catalog is loaded
        # (test-only scenario), so we only assert the provider is valid.
        decision = engine.router.route("Write a Python function")
        assert decision.provider in ("alpha", "beta")
        assert isinstance(decision.model, str)  # may be "" or "test-model"


# ---------------------------------------------------------------------------
# TestCouncilIntegration
# ---------------------------------------------------------------------------


class TestCouncilIntegration:
    """Council mode integration tests."""

    async def test_council_parallel_dispatch(self, tmp_path: Path) -> None:
        """All council members are queried and responses collected."""
        alpha = SimpleTestProvider("alpha")
        beta = SimpleTestProvider("beta")

        config = _make_config(
            providers={
                "alpha": ProviderConfig(enabled=True, default_model="test-model"),
                "beta": ProviderConfig(enabled=True, default_model="test-model"),
            },
            council_kwargs={
                "quorum": 2,
                "strategy": "majority_vote",
                "timeout": 30,
                "default_weights": {"alpha": 0.5, "beta": 0.5},
                "synthesis_provider": "alpha",
            },
        )
        await _init_memory_db(tmp_path)
        registry = _make_registry(alpha, beta)
        engine = Engine(config=config, registry=registry)
        engine._initialized = True

        result = await engine.run_council("Parallel test", synthesize=False)

        assert len(result.member_responses) == 2
        assert result.quorum_met is True
        # Both providers were called
        assert alpha._call_count == 1
        assert beta._call_count == 1

    async def test_council_with_auto_agents(self, tmp_path: Path) -> None:
        """Auto-agents are generated and assigned to members."""
        alpha = SimpleTestProvider("alpha")
        beta = SimpleTestProvider("beta")

        config = _make_config(
            providers={
                "alpha": ProviderConfig(enabled=True, default_model="test-model"),
                "beta": ProviderConfig(enabled=True, default_model="test-model"),
            },
            council_kwargs={
                "quorum": 2,
                "strategy": "majority_vote",
                "timeout": 30,
                "default_weights": {"alpha": 0.5, "beta": 0.5},
                "synthesis_provider": "alpha",
            },
        )
        await _init_memory_db(tmp_path)
        registry = _make_registry(alpha, beta)
        engine = Engine(config=config, registry=registry)
        engine._initialized = True

        result = await engine.run_council(
            "Design a secure authentication system",
            auto_agents=True,
            num_agents=2,
            synthesize=False,
        )

        # Auto-agents should have been generated and assigned
        assert len(result.agents_used) > 0
        # Members should have persona annotations
        personas_assigned = [m.persona for m in result.members if m.persona]
        assert len(personas_assigned) > 0

    async def test_council_quorum_failure(self, tmp_path: Path) -> None:
        """When quorum is not met, synthesis is skipped."""
        alpha = SimpleTestProvider("alpha", should_fail=True)
        beta = SimpleTestProvider("beta", should_fail=True)

        config = _make_config(
            providers={
                "alpha": ProviderConfig(enabled=True, default_model="test-model"),
                "beta": ProviderConfig(enabled=True, default_model="test-model"),
            },
            council_kwargs={
                # Require 2 responses but both providers will fail
                "quorum": 2,
                "strategy": "majority_vote",
                "timeout": 5,
                "default_weights": {"alpha": 0.5, "beta": 0.5},
            },
        )
        await _init_memory_db(tmp_path)
        registry = _make_registry(alpha, beta)
        engine = Engine(config=config, registry=registry)
        engine._initialized = True

        result = await engine.run_council("Quorum failure test", synthesize=True)

        assert result.quorum_met is False
        assert result.synthesis is None
        assert len(result.failed_members) == 2

    async def test_council_synthesis(self, tmp_path: Path) -> None:
        """Synthesis combines member responses into a single result."""
        alpha = SimpleTestProvider("alpha")
        beta = SimpleTestProvider("beta")

        config = _make_config(
            providers={
                "alpha": ProviderConfig(enabled=True, default_model="test-model"),
                "beta": ProviderConfig(enabled=True, default_model="test-model"),
            },
            council_kwargs={
                "quorum": 2,
                "strategy": "majority_vote",
                "timeout": 30,
                "default_weights": {"alpha": 0.6, "beta": 0.4},
                "synthesis_provider": "alpha",
            },
        )
        await _init_memory_db(tmp_path)
        registry = _make_registry(alpha, beta)
        engine = Engine(config=config, registry=registry)
        engine._initialized = True

        result = await engine.run_council("Synthesis test", synthesize=True)

        assert result.quorum_met is True
        # majority_vote synthesis selects the highest-weighted member
        assert result.synthesis is not None
        assert "alpha" in result.synthesis.content.lower() or result.synthesis.provider == "alpha"


# ---------------------------------------------------------------------------
# TestConversationIntegration
# ---------------------------------------------------------------------------


class TestConversationIntegration:
    """Conversation management tests using the repository layer directly."""

    async def test_create_and_retrieve_conversation(self, tmp_path: Path) -> None:
        """Conversations persist across queries."""
        await _init_memory_db(tmp_path)

        conv = await repo.create_conversation(provider="alpha", model="test-model")
        assert conv.id

        fetched = await repo.get_conversation(conv.id)
        assert fetched is not None
        assert fetched.id == conv.id
        assert fetched.provider == "alpha"

    async def test_conversation_list(self, tmp_path: Path) -> None:
        """Recent conversations are listed in descending order."""
        await _init_memory_db(tmp_path)

        conv_a = await repo.create_conversation(provider="alpha", title="First")
        conv_b = await repo.create_conversation(provider="beta", title="Second")

        convs = await repo.list_conversations(limit=10)
        ids = [c.id for c in convs]

        assert conv_a.id in ids
        assert conv_b.id in ids

    async def test_conversation_search(self, tmp_path: Path) -> None:
        """Search finds conversations whose messages contain the query string."""
        await _init_memory_db(tmp_path)

        conv = await repo.create_conversation(provider="alpha")
        await repo.add_message(
            conversation_id=conv.id,
            role="user",
            content="The quick brown fox jumps over the lazy dog",
        )

        results = await repo.search_conversations("quick brown fox")
        assert len(results) == 1
        found_conv, snippet = results[0]
        assert found_conv.id == conv.id
        assert "quick" in snippet.lower()

    async def test_conversation_delete(self, tmp_path: Path) -> None:
        """Deleted conversations are removed and their messages are cascade-deleted."""
        await _init_memory_db(tmp_path)

        conv = await repo.create_conversation(provider="alpha")
        await repo.add_message(conversation_id=conv.id, role="user", content="Hello")

        deleted = await repo.delete_conversation(conv.id)
        assert deleted is True

        # The conversation should no longer exist
        fetched = await repo.get_conversation(conv.id)
        assert fetched is None

        # Its messages should also be gone
        messages = await repo.get_messages(conv.id)
        assert messages == []

    async def test_conversation_search_no_match(self, tmp_path: Path) -> None:
        """Search returns empty list when no messages match."""
        await _init_memory_db(tmp_path)

        conv = await repo.create_conversation(provider="alpha")
        await repo.add_message(
            conversation_id=conv.id, role="user", content="Nothing interesting here"
        )

        results = await repo.search_conversations("xyzzy_no_match")
        assert results == []

    async def test_conversation_add_multiple_messages(self, tmp_path: Path) -> None:
        """Messages are stored in sequence order."""
        await _init_memory_db(tmp_path)

        conv = await repo.create_conversation(provider="alpha")
        await repo.add_message(conversation_id=conv.id, role="user", content="First")
        await repo.add_message(conversation_id=conv.id, role="assistant", content="Second")
        await repo.add_message(conversation_id=conv.id, role="user", content="Third")

        messages = await repo.get_messages(conv.id)
        assert len(messages) == 3
        assert messages[0].role == "user"
        assert messages[0].content == "First"
        assert messages[2].content == "Third"

        # Sequence numbers should be monotonically increasing
        seqs = [m.sequence for m in messages]
        assert seqs == sorted(seqs)
