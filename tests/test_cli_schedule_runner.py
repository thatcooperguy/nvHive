"""`nvh schedule start --once` records each run's outcome on the schedule.

Before 0.46 both branches of the runner called ``mark_completed``, so a run
that raised was persisted exactly like one that answered. The runner now
calls ``Scheduler.mark_run(status="ok")`` after an answer and
``mark_run(status="error", error=...)`` when the run raises, which is what
``nvh schedule list`` and the generated ``schedule-record`` block in
docs/CONFIGURATION.md describe. The engine and the notifier are replaced
in-process; no network, no model.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

import nvh.cli.main as cli_main
from nvh.core.scheduler import Scheduler


class _FakeEngine:
    """Stands in for ``nvh.core.engine.Engine`` inside the runner."""

    behaviour = "answer"

    def __init__(self, config=None):
        self.config = config

    async def initialize(self):
        return None

    async def query(self, prompt, provider=None):
        if type(self).behaviour == "raise":
            raise RuntimeError("provider exploded")
        return SimpleNamespace(content=f"answer to: {prompt}")


@pytest.fixture()
def runner_env(monkeypatch):
    """A due schedule, a fake engine and a silent notifier."""
    notified = []

    async def _notify(task_name, result_preview, cost=""):
        notified.append((task_name, result_preview))

    monkeypatch.setattr("nvh.core.engine.Engine", _FakeEngine)
    monkeypatch.setattr("nvh.core.notify.notify_task_complete", _notify)
    monkeypatch.setattr("nvh.config.settings.load_config", lambda *a, **k: {})
    task = Scheduler().add("what changed today?", 3600, mode="ask")
    assert Scheduler().get_due_tasks(), "a freshly added schedule is due at once"
    return SimpleNamespace(task=task, notified=notified)


def _run_once() -> str:
    result = CliRunner().invoke(cli_main.app, ["schedule", "start", "--once"])
    assert result.exit_code == 0, result.output
    return result.output


def test_a_run_that_answers_is_recorded_as_ok(runner_env):
    _FakeEngine.behaviour = "answer"
    out = _run_once()
    assert "Done:" in out

    task = Scheduler().get(runner_env.task.id)
    assert task is not None
    assert task.last_status == "ok"
    assert task.last_error == ""
    assert task.run_count == 1
    assert task.last_run != ""
    assert runner_env.notified and "answer to:" in runner_env.notified[0][1]


def test_a_run_that_raises_is_recorded_as_error_with_the_message(runner_env):
    _FakeEngine.behaviour = "raise"
    try:
        out = _run_once()
    finally:
        _FakeEngine.behaviour = "answer"
    assert "Error: provider exploded" in out

    task = Scheduler().get(runner_env.task.id)
    assert task is not None
    assert task.last_status == "error"
    assert "provider exploded" in task.last_error
    assert task.run_count == 1
    # the schedule stays enabled and is rescheduled, not dropped
    assert task.enabled is True
    assert task.next_run > 0
    assert runner_env.notified and runner_env.notified[0][1].startswith("Error:")


def test_mark_completed_is_still_the_ok_alias():
    """Pre-0.46 callers keep working: ``mark_completed`` is ``mark_run(status="ok")``."""
    sched = Scheduler()
    task = sched.add("alias check", 60)
    sched.mark_completed(task.id)
    again = Scheduler().get(task.id)
    assert again is not None and again.last_status == "ok" and again.run_count == 1
