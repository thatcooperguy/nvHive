"""NVHive Scheduler — run tasks on a schedule.

Stores scheduled tasks in $NVH_STATE/schedules.json (the storage layout's
``state_dir``). Uses a simple polling loop (no system cron needed — works
without root).

On-disk format (``nvh-schedules/2``, since 0.46)::

    {"schema": "nvh-schedules/2", "updated_at": "<ISO>", "schedules": [record, ...]}

Each record carries every ``ScheduledTask`` field; unknown record keys are
kept under ``extra`` and written back. The pre-0.46 file — a bare JSON array
of records — still loads; it is rewritten in the v2 shape on the next save,
never on load. A file that cannot be parsed is moved aside to
``schedules.json.corrupt-<YYYYmmddHHMMSS>`` (one log line) and the store
starts empty instead of silently losing the records.

Examples:
  nvh schedule add "Summarize my emails" --every 1h
  nvh schedule add "Check server status" --every 30m --advisor groq
  nvh schedule list
  nvh schedule remove <id>
  nvh schedule start   # start the scheduler daemon
"""

import contextlib
import hashlib
import json
import logging
import os
import re
import threading
import time
import uuid
from collections.abc import Iterator
from dataclasses import MISSING, dataclass, field, fields
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

SCHEDULE_SCHEMA = "nvh-schedules/2"
"""Value of the ``schema`` key in a v2 ``schedules.json``."""

RUN_STATUSES = frozenset({"ok", "error", "skipped", "deferred"})
"""The values ``mark_run`` accepts for ``status`` (and ``last_status`` carries)."""

MAX_ERROR_CHARS = 300
"""``last_error`` is truncated to this many characters."""

_SAVE_RETRIES = 5
_SAVE_RETRY_DELAY_S = 0.05

STORE_LOCK = threading.RLock()
"""Serialises every load-modify-save of the store inside one process.

``Scheduler`` is load-on-construct and every mutator rewrites the whole file,
so two writers in one process — a ``/v1/schedules`` route on a worker thread
and the runner's ``mark_run`` on the event loop — could each load, then each
save, and the later save would drop the earlier record. Hold this lock around
*construct + mutate* (``with STORE_LOCK: Scheduler().update(...)``); the
routes and ``nvh/integrations/services/schedule_runner.py`` do. Re-entrant so
a holder may build a ``Scheduler`` (which loads) and then call a mutator.
Across processes the runner's heartbeat lock keeps one runner per store.
"""
# The retry nap, bound here so tests patch ``scheduler._sleep`` instead of the
# process-global ``time.sleep`` (CI runs the whole suite in one process).
_sleep = time.sleep


def _doc(sentence: str) -> dict[str, str]:
    return {"doc": sentence}


@dataclass
class ScheduledTask:
    """One schedule record (``nvh-schedules/2``).

    The first nine fields are the pre-0.46 record and stay required; every
    later field has a default so an older file loads unchanged. Each field's
    ``metadata["doc"]`` is the sentence the generated ``schedule-record``
    block in docs/CONFIGURATION.md shows for it.
    """

    id: str = field(metadata=_doc(
        "Record id; new records get `uuid4().hex` (32 hex characters), legacy 8-character ids are kept verbatim."
    ))
    prompt: str = field(metadata=_doc("The prompt each run sends."))
    interval_seconds: int = field(metadata=_doc(
        "Seconds between runs; the CLI accepts `parse_interval` syntax such as `30m`, `1h` or `1d`."
    ))
    advisor: str = field(metadata=_doc("Provider override for the run; empty means normal routing."))
    mode: str = field(metadata=_doc(
        "`ask`, `convene` or `do`; `do` runs tools unattended and is accepted by the CLI runner only."
    ))
    last_run: str = field(metadata=_doc("ISO-8601 UTC timestamp of the last run; empty before the first run."))
    next_run: float = field(metadata=_doc("Epoch seconds at which the schedule is next due."))
    enabled: bool = field(metadata=_doc("Whether the schedule can become due; disabled schedules never run."))
    created_at: str = field(metadata=_doc("ISO-8601 UTC timestamp of when the schedule was added."))
    name: str = field(default="", metadata=_doc("Display label shown beside the prompt."))
    profile: str = field(default="", metadata=_doc(
        "Agent profile applied to each run; its `max_cost_usd_per_turn` caps the run and, on an unattended `ask` run, its tool whitelist is intersected with `SCHEDULE_AUTO_TOOLS` (`nvh/integrations/wizard/chat.py`), the read-only roster a scheduled turn may call."
    ))
    conversation_id: str = field(default="", metadata=_doc("The one conversation every run of this schedule appends to."))
    updated_at: str = field(default="", metadata=_doc(
        "ISO-8601 UTC timestamp of the last field change or recorded run; empty for records written before 0.46."
    ))
    last_status: str = field(default="", metadata=_doc(
        "Outcome of the last run: `ok`, `error`, `skipped` or `deferred`; empty before the first run."
    ))
    last_error: str = field(default="", metadata=_doc(
        "Error text of the last run, truncated to 300 characters; empty when the last run did not fail."
    ))
    last_job_id: str = field(default="", metadata=_doc("Id of the last run's job under `$NVH_HOME/jobs/`."))
    last_cost_usd: float | None = field(default=None, metadata=_doc(
        "Cost of the last run in USD; `null` means never run or no figure, and the UI shows nothing for it."
    ))
    last_cost_known: bool | None = field(default=None, metadata=_doc(
        "Whether `last_cost_usd` is a trusted figure; `null` together with a `null` cost on a never-run schedule."
    ))
    run_count: int = field(default=0, metadata=_doc("Number of runs recorded, whatever their status."))
    extra: dict[str, Any] = field(default_factory=dict, metadata=_doc("Unknown keys, round-tripped."))

    def to_dict(self) -> dict[str, Any]:
        """The record as written to ``schedules.json`` (every field, ``extra`` as a nested object)."""
        record = {f.name: getattr(self, f.name) for f in _FIELDS}
        record["extra"] = dict(self.extra)
        return record

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "ScheduledTask":
        """Build a record from a JSON object: missing keys defaulted, unknown keys kept in ``extra``.

        A record with no ``id`` gets :func:`derived_schedule_id`, the same
        value on every load until a save writes it, so ``nvh schedule list``
        and ``nvh schedule remove <id>`` (two processes) agree; an ``id`` that
        is not a string is coerced so the CLI's string argument can match it.
        A record missing its ``prompt`` or ``interval_seconds`` — half-written
        by hand, or by a tool that dropped the key — loads ``enabled=False``:
        visible and editable, never due, where the pre-0.46 loader threw the
        whole store away.
        """
        known: dict[str, Any] = {}
        extra: dict[str, Any] = {}
        for key, value in raw.items():
            if key == "extra":
                if isinstance(value, dict):
                    extra.update(value)
                else:
                    extra["extra"] = value
            elif key in _FIELD_NAMES:
                known[key] = value
            else:
                extra[key] = value
        if "id" in known and not isinstance(known["id"], str):
            known["id"] = "" if known["id"] is None else str(known["id"])
        if not known.get("id"):
            known["id"] = derived_schedule_id(raw)
        runnable = "prompt" in known and "interval_seconds" in known
        for f in _FIELDS:
            if f.name == "extra" or f.name in known:
                continue
            if f.default is not MISSING:
                known[f.name] = f.default
            elif f.default_factory is not MISSING:
                known[f.name] = f.default_factory()
            else:
                known[f.name] = _REQUIRED_FALLBACKS[f.name]
        if not runnable:
            known["enabled"] = False
        known["extra"] = extra
        return cls(**known)


_FIELDS = fields(ScheduledTask)
_FIELD_NAMES = frozenset(f.name for f in _FIELDS)

# Values used when a pre-0.46 record lacks one of the nine required keys
# (``id`` is derived, see ``derived_schedule_id``). ``next_run`` 0.0 makes the
# record due at once, as the legacy migration seed did; ``interval_seconds``
# falls back to one day so a record with no interval cannot run on every poll
# — and ``from_dict`` disables a record that lacked ``prompt`` or
# ``interval_seconds`` altogether, so the fallbacks describe it without
# running it.
_REQUIRED_FALLBACKS: dict[str, Any] = {
    "prompt": "",
    "interval_seconds": 86400,
    "advisor": "",
    "mode": "ask",
    "last_run": "",
    "next_run": 0.0,
    "enabled": True,
    "created_at": "",
}

_PROTECTED_FIELDS = frozenset({"id", "extra", "created_at", "updated_at"})

# ``update()`` coerces values to these before touching the record.
_INT_FIELDS = frozenset({"interval_seconds", "run_count"})
_FLOAT_FIELDS = frozenset({"next_run"})
_TRUE_WORDS = frozenset({"true", "1", "yes", "on"})
_FALSE_WORDS = frozenset({"false", "0", "no", "off"})


def derived_schedule_id(raw: dict[str, Any]) -> str:
    """The id a record without one reads as: ``sha1`` of its JSON, 32 hex characters.

    Deterministic, so every process that loads the same file sees the same
    id (``uuid4`` here made each ``nvh schedule list`` print a different id
    and ``remove`` never match). The first save persists it as ``id``.
    """
    payload = json.dumps(raw, sort_keys=True, default=str, ensure_ascii=True)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:32]


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _coerce_update(name: str, value: Any) -> Any:
    """``value`` as the type ``ScheduledTask.<name>`` holds, or ``ValueError``.

    Request bodies and CLI flags arrive as strings. ``int()``/``float()``
    spellings and the words ``true``/``false`` are accepted; anything else —
    ``interval_seconds="1h"`` is :func:`parse_interval`'s job, not this one —
    is refused before the record changes, so a wrong-typed value never
    reaches memory or disk.
    """
    if name == "enabled":
        if isinstance(value, bool):
            return value
        if isinstance(value, int) and value in (0, 1):
            return bool(value)
        if isinstance(value, str) and value.strip().lower() in _TRUE_WORDS | _FALSE_WORDS:
            return value.strip().lower() in _TRUE_WORDS
        raise ValueError(f"enabled must be true or false, got {value!r}")
    if name in _INT_FIELDS or name in _FLOAT_FIELDS:
        cast = int if name in _INT_FIELDS else float
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            raise ValueError(f"{name} must be a number, got {value!r}")
        try:
            out = cast(value.strip()) if isinstance(value, str) else cast(value)
        except (TypeError, ValueError):
            raise ValueError(f"{name} must be a number, got {value!r}") from None
        if name == "interval_seconds" and out <= 0:
            raise ValueError(f"interval_seconds must be positive, got {value!r}")
        return out
    if name == "last_cost_usd":
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            raise ValueError(f"last_cost_usd must be a number or null, got {value!r}")
        try:
            return float(value)
        except (TypeError, ValueError):
            raise ValueError(f"last_cost_usd must be a number or null, got {value!r}") from None
    if name == "last_cost_known":
        if value is None or isinstance(value, bool):
            return value
        raise ValueError(f"last_cost_known must be true, false or null, got {value!r}")
    if name == "last_status":
        if value not in RUN_STATUSES and value != "":
            raise ValueError(f"Unknown run status {value!r}; expected one of {sorted(RUN_STATUSES)}")
        return value
    # text fields: prompt, advisor, mode, last_run, name, profile, conversation_id, last_error, last_job_id
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string, got {type(value).__name__}")
    return value


def default_schedule_file() -> Path:
    """``$NVH_STATE/schedules.json`` from the one path oracle."""
    from nvh.integrations.workspace.storage import storage_layout

    return storage_layout().state_dir / "schedules.json"


def _replace_with_retry(src: Path, dst: Path) -> None:
    """``os.replace`` with a short bounded retry on ``PermissionError``.

    On Windows the destination cannot be replaced while another process (the
    CLI listing schedules while the API saves) holds it open; the sharing
    violation surfaces as ``PermissionError`` and clears within milliseconds.
    """
    for attempt in range(_SAVE_RETRIES):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt == _SAVE_RETRIES - 1:
                raise
            _sleep(_SAVE_RETRY_DELAY_S)


_FILE_LOCK_TIMEOUT_S = 10.0
_FILE_LOCK_STALE_S = 30.0
_FILE_LOCK_POLL_S = 0.02


@contextlib.contextmanager
def _file_lock(path: Path) -> Iterator[None]:
    """Hold ``<store>.lock`` (an ``O_CREAT|O_EXCL`` file) across a read-modify-write.

    Portable (Windows and POSIX): creating the file is the acquire, removing it
    the release. A lock file older than ``_FILE_LOCK_STALE_S`` belongs to a
    process that died holding it and is broken; after ``_FILE_LOCK_TIMEOUT_S``
    of waiting the write proceeds unlocked with a warning rather than failing,
    since the atomic replace still keeps the file whole.
    """
    lock = path.with_name(path.name + ".lock")
    deadline = time.monotonic() + _FILE_LOCK_TIMEOUT_S
    held = False
    while True:
        try:
            fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                if time.time() - lock.stat().st_mtime > _FILE_LOCK_STALE_S:
                    lock.unlink(missing_ok=True)
                    continue
            except OSError:
                continue  # released between the failed create and the stat
            if time.monotonic() >= deadline:
                logger.warning("Could not take %s within %.0fs; saving without it", lock, _FILE_LOCK_TIMEOUT_S)
                break
            _sleep(_FILE_LOCK_POLL_S)
        except PermissionError:
            # Windows: the file is mid-delete by its holder.
            if time.monotonic() >= deadline:
                break
            _sleep(_FILE_LOCK_POLL_S)
        else:
            os.close(fd)
            held = True
            break
    try:
        yield
    finally:
        if held:
            try:
                lock.unlink(missing_ok=True)
            except OSError:
                pass


class Scheduler:
    def __init__(self, schedule_file: Path | None = None):
        self.schedule_file = schedule_file or default_schedule_file()
        self.schedule_file.parent.mkdir(parents=True, exist_ok=True)
        self._tasks: list[ScheduledTask] = []
        self._known_ids: set[str] = set()
        self._load()

    # -- persistence --------------------------------------------------------

    def _load(self) -> None:
        """Read the store: a v2 wrapper or a pre-0.46 bare array.

        Never writes a record (a legacy file is rewritten only by the next
        save). The one filesystem change a load can make is the quarantine
        rename of a file it cannot parse (:meth:`_quarantine`), so even the
        read-only callers — ``nvh schedule list``, the ``scheduler`` report
        check — move a corrupt store aside rather than reading past it.
        """
        self._tasks = []
        if not self.schedule_file.exists():
            return
        try:
            raw = self.schedule_file.read_text(encoding="utf-8")
        except OSError as exc:
            logger.warning("Could not read %s (%s); starting with no schedules", self.schedule_file, exc)
            return
        if not raw.strip():
            return
        try:
            data = json.loads(raw)
        except ValueError:
            self._quarantine("invalid JSON")
            return
        if isinstance(data, dict):
            records = data.get("schedules")
            if not isinstance(records, list):
                self._quarantine("no 'schedules' list in the object")
                return
        elif isinstance(data, list):
            records = data
        else:
            self._quarantine(f"top-level value is {type(data).__name__}, not a list or object")
            return
        tasks: list[ScheduledTask] = []
        for index, item in enumerate(records):
            if not isinstance(item, dict):
                logger.warning("Skipping schedules.json record %d: not an object", index)
                continue
            tasks.append(ScheduledTask.from_dict(item))
        self._tasks = tasks
        self._known_ids = {t.id for t in tasks}

    def _disk_tasks(self) -> list[ScheduledTask]:
        """The records now in the file, read without side effects; ``[]`` when it cannot be parsed."""
        try:
            data = json.loads(self.schedule_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        records = data.get("schedules") if isinstance(data, dict) else data
        if not isinstance(records, list):
            return []
        return [ScheduledTask.from_dict(item) for item in records if isinstance(item, dict)]

    def _quarantine(self, reason: str) -> None:
        """Move an unparseable store aside once and log one line; the store starts empty."""
        stamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
        target = self.schedule_file.with_name(f"{self.schedule_file.name}.corrupt-{stamp}")
        suffix = 0
        while target.exists():
            suffix += 1
            target = self.schedule_file.with_name(f"{self.schedule_file.name}.corrupt-{stamp}-{suffix}")
        try:
            os.replace(self.schedule_file, target)
        except OSError as exc:
            logger.warning(
                "Could not parse %s (%s) and could not move it aside (%s); starting with no schedules",
                self.schedule_file, reason, exc,
            )
            return
        logger.warning(
            "Could not parse %s (%s); moved it to %s and starting with no schedules",
            self.schedule_file, reason, target.name,
        )

    def _save(self) -> None:
        """Write the v2 wrapper atomically: a private ``schedules.json.<pid>-<token>.tmp`` then ``os.replace``.

        The temp name carries this process's pid and a random token, so two
        writers saving at once (the CLI and the API, or two CLIs) never share
        a temp file — a shared fixed name let one writer's ``os.replace``
        install the other's payload and made the second raise
        ``FileNotFoundError``. The temp file is removed on every failure path,
        the write included, so no ``.tmp`` lingers and the destination is
        never partial. Atomic replacement protects the *file*, not the
        *records*: a load-modify-save that interleaves with another writer's
        still drops a record. Inside one process the API routes and the
        schedule runner hold :data:`STORE_LOCK` around construct + mutate;
        across processes the runner's heartbeat lock (``state/scheduler.lock``)
        keeps one runner per store, and a CLI edit beside a live API runner is
        the one unserialised pair left.

        Since 0.47 that pair is serialised too: the read-modify-write holds
        ``<store>.lock`` (:func:`_file_lock`) and, under it, folds in any record
        another process saved since this instance loaded (an id neither held
        here nor seen at load), so a CLI ``nvh schedule add`` racing the API
        runner keeps both records. A record this instance removed stays removed.
        """
        self.schedule_file.parent.mkdir(parents=True, exist_ok=True)
        with _file_lock(self.schedule_file):
            mine = {t.id for t in self._tasks}
            for other in self._disk_tasks():
                if other.id not in mine and other.id not in self._known_ids:
                    self._tasks.append(other)
            payload = {
                "schema": SCHEDULE_SCHEMA,
                "updated_at": _now_iso(),
                "schedules": [t.to_dict() for t in self._tasks],
            }
            text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
            token = f"{os.getpid()}-{uuid.uuid4().hex[:8]}"
            tmp = self.schedule_file.with_name(f"{self.schedule_file.name}.{token}.tmp")
            try:
                tmp.write_text(text, encoding="utf-8")
                _replace_with_retry(tmp, self.schedule_file)
            finally:
                try:
                    tmp.unlink(missing_ok=True)
                except OSError:
                    pass
            self._known_ids = {t.id for t in self._tasks}

    # -- records ------------------------------------------------------------

    def add(
        self,
        prompt: str,
        interval_seconds: int,
        advisor: str = "",
        mode: str = "ask",
        *,
        name: str = "",
        profile: str = "",
    ) -> ScheduledTask:
        now_iso = _now_iso()
        task = ScheduledTask(
            id=uuid.uuid4().hex,
            prompt=prompt,
            interval_seconds=interval_seconds,
            advisor=advisor,
            mode=mode,
            last_run="",
            next_run=time.time(),
            enabled=True,
            created_at=now_iso,
            name=name,
            profile=profile,
            updated_at=now_iso,
        )
        self._tasks.append(task)
        self._save()
        return task

    def remove(self, task_id: str) -> bool:
        before = len(self._tasks)
        self._tasks = [t for t in self._tasks if t.id != task_id]
        if len(self._tasks) < before:
            self._save()
            return True
        return False

    def get(self, task_id: str) -> ScheduledTask | None:
        for t in self._tasks:
            if t.id == task_id:
                return t
        return None

    def list_tasks(self) -> list[ScheduledTask]:
        return list(self._tasks)

    def get_due_tasks(self) -> list[ScheduledTask]:
        now = time.time()
        return [t for t in self._tasks if t.enabled and t.next_run <= now]

    def set_enabled(self, task_id: str, enabled: bool) -> ScheduledTask | None:
        """Enable or disable one schedule; returns it, or ``None`` when the id is unknown."""
        task = self.get(task_id)
        if task is None:
            return None
        task.enabled = bool(enabled)
        task.updated_at = _now_iso()
        self._save()
        return task

    def update(self, task_id: str, **changes: Any) -> ScheduledTask | None:
        """Change record fields by name and stamp ``updated_at``.

        Only ``ScheduledTask`` fields other than ``id``, ``extra``,
        ``created_at`` and ``updated_at`` may be changed; any other name
        raises ``ValueError`` before anything is written. Values are coerced
        to the field's type first (:func:`_coerce_update`: ``int`` seconds,
        ``bool`` for ``enabled``, ``str`` for the text fields) and a value
        that cannot be — ``interval_seconds="1h"``, ``enabled="maybe"`` —
        raises ``ValueError`` before the record changes in memory or on
        disk. A new ``interval_seconds`` pulls ``next_run`` in so the
        schedule never waits longer than one new interval. Returns the
        record, or ``None`` when the id is unknown.
        """
        unknown = sorted(k for k in changes if k not in _FIELD_NAMES or k in _PROTECTED_FIELDS)
        if unknown:
            raise ValueError(f"Cannot update schedule field(s): {', '.join(unknown)}")
        task = self.get(task_id)
        if task is None:
            return None
        if not changes:
            return task
        coerced = {key: _coerce_update(key, value) for key, value in changes.items()}
        new_interval = coerced.get("interval_seconds")
        for key, value in coerced.items():
            setattr(task, key, value)
        if new_interval is not None and "next_run" not in changes:
            task.next_run = min(task.next_run, time.time() + task.interval_seconds)
        task.updated_at = _now_iso()
        self._save()
        return task

    def mark_run(
        self,
        task_id: str,
        *,
        status: str,
        job_id: str = "",
        error: str = "",
        cost_usd: float | None = None,
        cost_known: bool | None = None,
        conversation_id: str = "",
    ) -> ScheduledTask | None:
        """Record one run: ``last_*`` fields, ``run_count`` and the next due time.

        ``status`` must be one of ``RUN_STATUSES``; ``error`` is cut to
        ``MAX_ERROR_CHARS``; ``cost_usd`` ``None`` means no figure. A non-empty
        ``conversation_id`` is stored as the schedule's thread. Returns the
        record, or ``None`` when the id is unknown (nothing is written).
        """
        if status not in RUN_STATUSES:
            raise ValueError(f"Unknown run status {status!r}; expected one of {sorted(RUN_STATUSES)}")
        task = self.get(task_id)
        if task is None:
            return None
        now_iso = _now_iso()
        task.last_run = now_iso
        task.next_run = time.time() + task.interval_seconds
        task.run_count += 1
        task.last_status = status
        task.last_error = (error or "")[:MAX_ERROR_CHARS]
        task.last_job_id = job_id or ""
        task.last_cost_usd = None if cost_usd is None else float(cost_usd)
        task.last_cost_known = None if cost_known is None else bool(cost_known)
        if conversation_id:
            task.conversation_id = conversation_id
        task.updated_at = now_iso
        self._save()
        return task

    def mark_completed(self, task_id: str) -> None:
        """Pre-0.46 spelling of ``mark_run(task_id, status="ok")``."""
        self.mark_run(task_id, status="ok")


def parse_interval(interval_str: str) -> int:
    """Parse interval string like '1h', '30m', '1d' to seconds."""
    match = re.match(r'^(\d+)(s|m|h|d)$', interval_str.strip())
    if not match:
        raise ValueError(f"Invalid interval: {interval_str}. Use format: 30m, 1h, 1d")
    value, unit = int(match.group(1)), match.group(2)
    multipliers = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    return value * multipliers[unit]
