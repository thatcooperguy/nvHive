"""``/v1/schedules`` — the durable schedules store and its runner (design 0.46 §6).

Seven routes over ``nvh.core.scheduler.Scheduler`` (the ``nvh-schedules/2``
store at ``$NVH_HOME/state/schedules.json``) and
``nvh.integrations.services.schedule_runner``: list (with the runner's lock
status and the unattended tool roster), create, read, patch, delete, run now
and the run log. Every route takes ``require_auth`` from :mod:`nvh.api.deps`
and reads the process home only.

``mode: do`` is refused with 422 (owner decision O5): the API runner has no
terminal to show an approval card, so only ``ask`` (an unattended Wizard turn
under ``SCHEDULE_AUTO_TOOLS``) and ``convene`` (a council) can be scheduled
here; ``nvh schedule add --mode do`` stays a CLI-runner choice.

Every write (create, patch, delete) builds its ``Scheduler`` and saves under
``nvh.core.scheduler.STORE_LOCK``, the same in-process lock the runner's
``mark_run`` takes, so a route on a worker thread and a run finishing on the
event loop never drop each other's record. ``POST /{id}/run`` answers 409
while a run of that schedule is still in flight in this process.

Mounted by ``nvh.api.server`` with ``app.include_router(router)``; the
lifespan task that polls these schedules lives in the server module.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from nvh.api.deps import _response_envelope, require_auth
from nvh.core.scheduler import STORE_LOCK, Scheduler, parse_interval
from nvh.integrations.services import schedule_runner
from nvh.integrations.services.jobs import list_jobs

logger = logging.getLogger(__name__)

router = APIRouter(tags=["schedules"])

API_MODES = ("ask", "convene")
"""Modes the API runner executes; ``do`` is CLI-only."""

PROMPT_MAX = 4000
NAME_MAX = 120
RUNS_LIMIT_DEFAULT = 20
RUNS_LIMIT_MAX = 100


class ScheduleCreateRequest(BaseModel):
    prompt: str = Field(..., min_length=1, max_length=PROMPT_MAX)
    every: str = Field(..., min_length=1, max_length=16, description="Interval in `parse_interval` syntax: 30m, 1h, 1d.")
    name: str = Field("", max_length=NAME_MAX)
    advisor: str = Field("", max_length=64, description="Provider override; empty routes normally.")
    profile: str = Field("", max_length=64, description="Agent profile name; empty lets the concierge choose.")
    mode: str = Field("ask", max_length=16, description="`ask` or `convene`; `do` is refused here.")
    enabled: bool = True


class SchedulePatchRequest(BaseModel):
    enabled: bool | None = None
    every: str | None = Field(None, min_length=1, max_length=16)
    prompt: str | None = Field(None, min_length=1, max_length=PROMPT_MAX)
    name: str | None = Field(None, max_length=NAME_MAX)
    advisor: str | None = Field(None, max_length=64)
    profile: str | None = Field(None, max_length=64)


def _unprocessable(detail: str) -> HTTPException:
    return HTTPException(status_code=422, detail=detail)


def _interval_or_422(every: str) -> int:
    try:
        return parse_interval(every)
    except ValueError as exc:
        raise _unprocessable(str(exc)) from exc


def _mode_or_422(mode: str) -> str:
    cleaned = (mode or "ask").strip().lower()
    if cleaned == "do":
        raise _unprocessable(schedule_runner.DO_REFUSED)
    if cleaned not in API_MODES:
        raise _unprocessable(f"Unknown mode {mode!r}; expected one of {', '.join(API_MODES)}")
    return cleaned


def _profile_or_422(profile: str) -> str:
    """Empty means "let the concierge choose"; any other name must resolve to a profile."""
    cleaned = (profile or "").strip()
    if not cleaned or cleaned.lower() == "auto":
        return ""
    from nvh.integrations.wizard.profiles import get_profile

    if get_profile(cleaned) is None:
        raise _unprocessable(f"Unknown profile {cleaned!r}; see GET /v1/wizard/profiles")
    return cleaned


def _task_or_404(scheduler: Scheduler, schedule_id: str) -> Any:
    task = scheduler.get(schedule_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"Unknown schedule: {schedule_id}")
    return task


def _runner_block() -> dict[str, Any]:
    from nvh.integrations.wizard.chat import SCHEDULE_AUTO_TOOLS

    status = schedule_runner.runner_status()
    status["tools"] = sorted(SCHEDULE_AUTO_TOOLS)
    status["history"] = schedule_runner.SCHEDULE_RUN_HISTORY
    return status


@router.get("/v1/schedules", dependencies=[Depends(require_auth)])
async def schedules_list() -> dict[str, Any]:
    """Every schedule record plus the runner's lock status and unattended tool roster."""
    scheduler = await asyncio.to_thread(Scheduler)
    records = [task.to_dict() for task in scheduler.list_tasks()]
    return _response_envelope({
        "schedules": records,
        "count": len(records),
        "runner": await asyncio.to_thread(_runner_block),
    })


@router.post("/v1/schedules", dependencies=[Depends(require_auth)], status_code=201)
async def schedules_create(request: ScheduleCreateRequest) -> dict[str, Any]:
    """Add a schedule; ``every`` takes ``30m|1h|1d``, ``mode`` is ``ask`` or ``convene``."""
    interval = _interval_or_422(request.every)
    mode = _mode_or_422(request.mode)
    profile = await asyncio.to_thread(_profile_or_422, request.profile)

    def _add() -> dict[str, Any]:
        with STORE_LOCK:
            scheduler = Scheduler()
            task = scheduler.add(
                prompt=request.prompt.strip(),
                interval_seconds=interval,
                advisor=request.advisor.strip(),
                mode=mode,
                name=" ".join(request.name.split()),
                profile=profile,
            )
            if not request.enabled:
                scheduler.set_enabled(task.id, False)
            return task.to_dict()

    return _response_envelope(await asyncio.to_thread(_add))


@router.get("/v1/schedules/{schedule_id}", dependencies=[Depends(require_auth)])
async def schedules_get(schedule_id: str) -> dict[str, Any]:
    scheduler = await asyncio.to_thread(Scheduler)
    return _response_envelope(_task_or_404(scheduler, schedule_id).to_dict())


@router.patch("/v1/schedules/{schedule_id}", dependencies=[Depends(require_auth)])
async def schedules_patch(schedule_id: str, request: SchedulePatchRequest) -> dict[str, Any]:
    """Change ``enabled``, ``every``, ``prompt``, ``name``, ``advisor`` or ``profile``; wrong types are 422."""
    changes: dict[str, Any] = {}
    if request.every is not None:
        changes["interval_seconds"] = _interval_or_422(request.every)
    if request.prompt is not None:
        changes["prompt"] = request.prompt.strip()
    if request.name is not None:
        changes["name"] = " ".join(request.name.split())
    if request.advisor is not None:
        changes["advisor"] = request.advisor.strip()
    if request.profile is not None:
        changes["profile"] = await asyncio.to_thread(_profile_or_422, request.profile)
    if request.enabled is not None:
        changes["enabled"] = request.enabled

    def _apply() -> dict[str, Any]:
        with STORE_LOCK:
            scheduler = Scheduler()
            task = _task_or_404(scheduler, schedule_id)
            if not changes:
                return task.to_dict()
            try:
                updated = scheduler.update(schedule_id, **changes)
            except ValueError as exc:
                raise _unprocessable(str(exc)) from exc
            return (updated or task).to_dict()

    return _response_envelope(await asyncio.to_thread(_apply))


@router.delete("/v1/schedules/{schedule_id}", dependencies=[Depends(require_auth)])
async def schedules_delete(schedule_id: str) -> dict[str, Any]:
    def _remove() -> dict[str, Any]:
        with STORE_LOCK:
            scheduler = Scheduler()
            _task_or_404(scheduler, schedule_id)
            return {"deleted": scheduler.remove(schedule_id), "id": schedule_id}

    return _response_envelope(await asyncio.to_thread(_remove))


@router.post("/v1/schedules/{schedule_id}/run", dependencies=[Depends(require_auth)], status_code=202)
async def schedules_run(schedule_id: str) -> dict[str, Any]:
    """Run the schedule now as a ``schedule-run`` job; ``{job_id}`` to watch through ``/v1/jobs``.

    409 while a run of this schedule is still in flight in this process (the
    poll's or an earlier "Run now"); the ``detail`` names that job.
    """
    scheduler = await asyncio.to_thread(Scheduler)
    task = _task_or_404(scheduler, schedule_id)
    try:
        job_id = await schedule_runner.run_schedule_once(task)
    except schedule_runner.ScheduleRunInFlightError as exc:
        raise HTTPException(
            status_code=409,
            detail={"message": str(exc), "job_id": exc.job_id, "schedule_id": schedule_id},
        ) from exc
    return _response_envelope({"job_id": job_id, "schedule_id": schedule_id})


@router.get("/v1/schedules/{schedule_id}/runs", dependencies=[Depends(require_auth)])
async def schedules_runs(
    schedule_id: str,
    limit: int = Query(RUNS_LIMIT_DEFAULT, ge=1, le=RUNS_LIMIT_MAX),
) -> dict[str, Any]:
    """The schedule's recent runs (``schedule-run`` jobs), newest first."""
    runs = await asyncio.to_thread(
        list_jobs, kind=schedule_runner.SCHEDULE_RUN_KIND, schedule_id=schedule_id, limit=limit,
    )
    return _response_envelope({"runs": runs, "count": len(runs), "schedule_id": schedule_id})
