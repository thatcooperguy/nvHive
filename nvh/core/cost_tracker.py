"""Track and report costs across cloud and local inference."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

# Rough per-query cost estimates (USD) used when actual billing is unavailable.
_CLOUD_COST_ESTIMATE = Decimal("0.003")

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class CostReport:
    """Aggregated cost report for a given period."""

    period: str = "today"
    total_queries: int = 0
    cloud_queries: int = 0
    local_queries: int = 0
    cloud_cost_usd: Decimal = Decimal(0)
    local_cost_usd: Decimal = Decimal(0)
    savings_usd: Decimal = Decimal(0)
    top_providers: list[tuple[str, int, Decimal]] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Report generation
# ---------------------------------------------------------------------------


async def get_cost_report(period: str = "today") -> CostReport:
    """Build a :class:`CostReport` for *period*.

    Queries the local repository for logged queries, calculates costs and
    estimates savings from local inference.
    """
    # Late import to avoid circular deps and keep module lightweight when
    # only the dataclass is needed.
    try:
        from nvh.core.engine import _load_db  # type: ignore[import-untyped]

        db = _load_db()
        rows = db.execute(
            "SELECT provider, is_local FROM query_log WHERE period = ?",
            (period,),
        ).fetchall()
    except Exception:
        rows = []

    report = CostReport(period=period)
    provider_counts: dict[str, list[int]] = {}

    for row in rows:
        provider = row[0] if isinstance(row, (tuple, list)) else row["provider"]
        is_local = row[1] if isinstance(row, (tuple, list)) else row["is_local"]
        report.total_queries += 1
        bucket = provider_counts.setdefault(provider, [0, 0])
        bucket[0] += 1
        if is_local:
            report.local_queries += 1
        else:
            report.cloud_queries += 1
            cost = _CLOUD_COST_ESTIMATE
            report.cloud_cost_usd += cost
            bucket[1] = int(Decimal(bucket[1]) + cost)

    # Estimated savings: what local queries *would* have cost on cloud.
    report.savings_usd = _CLOUD_COST_ESTIMATE * report.local_queries

    report.top_providers = [
        (name, counts[0], Decimal(counts[1]))
        for name, counts in sorted(
            provider_counts.items(), key=lambda kv: kv[1][0], reverse=True
        )
    ]
    return report


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------


def format_cost_report(report: CostReport) -> str:
    """Return a human-readable cost summary."""
    lines = [
        f"Cost Report  ({report.period})",
        "=" * 40,
        f"Total queries : {report.total_queries}",
        f"  Cloud       : {report.cloud_queries}  (${report.cloud_cost_usd:.4f})",
        f"  Local       : {report.local_queries}  (${report.local_cost_usd:.4f})",
        f"Savings       : ${report.savings_usd:.4f}",
    ]
    if report.top_providers:
        lines.append("")
        lines.append("Top providers:")
        for name, count, cost in report.top_providers:
            lines.append(f"  {name:20s}  {count:>5d} queries  ${cost:.4f}")
    if report.savings_usd > 0:
        lines.append("")
        lines.append(
            "Tip: local inference saved you "
            f"${report.savings_usd:.2f} this period."
        )
    return "\n".join(lines)
