"""Tests for nvh.storage — ORM models and the repository DAO."""

from __future__ import annotations

import asyncio
from decimal import Decimal
from pathlib import Path

import pytest

import nvh.storage.repository as repo


class TestStorageModels:
    def test_import_models(self):
        from nvh.storage import models
        assert hasattr(models, "Base") or hasattr(models, "QueryLog")

    def test_query_log_construction(self):
        from nvh.storage.models import QueryLog
        log = QueryLog(
            mode="simple",
            provider="groq",
            model="llama-70b",
            input_tokens=10,
            output_tokens=20,
            cost_usd=0.001,
            latency_ms=100,
            status="success",
        )
        assert log.provider == "groq"
        assert log.cost_usd == 0.001


@pytest.mark.asyncio
class TestQueryLog:
    async def test_init_db_creates_tables(self, tmp_path: Path):
        db_file = tmp_path / "t.db"
        await repo.init_db(db_file)
        try:
            session = repo.get_session()
            assert session is not None
        finally:
            await repo.close_db()

    async def test_log_query_inserts(self, db):
        ql = await repo.log_query(
            mode="single", provider="openai", model="gpt-4o",
            input_tokens=100, output_tokens=50,
            cost_usd=Decimal("0.005"), latency_ms=200,
        )
        assert ql.provider == "openai"
        assert ql.cost_usd == Decimal("0.005")

    async def test_get_spend_accumulates(self, db):
        await repo.log_query(
            mode="single", provider="a", model="m",
            cost_usd=Decimal("0.010"),
        )
        await repo.log_query(
            mode="single", provider="b", model="m",
            cost_usd=Decimal("0.020"),
        )
        spend = await repo.get_spend("daily")
        assert spend >= Decimal("0.030")

    async def test_get_analytics_returns_structure(self, db):
        await repo.log_query(mode="single", provider="x", model="m")
        analytics = await repo.get_analytics()
        assert "queries_today" in analytics
        assert "cost_by_provider" in analytics
        assert "savings" in analytics

    async def test_get_session_raises_before_init(self):
        old_factory = repo._session_factory
        repo._session_factory = None
        try:
            with pytest.raises(RuntimeError, match="not initialized"):
                repo.get_session()
        finally:
            repo._session_factory = old_factory


class TestConversations:
    @pytest.mark.asyncio
    async def test_create_and_get_conversation(self, db):
        conv = await repo.create_conversation(
            provider="openai", model="gpt-4", title="Test chat",
        )
        assert conv.id
        assert conv.title == "Test chat"

        fetched = await repo.get_conversation(conv.id)
        assert fetched is not None
        assert fetched.provider == "openai"
        assert fetched.model == "gpt-4"

    @pytest.mark.asyncio
    async def test_list_conversations_pagination(self, db):
        # Create 5 conversations
        for i in range(5):
            await repo.create_conversation(title=f"Conv {i}")

        all_convs = await repo.list_conversations(limit=20)
        assert len(all_convs) == 5

        limited = await repo.list_conversations(limit=3)
        assert len(limited) == 3

    @pytest.mark.asyncio
    async def test_delete_conversation(self, db):
        conv = await repo.create_conversation(title="To delete")

        result = await repo.delete_conversation(conv.id)
        assert result is True

        fetched = await repo.get_conversation(conv.id)
        assert fetched is None

    @pytest.mark.asyncio
    async def test_delete_conversation_nonexistent(self, db):
        result = await repo.delete_conversation("nonexistent-id")
        assert result is False

    @pytest.mark.asyncio
    async def test_get_analytics_with_data(self, db):
        # Log several queries across providers
        await repo.log_query(
            mode="single", provider="openai", model="gpt-4",
            input_tokens=100, output_tokens=200,
            cost_usd=Decimal("0.005"), latency_ms=500,
        )
        await repo.log_query(
            mode="single", provider="groq", model="llama-3",
            input_tokens=50, output_tokens=100,
            cost_usd=Decimal("0"), latency_ms=200,
        )
        await repo.log_query(
            mode="council", provider="openai", model="gpt-4",
            input_tokens=80, output_tokens=150,
            cost_usd=Decimal("0.003"), latency_ms=400,
        )

        analytics = await repo.get_analytics()

        assert analytics["queries_today"] >= 3
        assert analytics["queries_this_month"] >= 3
        assert "openai" in analytics["cost_by_provider"]
        assert "openai" in analytics["queries_by_provider"]
        assert analytics["queries_by_provider"]["openai"] >= 2
        assert len(analytics["most_used_models"]) >= 1
        assert analytics["free_queries"] >= 1
        assert analytics["paid_queries"] >= 2
        assert "savings" in analytics

    @pytest.mark.asyncio
    async def test_create_conversation_add_messages_roundtrip(self, db):
        conv = await repo.create_conversation(provider="test", model="m1")

        _ = await repo.add_message(
            conversation_id=conv.id, role="user", content="Hello",
            provider="test", model="m1",
        )
        _ = await repo.add_message(
            conversation_id=conv.id, role="assistant", content="Hi there!",
            provider="test", model="m1",
            input_tokens=5, output_tokens=10, cost_usd=Decimal("0.001"),
        )

        messages = await repo.get_messages(conv.id)
        assert len(messages) == 2
        assert messages[0].role == "user"
        assert messages[1].role == "assistant"

        # Check conversation was updated
        updated = await repo.get_conversation(conv.id)
        assert updated.message_count == 2
        assert updated.total_cost_usd >= Decimal("0.001")


@pytest.mark.asyncio
async def test_conversation_messages_round_trip(db):
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
async def test_create_conversation_with_messages_is_one_transaction(db):
    """The localStorage import seeds a whole thread per create: turns,
    totals, pinned flag and auto-title all land together."""
    conv = await repo.create_conversation(
        title="", mode="council", pinned=True,
        messages=[
            {"role": "user", "content": "q", "input_tokens": 3},
            {"role": "assistant", "content": "a", "output_tokens": 5,
             "cost_usd": Decimal("0.002"), "provider": "groq", "model": "m"},
        ],
    )
    assert conv.pinned is True
    assert conv.message_count == 2
    assert conv.total_tokens == 8
    assert conv.total_cost_usd == Decimal("0.002")
    assert (conv.title, conv.provider, conv.model) == ("q", "groq", "m")
    msgs = await repo.get_messages(conv.id)
    assert [(m.sequence, m.role) for m in msgs] == [(1, "user"), (2, "assistant")]
    # A later append continues the numbering.
    assert (await repo.add_message(conv.id, role="user", content="more")).sequence == 3


@pytest.mark.asyncio
async def test_create_conversation_with_bad_message_leaves_nothing(db):
    with pytest.raises(TypeError):
        await repo.create_conversation(
            title="x", messages=[{"role": "user", "content": "ok"}, {"bogus": 1}],
        )
    assert await repo.list_conversations() == []


@pytest.mark.asyncio
async def test_concurrent_appends_keep_both_turns(db):
    conv = await repo.create_conversation(title="race")
    await asyncio.gather(
        repo.add_message(conv.id, role="user", content="a"),
        repo.add_message(conv.id, role="assistant", content="b"),
    )
    msgs = await repo.get_messages(conv.id)
    assert sorted(m.sequence for m in msgs) == [1, 2]
    assert (await repo.get_conversation(conv.id)).message_count == 2


@pytest.mark.asyncio
async def test_add_message_recomputes_sequence_after_collision(db, monkeypatch):
    """A writer that read a stale MAX(sequence) hits UNIQUE(conversation_id,
    sequence) and must recompute once rather than lose the turn."""
    conv = await repo.create_conversation(title="race")
    await repo.add_message(conv.id, role="user", content="first")
    real = repo._next_sequence
    seen: list[int] = []

    async def stale_once(session, conversation_id):
        seq = await real(session, conversation_id)
        seen.append(seq)
        return 1 if len(seen) == 1 else seq  # what a racer saw before "first" landed

    monkeypatch.setattr(repo, "_next_sequence", stale_once)
    msg = await repo.add_message(conv.id, role="assistant", content="second")
    assert msg.sequence == 2
    assert seen == [2, 2]
    assert [m.content for m in await repo.get_messages(conv.id)] == ["first", "second"]


@pytest.mark.asyncio
async def test_delete_conversation_drops_from_list(db):
    conv = await repo.create_conversation(provider="p", model="m", title="del")
    cid = conv.id
    assert await repo.delete_conversation(cid) is True
    assert await repo.get_conversation(cid) is None
    convs = await repo.list_conversations()
    assert all(c.id != cid for c in convs)


@pytest.mark.asyncio
async def test_get_spend_sums_across_providers(db):
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
