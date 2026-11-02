"""The schedule runner: one run of a schedule is a job, one process runs them.

Design 0.46 §6. ``run_schedule_once`` turns one :class:`ScheduledTask` into a
``schedule-run`` job under ``$NVH_HOME/jobs/`` (``kind``
:data:`SCHEDULE_RUN_KIND`), runs the schedule's mode and records the outcome
on the schedule with ``Scheduler.mark_run``; ``run_loop`` polls for due
schedules under the heartbeat lock ``state/scheduler.lock`` so the API
process and ``nvh schedule start`` never run the same store side by side.

Modes:

- ``ask`` — an **unattended** Wizard turn (``wizard_chat(..., unattended=True)``):
  the dialogue tools are not offered, the whitelist is the profile's
  ``tools_allowed`` ∩ ``SCHEDULE_AUTO_TOOLS`` (chat.py derives the roster),
  and every confirm or privileged call the model requests is recorded on the
  run as ``{name, arguments, privileged, unattended: true}`` — no plan, no
  approval token, nothing executed. The run's ``complete`` payload passes
  those entries through ``_persistable_calls`` as well (belt and braces: no
  token can reach ``jobs/<id>.jsonl`` or ``GET /v1/jobs/{id}/events``).
- ``convene`` — ``engine.run_council(prompt)``; the synthesis is the answer.
- ``do`` — refused here (owner decision O5): a failed job carrying
  :data:`DO_REFUSED` and ``last_status: error``. Only the CLI runner, which
  has a terminal, passes ``allow_do=True`` and runs the agent loop as before.

Every run first awaits ``engine._check_budget()``; a ``BudgetExceededError``
records ``last_status: skipped`` and a ``complete`` event whose ``outcome`` is
``skipped`` (the job did finish; nothing ran). A run that raises records
``last_status: error`` with the message and a ``failed`` job, and never claims
completion. After ``mark_run`` the runner prunes that schedule's runs to the
last :data:`SCHEDULE_RUN_HISTORY` (the retention rule).

The lock is a JSON file ``{pid, started_at, heartbeat, poll_s, runner}``
created with ``os.open(O_CREAT | O_EXCL)`` and rewritten every poll. It is
stale **only** when its heartbeat is older than ``3 × poll_s`` of the holder;
there is no pid probe (a pid-0 signal probe terminates the target on Windows),
so ``pid`` and ``runner`` are information for ``GET /v1/schedules`` and the
doctor row, never a liveness rule. The holder beats from a side task while a
run is in flight (a long turn never lets the lock read stale), every beat
re-reads the file and stands down when another runner's pid is in it, and a
lock that exists but cannot be read counts as *live* (never taken over). The
API's loop waits for a held lock and takes over polling when it is released
(``wait_for_lock``); the CLI's returns at once.

One run per schedule at a time: ``run_schedule_once`` raises
:class:`ScheduleRunInFlightError` while a run of the same schedule is still in
flight in this process (``POST /v1/schedules/{id}/run`` answers 409) and the
poll skips it. The poll also leaves a due ``mode: do`` schedule alone unless
``allow_do`` (the CLI runner): refusing it every interval would consume the
slot the CLI needs. An ``ask`` run whose turn fell back to the offline helper
(``mode: "deterministic"``) is an ``error`` naming the ``fallback_reason``,
never an ``ok`` with the helper's canned text as the answer.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import sys
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Collection
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from nvh.core.scheduler import STORE_LOCK
from nvh.integrations.services import jobs
from nvh.integrations.workspace.storage import storage_layout

logger = logging.getLogger(__name__)

SCHEDULE_RUN_KIND = "schedule-run"
"""``kind`` of the job records the runner writes under ``$NVH_HOME/jobs/``."""

SCHEDULE_RUN_HISTORY = 20
"""Runs kept per schedule; :func:`prune_schedule_runs` deletes older ``.json`` + ``.jsonl`` pairs."""

STALE_HEARTBEATS = 3
"""A lock whose heartbeat is older than this many ``poll_s`` is stale and may be taken over."""

DEFAULT_POLL_S = 60.0

LOCK_READ_RETRIES = 3
LOCK_READ_RETRY_S = 0.05
"""A lock read that fails with an ``OSError`` other than "missing" (a reader racing the holder's
``os.replace`` on Windows) is retried this many times, this far apart, before it counts as unreadable."""

DO_REFUSED = (
    "mode 'do' runs tools unattended and cannot show an approval card; "
    "use mode ask (the Wizard runs safe tools on its own) or `nvh schedule start` in a terminal"
)
"""The sentence a ``mode: do`` schedule gets from the API runner (a failed job, ``last_status: error``)."""

RUNNER_API = "api"
RUNNER_CLI = "cli"

EngineFactory = Callable[[], Awaitable[Any]]


class ScheduleRunInFlightError(RuntimeError):
    """A run of this schedule is already in flight in this process; ``job_id`` names it."""

    def __init__(self, schedule_id: str, job_id: str) -> None:
        super().__init__(f"schedule {schedule_id} already has a run in flight (job {job_id})")
        self.schedule_id = schedule_id
        self.job_id = job_id


class NoModelAnsweredError(RuntimeError):
    """An ``ask`` run whose Wizard turn no model answered (the offline helper replied instead)."""


#: ``schedule_id -> job_id`` of the runs this process started; consulted through :func:`in_flight_job`.
_IN_FLIGHT: dict[str, str] = {}


def in_flight_job(schedule_id: str) -> str | None:
    """The id of the ``schedule-run`` job still running for ``schedule_id`` in this process, else ``None``.

    The entry is dropped as soon as the job's consumer task is gone or done
    (``jobs._TASKS``), so a finished, failed or cancelled run never blocks the
    next one.
    """
    job_id = _IN_FLIGHT.get(schedule_id)
    if job_id is None:
        return None
    task = jobs._TASKS.get(job_id)
    if task is None or task.done():
        _IN_FLIGHT.pop(schedule_id, None)
        return None
    return job_id


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


# ---------------------------------------------------------------------------
# Paths and the engine
# ---------------------------------------------------------------------------


def lock_path(home_dir: str | Path | None = None) -> Path:
    """``$NVH_HOME/state/scheduler.lock`` (the layout's ``state_dir``)."""
    return storage_layout(home_dir).state_dir / "scheduler.lock"


def schedule_file(home_dir: str | Path | None = None) -> Path:
    """``$NVH_HOME/state/schedules.json`` for ``home_dir`` (``default_schedule_file()`` for the process home)."""
    return storage_layout(home_dir).state_dir / "schedules.json"


def _scheduler(home_dir: str | Path | None = None):
    """A fresh ``Scheduler`` over the store — re-read at every use so a save never clobbers another writer's record."""
    from nvh.core.scheduler import Scheduler

    return Scheduler(schedule_file=schedule_file(home_dir))


async def _server_engine() -> Any | None:
    """The engine the API lifespan built, when this process is the server."""
    server = sys.modules.get("nvh.api.server")
    return getattr(server, "_engine", None) if server is not None else None


async def default_engine() -> Any:
    """The API's engine (``nvh.api.deps.get_engine``) or, outside the server, one built as the CLI builds it.

    The CLI runner wraps its loop in :func:`cli_engine_scope` so the engine it
    built is also what ``wizard_chat`` finds through ``nvh.api.server.get_engine``.
    """
    engine = await _server_engine()
    if engine is not None:
        return engine
    from nvh.core.engine import Engine

    engine = Engine()
    await engine.initialize()
    return engine


@contextmanager
def cli_engine_scope(engine: Any):
    """Lend ``engine`` to ``nvh.api.server._engine`` for the duration of a CLI run.

    ``wizard_chat`` reaches its engine only through ``nvh.api.deps.get_engine``,
    which reads the server module's ``_engine``; the CLI runner has no server
    lifespan, so it installs the engine it built there and restores whatever
    was there before (``None``) on the way out. Nothing is installed over an
    engine a lifespan owns.
    """
    import nvh.api.server as server_module

    previous = getattr(server_module, "_engine", None)
    installed = previous is None
    if installed:
        server_module._engine = engine
    try:
        yield engine
    finally:
        if installed and getattr(server_module, "_engine", None) is engine:
            server_module._engine = previous


# ---------------------------------------------------------------------------
# One run
# ---------------------------------------------------------------------------


def _task_label(task: Any) -> str:
    name = str(getattr(task, "name", "") or "").strip()
    return name or str(task.prompt)[:60]


def _request_for(task: Any, conversation_id: str) -> dict[str, Any]:
    return {
        "schedule_id": task.id,
        "mode": task.mode,
        "advisor": task.advisor,
        "profile": task.profile,
        "conversation_id": conversation_id,
    }


def _persistable(calls: list[Any]) -> list[Any]:
    """``chat._persistable_calls`` — strips ``approval_token``/``approval_expires_at`` from every entry."""
    from nvh.integrations.wizard.chat import _persistable_calls

    return _persistable_calls([c for c in calls if isinstance(c, dict)])


async def _ensure_conversation(task: Any) -> str:
    """The schedule's one thread: its recorded id, or a new ``mode: wizard`` conversation.

    Best-effort — when the conversations store is unavailable (no database in
    this process) a fresh id is still returned so the run can proceed; the
    Wizard turn's persistence is best-effort on its side as well.
    """
    existing = str(getattr(task, "conversation_id", "") or "").strip()
    if existing:
        return existing
    try:
        from nvh.storage import repository as repo

        conv = await repo.create_conversation(title=_task_label(task), mode="wizard")
        return str(conv.id)
    except Exception as exc:
        logger.warning("schedule %s: could not create its conversation (%s); using an unsaved id", task.id, exc)
        return uuid.uuid4().hex


def _preview(text: str, limit: int = 120) -> str:
    return " ".join(str(text or "").split())[:limit]


async def _run_ask(task: Any, conversation_id: str) -> dict[str, Any]:
    """An unattended Wizard turn; the envelope reduced to what the run records.

    The schedule's ``advisor`` is the turn's provider pin (``provider=``, the
    same advisory pin a profile's own ``provider`` is). A turn no model
    answered — ``mode: "deterministic"``: the provider raised, the engine was
    not initialised, or a pinned local-only profile declined — raises
    :class:`NoModelAnsweredError` with the ``fallback_reason``, so the run is
    recorded as ``error`` instead of an ``ok`` carrying the offline helper's
    canned text.
    """
    from nvh.integrations.wizard import chat as chat_mod

    result = await chat_mod.wizard_chat(
        task.prompt,
        profile=task.profile or None,
        provider=task.advisor or None,
        conversation_id=conversation_id,
        unattended=True,
    )
    if result.get("mode") == "deterministic":
        reason = str(result.get("fallback_reason") or "no model answered")
        raise NoModelAnsweredError(f"no model answered the scheduled turn: {reason}")
    surfaced = list(result.get("tool_calls") or [])
    deferred = list(result.get("deferred_tool_calls") or [])
    return {
        "answer": str(result.get("answer") or ""),
        "provider": result.get("used_provider") or "",
        "model": result.get("used_model") or "",
        "cost_usd": result.get("cost_usd"),
        "cost_known": result.get("cost_known"),
        "latency_ms": int(result.get("latency_ms") or 0),
        "iterations": int(result.get("iterations") or 0),
        "used_profile": result.get("used_profile"),
        "mode": result.get("mode"),
        "deferred": _persistable(surfaced + deferred),
        "awaiting_approval": len(surfaced),
    }


async def _run_convene(task: Any, engine: Any, conversation_id: str) -> dict[str, Any]:
    """``engine.run_council`` on the prompt; the synthesis (or the members' answers) is the answer."""
    kwargs: dict[str, Any] = {}
    if task.advisor:
        kwargs["members"] = [task.advisor]
    result = await engine.run_council(task.prompt, conversation_id=conversation_id, **kwargs)
    synthesis = getattr(result, "synthesis", None)
    if synthesis is not None and getattr(synthesis, "content", None):
        answer = str(synthesis.content)
        provider = getattr(synthesis, "provider", "") or ""
        model = getattr(synthesis, "model", "") or ""
    else:
        members = getattr(result, "member_responses", {}) or {}
        answer = "\n\n".join(
            f"[{name}] {getattr(resp, 'content', '')}" for name, resp in members.items()
        )
        provider = ",".join(members) if isinstance(members, dict) else ""
        model = ""
    total_cost = getattr(result, "total_cost_usd", None)
    return {
        "answer": answer,
        "provider": provider,
        "model": model,
        "cost_usd": float(total_cost) if total_cost is not None else None,
        # A council's figure is the sum of its members' meters; the catalog's
        # verdict per member is not carried on CouncilResponse, so it is only
        # known when something was metered.
        "cost_known": bool(total_cost is not None and float(total_cost) > 0) or None,
        "latency_ms": int(getattr(result, "total_latency_ms", 0) or 0),
        "deferred": [],
        "awaiting_approval": 0,
    }


async def _run_do(task: Any, engine: Any) -> dict[str, Any]:
    """The CLI runner's ``do``: the agent loop with safe tools auto-approved (pre-0.46 behaviour)."""
    from nvh.core.agent_loop import run_agent_loop
    from nvh.core.tools import ToolRegistry

    result = await run_agent_loop(
        task=task.prompt,
        engine=engine,
        tools=ToolRegistry(),
        provider=task.advisor or None,
        auto_approve_safe=True,
    )
    cost = getattr(result, "total_cost_usd", None)
    return {
        "answer": str(getattr(result, "final_response", "") or ""),
        "provider": "",
        "model": "",
        "cost_usd": float(cost) if cost is not None else None,
        "cost_known": getattr(result, "cost_known", None),
        "latency_ms": 0,
        "deferred": [],
        "awaiting_approval": 0,
    }


async def _notify(label: str, preview: str, cost: str = "") -> None:
    try:
        from nvh.core.notify import notify_task_complete

        await notify_task_complete(label, preview, cost)
    except Exception as exc:  # a notifier failure never changes the run's outcome
        logger.debug("schedule notification failed: %s", exc)


async def run_schedule_once(
    task: Any,
    *,
    home_dir: str | Path | None = None,
    engine_factory: EngineFactory | None = None,
    allow_do: bool = False,
    notify: bool = False,
) -> str:
    """Start one run of ``task`` as a ``schedule-run`` job; returns the job id.

    The job runs in the background (``jobs.start_job_with_id``); await
    :func:`wait_for_run` for its final record. ``engine_factory`` supplies the
    engine (:func:`default_engine` when omitted); ``allow_do`` is the CLI
    runner's terminal (the API runner refuses ``do``); ``notify`` fires
    ``notify_task_complete`` after the run (CLI only). Raises
    :class:`ScheduleRunInFlightError` while a run of this schedule started by this
    process is still in flight (:func:`in_flight_job`): one run per schedule
    at a time, whether the poll or "Run now" asked.
    """
    existing = in_flight_job(task.id)
    if existing is not None:
        raise ScheduleRunInFlightError(task.id, existing)
    conversation_id = await _ensure_conversation(task)
    factory = engine_factory or default_engine
    label = _task_label(task)

    async def _source(job_id: str) -> AsyncIterator[dict[str, Any]]:
        recorded = False

        def mark(status: str, **fields: Any) -> None:
            nonlocal recorded
            recorded = True
            try:
                # Load-modify-save under the in-process store lock, so a route
                # saving on a worker thread at the same moment cannot drop this
                # record (or lose its own change).
                with STORE_LOCK:
                    _scheduler(home_dir).mark_run(
                        task.id, status=status, job_id=job_id, conversation_id=conversation_id, **fields,
                    )
            except Exception as exc:  # the schedule was removed mid-run, or the store is unwritable
                logger.warning("schedule %s: could not record the run (%s)", task.id, exc)

        try:
            if task.mode == "do" and not allow_do:
                mark("error", error=DO_REFUSED)
                yield {"event": "error", "status": "failed", "message": DO_REFUSED, "schedule_id": task.id}
                return

            try:
                engine = await factory()
                await engine._check_budget()
            except Exception as exc:
                from nvh.core.engine import BudgetExceededError

                if not isinstance(exc, BudgetExceededError):
                    raise
                reason = str(exc) or "budget exceeded"
                mark("skipped", error=reason)
                yield {
                    "event": "complete", "status": "complete", "outcome": "skipped",
                    "message": f"Skipped: {reason}", "schedule_id": task.id, "answer": "",
                    "deferred": [], "cost_usd": None, "cost_known": None,
                }
                return

            yield {
                "event": "step", "status": "running", "schedule_id": task.id,
                "message": f"Running {label} (mode {task.mode})",
            }
            if task.mode == "convene":
                outcome = await _run_convene(task, engine, conversation_id)
            elif task.mode == "do":
                outcome = await _run_do(task, engine)
            else:
                outcome = await _run_ask(task, conversation_id)
        except asyncio.CancelledError:
            if not recorded:
                mark("error", error="run canceled before it finished")
            raise
        except Exception as exc:
            message = str(exc) or type(exc).__name__
            mark("error", error=message[:300])
            if notify:
                await _notify(label[:50], f"Error: {message}", "")
            yield {"event": "error", "status": "failed", "message": message, "schedule_id": task.id}
            return

        status = "deferred" if outcome["awaiting_approval"] else "ok"
        mark(status, cost_usd=outcome.get("cost_usd"), cost_known=outcome.get("cost_known"))
        try:
            prune_schedule_runs(task.id, keep=SCHEDULE_RUN_HISTORY, home_dir=home_dir, protect=job_id)
        except Exception as exc:
            logger.debug("schedule %s: prune failed (%s)", task.id, exc)
        if notify:
            await _notify(label[:50], _preview(outcome["answer"], 100))
        payload = {k: v for k, v in outcome.items() if k != "awaiting_approval"}
        payload["deferred"] = _persistable(list(payload.get("deferred") or []))
        yield {
            "event": "complete", "status": "complete", "outcome": status, "schedule_id": task.id,
            "conversation_id": conversation_id,
            "message": _preview(outcome["answer"])
            or ("Done" if status == "ok" else "Done; a request awaits your approval"),
            **payload,
        }

    job = jobs.start_job_with_id(
        kind=SCHEDULE_RUN_KIND,
        title=label,
        request=_request_for(task, conversation_id),
        source_factory=_source,
    )
    _IN_FLIGHT[task.id] = str(job["id"])
    return str(job["id"])


async def wait_for_run(job_id: str, *, home_dir: str | Path | None = None) -> dict[str, Any]:
    """Await the background job ``run_schedule_once`` started and return its final record (events included)."""
    task = jobs._TASKS.get(job_id)
    if task is not None:
        try:
            await task
        except asyncio.CancelledError:
            raise
        except Exception:  # the consumer logs and records the failure on the job itself
            pass
    return jobs.load_job(job_id, home_dir=home_dir)


def prune_schedule_runs(
    schedule_id: str,
    *,
    keep: int = SCHEDULE_RUN_HISTORY,
    home_dir: str | Path | None = None,
    protect: str = "",
) -> int:
    """Delete the oldest ``schedule-run`` job pairs of ``schedule_id`` beyond ``keep``; the number removed.

    Newest first by id (``<YYYYmmddHHMMSS>-<8hex>``, so name order is time
    order). ``protect`` is the run being recorded: it counts towards ``keep``
    but is never removed, and neither is a run still in flight.
    """
    root = jobs.jobs_root(home_dir)
    mine: list[Path] = []
    for path in sorted(root.glob("*.json"), reverse=True):
        try:
            job = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if job.get("kind") != SCHEDULE_RUN_KIND:
            continue
        if (job.get("request") or {}).get("schedule_id") != schedule_id:
            continue
        if job.get("id") == protect or job.get("status") in jobs.RUNNING_STATUSES:
            continue
        mine.append(path)
    kept = max(0, int(keep)) - (1 if protect else 0)
    removed = 0
    for path in mine[max(0, kept):]:
        for victim in (path, path.with_suffix(".jsonl")):
            try:
                victim.unlink()
            except FileNotFoundError:
                pass
            except OSError as exc:
                logger.debug("prune: could not remove %s (%s)", victim, exc)
                continue
        removed += 1
    return removed


# ---------------------------------------------------------------------------
# The lock
# ---------------------------------------------------------------------------


def _read_lock_text(path: Path) -> str:
    """One read of the lock file (its own function so tests can make it fail)."""
    return path.read_text(encoding="utf-8")


def _read_lock(path: Path) -> dict[str, Any] | None:
    """The lock's record: ``None`` when absent, ``{}`` when it holds no JSON object, ``{"unreadable": True}``
    when it exists but cannot be read after :data:`LOCK_READ_RETRIES` tries.

    An unreadable lock is *not* evidence that its holder is gone — a
    permission difference between the API's account and the CLI's, or a
    transient sharing violation on Windows — so it is never treated as stale
    (:func:`_is_stale`) and never taken over.
    """
    raw: str | None = None
    for attempt in range(LOCK_READ_RETRIES):
        try:
            raw = _read_lock_text(path)
            break
        except FileNotFoundError:
            return None
        except OSError as exc:
            if attempt + 1 < LOCK_READ_RETRIES:
                time.sleep(LOCK_READ_RETRY_S)
                continue
            logger.debug("scheduler lock %s unreadable (%s)", path, exc)
            return {"unreadable": True}
    if raw is None:
        return {"unreadable": True}
    try:
        data = json.loads(raw)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _unreadable(info: dict[str, Any] | None) -> bool:
    return bool(info) and bool(info.get("unreadable"))


def _parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _heartbeat_age_s(info: dict[str, Any] | None, now: datetime | None = None) -> float | None:
    if not info:
        return None
    beat = _parse_iso(info.get("heartbeat")) or _parse_iso(info.get("started_at"))
    if beat is None:
        return None
    return ((now or datetime.now(UTC)) - beat).total_seconds()


def _stale_after_s(info: dict[str, Any] | None, fallback_poll_s: float = DEFAULT_POLL_S) -> float:
    poll = fallback_poll_s
    if info:
        try:
            poll = float(info.get("poll_s") or fallback_poll_s)
        except (TypeError, ValueError):
            poll = fallback_poll_s
    return STALE_HEARTBEATS * max(1.0, poll)


def _is_stale(info: dict[str, Any] | None, *, fallback_poll_s: float = DEFAULT_POLL_S) -> bool:
    """Stale by heartbeat age only: a missing, corrupt or beat-less lock counts as stale; a lock that
    exists but cannot be read (``{"unreadable": True}``) counts as live."""
    if info is None:
        return True
    if _unreadable(info):
        return False
    age = _heartbeat_age_s(info)
    if age is None:
        return True
    return age > _stale_after_s(info, fallback_poll_s)


def runner_status(home_dir: str | Path | None = None) -> dict[str, Any]:
    """What the lock file says: ``{active, pid, lock_path, heartbeat, stale, runner, started_at, poll_s, unreadable}``.

    ``active`` is "a lock exists and its heartbeat is fresh"; ``runner`` is
    ``api`` or ``cli`` (what the holder wrote), empty when no live lock is held.
    ``unreadable`` is a lock that exists but could not be read: counted as
    active (nothing takes it over) with no pid, beat or runner to show.
    """
    path = lock_path(home_dir)
    info = _read_lock(path)
    stale = _is_stale(info)
    unreadable = _unreadable(info)
    pid = (info or {}).get("pid")
    return {
        "active": info is not None and not stale,
        "pid": int(pid) if isinstance(pid, int) and not isinstance(pid, bool) else None,
        "lock_path": str(path),
        "heartbeat": (info or {}).get("heartbeat") or None,
        "started_at": (info or {}).get("started_at") or None,
        "stale": bool(info is not None and stale),
        "runner": str((info or {}).get("runner") or "") if info is not None and not stale else "",
        "poll_s": (info or {}).get("poll_s"),
        "unreadable": unreadable,
    }


class ScheduleLock:
    """The heartbeat lock at :func:`lock_path`.

    ``acquire`` creates the file exclusively, or takes over a stale one (never
    one it cannot read); ``heartbeat`` re-reads the file and rewrites it (tmp +
    ``os.replace``) only while it still carries this process's pid — a foreign
    pid means another runner took over and this one stands down (``held``
    cleared, ``False`` returned); ``release`` removes it under the same rule,
    so a takeover's lock is never deleted by the process it replaced.
    """

    def __init__(self, path: Path, *, poll_s: float = DEFAULT_POLL_S, runner: str = RUNNER_API) -> None:
        self.path = path
        self.poll_s = float(poll_s)
        self.runner = runner
        self.pid = os.getpid()
        self.started_at = ""
        self.held = False

    def _payload(self) -> str:
        return json.dumps({
            "pid": self.pid,
            "started_at": self.started_at,
            "heartbeat": _now_iso(),
            "poll_s": self.poll_s,
            "runner": self.runner,
        }, sort_keys=True) + "\n"

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.started_at = _now_iso()
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            info = _read_lock(self.path)
            if info is None:  # vanished between the two calls: retry once
                return self.acquire()
            if _unreadable(info):
                logger.warning(
                    "Scheduler lock %s exists but cannot be read; treating it as held (not taking it over)",
                    self.path,
                )
                return False
            if not _is_stale(info, fallback_poll_s=self.poll_s):
                return False
            logger.warning(
                "Taking over a stale scheduler lock %s (pid %s, heartbeat %s)",
                self.path, info.get("pid"), info.get("heartbeat"),
            )
            self.held = True
            self._write_payload()  # not heartbeat(): the file still carries the stale holder's pid
            return True
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(self._payload())
        self.held = True
        return True

    def _lost_to(self, info: dict[str, Any]) -> bool:
        self.held = False
        logger.warning(
            "Scheduler lock %s is now held by a %s runner (pid %s); this runner stands down",
            self.path, info.get("runner") or "other", info.get("pid"),
        )
        return False

    def heartbeat(self) -> bool:
        """Rewrite the lock with a fresh beat; ``False`` once another runner holds it.

        The file is re-read first. A record with another pid means a takeover
        happened (a run of ours outlasted the staleness window): ``held`` is
        cleared and nothing is written, so the two runners never overwrite
        each other's lock. A missing file is re-created exclusively — if that
        fails, another runner acquired it in the gap and this one stands down.
        An unreadable file (a transient sharing violation) or a failed write
        is retried at the next beat.
        """
        if not self.held:
            return False
        info = _read_lock(self.path)
        if info is None:
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                return self._lost_to(_read_lock(self.path) or {})
            except OSError as exc:
                logger.debug("scheduler heartbeat not re-created (%s)", exc)
                return True
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(self._payload())
            return True
        if _unreadable(info):
            logger.debug("scheduler heartbeat skipped: %s unreadable this beat", self.path)
            return True
        if info and info.get("pid") != self.pid:
            return self._lost_to(info)
        self._write_payload()
        return True

    def _write_payload(self) -> None:
        """Replace the lock file with this runner's record (tmp + ``os.replace``); a failed write is retried at the next beat."""
        tmp = self.path.with_name(f"{self.path.name}.{self.pid}-{uuid.uuid4().hex[:8]}.tmp")
        try:
            tmp.write_text(self._payload(), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError as exc:  # a reader held the file open on Windows; the next beat retries
            logger.debug("scheduler heartbeat not written (%s)", exc)
        finally:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass

    def release(self) -> None:
        if not self.held:
            return
        self.held = False
        info = _read_lock(self.path)
        if info is None or _unreadable(info) or info.get("pid") != self.pid:
            return
        try:
            self.path.unlink()
        except OSError as exc:
            logger.debug("scheduler lock not removed (%s)", exc)


# ---------------------------------------------------------------------------
# The loop
# ---------------------------------------------------------------------------


async def run_due_tasks(
    *,
    home_dir: str | Path | None = None,
    engine_factory: EngineFactory | None = None,
    allow_do: bool = False,
    notify: bool = False,
    on_start: Callable[[Any, str], None] | None = None,
    on_done: Callable[[Any, dict[str, Any]], None] | None = None,
    only_modes: Collection[str] | None = None,
    keep_going: Callable[[], bool] | None = None,
) -> list[dict[str, Any]]:
    """Run every due schedule once, sequentially; the final job record of each run.

    Skipped, and left due: a schedule whose run is still in flight in this
    process (:func:`in_flight_job` — "Run now" and the poll never overlap), a
    ``mode: do`` schedule when ``allow_do`` is false (the API runner leaves it
    for the CLI runner instead of failing it every interval, which would
    consume its due slot), and any mode outside ``only_modes`` when that is
    given (``nvh schedule start --once`` beside a live API runner runs only
    ``do``). ``keep_going`` is asked before each run; ``False`` ends the pass
    (the loop passes "do we still hold the lock").
    """
    due = _scheduler(home_dir).get_due_tasks()
    finished: list[dict[str, Any]] = []
    for task in due:
        if keep_going is not None and not keep_going():
            break
        if only_modes is not None and task.mode not in only_modes:
            continue
        if task.mode == "do" and not allow_do:
            logger.debug("schedule %s: mode do is left for `nvh schedule start`", task.id)
            continue
        running = in_flight_job(task.id)
        if running is not None:
            logger.info("schedule %s: run %s still in flight; not starting another", task.id, running)
            continue
        job_id = await run_schedule_once(
            task, home_dir=home_dir, engine_factory=engine_factory, allow_do=allow_do, notify=notify,
        )
        if on_start is not None:
            on_start(task, job_id)
        job = await wait_for_run(job_id, home_dir=home_dir)
        if on_done is not None:
            on_done(task, job)
        finished.append(job)
    return finished


async def _stopped_within(stop: asyncio.Event, timeout: float) -> bool:
    """``True`` when ``stop`` was set within ``timeout`` seconds, ``False`` when the time elapsed."""
    try:
        await asyncio.wait_for(stop.wait(), timeout=timeout)
    except TimeoutError:
        return False
    return True


async def _beat_while_running(lock: ScheduleLock, every_s: float) -> None:
    """Rewrite the heartbeat every ``every_s`` seconds until the lock is lost or the task is cancelled.

    Runs beside the poll loop so a run that outlasts ``3 × poll_s`` (a slow
    unattended turn, a council of several members) never lets the lock read
    stale to a second runner.
    """
    while True:
        await asyncio.sleep(max(0.05, every_s))
        if not lock.heartbeat():
            return


async def run_loop(
    stop: asyncio.Event,
    *,
    poll_s: float = DEFAULT_POLL_S,
    home_dir: str | Path | None = None,
    runner: str = RUNNER_API,
    engine_factory: EngineFactory | None = None,
    allow_do: bool = False,
    notify: bool = False,
    on_start: Callable[[Any, str], None] | None = None,
    on_done: Callable[[Any, dict[str, Any]], None] | None = None,
    wait_for_lock: bool = False,
    only_modes: Collection[str] | None = None,
) -> bool:
    """Poll for due schedules every ``poll_s`` seconds under the heartbeat lock until ``stop`` is set.

    When another live runner holds the lock: with ``wait_for_lock`` (the API
    lifespan) the loop logs once and retries ``acquire`` every ``poll_s``
    until ``stop`` is set or the lock is released, then takes over polling;
    without it (``nvh schedule start``) it returns ``False`` at once after one
    log line. The heartbeat is rewritten at the top of every poll and from a
    side task while runs are in flight; due schedules run one after another
    through :func:`run_schedule_once`. Returns ``True`` after a loop that ran
    and released the lock, ``False`` when it never polled (stopped while
    waiting, or a live holder) or lost the lock to another runner mid-way
    (logged; this loop stands down rather than run beside it).
    """
    lock = ScheduleLock(lock_path(home_dir), poll_s=poll_s, runner=runner)
    if not lock.acquire():
        held = runner_status(home_dir)
        if not wait_for_lock:
            logger.info(
                "Schedule runner not started: a live %s runner (pid %s, heartbeat %s) holds %s",
                held.get("runner") or "other", held.get("pid"), held.get("heartbeat"), lock.path,
            )
            return False
        logger.info(
            "Schedule runner waiting: a live %s runner (pid %s) holds %s; retrying every %gs",
            held.get("runner") or "other", held.get("pid"), lock.path, poll_s,
        )
        while True:
            if await _stopped_within(stop, poll_s):
                return False
            if lock.acquire():
                logger.info("Schedule runner took over polling: %s was released", lock.path)
                break
    beats = asyncio.create_task(_beat_while_running(lock, poll_s), name="nvh-schedule-heartbeat")
    lost = False
    try:
        while not stop.is_set():
            if not lock.heartbeat():
                lost = True
                break
            try:
                await run_due_tasks(
                    home_dir=home_dir, engine_factory=engine_factory, allow_do=allow_do,
                    notify=notify, on_start=on_start, on_done=on_done, only_modes=only_modes,
                    keep_going=lambda: lock.held,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # one bad poll never ends the loop
                logger.warning("schedule poll failed: %s", exc)
            if not lock.held:
                lost = True
                break
            if await _stopped_within(stop, poll_s):
                break
    finally:
        beats.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await beats
        lock.release()
    if lost:
        logger.warning("Schedule runner stopped: another runner took over %s", lock.path)
        return False
    return True


__all__ = [
    "DO_REFUSED",
    "RUNNER_API",
    "RUNNER_CLI",
    "SCHEDULE_RUN_HISTORY",
    "SCHEDULE_RUN_KIND",
    "STALE_HEARTBEATS",
    "NoModelAnsweredError",
    "ScheduleLock",
    "ScheduleRunInFlightError",
    "cli_engine_scope",
    "default_engine",
    "in_flight_job",
    "lock_path",
    "prune_schedule_runs",
    "run_due_tasks",
    "run_loop",
    "run_schedule_once",
    "runner_status",
    "schedule_file",
    "wait_for_run",
]
