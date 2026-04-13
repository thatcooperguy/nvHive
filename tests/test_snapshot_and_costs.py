"""Tests for nvh.core.snapshot and nvh.core.cost_tracker."""

from __future__ import annotations

import tarfile
from decimal import Decimal
from pathlib import Path

import pytest

from nvh.core.cost_tracker import CostReport, format_cost_report, get_cost_report
from nvh.core.snapshot import SnapshotInfo, list_snapshot_contents, save_snapshot

# ---------------------------------------------------------------------------
# snapshot tests
# ---------------------------------------------------------------------------


def test_snapshot_info_construction():
    info = SnapshotInfo(
        files=["a.txt", "b.txt"],
        total_size_bytes=1024,
        timestamp=1_700_000_000.0,
    )
    assert info.files == ["a.txt", "b.txt"]
    assert info.total_size_bytes == 1024
    assert info.error is None


def test_snapshot_info_defaults():
    info = SnapshotInfo()
    assert info.files == []
    assert info.total_size_bytes == 0
    assert info.timestamp == 0.0
    assert info.error is None


def test_list_snapshot_contents_empty_tarball(tmp_path: Path):
    tarball = tmp_path / "empty.tar.gz"
    with tarfile.open(tarball, "w:gz"):
        pass  # empty archive
    assert list_snapshot_contents(tarball) == []


def test_list_snapshot_contents_with_files(tmp_path: Path):
    tarball = tmp_path / "snap.tar.gz"
    dummy = tmp_path / "hello.txt"
    dummy.write_text("hi")
    with tarfile.open(tarball, "w:gz") as tar:
        tar.add(str(dummy), arcname="hello.txt")
    assert list_snapshot_contents(tarball) == ["hello.txt"]


def test_save_snapshot_no_home_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """save_snapshot should succeed even when none of the expected files exist."""
    monkeypatch.setattr("nvh.core.snapshot._home", lambda: tmp_path / "fakehome")
    out = tmp_path / "out.tar.gz"
    info = save_snapshot(out)
    assert info.files == []
    assert info.total_size_bytes == 0
    assert info.error is None


# ---------------------------------------------------------------------------
# cost_tracker tests
# ---------------------------------------------------------------------------


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
