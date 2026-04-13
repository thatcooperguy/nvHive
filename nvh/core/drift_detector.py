"""Model drift detection.

Monitors LLM quality over time, alerts when a provider degrades.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

DRIFT_THRESHOLD = 0.20  # 20% drop triggers alert
RECENT_WINDOW = 10  # last N queries for comparison


@dataclass
class DriftAlert:
    """Alert raised when a provider's quality drops significantly."""

    provider: str
    task_type: str
    previous_score: float
    current_score: float
    drop_pct: float
    timestamp: float = field(default_factory=time.time)
    recommendation: str = ""


def check_for_drift(engine: object) -> list[DriftAlert]:
    """Compare recent performance to historical average.

    Reads ``engine.score_history``, a dict mapping
    ``(provider, task_type)`` to a list of float scores.
    """
    history: dict[tuple[str, str], list[float]] = getattr(
        engine, "score_history", {},
    )
    alerts: list[DriftAlert] = []
    for (provider, task_type), scores in history.items():
        if len(scores) < RECENT_WINDOW + 1:
            continue
        historical = scores[:-RECENT_WINDOW]
        recent = scores[-RECENT_WINDOW:]
        hist_avg = sum(historical) / len(historical)
        recent_avg = sum(recent) / len(recent)
        if hist_avg == 0:
            continue
        drop = (hist_avg - recent_avg) / hist_avg
        if drop > DRIFT_THRESHOLD:
            alerts.append(DriftAlert(
                provider=provider,
                task_type=task_type,
                previous_score=round(hist_avg, 4),
                current_score=round(recent_avg, 4),
                drop_pct=round(drop * 100, 1),
                recommendation=(
                    f"Consider routing {task_type} tasks away from {provider}"
                ),
            ))
    return alerts


def format_drift_alerts(alerts: list[DriftAlert]) -> str:
    """Rich-formatted output showing degraded providers."""
    if not alerts:
        return "No drift detected."
    lines = ["=== Drift Alerts ==="]
    for a in alerts:
        lines.append(
            f"  [{a.provider}] {a.task_type}: "
            f"{a.previous_score:.2f} -> {a.current_score:.2f} "
            f"({a.drop_pct:.1f}% drop)",
        )
        lines.append(f"    -> {a.recommendation}")
    return "\n".join(lines)


def auto_reroute(engine: object, alerts: list[DriftAlert]) -> list[str]:
    """Temporarily deprioritize degraded providers.

    Mutates ``engine.provider_weights`` if present.
    """
    weights: dict[str, float] = getattr(engine, "provider_weights", {})
    actions: list[str] = []
    for alert in alerts:
        key = alert.provider
        if key in weights:
            old = weights[key]
            weights[key] = old * 0.5
            action = f"Halved weight for {key}: {old:.2f} -> {weights[key]:.2f}"
        else:
            action = f"No weight entry for {key}; skipped reroute"
        actions.append(action)
        log.info(action)
    return actions
