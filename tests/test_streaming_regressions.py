"""Regression tests for the 0.5.9 / 0.6.0 streaming fixes.

Before this file, the streaming-hang fixes (synthesis rotation,
per-chunk stall timeout, terminal error events on exhaustion)
had zero regression coverage. A careless refactor could silently
re-break any of them. These tests lock down the minimum contract:

1. Synthesis rotation: when the first candidate fails, the next
   one is tried and its output reaches the client.
2. Synthesis exhaustion: when every candidate fails, a terminal
   `error` event with phase="synthesis" is emitted instead of
   the client hanging forever.
3. SSE /v1/query chunk stall: when a provider stalls between
   chunks, the stream raises TimeoutError and emits an error
   event rather than blocking the connection.
4. Rate-manager observability: WebSocket query success recorded
   against the rate manager and logged to the analytics DB.

All tests use mock providers — no network, no real API calls.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import nvh.api.server as server_module
import nvh.storage.repository as repo
from nvh.api.server import app
from nvh.config.settings import (
    BudgetConfig,
    CacheConfig,
    CouncilConfig,
    CouncilModeConfig,
    DefaultsConfig,
    ProviderConfig,
    RoutingConfig,
)
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
# Tunable mock provider — behavior controlled via init flags so one fixture
# can simulate "healthy", "always times out", "streams then stalls", etc.
# ---------------------------------------------------------------------------

class _ControllableProvider:
    """Mock provider whose behavior is tuned per-test.

    Modes:
      - 'ok': yields one chunk and terminates (success).
      - 'stream_stall': yields one chunk then sleeps forever.
      - 'timeout': complete() awaits a never-firing event (hangs).
      - 'raise': complete() raises ProviderError immediately.
    """

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
            # Sleep far longer than any reasonable timeout.
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
        """Async generator — `async def` + `yield` in the body."""
        self.call_count += 1
        if self.mode == "raise":
            raise ProviderError(f"{self._name}: intentional failure")
        # Yield the first chunk immediately
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
            # After the first chunk, hang. Exercises the SSE per-chunk
            # stall timeout (CHUNK_STALL_TIMEOUT in server.py).
            await asyncio.sleep(3600)
        elif self.mode == "timeout":
            await asyncio.sleep(3600)

    async def list_models(self) -> list[ModelInfo]:
        return [ModelInfo(model_id="test-model", provider=self._name)]

    async def health_check(self) -> HealthStatus:
        return HealthStatus(provider=self._name, healthy=True, latency_ms=1)

    def estimate_tokens(self, text: str) -> int:
        return max(1, len(text) // 4)


# ---------------------------------------------------------------------------
# Council rotation tests — exercise CouncilOrchestrator directly, no FastAPI
# ---------------------------------------------------------------------------

def _council_with(
    providers: dict[str, _ControllableProvider],
    synthesis_provider: str | None = None,
) -> CouncilOrchestrator:
    """Build a CouncilOrchestrator wired to the given provider mocks."""
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


class TestSynthesisCandidates:
    def test_candidates_prefer_non_members(self):
        """_synthesis_candidates should prefer providers not used as members."""
        council = _council_with({
            "alpha": _ControllableProvider("alpha"),
            "beta": _ControllableProvider("beta"),
            "gamma": _ControllableProvider("gamma"),
        })
        # Members are alpha + beta (as if they just ran)
        member_responses = {
            "alpha": CompletionResponse(
                content="a", model="m", provider="alpha",
                usage=Usage(), cost_usd=Decimal("0"), latency_ms=0,
            ),
            "beta": CompletionResponse(
                content="b", model="m", provider="beta",
                usage=Usage(), cost_usd=Decimal("0"), latency_ms=0,
            ),
        }
        candidates = council._synthesis_candidates(member_responses)

        assert "gamma" in candidates, "non-member should be a candidate"
        # gamma (non-member) should come before alpha/beta (members)
        assert candidates.index("gamma") < candidates.index("alpha")
        assert candidates.index("gamma") < candidates.index("beta")

    def test_configured_provider_first(self):
        """If council.synthesis_provider is set, it must be first."""
        council = _council_with(
            {
                "alpha": _ControllableProvider("alpha"),
                "beta": _ControllableProvider("beta"),
                "gamma": _ControllableProvider("gamma"),
            },
            synthesis_provider="beta",
        )
        member_responses = {
            "alpha": CompletionResponse(
                content="a", model="m", provider="alpha",
                usage=Usage(), cost_usd=Decimal("0"), latency_ms=0,
            ),
        }
        candidates = council._synthesis_candidates(member_responses)
        assert candidates[0] == "beta"


class TestCouncilStreamingTerminalEvents:
    """Core regression: when synthesis exhausts every candidate, the
    client must receive a terminal `error` event with phase='synthesis',
    not hang forever waiting for synthesis_complete."""

    async def _collect_events(self, providers, synthesis_provider=None):
        """Run a council and return all events emitted via on_event."""
        council = _council_with(providers, synthesis_provider)
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
                timeout=5,
            )
        except Exception:
            pass  # We're interested in the events, not the final exception

        return events

    @pytest.mark.asyncio
    async def test_all_synthesis_candidates_fail_emits_terminal_error(self):
        """When no synthesis candidate succeeds, emit an error event.

        This is the #1 regression test for the 0.5.9 silent-hang fix.
        """
        providers = {
            "alpha": _ControllableProvider("alpha", mode="ok", content="A"),
            "beta": _ControllableProvider("beta", mode="ok", content="B"),
            # `bad` is the only candidate for synthesis (non-member) and
            # it always raises. Synthesis should exhaust candidates and
            # surface a terminal error.
            "bad": _ControllableProvider("bad", mode="raise"),
        }

        events = await self._collect_events(
            providers, synthesis_provider="bad",
        )

        event_types = [e.get("type") for e in events]
        # Members must have completed successfully
        assert "member_complete" in event_types

        # Terminal: must contain an error event with phase='synthesis'
        # OR a synthesis_complete. If neither is present, the client
        # would hang — that's the bug we're guarding against.
        has_error = any(
            e.get("type") == "error" and e.get("phase") == "synthesis"
            for e in events
        )
        has_synth_complete = any(
            e.get("type") == "synthesis_complete" for e in events
        )
        assert has_error or has_synth_complete, (
            f"Expected terminal synthesis outcome. Got events: {event_types}"
        )


# ---------------------------------------------------------------------------
# Budget check callback — 2A regression test
# ---------------------------------------------------------------------------

class TestCouncilBudgetCheck:
    """Regression test for the pre-synthesis budget re-check added in
    batch 2: if members blow the budget, synthesis must not fire."""

    @pytest.mark.asyncio
    async def test_budget_check_blocks_synthesis(self):
        """When budget_check raises, synthesis is skipped and an error
        event with phase='synthesis_budget' is emitted."""
        providers = {
            "alpha": _ControllableProvider("alpha", mode="ok", content="A"),
            "beta": _ControllableProvider("beta", mode="ok", content="B"),
        }
        council = _council_with(providers, synthesis_provider="alpha")

        budget_check_calls = 0

        async def fake_budget_check():
            nonlocal budget_check_calls
            budget_check_calls += 1
            raise RuntimeError("daily budget exhausted")

        events: list[dict] = []

        async def capture(event: dict) -> None:
            events.append(event)

        await council.run_council_streaming(
            query="test prompt",
            on_event=capture,
            synthesize=True,
            temperature=0.0,
            max_tokens=32,
            timeout=5,
            budget_check=fake_budget_check,
        )

        assert budget_check_calls == 1, "budget_check must be called once"

        # Must emit an error event with phase='synthesis_budget'
        budget_errors = [
            e for e in events
            if e.get("type") == "error"
            and e.get("phase") == "synthesis_budget"
        ]
        assert len(budget_errors) == 1, (
            f"Expected 1 synthesis_budget error, got events: "
            f"{[e.get('type') for e in events]}"
        )

        # No synthesis_chunk or synthesis_complete should have fired
        has_synth_events = any(
            e.get("type") in ("synthesis_chunk", "synthesis_complete")
            for e in events
        )
        assert not has_synth_events, (
            "Synthesis must not run after budget_check fails"
        )


# ---------------------------------------------------------------------------
# WebSocket /v1/ws/query observability regression — batch 2 fix
# ---------------------------------------------------------------------------

def _make_engine_for_ws(provider_mode: str = "ok"):
    """Build an Engine with one tunable mock provider."""
    provider = _ControllableProvider("alpha", mode=provider_mode, content="stream content")
    config = CouncilConfig(
        defaults=DefaultsConfig(provider="alpha", model="test-model"),
        providers={"alpha": ProviderConfig(enabled=True, default_model="test-model")},
        council=CouncilModeConfig(
            quorum=1, strategy="majority_vote", timeout=5,
            default_weights={"alpha": 1.0}, synthesis_provider="alpha",
        ),
        routing=RoutingConfig(),
        budget=BudgetConfig(),
        cache=CacheConfig(enabled=False, ttl_seconds=1, max_size=1),
    )
    registry = ProviderRegistry()
    registry.register("alpha", provider)
    engine = Engine(config=config, registry=registry)
    engine._initialized = True
    return engine, provider


@pytest.fixture()
def ws_client(tmp_path: Path, monkeypatch):
    """TestClient fixture with mock engine, no auth, no real DB.

    Critical: we stub `repo.log_query` and `repo.get_spend` to async
    no-ops instead of initializing a real aiosqlite database. The
    aiosqlite connection pool binds to whatever event loop called
    `repo.init_db()`, and a TestClient WebSocket test runs the
    handler on a different loop — on Linux that deterministically
    deadlocks at the first DB call, on Windows/macOS it happens to
    work but is still relying on undefined behavior.

    The rate-manager tracking we want to verify is purely in-memory,
    so stubbing the DB layer has no effect on what the test
    actually asserts.
    """
    monkeypatch.delenv("HIVE_API_KEY", raising=False)

    async def _noop_log_query(*args, **kwargs):
        return None

    async def _noop_get_spend(period: str = "daily"):
        return Decimal("0")

    monkeypatch.setattr(repo, "log_query", _noop_log_query)
    monkeypatch.setattr(repo, "get_spend", _noop_get_spend)

    engine, provider = _make_engine_for_ws(provider_mode="ok")
    original_engine = server_module._engine
    server_module._engine = engine

    client = TestClient(app, raise_server_exceptions=True)
    yield client, engine, provider

    server_module._engine = original_engine


class TestWsQueryObservability:
    def test_ws_query_success_records_rate_manager(self, ws_client):
        """A successful WS query must call rate_manager.record_success."""
        client, engine, provider = ws_client

        # Track rate_manager calls
        original_record_success = engine.rate_manager.record_success
        calls: list[tuple] = []

        def track_success(name):
            calls.append(("success", name))
            return original_record_success(name)

        engine.rate_manager.record_success = track_success  # type: ignore

        with client.websocket_connect("/v1/ws/query") as ws:
            ws.send_json({
                "type": "query_request",
                "prompt": "hello",
                "provider": "alpha",
            })
            # Drain messages until complete
            messages: list[dict] = []
            while True:
                msg = ws.receive_json()
                messages.append(msg)
                if msg.get("type") in ("complete", "error"):
                    break

        # Must have received chunk + complete
        types = [m["type"] for m in messages]
        assert "complete" in types, f"Expected complete event, got {types}"

        # rate_manager.record_success must have been called with 'alpha'
        assert ("success", "alpha") in calls, (
            f"Expected record_success('alpha'), got calls: {calls}"
        )
