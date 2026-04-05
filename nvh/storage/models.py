"""SQLAlchemy async models for conversation persistence and cost tracking."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _new_id() -> str:
    return str(uuid.uuid4())


class Base(DeclarativeBase):
    pass


class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    title: Mapped[str] = mapped_column(String(255), default="")
    provider: Mapped[str] = mapped_column(String(64), default="")
    model: Mapped[str] = mapped_column(String(128), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)
    message_count: Mapped[int] = mapped_column(Integer, default=0)
    total_tokens: Mapped[int] = mapped_column(Integer, default=0)
    total_cost_usd: Mapped[Decimal] = mapped_column(Numeric(12, 6), default=Decimal("0"))

    messages: Mapped[list[ConversationMessage]] = relationship(
        back_populates="conversation",
        cascade="all, delete-orphan",
        order_by="ConversationMessage.sequence",
    )


class ConversationMessage(Base):
    __tablename__ = "conversation_messages"
    __table_args__ = (
        Index("ix_convo_msg_conv_id", "conversation_id"),
        Index("ix_convo_msg_conv_seq", "conversation_id", "sequence"),
        UniqueConstraint("conversation_id", "sequence", name="uq_conv_seq"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    conversation_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("conversations.id", ondelete="CASCADE")
    )
    sequence: Mapped[int] = mapped_column(Integer)
    role: Mapped[str] = mapped_column(String(16))  # system, user, assistant
    content: Mapped[str] = mapped_column(Text)
    provider: Mapped[str] = mapped_column(String(64), default="")
    model: Mapped[str] = mapped_column(String(128), default="")
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(12, 6), default=Decimal("0"))
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    conversation: Mapped[Conversation] = relationship(back_populates="messages")


class QueryLog(Base):
    __tablename__ = "query_logs"
    __table_args__ = (
        Index("ix_query_log_created", "created_at"),
        Index("ix_query_log_provider", "provider"),
        Index("ix_query_log_prov_created", "provider", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    conversation_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    mode: Mapped[str] = mapped_column(String(16))  # simple, council, compare
    provider: Mapped[str] = mapped_column(String(64))
    model: Mapped[str] = mapped_column(String(128))
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(12, 6), default=Decimal("0"))
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(16), default="success")  # success, error, fallback
    error_type: Mapped[str] = mapped_column(String(64), default="")
    cache_hit: Mapped[bool] = mapped_column(Boolean, default=False)
    fallback_from: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class ProviderKeyMeta(Base):
    __tablename__ = "provider_key_meta"

    provider: Mapped[str] = mapped_column(String(64), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    last_validated: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_used: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str] = mapped_column(String(255), default="")
    total_requests: Mapped[int] = mapped_column(Integer, default=0)
    is_valid: Mapped[bool] = mapped_column(Boolean, default=True)


class QualityBenchmarkLog(Base):
    """Stores individual evaluation results from quality benchmarks."""
    __tablename__ = "quality_benchmark_logs"
    __table_args__ = (
        Index("ix_qbench_run", "run_id"),
        Index("ix_qbench_created", "created_at"),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=_new_id,
    )
    run_id: Mapped[str] = mapped_column(String(36))
    prompt_id: Mapped[str] = mapped_column(String(64))
    task_type: Mapped[str] = mapped_column(String(32))
    mode: Mapped[str] = mapped_column(String(32))
    provider: Mapped[str] = mapped_column(String(64))
    model: Mapped[str] = mapped_column(String(128))
    overall_score: Mapped[float] = mapped_column(
        Numeric(4, 2), default=0.0,
    )
    cost_usd: Mapped[Decimal] = mapped_column(
        Numeric(12, 6), default=Decimal("0"),
    )
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    scores_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow,
    )


class RoutingOutcome(Base):
    """Links every routing decision to its measured outcome."""
    __tablename__ = "routing_outcomes"
    __table_args__ = (
        Index("ix_ro_provider_task", "provider", "task_type"),
        Index("ix_ro_model_task", "model", "task_type"),
        Index("ix_ro_created", "created_at"),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=_new_id,
    )
    query_log_id: Mapped[str | None] = mapped_column(
        String(36), nullable=True,
    )
    provider: Mapped[str] = mapped_column(String(64))
    model: Mapped[str] = mapped_column(String(128))
    task_type: Mapped[str] = mapped_column(String(32))
    classification_confidence: Mapped[float] = mapped_column(
        Numeric(4, 3), default=0.0,
    )
    routing_strategy: Mapped[str] = mapped_column(
        String(16), default="best",
    )
    composite_score: Mapped[float] = mapped_column(
        Numeric(6, 4), default=0.0,
    )
    capability_score_used: Mapped[float] = mapped_column(
        Numeric(4, 3), default=0.0,
    )
    quality_score: Mapped[float | None] = mapped_column(
        Numeric(4, 2), nullable=True,
    )
    user_feedback: Mapped[int | None] = mapped_column(
        Integer, nullable=True,
    )
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[Decimal] = mapped_column(
        Numeric(12, 6), default=Decimal("0"),
    )
    status: Mapped[str] = mapped_column(
        String(16), default="success",
    )
    was_fallback: Mapped[bool] = mapped_column(
        Boolean, default=False,
    )
    was_retry: Mapped[bool] = mapped_column(
        Boolean, default=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow,
    )


class LearnedScore(Base):
    """EMA capability scores learned from actual query outcomes."""
    __tablename__ = "learned_scores"
    __table_args__ = (
        UniqueConstraint(
            "provider", "model", "task_type",
            name="uq_learned_score",
        ),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=_new_id,
    )
    provider: Mapped[str] = mapped_column(String(64))
    model: Mapped[str] = mapped_column(String(128))
    task_type: Mapped[str] = mapped_column(String(32))
    learned_capability: Mapped[float] = mapped_column(
        Numeric(6, 4), default=0.0,
    )
    learned_latency_ms: Mapped[float] = mapped_column(
        Numeric(10, 2), default=0.0,
    )
    learned_reliability: Mapped[float] = mapped_column(
        Numeric(6, 4), default=1.0,
    )
    sample_count: Mapped[int] = mapped_column(
        Integer, default=0,
    )
    last_updated: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow,
    )
