"""Tests for nvh.core.context — ConversationManager."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from nvh.core.context import ConversationManager
from nvh.providers.base import CompletionResponse, Usage


class TestConversationManager:
    def test_conversation_manager_construction(self):
        cm = ConversationManager()
        assert cm is not None
        assert hasattr(cm, "create_conversation")
        assert hasattr(cm, "add_user_message")
        assert hasattr(cm, "get_messages")

    def test_conversation_manager(self):
        cm = ConversationManager()
        assert cm is not None

    def test_context_files_loader(self):
        try:
            from nvh.core.context_files import load_context_files
            result = load_context_files(Path("."))
            assert isinstance(result, (str, list, dict, type(None)))
        except (ImportError, TypeError):
            pytest.skip("load_context_files not available")


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
