"""NVHive Scheduler — run tasks on a schedule.

Stores scheduled tasks in ~/.hive/schedules.json.
Uses a simple polling loop (no system cron needed — works without root).

Examples:
  nvh schedule add "Summarize my emails" --every 1h
  nvh schedule add "Check server status" --every 30m --advisor groq
  nvh schedule list
  nvh schedule remove <id>
  nvh schedule start   # start the scheduler daemon
"""

import json
import re
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path


@dataclass
class ScheduledTask:
    id: str
    prompt: str
    interval_seconds: int
    advisor: str
    mode: str               # ask, convene, do
    last_run: str
    next_run: float
    enabled: bool
    created_at: str

class Scheduler:
    def __init__(self, schedule_file: Path | None = None):
        self.schedule_file = schedule_file or (Path.home() / ".hive" / "schedules.json")
        self.schedule_file.parent.mkdir(parents=True, exist_ok=True)
        self._tasks: list[ScheduledTask] = []
        self._load()

    def _load(self):
        if self.schedule_file.exists():
            try:
                data = json.loads(self.schedule_file.read_text())
                self._tasks = [ScheduledTask(**t) for t in data]
            except Exception:
                self._tasks = []

    def _save(self):
        data = [
            {"id": t.id, "prompt": t.prompt, "interval_seconds": t.interval_seconds,
             "advisor": t.advisor, "mode": t.mode, "last_run": t.last_run,
             "next_run": t.next_run, "enabled": t.enabled, "created_at": t.created_at}
            for t in self._tasks
        ]
        self.schedule_file.write_text(json.dumps(data, indent=2))

    def add(self, prompt: str, interval_seconds: int, advisor: str = "", mode: str = "ask") -> ScheduledTask:
        task = ScheduledTask(
            id=str(uuid.uuid4())[:8],
            prompt=prompt,
            interval_seconds=interval_seconds,
            advisor=advisor,
            mode=mode,
            last_run="",
            next_run=time.time(),
            enabled=True,
            created_at=datetime.now(UTC).isoformat(),
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

    def list_tasks(self) -> list[ScheduledTask]:
        return list(self._tasks)

    def get_due_tasks(self) -> list[ScheduledTask]:
        now = time.time()
        return [t for t in self._tasks if t.enabled and t.next_run <= now]

    def mark_completed(self, task_id: str):
        for t in self._tasks:
            if t.id == task_id:
                t.last_run = datetime.now(UTC).isoformat()
                t.next_run = time.time() + t.interval_seconds
                break
        self._save()


def parse_interval(interval_str: str) -> int:
    """Parse interval string like '1h', '30m', '1d' to seconds."""
    match = re.match(r'^(\d+)(s|m|h|d)$', interval_str.strip())
    if not match:
        raise ValueError(f"Invalid interval: {interval_str}. Use format: 30m, 1h, 1d")
    value, unit = int(match.group(1)), match.group(2)
    multipliers = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    return value * multipliers[unit]
