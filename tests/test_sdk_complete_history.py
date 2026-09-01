"""Regression tests: nvh.complete() forwards full multi-turn history.

complete() used to extract only the last user message and drop all prior
user/assistant turns. These tests fake the provider layer and assert the
exact message list delivered, both through the SDK and directly via
Engine.query(history=...).
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import nvh.sdk as sdk
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
from nvh.core.engine import Engine
from nvh.providers.base import CompletionResponse, Message, Usage
from nvh.providers.registry import ProviderRegistry


class CapturingProvider:
    """Mock provider that records the message lists it receives."""

    def __init__(self, name: str = "alpha") -> None:
        self._name = name
        self.calls: list[list[Message]] = []

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
        self.calls.append(list(messages))
        return CompletionResponse(
            content=f"Mock response from {self._name}",
            model=model or "test-model",
            provider=self._name,
            usage=Usage(input_tokens=10, output_tokens=20, total_tokens=30),
            cost_usd=Decimal("0.001"),
            latency_ms=100,
        )

    def estimate_tokens(self, text: str) -> int:
        return len(text) // 4


async def _make_engine(tmp_path: Path) -> tuple[Engine, CapturingProvider]:
    """Build an Engine wired to a capturing provider and a per-test DB."""
    db_file = tmp_path / "test_history.db"
    repo._engine = None
    repo._session_factory = None
    await repo.init_db(db_path=db_file)

    provider = CapturingProvider("alpha")
    config = CouncilConfig(
        defaults=DefaultsConfig(
            provider="alpha",
            model="test-model",
            temperature=1.0,
            max_tokens=256,
            system_prompt="",
        ),
        providers={"alpha": ProviderConfig(enabled=True, default_model="test-model")},
        council=CouncilModeConfig(),
        routing=RoutingConfig(),
        budget=BudgetConfig(),
        cache=CacheConfig(),
    )
    registry = ProviderRegistry()
    registry.register(provider.name, provider)

    engine = Engine(config=config, registry=registry)
    engine._initialized = True
    # Keep system prompts byte-identical to what the tests pass in
    engine._context_files = []
    return engine, provider


def _roles_and_contents(messages: list[Message]) -> list[tuple[str, str]]:
    return [(m.role, m.content) for m in messages]


class TestCompleteHistory:
    """nvh.complete() must deliver every prior turn to the provider."""

    async def test_full_history_delivered_in_order(self, tmp_path: Path) -> None:
        engine, provider = await _make_engine(tmp_path)
        sdk._engine = engine
        try:
            with patch.object(engine, "_check_connectivity", return_value=True):
                response = await sdk.complete([
                    {"role": "system", "content": "You are terse."},
                    {"role": "user", "content": "user1"},
                    {"role": "assistant", "content": "assistant1"},
                    {"role": "user", "content": "user2"},
                ])
        finally:
            sdk._engine = None

        assert response.content == "Mock response from alpha"
        assert len(provider.calls) == 1
        assert _roles_and_contents(provider.calls[0]) == [
            ("system", "You are terse."),
            ("user", "user1"),
            ("assistant", "assistant1"),
            ("user", "user2"),
        ]
        # Routing saw the final user turn, not the whole transcript
        assert engine._last_prompt == "user2"

    async def test_single_user_message_unchanged(self, tmp_path: Path) -> None:
        engine, provider = await _make_engine(tmp_path)
        sdk._engine = engine
        try:
            with patch.object(engine, "_check_connectivity", return_value=True):
                await sdk.complete([{"role": "user", "content": "hello"}])
        finally:
            sdk._engine = None

        assert _roles_and_contents(provider.calls[0]) == [("user", "hello")]

    async def test_no_user_message_keeps_empty_prompt(self, tmp_path: Path) -> None:
        # An assistant turn must not be re-rolled as a user message; with no
        # user turn the prompt stays empty (an obvious, debuggable failure)
        engine, provider = await _make_engine(tmp_path)
        sdk._engine = engine
        try:
            with patch.object(engine, "_check_connectivity", return_value=True):
                await sdk.complete([
                    {"role": "system", "content": "sys"},
                    {"role": "assistant", "content": "prior answer"},
                ])
        finally:
            sdk._engine = None

        assert engine._last_prompt == ""
        assert _roles_and_contents(provider.calls[0]) == [
            ("system", "sys"),
            ("user", ""),
        ]

    async def test_trailing_assistant_prefill_dropped_not_reordered(
        self, tmp_path: Path,
    ) -> None:
        # The engine appends the prompt last, so a trailing assistant prefill
        # cannot be forwarded without inverting the conversation (which
        # Anthropic rejects outright). It is dropped, as before history
        # support existed — never moved ahead of the final user turn.
        engine, provider = await _make_engine(tmp_path)
        sdk._engine = engine
        try:
            with patch.object(engine, "_check_connectivity", return_value=True):
                await sdk.complete([
                    {"role": "user", "content": "Return JSON"},
                    {"role": "assistant", "content": "{"},
                ])
        finally:
            sdk._engine = None

        assert _roles_and_contents(provider.calls[0]) == [
            ("user", "Return JSON"),
        ]

    async def test_multiple_system_messages_joined_in_order(
        self, tmp_path: Path,
    ) -> None:
        engine, provider = await _make_engine(tmp_path)
        sdk._engine = engine
        try:
            with patch.object(engine, "_check_connectivity", return_value=True):
                await sdk.complete([
                    {"role": "system", "content": "safety policy"},
                    {"role": "system", "content": "tone: terse"},
                    {"role": "user", "content": "q"},
                ])
        finally:
            sdk._engine = None

        assert _roles_and_contents(provider.calls[0]) == [
            ("system", "safety policy\n\ntone: terse"),
            ("user", "q"),
        ]


class TestEngineQueryHistory:
    """Engine.query(history=...) inserts turns between system and prompt."""

    async def test_history_builds_expected_message_list(self, tmp_path: Path) -> None:
        engine, provider = await _make_engine(tmp_path)

        await engine.query(
            prompt="user2",
            provider="alpha",
            system_prompt="sys",
            history=[
                Message(role="user", content="user1"),
                Message(role="assistant", content="assistant1"),
            ],
        )

        assert _roles_and_contents(provider.calls[0]) == [
            ("system", "sys"),
            ("user", "user1"),
            ("assistant", "assistant1"),
            ("user", "user2"),
        ]

    async def test_privacy_mode_keeps_history(self, tmp_path: Path) -> None:
        engine, provider = await _make_engine(tmp_path)

        await engine.query(
            prompt="user2",
            provider="alpha",
            system_prompt="sys",
            privacy=True,
            history=[
                Message(role="user", content="user1"),
                Message(role="assistant", content="assistant1"),
            ],
        )

        assert _roles_and_contents(provider.calls[0]) == [
            ("system", "sys"),
            ("user", "user1"),
            ("assistant", "assistant1"),
            ("user", "user2"),
        ]

    async def test_conversation_context_ignores_history(self, tmp_path: Path) -> None:
        engine, provider = await _make_engine(tmp_path)

        await engine.query(
            prompt="user2",
            provider="alpha",
            system_prompt="sys",
            continue_last=True,
            history=[Message(role="user", content="user1")],
        )

        assert _roles_and_contents(provider.calls[0]) == [
            ("system", "sys"),
            ("user", "user2"),
        ]
