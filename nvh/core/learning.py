"""Adaptive learning engine — makes routing smarter over time.

Tracks query outcomes (quality, latency, reliability) per provider/model/task
and updates capability scores using exponential moving averages. The router
blends these learned scores with static YAML scores, gradually shifting to
fully data-driven routing as sample counts grow.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select

from nvh.storage.models import LearnedScore, RoutingOutcome
from nvh.storage.repository import get_session

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ALPHA = 0.15  # EMA learning rate: ~50% influence from last 4 observations
MIN_SAMPLES_TO_BLEND = 5  # Need 5+ obs before learned score influences routing
FULL_LEARNED_SAMPLES = 20  # At 20+ samples, fully data-driven
CACHE_REFRESH_INTERVAL = 30  # Seconds between in-memory cache refreshes


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class LearnedScoreEntry:
    """In-memory representation of a learned score."""

    provider: str
    model: str
    task_type: str
    learned_capability: float  # 0.0-1.0
    learned_latency_ms: float
    learned_reliability: float  # 0.0-1.0
    sample_count: int = 0


# ---------------------------------------------------------------------------
# Pure functions (no DB, easy to test)
# ---------------------------------------------------------------------------

def ema_update(current: float, observation: float, alpha: float = ALPHA) -> float:
    """Exponential moving average update.

    Returns the new EMA value blending *current* with *observation*.
    """
    return current * (1.0 - alpha) + observation * alpha


def blend_score(static: float, learned: float, sample_count: int) -> float:
    """Blend a static YAML score with a learned score based on sample count.

    Below ``MIN_SAMPLES_TO_BLEND`` samples the static score is returned
    unchanged.  Between that and ``FULL_LEARNED_SAMPLES`` a linear
    interpolation is used.  At ``FULL_LEARNED_SAMPLES`` or above the
    learned score is returned directly.
    """
    if sample_count < MIN_SAMPLES_TO_BLEND:
        return static
    if sample_count >= FULL_LEARNED_SAMPLES:
        return learned
    # Linear interpolation between MIN and FULL thresholds
    t = (sample_count - MIN_SAMPLES_TO_BLEND) / (
        FULL_LEARNED_SAMPLES - MIN_SAMPLES_TO_BLEND
    )
    return static * (1.0 - t) + learned * t


def quality_to_capability(quality_score: float) -> float:
    """Convert a 1-10 quality score to a 0.0-1.0 capability value."""
    clamped = max(1.0, min(10.0, quality_score))
    return (clamped - 1.0) / 9.0


def implicit_quality(
    status: str,
    was_fallback: bool,
    user_feedback: int | None,
) -> float:
    """Derive an implicit quality signal when no explicit score is available.

    Returns a value in 0.0-1.0.
    """
    if user_feedback == 1:
        return 0.9
    if user_feedback == -1:
        return 0.3
    if status == "error" or was_fallback:
        return 0.1
    # Success with no explicit evaluation
    return 0.7


# ---------------------------------------------------------------------------
# Learning Engine
# ---------------------------------------------------------------------------

class LearningEngine:
    """Manages the learning loop: capture outcomes, update scores, serve to router."""

    def __init__(self) -> None:
        self._cache: dict[tuple[str, str, str], LearnedScoreEntry] = {}
        self._last_refresh: float = 0.0

    # -- cache management ---------------------------------------------------

    async def load_scores(self) -> None:
        """Load all LearnedScore rows from DB into memory cache."""
        async with get_session() as session:
            result = await session.execute(select(LearnedScore))
            rows = result.scalars().all()

        new_cache: dict[tuple[str, str, str], LearnedScoreEntry] = {}
        for row in rows:
            key = (row.provider, row.model, row.task_type)
            new_cache[key] = LearnedScoreEntry(
                provider=row.provider,
                model=row.model,
                task_type=row.task_type,
                learned_capability=float(row.learned_capability),
                learned_latency_ms=float(row.learned_latency_ms),
                learned_reliability=float(row.learned_reliability),
                sample_count=row.sample_count,
            )
        self._cache = new_cache
        self._last_refresh = time.monotonic()
        log.debug("Loaded %d learned scores into cache", len(new_cache))

    def get_score_map(self) -> dict[tuple[str, str, str], LearnedScoreEntry]:
        """Return the current in-memory score map for the router."""
        return self._cache

    def get_blended_capability(
        self,
        provider: str,
        model: str,
        task_type: str,
        static_score: float,
    ) -> float:
        """Get the blended (static + learned) capability score."""
        key = (provider, model, task_type)
        entry = self._cache.get(key)
        if entry is None:
            return static_score
        return blend_score(static_score, entry.learned_capability, entry.sample_count)

    async def _maybe_refresh(self) -> None:
        """Reload scores from DB if the cache is stale."""
        if time.monotonic() - self._last_refresh > CACHE_REFRESH_INTERVAL:
            await self.load_scores()

    # -- outcome recording --------------------------------------------------

    async def record_outcome(
        self,
        provider: str,
        model: str,
        task_type: str,
        classification_confidence: float,
        routing_strategy: str,
        composite_score: float,
        capability_score_used: float,
        quality_score: float | None,
        user_feedback: int | None,
        latency_ms: int,
        cost_usd: Decimal,
        status: str,
        was_fallback: bool,
        was_retry: bool,
        query_log_id: str | None = None,
    ) -> None:
        """Record an outcome and update learned scores via EMA."""
        # 1. Determine effective quality signal
        if quality_score is not None:
            effective_quality = quality_to_capability(quality_score)
        else:
            effective_quality = implicit_quality(status, was_fallback, user_feedback)

        reliability_obs = 0.0 if status == "error" else 1.0

        # 2. Insert RoutingOutcome row
        async with get_session() as session:
            outcome = RoutingOutcome(
                query_log_id=query_log_id,
                provider=provider,
                model=model,
                task_type=task_type,
                classification_confidence=classification_confidence,
                routing_strategy=routing_strategy,
                composite_score=composite_score,
                capability_score_used=capability_score_used,
                quality_score=quality_score,
                user_feedback=user_feedback,
                latency_ms=latency_ms,
                cost_usd=cost_usd,
                status=status,
                was_fallback=was_fallback,
                was_retry=was_retry,
            )
            session.add(outcome)
            await session.commit()

        # 3. Load or create LearnedScore
        key = (provider, model, task_type)
        async with get_session() as session:
            result = await session.execute(
                select(LearnedScore).where(
                    LearnedScore.provider == provider,
                    LearnedScore.model == model,
                    LearnedScore.task_type == task_type,
                )
            )
            ls_row = result.scalar_one_or_none()

            if ls_row is None:
                # First observation — seed directly
                ls_row = LearnedScore(
                    provider=provider,
                    model=model,
                    task_type=task_type,
                    learned_capability=effective_quality,
                    learned_latency_ms=float(latency_ms),
                    learned_reliability=reliability_obs,
                    sample_count=1,
                )
                session.add(ls_row)
            else:
                # 4. EMA update all three dimensions
                ls_row.learned_capability = ema_update(
                    float(ls_row.learned_capability), effective_quality,
                )
                ls_row.learned_latency_ms = ema_update(
                    float(ls_row.learned_latency_ms), float(latency_ms),
                )
                ls_row.learned_reliability = ema_update(
                    float(ls_row.learned_reliability), reliability_obs,
                )
                ls_row.sample_count += 1

            # 5. Persist
            await session.commit()
            await session.refresh(ls_row)

        # 6. Update in-memory cache
        self._cache[key] = LearnedScoreEntry(
            provider=ls_row.provider,
            model=ls_row.model,
            task_type=ls_row.task_type,
            learned_capability=float(ls_row.learned_capability),
            learned_latency_ms=float(ls_row.learned_latency_ms),
            learned_reliability=float(ls_row.learned_reliability),
            sample_count=ls_row.sample_count,
        )
        log.debug(
            "Updated learned score %s/%s/%s n=%d cap=%.3f",
            provider, model, task_type, ls_row.sample_count,
            float(ls_row.learned_capability),
        )

    async def get_stats(
        self,
        provider: str | None = None,
        task_type: str | None = None,
    ) -> list[dict]:
        """Get learning stats for the routing-stats CLI command."""
        await self._maybe_refresh()

        results: list[dict] = []
        for key, entry in self._cache.items():
            if provider and entry.provider != provider:
                continue
            if task_type and entry.task_type != task_type:
                continue
            results.append({
                "provider": entry.provider,
                "model": entry.model,
                "task_type": entry.task_type,
                "learned_capability": round(entry.learned_capability, 4),
                "learned_latency_ms": round(entry.learned_latency_ms, 1),
                "learned_reliability": round(entry.learned_reliability, 4),
                "sample_count": entry.sample_count,
            })

        # Sort by capability descending for readability
        results.sort(key=lambda r: r["learned_capability"], reverse=True)
        return results
