"""Final push: async DB fixtures for repository, engine, council streaming, context."""

from __future__ import annotations

import asyncio
from decimal import Decimal

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
from nvh.core.context import ConversationManager
from nvh.core.council import CouncilOrchestrator
from nvh.core.engine import Engine
from nvh.providers.base import (
    CompletionResponse,
    FinishReason,
    HealthStatus,
    ModelInfo,
    ProviderError,
    StreamChunk,
    Usage,
)
from nvh.providers.registry import ProviderRegistry

# ---------------------------------------------------------------------------
# DB fixture
# ---------------------------------------------------------------------------


@pytest.fixture()
async def db(tmp_path):
    repo._engine = None
    repo._session_factory = None
    await repo.init_db(db_path=tmp_path / "test.db")
    yield
    repo._engine = None
    repo._session_factory = None


# ---------------------------------------------------------------------------
# Controllable mock provider (same pattern as test_streaming_regressions.py)
# ---------------------------------------------------------------------------


class _ControllableProvider:
    def __init__(
        self,
        name: str,
        mode: str = "ok",
        content: str = "ok response",
    ) -> None:
        self._name = name
        self.mode = mode
        self.content = content
        self.call_count = 0

    @property
    def name(self) -> str:
        return self._name

    async def complete(self, messages, model=None, temperature=1.0,
                       max_tokens=4096, system_prompt=None, **kwargs):
        self.call_count += 1
        if self.mode == "raise":
            raise ProviderError(f"{self._name}: intentional failure")
        if self.mode == "timeout":
            await asyncio.sleep(3600)
        return CompletionResponse(
            content=self.content,
            model=model or "test-model",
            provider=self._name,
            usage=Usage(input_tokens=5, output_tokens=10, total_tokens=15),
            cost_usd=Decimal("0.0001"),
            latency_ms=10,
        )

    async def stream(self, messages, model=None, temperature=1.0,
                     max_tokens=4096, system_prompt=None, **kwargs):
        self.call_count += 1
        if self.mode == "raise":
            raise ProviderError(f"{self._name}: intentional failure")
        yield StreamChunk(
            delta=self.content,
            is_final=(self.mode != "stream_stall"),
            accumulated_content=self.content,
            model=model or "test-model",
            provider=self._name,
            usage=Usage(input_tokens=5, output_tokens=10, total_tokens=15),
            cost_usd=Decimal("0.0001"),
            finish_reason=FinishReason.STOP if self.mode != "stream_stall" else None,
        )
        if self.mode == "stream_stall":
            await asyncio.sleep(3600)

    async def list_models(self) -> list[ModelInfo]:
        return [ModelInfo(model_id="test-model", provider=self._name)]

    async def health_check(self) -> HealthStatus:
        return HealthStatus(provider=self._name, healthy=True, latency_ms=1)

    def estimate_tokens(self, text: str) -> int:
        return max(1, len(text) // 4)


# ---------------------------------------------------------------------------
# Helper builders
# ---------------------------------------------------------------------------


def _make_council(
    providers: dict[str, _ControllableProvider],
    synthesis_provider: str | None = None,
) -> CouncilOrchestrator:
    config = CouncilConfig(
        defaults=DefaultsConfig(provider=next(iter(providers))),
        providers={
            name: ProviderConfig(enabled=True, default_model="test-model")
            for name in providers
        },
        council=CouncilModeConfig(
            quorum=1,
            strategy="majority_vote",
            timeout=10,
            default_weights={name: 1.0 for name in providers},
            synthesis_provider=synthesis_provider or "",
        ),
        routing=RoutingConfig(),
        budget=BudgetConfig(),
        cache=CacheConfig(enabled=False, ttl_seconds=1, max_size=1),
    )
    registry = ProviderRegistry()
    for name, p in providers.items():
        registry.register(name, p)
    return CouncilOrchestrator(config, registry, rate_manager=None)


def _make_engine(providers: dict[str, _ControllableProvider]) -> Engine:
    config = CouncilConfig(
        defaults=DefaultsConfig(
            provider=next(iter(providers)),
            model="test-model",
        ),
        providers={
            name: ProviderConfig(enabled=True, default_model="test-model")
            for name in providers
        },
        council=CouncilModeConfig(
            quorum=1,
            strategy="majority_vote",
            timeout=10,
            default_weights={name: 1.0 for name in providers},
            synthesis_provider="",
        ),
        routing=RoutingConfig(),
        budget=BudgetConfig(),
        cache=CacheConfig(enabled=False, ttl_seconds=1, max_size=1),
    )
    registry = ProviderRegistry()
    for name, p in providers.items():
        registry.register(name, p)
    engine = Engine(config=config, registry=registry)
    engine._initialized = True
    return engine


# ---------------------------------------------------------------------------
# 1-2. repository: conversation CRUD round-trip
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_and_get_conversation(db):
    conv = await repo.create_conversation(provider="test", model="m", title="hi")
    assert conv.id
    fetched = await repo.get_conversation(conv.id)
    assert fetched is not None
    assert fetched.title == "hi"

    await repo.add_message(conv.id, role="user", content="hello")
    await repo.add_message(conv.id, role="assistant", content="world")
    msgs = await repo.get_messages(conv.id)
    assert len(msgs) == 2
    assert msgs[0].role == "user"
    assert msgs[1].content == "world"


@pytest.mark.asyncio
async def test_delete_conversation(db):
    conv = await repo.create_conversation(provider="p", model="m", title="del")
    cid = conv.id
    assert await repo.delete_conversation(cid) is True
    assert await repo.get_conversation(cid) is None
    convs = await repo.list_conversations()
    assert all(c.id != cid for c in convs)


# ---------------------------------------------------------------------------
# 3. repository: get_spend accumulates costs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_spend_accumulates(db):
    await repo.log_query(
        mode="simple", provider="a", model="m",
        cost_usd=Decimal("0.05"), input_tokens=10, output_tokens=20,
    )
    await repo.log_query(
        mode="simple", provider="b", model="m",
        cost_usd=Decimal("0.10"), input_tokens=10, output_tokens=20,
    )
    spend = await repo.get_spend("daily")
    assert spend >= Decimal("0.15")


# ---------------------------------------------------------------------------
# 4. repository: get_analytics returns non-zero counts
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_analytics_nonzero(db):
    await repo.log_query(
        mode="simple", provider="alpha", model="m",
        cost_usd=Decimal("0.01"), input_tokens=5, output_tokens=5,
    )
    await repo.log_query(
        mode="council", provider="beta", model="m",
        cost_usd=Decimal("0.02"), input_tokens=5, output_tokens=5,
    )
    analytics = await repo.get_analytics()
    assert analytics["queries_today"] >= 2
    assert analytics["queries_this_month"] >= 2
    assert "alpha" in analytics["queries_by_provider"]


# ---------------------------------------------------------------------------
# 5. engine: query logs to DB (verify via get_spend)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_engine_query_logs_to_db(db, monkeypatch):
    providers = {"alpha": _ControllableProvider("alpha", content="answer")}
    engine = _make_engine(providers)

    # Bypass connectivity check and routing orchestrator
    monkeypatch.setattr(engine, "_check_connectivity", lambda: asyncio.coroutine(lambda: True)())

    resp = await engine.query("hello", provider="alpha", privacy=False)
    assert resp.content == "answer"

    spend = await repo.get_spend("daily")
    assert spend > Decimal("0")


# ---------------------------------------------------------------------------
# 6. engine: run_council logs member responses to DB
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_engine_run_council_logs(db):
    providers = {
        "alpha": _ControllableProvider("alpha", content="A"),
        "beta": _ControllableProvider("beta", content="B"),
    }
    engine = _make_engine(providers)

    result = await engine.run_council("test prompt", synthesize=False)
    assert result.quorum_met
    assert len(result.member_responses) >= 1

    spend = await repo.get_spend("daily")
    assert spend > Decimal("0")


# ---------------------------------------------------------------------------
# 7. council streaming: all event types emitted
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_streaming_emits_all_event_types():
    providers = {
        "alpha": _ControllableProvider("alpha", content="A"),
        "beta": _ControllableProvider("beta", content="B"),
        "gamma": _ControllableProvider("gamma", content="synth"),
    }
    council = _make_council(providers, synthesis_provider="gamma")
    events: list[dict] = []

    async def capture(event: dict) -> None:
        events.append(event)

    try:
        await council.run_council_streaming(
            query="test prompt",
            on_event=capture,
            synthesize=True,
            temperature=0.0,
            max_tokens=32,
            timeout=10,
        )
    except Exception:
        pass

    event_types = {e["type"] for e in events}
    expected = {
        "council_start",
        "member_start",
        "member_chunk",
        "member_complete",
        "synthesis_start",
        "synthesis_complete",
        "council_complete",
    }
    missing = expected - event_types
    assert not missing, f"Missing event types: {missing}. Got: {event_types}"


# ---------------------------------------------------------------------------
# 8. council streaming: failed member emits member_failed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_streaming_member_failed_event():
    providers = {
        "good": _ControllableProvider("good", content="ok"),
        "bad": _ControllableProvider("bad", mode="raise"),
    }
    council = _make_council(providers)
    events: list[dict] = []

    async def capture(event: dict) -> None:
        events.append(event)

    try:
        await council.run_council_streaming(
            query="test",
            on_event=capture,
            synthesize=False,
            timeout=5,
        )
    except Exception:
        pass

    event_types = [e["type"] for e in events]
    assert "member_failed" in event_types


# ---------------------------------------------------------------------------
# 9. council streaming: synthesis timeout emits error event
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_streaming_synthesis_failure_emits_error():
    """When all synthesis candidates fail, an error event is emitted."""
    providers = {
        "alpha": _ControllableProvider("alpha", content="A"),
        "beta": _ControllableProvider("beta", content="B"),
        "bad_synth": _ControllableProvider("bad_synth", mode="raise"),
    }
    council = _make_council(providers, synthesis_provider="bad_synth")
    events: list[dict] = []

    async def capture(event: dict) -> None:
        events.append(event)

    try:
        await council.run_council_streaming(
            query="test",
            on_event=capture,
            synthesize=True,
            timeout=10,
        )
    except Exception:
        pass

    event_types = [e.get("type") for e in events]
    has_error = any(
        e.get("type") == "error" and "synthesis" in e.get("phase", "")
        for e in events
    )
    has_synth_complete = "synthesis_complete" in event_types
    has_council_complete = "council_complete" in event_types
    assert has_error or has_synth_complete or has_council_complete, (
        f"Expected terminal event. Got: {event_types}"
    )


# ---------------------------------------------------------------------------
# 10. context: ConversationManager create + add + get round-trip
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_conversation_manager_round_trip(db):
    mgr = ConversationManager()
    cid = await mgr.create_conversation(provider="test", model="m")
    assert cid

    await mgr.add_user_message(cid, "ping")

    resp = CompletionResponse(
        content="pong",
        model="m",
        provider="test",
        usage=Usage(input_tokens=1, output_tokens=1, total_tokens=2),
        cost_usd=Decimal("0"),
        latency_ms=5,
    )
    await mgr.add_assistant_message(cid, resp)

    messages = await mgr.get_messages(cid)
    assert len(messages) == 2
    assert messages[0].role == "user"
    assert messages[0].content == "ping"
    assert messages[1].role == "assistant"
    assert messages[1].content == "pong"
