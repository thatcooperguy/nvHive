"""Tests for nvh.core.cost_tracker.

The ``nvh.core.snapshot`` cases that used to live here went away with the
module in 0.41.1 — ``nvh snapshot`` now drives
``nvh.integrations.workspace.snapshot`` (see tests/test_cli_snapshot.py).
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from nvh.core.cost_tracker import CostReport, format_cost_report, get_cost_report


def test_cost_report_construction():
    report = CostReport(
        period="week",
        total_queries=100,
        cloud_queries=40,
        local_queries=60,
        cloud_cost_usd=Decimal("0.12"),
        savings_usd=Decimal("0.18"),
        top_providers=[("openai", 40, Decimal("0.12"))],
    )
    assert report.period == "week"
    assert report.total_queries == 100
    assert report.local_cost_usd == Decimal(0)


def test_format_cost_report_non_empty():
    report = CostReport(
        period="today",
        total_queries=10,
        cloud_queries=3,
        local_queries=7,
        cloud_cost_usd=Decimal("0.009"),
        savings_usd=Decimal("0.021"),
        top_providers=[("ollama", 7, Decimal(0)), ("openai", 3, Decimal("0.009"))],
    )
    text = format_cost_report(report)
    assert "Cost Report" in text
    assert "Savings" in text
    assert "ollama" in text
    assert "openai" in text
    assert "Tip:" in text


@pytest.mark.asyncio
async def test_get_cost_report_returns_empty_on_missing_db():
    """get_cost_report should return an empty report when no DB is available."""
    report = await get_cost_report("month")
    assert report.period == "month"
    assert report.total_queries == 0
    assert report.cloud_cost_usd == Decimal(0)
