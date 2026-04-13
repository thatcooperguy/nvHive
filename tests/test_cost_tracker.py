"""Tests for nvh.core.cost_tracker."""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from nvh.core.cost_tracker import (
    _CLOUD_COST_ESTIMATE,
    CostReport,
    format_cost_report,
    get_cost_report,
)


class TestCostReport:
    def test_default_values(self):
        r = CostReport()
        assert r.period == "today"
        assert r.total_queries == 0
        assert r.cloud_queries == 0
        assert r.local_queries == 0
        assert r.cloud_cost_usd == Decimal(0)
        assert r.local_cost_usd == Decimal(0)
        assert r.savings_usd == Decimal(0)
        assert r.top_providers == []

    def test_custom_period(self):
        r = CostReport(period="last_week")
        assert r.period == "last_week"


def _mock_db_with_rows(rows):
    """Create a mock _load_db that returns the given rows."""
    mock_db = MagicMock()
    mock_db.execute.return_value.fetchall.return_value = rows
    return mock_db


class TestGetCostReport:
    @pytest.mark.asyncio
    async def test_returns_empty_report_when_db_fails(self):
        # The import of _load_db happens inside the function; patch at source
        mock_mod = MagicMock(_load_db=MagicMock(side_effect=Exception("no db")))
        with patch.dict("sys.modules", {"nvh.core.engine": mock_mod}):
            report = await get_cost_report("today")
        assert report.total_queries == 0
        assert report.period == "today"

    @pytest.mark.asyncio
    async def test_counts_cloud_and_local_queries(self):
        mock_db = _mock_db_with_rows([
            ("groq", False),
            ("groq", False),
            ("ollama", True),
        ])
        mock_engine = MagicMock(_load_db=MagicMock(return_value=mock_db))
        with patch.dict("sys.modules", {"nvh.core.engine": mock_engine}):
            report = await get_cost_report("today")
        assert report.total_queries == 3
        assert report.cloud_queries == 2
        assert report.local_queries == 1

    @pytest.mark.asyncio
    async def test_cloud_cost_calculated(self):
        mock_db = _mock_db_with_rows([("openai", False)])
        mock_engine = MagicMock(_load_db=MagicMock(return_value=mock_db))
        with patch.dict("sys.modules", {"nvh.core.engine": mock_engine}):
            report = await get_cost_report("today")
        assert report.cloud_cost_usd == _CLOUD_COST_ESTIMATE

    @pytest.mark.asyncio
    async def test_savings_calculated_for_local(self):
        mock_db = _mock_db_with_rows([
            ("ollama", True),
            ("ollama", True),
            ("ollama", True),
        ])
        mock_engine = MagicMock(_load_db=MagicMock(return_value=mock_db))
        with patch.dict("sys.modules", {"nvh.core.engine": mock_engine}):
            report = await get_cost_report("today")
        assert report.savings_usd == _CLOUD_COST_ESTIMATE * 3

    @pytest.mark.asyncio
    async def test_top_providers_sorted_by_count(self):
        mock_db = _mock_db_with_rows([
            ("groq", False),
            ("groq", False),
            ("groq", False),
            ("openai", False),
        ])
        mock_engine = MagicMock(_load_db=MagicMock(return_value=mock_db))
        with patch.dict("sys.modules", {"nvh.core.engine": mock_engine}):
            report = await get_cost_report("today")
        assert len(report.top_providers) == 2
        assert report.top_providers[0][0] == "groq"
        assert report.top_providers[0][1] == 3

    @pytest.mark.asyncio
    async def test_custom_period_passed_through(self):
        mock_db = _mock_db_with_rows([])
        mock_engine = MagicMock(_load_db=MagicMock(return_value=mock_db))
        with patch.dict("sys.modules", {"nvh.core.engine": mock_engine}):
            report = await get_cost_report("last_month")
        assert report.period == "last_month"


class TestFormatCostReport:
    def test_format_empty_report(self):
        report = CostReport()
        text = format_cost_report(report)
        assert "Cost Report" in text
        assert "today" in text
        assert "Total queries : 0" in text

    def test_format_with_savings_shows_tip(self):
        report = CostReport(
            total_queries=5,
            local_queries=3,
            cloud_queries=2,
            savings_usd=Decimal("0.009"),
        )
        text = format_cost_report(report)
        assert "Tip:" in text
        assert "local inference saved" in text

    def test_format_with_top_providers(self):
        report = CostReport(
            total_queries=10,
            top_providers=[
                ("groq", 6, Decimal("0.018")),
                ("openai", 4, Decimal("0.012")),
            ],
        )
        text = format_cost_report(report)
        assert "Top providers" in text
        assert "groq" in text
        assert "openai" in text

    def test_format_no_savings_no_tip(self):
        report = CostReport(total_queries=2, cloud_queries=2, savings_usd=Decimal(0))
        text = format_cost_report(report)
        assert "Tip:" not in text
