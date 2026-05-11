"""Tests for persistent setup install jobs."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest

from nvh.integrations import jobs


def test_job_store_persists_events(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("NVH_HOME", str(tmp_path / "nvh"))
    jobs._TASKS.clear()

    job = jobs.create_job(
        kind="studio-model-install",
        title="Download local models",
        request={"model_ids": ["llama3.1:8b"]},
    )

    event = jobs.append_event(
        job["id"],
        {"event": "plan", "status": "running", "message": "Preparing download"},
    )
    assert event["sequence"] == 1

    stored = jobs.load_job(job["id"], reconcile=False)
    assert stored["status"] == "running"
    assert stored["progress"] >= 5
    assert stored["storage_home"] == str(tmp_path / "nvh")

    events = jobs.read_events(job["id"])
    assert len(events) == 1
    assert events[0]["message"] == "Preparing download"
    listed = jobs.list_jobs(limit=1)
    assert listed[0]["recent_events"][0]["message"] == "Preparing download"

    jobs.append_event(
        job["id"],
        {"event": "complete", "status": "complete", "message": "Done"},
    )
    complete = jobs.load_job(job["id"], reconcile=False)
    assert complete["status"] == "complete"
    assert complete["progress"] == 100


@pytest.mark.asyncio
async def test_start_job_consumes_async_source(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("NVH_HOME", str(tmp_path / "nvh"))
    jobs._TASKS.clear()

    async def source() -> AsyncIterator[dict[str, object]]:
        yield {"event": "plan", "status": "running", "message": "Planning"}
        await asyncio.sleep(0)
        yield {"event": "complete", "status": "complete", "message": "Finished"}

    job = jobs.start_job(
        kind="comfyui-install",
        title="Install ComfyUI",
        request={"torch_profile": "skip"},
        source_factory=source,
    )

    for _ in range(20):
        loaded = jobs.load_job(job["id"])
        if loaded["status"] == "complete":
            break
        await asyncio.sleep(0.01)

    loaded = jobs.load_job(job["id"])
    assert loaded["status"] == "complete"
    assert loaded["event_count"] == 2
    assert [event["event"] for event in jobs.read_events(job["id"])] == ["plan", "complete"]


def test_job_progress_accepts_numeric_payload(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("NVH_HOME", str(tmp_path / "nvh"))
    jobs._TASKS.clear()

    job = jobs.create_job(kind="studio-pack-install", title="Build Creator Studio")
    jobs.append_event(
        job["id"],
        {
            "event": "download",
            "status": "running",
            "message": "Downloaded 164.0 MB",
            "progress": 38,
        },
    )

    loaded = jobs.load_job(job["id"])
    assert loaded["progress"] == 38
    assert loaded["recent_events"][-1]["message"] == "Downloaded 164.0 MB"


def test_step_complete_does_not_finish_parent_job(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("NVH_HOME", str(tmp_path / "nvh"))
    jobs._TASKS.clear()

    job = jobs.create_job(kind="comfyui-install", title="Install ComfyUI")
    jobs.append_event(
        job["id"],
        {"event": "step", "status": "complete", "message": "Clone ComfyUI complete"},
    )

    loaded = jobs.load_job(job["id"], reconcile=False)
    assert loaded["status"] == "running"
    assert loaded["progress"] < 100
