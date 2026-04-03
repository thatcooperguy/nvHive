"""Async data access layer for conversations, query logs, and cost tracking."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from nvh.storage.models import (
    Base,
    Conversation,
    ConversationMessage,
    ProviderKeyMeta,
    QueryLog,
)

# Import auth models so they are registered with Base.metadata before
# create_all is called in init_db.
try:
    from nvh.auth import models as _auth_models  # noqa: F401
except ImportError:
    pass


_engine = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


async def init_db(db_path: Path | None = None) -> None:
    """Initialize the database engine and create tables."""
    global _engine, _session_factory
    if db_path is None:
        db_path = Path.home() / ".council" / "council.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    url = f"sqlite+aiosqlite:///{db_path}"
    _engine = create_async_engine(
        url,
        echo=False,
        connect_args={"timeout": 30},
        pool_pre_ping=True,
    )
    _session_factory = async_sessionmaker(_engine, expire_on_commit=False)
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # Enable WAL mode for concurrent read/write performance
        await conn.execute(text("PRAGMA journal_mode=WAL"))
        await conn.execute(text("PRAGMA busy_timeout=5000"))


async def close_db() -> None:
    """Dispose of the database engine and reset module state."""
    global _engine, _session_factory
    if _engine:
        await _engine.dispose()
    _engine = None
    _session_factory = None


def get_session() -> AsyncSession:
    """Get a new async session."""
    if _session_factory is None:
        raise RuntimeError("Database not initialized. Call init_db() first.")
    return _session_factory()


# ---------------------------------------------------------------------------
# Conversations
# ---------------------------------------------------------------------------

async def create_conversation(
    provider: str = "",
    model: str = "",
    title: str = "",
) -> Conversation:
    async with get_session() as session:
        conv = Conversation(provider=provider, model=model, title=title)
        session.add(conv)
        await session.commit()
        await session.refresh(conv)
        return conv


async def get_conversation(conversation_id: str) -> Conversation | None:
    async with get_session() as session:
        return await session.get(Conversation, conversation_id)


async def get_latest_conversation() -> Conversation | None:
    async with get_session() as session:
        result = await session.execute(
            select(Conversation).order_by(Conversation.updated_at.desc()).limit(1)
        )
        return result.scalar_one_or_none()


async def list_conversations(limit: int = 20) -> list[Conversation]:
    async with get_session() as session:
        result = await session.execute(
            select(Conversation).order_by(Conversation.updated_at.desc()).limit(limit)
        )
        return list(result.scalars().all())


async def add_message(
    conversation_id: str,
    role: str,
    content: str,
    provider: str = "",
    model: str = "",
    input_tokens: int = 0,
    output_tokens: int = 0,
    cost_usd: Decimal = Decimal("0"),
    latency_ms: int = 0,
) -> ConversationMessage:
    async with get_session() as session:
        conv = await session.get(Conversation, conversation_id)
        if conv is None:
            raise ValueError(f"Conversation {conversation_id} not found")

        seq = conv.message_count + 1
        msg = ConversationMessage(
            conversation_id=conversation_id,
            sequence=seq,
            role=role,
            content=content,
            provider=provider,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost_usd,
            latency_ms=latency_ms,
        )
        session.add(msg)

        conv.message_count = seq
        conv.total_tokens += input_tokens + output_tokens
        conv.total_cost_usd += cost_usd
        conv.updated_at = datetime.now(UTC)
        if not conv.provider:
            conv.provider = provider
        if not conv.model:
            conv.model = model
        if not conv.title and role == "user":
            conv.title = content[:100]

        await session.commit()
        await session.refresh(msg)
        return msg


async def get_messages(conversation_id: str) -> list[ConversationMessage]:
    async with get_session() as session:
        result = await session.execute(
            select(ConversationMessage)
            .where(ConversationMessage.conversation_id == conversation_id)
            .order_by(ConversationMessage.sequence)
        )
        return list(result.scalars().all())


async def delete_conversation(conversation_id: str) -> bool:
    """Delete a conversation and all its messages. Returns True on success."""
    async with get_session() as session:
        conv = await session.get(Conversation, conversation_id)
        if conv is None:
            return False
        await session.delete(conv)
        await session.commit()
        return True


async def search_conversations(
    query: str,
    limit: int = 20,
) -> list[tuple[Conversation, str]]:
    """Search conversations by message content.

    Returns a list of (conversation, matching_snippet) pairs ordered by the
    conversation's last-updated timestamp descending.  The snippet is a
    ~200-character excerpt from the first matching message, trimmed to whole
    words where possible.
    """
    async with get_session() as session:
        # Find messages whose content contains the query (case-insensitive via
        # SQLite's default LIKE behaviour which is case-insensitive for ASCII).
        like_pattern = f"%{query}%"
        msg_result = await session.execute(
            select(ConversationMessage)
            .where(ConversationMessage.content.ilike(like_pattern))
            .order_by(ConversationMessage.created_at.desc())
            .limit(limit * 5)  # over-fetch to allow dedup by conversation
        )
        messages = list(msg_result.scalars().all())

    # Deduplicate by conversation_id, keeping one representative message each,
    # then fetch the parent conversations.
    seen: dict[str, ConversationMessage] = {}
    for msg in messages:
        if msg.conversation_id not in seen:
            seen[msg.conversation_id] = msg
        if len(seen) >= limit:
            break

    if not seen:
        return []

    async with get_session() as session:
        conv_result = await session.execute(
            select(Conversation)
            .where(Conversation.id.in_(list(seen.keys())))
            .order_by(Conversation.updated_at.desc())
        )
        conversations = list(conv_result.scalars().all())

    pairs: list[tuple[Conversation, str]] = []
    for conv in conversations:
        msg = seen[conv.id]
        snippet = _extract_snippet(msg.content, query, context=120)
        pairs.append((conv, snippet))

    return pairs


def _extract_snippet(text: str, query: str, context: int = 120) -> str:
    """Return a short excerpt of *text* centred around the first occurrence of
    *query*, padded with up to *context* characters on each side."""
    lower = text.lower()
    idx = lower.find(query.lower())
    if idx == -1:
        # Query not found (shouldn't happen, but be safe)
        return text[:context * 2]
    start = max(0, idx - context)
    end = min(len(text), idx + len(query) + context)
    snippet = text[start:end]
    if start > 0:
        snippet = "…" + snippet
    if end < len(text):
        snippet = snippet + "…"
    return snippet


# ---------------------------------------------------------------------------
# Query Logs & Cost Tracking
# ---------------------------------------------------------------------------

async def log_query(
    mode: str,
    provider: str,
    model: str,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cost_usd: Decimal = Decimal("0"),
    latency_ms: int = 0,
    status: str = "success",
    error_type: str = "",
    cache_hit: bool = False,
    fallback_from: str = "",
    conversation_id: str | None = None,
) -> QueryLog:
    async with get_session() as session:
        log = QueryLog(
            conversation_id=conversation_id,
            mode=mode,
            provider=provider,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost_usd,
            latency_ms=latency_ms,
            status=status,
            error_type=error_type,
            cache_hit=cache_hit,
            fallback_from=fallback_from,
        )
        session.add(log)
        await session.commit()
        await session.refresh(log)
        return log


async def get_spend(period: str = "daily") -> Decimal:
    """Get total spend for the given period ('daily' or 'monthly')."""
    now = datetime.now(UTC)
    if period == "daily":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    elif period == "monthly":
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    else:
        start = now - timedelta(days=1)

    async with get_session() as session:
        result = await session.execute(
            select(func.coalesce(func.sum(QueryLog.cost_usd), 0)).where(
                QueryLog.created_at >= start
            )
        )
        value = result.scalar_one()
        return Decimal(str(value)) if value else Decimal("0")


async def get_spend_by_provider(period: str = "daily") -> dict[str, Decimal]:
    """Get spend per provider for the given period."""
    now = datetime.now(UTC)
    if period == "daily":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    else:
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    async with get_session() as session:
        result = await session.execute(
            select(
                QueryLog.provider,
                func.coalesce(func.sum(QueryLog.cost_usd), 0),
            )
            .where(QueryLog.created_at >= start)
            .group_by(QueryLog.provider)
        )
        return {row[0]: Decimal(str(row[1])) for row in result.all()}


async def get_query_count(period: str = "daily") -> int:
    now = datetime.now(UTC)
    if period == "daily":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    else:
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    async with get_session() as session:
        result = await session.execute(
            select(func.count(QueryLog.id)).where(QueryLog.created_at >= start)
        )
        return result.scalar_one() or 0


# ---------------------------------------------------------------------------
# Savings Tracker
# ---------------------------------------------------------------------------

# GPT-4o pricing used as the baseline "what it would have cost on cloud"
_GPT4O_INPUT_COST_PER_TOKEN = Decimal("2.50") / Decimal("1_000_000")   # $2.50 / 1M input tokens
_GPT4O_OUTPUT_COST_PER_TOKEN = Decimal("10.00") / Decimal("1_000_000")  # $10.00 / 1M output tokens


async def get_savings(period: str = "monthly") -> dict:
    """Calculate money saved by using local models vs cloud.

    For each query that used a local model (cost = $0), estimate what it
    would have cost on the default cloud model (GPT-4o pricing).

    Returns:
        {
            "local_queries": int,
            "cloud_queries": int,
            "total_queries": int,
            "cloud_spend": Decimal,
            "estimated_cloud_cost": Decimal,  # what local queries would have cost
            "total_savings": Decimal,
            "savings_pct": float,
        }
    """
    now = datetime.now(UTC)
    if period == "daily":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    else:
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    async with get_session() as session:
        # Fetch all query logs in the period
        result = await session.execute(
            select(
                QueryLog.cost_usd,
                QueryLog.input_tokens,
                QueryLog.output_tokens,
            ).where(QueryLog.created_at >= start)
        )
        rows = result.all()

    local_queries = 0
    cloud_queries = 0
    cloud_spend = Decimal("0")
    estimated_cloud_cost = Decimal("0")  # what the local queries would have cost

    for cost_usd, input_tokens, output_tokens in rows:
        cost = Decimal(str(cost_usd)) if cost_usd else Decimal("0")
        if cost == Decimal("0"):
            # Local model — estimate what GPT-4o would have charged
            local_queries += 1
            estimated_cloud_cost += (
                Decimal(str(input_tokens)) * _GPT4O_INPUT_COST_PER_TOKEN
                + Decimal(str(output_tokens)) * _GPT4O_OUTPUT_COST_PER_TOKEN
            )
        else:
            cloud_queries += 1
            cloud_spend += cost

    total_queries = local_queries + cloud_queries
    total_savings = estimated_cloud_cost  # savings = what we didn't spend on local queries
    total_hypothetical = cloud_spend + estimated_cloud_cost
    savings_pct = (
        float(total_savings / total_hypothetical * 100)
        if total_hypothetical > 0
        else 0.0
    )

    return {
        "local_queries": local_queries,
        "cloud_queries": cloud_queries,
        "total_queries": total_queries,
        "cloud_spend": cloud_spend,
        "estimated_cloud_cost": estimated_cloud_cost,
        "total_savings": total_savings,
        "savings_pct": savings_pct,
    }


# ---------------------------------------------------------------------------
# Analytics
# ---------------------------------------------------------------------------


async def get_analytics() -> dict:
    """Return comprehensive analytics data for the dashboard.

    Includes query counts by period, cost and latency breakdowns per provider,
    most-used models, free-vs-paid ratio, and savings data.
    """
    now = datetime.now(UTC)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_start = now - timedelta(days=7)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    async with get_session() as session:
        # --- Query counts by period ---
        today_count = (await session.execute(
            select(func.count(QueryLog.id)).where(QueryLog.created_at >= today_start)
        )).scalar_one() or 0

        week_count = (await session.execute(
            select(func.count(QueryLog.id)).where(QueryLog.created_at >= week_start)
        )).scalar_one() or 0

        month_count = (await session.execute(
            select(func.count(QueryLog.id)).where(QueryLog.created_at >= month_start)
        )).scalar_one() or 0

        # --- Per-provider stats (this month) ---
        provider_rows = (await session.execute(
            select(
                QueryLog.provider,
                func.count(QueryLog.id),
                func.coalesce(func.sum(QueryLog.cost_usd), 0),
                func.coalesce(func.avg(QueryLog.latency_ms), 0),
            )
            .where(QueryLog.created_at >= month_start)
            .group_by(QueryLog.provider)
        )).all()

        cost_by_provider: dict[str, str] = {}
        queries_by_provider: dict[str, int] = {}
        latency_by_provider: dict[str, float] = {}

        for prov, cnt, cost, latency in provider_rows:
            cost_by_provider[prov] = str(Decimal(str(cost)))
            queries_by_provider[prov] = cnt
            latency_by_provider[prov] = round(float(latency), 1)

        # --- Most used models (this month, top 10) ---
        model_rows = (await session.execute(
            select(
                QueryLog.model,
                QueryLog.provider,
                func.count(QueryLog.id),
            )
            .where(QueryLog.created_at >= month_start)
            .group_by(QueryLog.model, QueryLog.provider)
            .order_by(func.count(QueryLog.id).desc())
            .limit(10)
        )).all()

        most_used_models = [
            {"model": m, "provider": p, "count": c}
            for m, p, c in model_rows
        ]

        # --- Free vs paid ratio (this month) ---
        free_count = (await session.execute(
            select(func.count(QueryLog.id)).where(
                QueryLog.created_at >= month_start,
                QueryLog.cost_usd == Decimal("0"),
            )
        )).scalar_one() or 0

        paid_count = (await session.execute(
            select(func.count(QueryLog.id)).where(
                QueryLog.created_at >= month_start,
                QueryLog.cost_usd > Decimal("0"),
            )
        )).scalar_one() or 0

    # --- Savings ---
    savings = await get_savings("monthly")

    return {
        "queries_today": today_count,
        "queries_this_week": week_count,
        "queries_this_month": month_count,
        "cost_by_provider": cost_by_provider,
        "queries_by_provider": queries_by_provider,
        "latency_by_provider": latency_by_provider,
        "most_used_models": most_used_models,
        "free_queries": free_count,
        "paid_queries": paid_count,
        "savings": {
            "local_queries": savings["local_queries"],
            "cloud_queries": savings["cloud_queries"],
            "estimated_cloud_cost": str(savings["estimated_cloud_cost"]),
            "total_savings": str(savings["total_savings"]),
            "savings_pct": savings["savings_pct"],
        },
    }


# ---------------------------------------------------------------------------
# Provider Key Metadata
# ---------------------------------------------------------------------------

async def update_provider_meta(
    provider: str,
    is_valid: bool = True,
    last_error: str = "",
) -> None:
    async with get_session() as session:
        meta = await session.get(ProviderKeyMeta, provider)
        now = datetime.now(UTC)
        if meta is None:
            meta = ProviderKeyMeta(
                provider=provider,
                is_valid=is_valid,
                last_validated=now,
                last_used=now,
                last_error=last_error,
                total_requests=1,
            )
            session.add(meta)
        else:
            meta.is_valid = is_valid
            meta.last_validated = now
            meta.last_used = now
            meta.last_error = last_error
            meta.total_requests += 1
        await session.commit()
