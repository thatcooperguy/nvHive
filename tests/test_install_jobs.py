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


def test_list_jobs_filters_by_schedule_id_and_excludes_a_kind(tmp_path, monkeypatch) -> None:
    """0.46: a schedule's run log is ``schedule_id``; the Setup page hides runs with ``exclude_kind``."""
    monkeypatch.setenv("NVH_HOME", str(tmp_path / "nvh"))
    jobs._TASKS.clear()

    install = jobs.create_job(kind="comfyui-install", title="Install ComfyUI")
    run_a1 = jobs.create_job(kind="schedule-run", title="a", request={"schedule_id": "a"})
    run_b = jobs.create_job(kind="schedule-run", title="b", request={"schedule_id": "b"})
    run_a2 = jobs.create_job(kind="schedule-run", title="a", request={"schedule_id": "a"})
    for job in (install, run_a1, run_b, run_a2):
        jobs.append_event(job["id"], {"event": "complete", "status": "complete", "message": "done"})

    ids = lambda rows: {j["id"] for j in rows}  # noqa: E731
    assert ids(jobs.list_jobs(schedule_id="a", limit=10)) == {run_a1["id"], run_a2["id"]}
    assert ids(jobs.list_jobs(kind="schedule-run", schedule_id="b", limit=10)) == {run_b["id"]}
    assert jobs.list_jobs(schedule_id="nobody", limit=10) == []
    assert ids(jobs.list_jobs(exclude_kind="schedule-run", limit=10)) == {install["id"]}
    # The filters run before the limit: the one install job is found behind three runs.
    assert ids(jobs.list_jobs(exclude_kind="schedule-run", limit=1)) == {install["id"]}
    assert ids(jobs.list_jobs(schedule_id="a", limit=1)) <= {run_a1["id"], run_a2["id"]}
    assert len(jobs.list_jobs(limit=10)) == 4, "no filter, no change"


def test_reconcile_wording_is_kind_aware(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("NVH_HOME", str(tmp_path / "nvh"))
    jobs._TASKS.clear()

    install = jobs.create_job(kind="comfyui-install", title="Install ComfyUI")
    run = jobs.create_job(kind="schedule-run", title="r", request={"schedule_id": "a"})
    for job in (install, run):
        jobs.append_event(job["id"], {"event": "step", "status": "running", "message": "working"})

    reconciled = {j["kind"]: j for j in jobs.list_jobs(limit=10)}
    assert reconciled["comfyui-install"]["status"] == "interrupted"
    assert reconciled["comfyui-install"]["message"] == (
        "Server restarted or disconnected while this job was running. Retry to resume setup."
    )
    assert reconciled["schedule-run"]["status"] == "interrupted"
    assert reconciled["schedule-run"]["message"] == (
        "Server restarted or disconnected while this run was in flight; "
        "the schedule will run again at its next interval."
    )


def test_canceled_wording_is_kind_aware_and_knows_who_canceled() -> None:
    assert jobs._canceled_message("comfyui-install", by_user=True) == "Install job canceled by user."
    assert jobs._canceled_message("comfyui-install", by_user=False) == "Install job canceled by user."
    assert jobs._canceled_message("schedule-run", by_user=True) == "Run canceled by user."
    assert jobs._canceled_message("schedule-run", by_user=False) == (
        "Run canceled before it finished (the server stopped); the schedule will run again at its next interval."
    )


@pytest.mark.asyncio
async def test_a_schedule_run_cancelled_by_the_process_is_not_blamed_on_the_user(tmp_path, monkeypatch) -> None:
    """The lifespan cancelling the runner mid-run propagates into the job task: the record says the
    server stopped, not "Install job canceled by user"; ``cancel_job`` (the user's click) still does."""
    monkeypatch.setenv("NVH_HOME", str(tmp_path / "nvh"))
    jobs._TASKS.clear()
    gate = asyncio.Event()
    started = asyncio.Event()

    async def source(job_id: str) -> AsyncIterator[dict[str, object]]:
        yield {"event": "step", "status": "running", "message": "running the turn"}
        started.set()
        await gate.wait()
        yield {"event": "complete", "status": "complete", "message": "never"}

    job = jobs.start_job_with_id(kind="schedule-run", title="r", request={"schedule_id": "a"}, source_factory=source)
    await asyncio.wait_for(started.wait(), timeout=5)
    task = jobs._TASKS[job["id"]]
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    loaded = jobs.load_job(job["id"])
    assert loaded["status"] == "canceled"
    assert loaded["message"] == (
        "Run canceled before it finished (the server stopped); the schedule will run again at its next interval."
    )
    assert jobs.read_events(job["id"])[-1]["message"] == loaded["message"]

    # The user's own cancel keeps saying so, in the run's words.
    second = jobs.start_job_with_id(kind="schedule-run", title="r2", request={"schedule_id": "a"}, source_factory=source)
    started.clear()
    await asyncio.wait_for(started.wait(), timeout=5)
    second_task = jobs._TASKS[second["id"]]
    jobs.cancel_job(second["id"])
    with pytest.raises(asyncio.CancelledError):
        await second_task
    assert jobs.load_job(second["id"])["message"] == "Run canceled by user."

    install = jobs.start_job_with_id(kind="comfyui-install", title="i", request={}, source_factory=source)
    started.clear()
    await asyncio.wait_for(started.wait(), timeout=5)
    install_task = jobs._TASKS[install["id"]]
    install_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await install_task
    assert jobs.load_job(install["id"])["message"] == "Install job canceled by user."


@pytest.mark.asyncio
async def test_start_job_with_id_hands_the_source_its_job_id(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("NVH_HOME", str(tmp_path / "nvh"))
    jobs._TASKS.clear()
    seen: list[str] = []

    async def source(job_id: str) -> AsyncIterator[dict[str, object]]:
        seen.append(job_id)
        yield {"event": "complete", "status": "complete", "message": f"finished {job_id}"}

    job = jobs.start_job_with_id(kind="schedule-run", title="r", request={"schedule_id": "a"}, source_factory=source)
    for _ in range(50):
        if jobs.load_job(job["id"])["status"] == "complete":
            break
        await asyncio.sleep(0.01)
    loaded = jobs.load_job(job["id"])
    assert seen == [job["id"]] and loaded["status"] == "complete"
    assert loaded["message"] == f"finished {job['id']}" and job["id"] not in jobs._TASKS
