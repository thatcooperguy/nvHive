"""`nvh schedule start --once` records each run's outcome on the schedule.

Before 0.46 both branches of the runner called ``mark_completed``, so a run
that raised was persisted exactly like one that answered. Since Wave B the
CLI drives the shared runner (``nvh/integrations/services/schedule_runner.py``):
an ``ask`` schedule is an unattended Wizard turn recorded as a ``schedule-run``
job, ``mark_run(status="ok")`` follows an answer and
``mark_run(status="error", error=...)`` a raise, which is what
``nvh schedule list`` and the generated ``schedule-record`` block in
docs/CONFIGURATION.md describe. The engine, the Wizard turn and the notifier
are replaced in-process; no network, no model.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

import nvh.cli.main as cli_main
from nvh.core.scheduler import Scheduler
from nvh.integrations.services import jobs


class _FakeEngine:
    """Stands in for ``nvh.core.engine.Engine`` inside the runner."""

    behaviour = "answer"

    def __init__(self, config=None):
        self.config = config

    async def initialize(self):
        return None

    async def _check_budget(self):
        return None


async def _fake_wizard_chat(question, **kwargs):
    """The unattended Wizard turn the runner awaits: answers, or explodes like a provider."""
    _fake_wizard_chat.calls.append({"question": question, **kwargs})
    if _FakeEngine.behaviour == "raise":
        raise RuntimeError("provider exploded")
    return {
        "answer": f"answer to: {question}", "mode": "llm", "used_provider": "ollama",
        "used_model": "ollama/fake", "cost_usd": 0.0, "cost_known": True, "latency_ms": 3,
        "tool_calls": [], "tool_results": [], "deferred_tool_calls": [], "iterations": 1,
        "used_profile": None, "profile_reason": "",
    }


_fake_wizard_chat.calls = []  # type: ignore[attr-defined]


@pytest.fixture()
def runner_env(monkeypatch):
    """A due schedule, a fake engine, a fake Wizard turn and a silent notifier."""
    notified = []
    _fake_wizard_chat.calls.clear()
    jobs._TASKS.clear()

    async def _notify(task_name, result_preview, cost=""):
        notified.append((task_name, result_preview))

    monkeypatch.setattr("nvh.core.engine.Engine", _FakeEngine)
    monkeypatch.setattr("nvh.core.notify.notify_task_complete", _notify)
    monkeypatch.setattr("nvh.config.settings.load_config", lambda *a, **k: {})
    monkeypatch.setattr("nvh.integrations.wizard.chat.wizard_chat", _fake_wizard_chat)
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
    # The CLI runs the same unattended turn the API runner does.
    assert _fake_wizard_chat.calls and _fake_wizard_chat.calls[0]["unattended"] is True
    # ... recorded as a schedule-run job the schedule points at.
    assert task.last_job_id and jobs.load_job(task.last_job_id)["kind"] == "schedule-run"
    assert jobs.load_job(task.last_job_id)["status"] == "complete"


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
    assert jobs.load_job(task.last_job_id)["status"] == "failed"


def test_once_with_nothing_due_says_so(runner_env):
    Scheduler().set_enabled(runner_env.task.id, False)
    out = _run_once()
    assert "No due tasks" in out
    assert _fake_wizard_chat.calls == []


def test_start_defers_to_a_live_runner(runner_env, monkeypatch):
    """A live heartbeat in ``state/scheduler.lock`` means another process runs schedules: exit 0, run nothing."""
    from nvh.integrations.services import schedule_runner as runner

    lock = runner.ScheduleLock(runner.lock_path(), poll_s=60, runner=runner.RUNNER_API)
    assert lock.acquire()
    try:
        result = CliRunner().invoke(cli_main.app, ["schedule", "start"])
    finally:
        lock.release()
    assert result.exit_code == 0, result.output
    assert "the API server is running schedules; use `nvh schedule list`" in result.output
    assert _fake_wizard_chat.calls == []
    assert Scheduler().get(runner_env.task.id).run_count == 0


def test_schedule_run_runs_one_schedule_now(runner_env):
    result = CliRunner().invoke(cli_main.app, ["schedule", "run", runner_env.task.id])
    assert result.exit_code == 0, result.output
    assert "Done:" in result.output
    assert Scheduler().get(runner_env.task.id).run_count == 1

    missing = CliRunner().invoke(cli_main.app, ["schedule", "run", "nope"])
    assert missing.exit_code == 1 and "Task not found" in missing.output


def test_enable_disable_and_list_columns(runner_env, monkeypatch):
    monkeypatch.setenv("COLUMNS", "220")  # Rich wraps the table headers at 80 columns
    cli = CliRunner()
    assert cli.invoke(cli_main.app, ["schedule", "disable", runner_env.task.id]).exit_code == 0
    assert Scheduler().get(runner_env.task.id).enabled is False
    assert cli.invoke(cli_main.app, ["schedule", "enable", runner_env.task.id]).exit_code == 0
    assert Scheduler().get(runner_env.task.id).enabled is True

    added = cli.invoke(cli_main.app, [
        "schedule", "add", "digest", "--every", "1d", "--name", "Vault digest", "--profile", "vault-rag", "--mode", "do",
    ])
    assert added.exit_code == 0, added.output
    assert "Vault digest" in added.output and "Warning:" in added.output and "mode 'do'" in added.output
    listed = cli.invoke(cli_main.app, ["schedule", "list"])
    assert listed.exit_code == 0, listed.output
    for column in ("Name", "Status", "Last", "Last cost"):
        assert column in listed.output
    assert "Vault digest" in listed.output

    unknown = cli.invoke(cli_main.app, ["schedule", "add", "x", "--every", "1h", "--profile", "no-such-profile"])
    assert unknown.exit_code == 1 and "Unknown profile" in unknown.output


def test_list_shows_the_last_cost_pill_and_names_the_live_runner(runner_env, monkeypatch):
    """After a run the row carries the three-state pill's figure (a trusted $0 is ``$0.0000``, never
    ``unknown``) and the footer says who runs schedules: nobody (the hint), the API server, or
    another ``nvh schedule start``."""
    from nvh.integrations.services import schedule_runner as runner

    monkeypatch.setenv("COLUMNS", "220")
    cli = CliRunner()
    never = cli.invoke(cli_main.app, ["schedule", "add", "later", "--every", "1d", "--name", "Never ran"])
    assert never.exit_code == 0, never.output
    never_id = Scheduler().list_tasks()[-1].id
    assert cli.invoke(cli_main.app, ["schedule", "disable", never_id]).exit_code == 0  # stays never-run
    _run_once()
    listed = cli.invoke(cli_main.app, ["schedule", "list"])
    assert listed.exit_code == 0, listed.output
    ran_row = next(line for line in listed.output.splitlines() if "what changed today?" in line)
    never_row = next(line for line in listed.output.splitlines() if "Never ran" in line)
    assert "$0.0000" in ran_row and "ok" in ran_row and "unknown" not in ran_row
    assert "never" in never_row and "$" not in never_row and "unknown" not in never_row  # blank pill
    assert "Run 'nvh schedule start' to execute due tasks" in listed.output
    assert "is running schedules" not in listed.output

    lock = runner.ScheduleLock(runner.lock_path(), poll_s=60, runner=runner.RUNNER_API)
    assert lock.acquire()
    try:
        listed = cli.invoke(cli_main.app, ["schedule", "list"])
    finally:
        lock.release()
    assert f"the API server is running schedules (pid {lock.pid})" in listed.output
    assert "Run 'nvh schedule start'" not in listed.output

    cli_lock = runner.ScheduleLock(runner.lock_path(), poll_s=60, runner=runner.RUNNER_CLI)
    assert cli_lock.acquire()
    try:
        listed = cli.invoke(cli_main.app, ["schedule", "list"])
    finally:
        cli_lock.release()
    assert f"`nvh schedule start` is running schedules (pid {cli_lock.pid})" in listed.output

    # An untrusted figure reads ``unknown`` (or ``$x.xxxx?``), a trusted one never does.
    Scheduler().mark_run(runner_env.task.id, status="ok", cost_usd=0.0, cost_known=False)
    assert "unknown" in cli.invoke(cli_main.app, ["schedule", "list"]).output
    Scheduler().mark_run(runner_env.task.id, status="ok", cost_usd=0.0123, cost_known=False)
    assert "$0.0123?" in cli.invoke(cli_main.app, ["schedule", "list"]).output


def test_once_beside_a_live_api_runner_runs_only_do_schedules(runner_env, monkeypatch):
    """The API server runs ``ask``/``convene`` within its poll and never ``do``: ``--once`` beside its
    live lock runs the due ``do`` schedules only, so one schedule is never run from two processes."""
    from nvh.integrations.services import schedule_runner as runner

    loops = []

    async def fake_loop(**kwargs):
        loops.append(kwargs["task"])
        return SimpleNamespace(final_response="tidied", total_cost_usd=0.0, cost_known=True)

    monkeypatch.setattr("nvh.core.agent_loop.run_agent_loop", fake_loop)
    do_task = Scheduler().add("tidy the downloads folder", 3600, mode="do")
    lock = runner.ScheduleLock(runner.lock_path(), poll_s=60, runner=runner.RUNNER_API)
    assert lock.acquire()
    try:
        out = _run_once()
    finally:
        lock.release()
    assert "running only the due `do` schedules here" in out
    assert loops == ["tidy the downloads folder"] and _fake_wizard_chat.calls == []
    assert Scheduler().get(do_task.id).run_count == 1 and Scheduler().get(do_task.id).last_status == "ok"
    assert Scheduler().get(runner_env.task.id).run_count == 0, "the ask schedule is the API's"

    # Without the API, ``--once`` runs everything due (the ask schedule is still due).
    out = _run_once()
    assert "Done:" in out and _fake_wizard_chat.calls and Scheduler().get(runner_env.task.id).run_count == 1


def test_schedule_run_defers_an_ask_schedule_to_a_live_api_runner_but_runs_do(runner_env, monkeypatch):
    from nvh.integrations.services import schedule_runner as runner

    async def fake_loop(**kwargs):
        return SimpleNamespace(final_response="tidied", total_cost_usd=0.0, cost_known=True)

    monkeypatch.setattr("nvh.core.agent_loop.run_agent_loop", fake_loop)
    do_task = Scheduler().add("tidy", 3600, mode="do")
    lock = runner.ScheduleLock(runner.lock_path(), poll_s=60, runner=runner.RUNNER_API)
    assert lock.acquire()
    try:
        refused = CliRunner().invoke(cli_main.app, ["schedule", "run", runner_env.task.id])
        ran = CliRunner().invoke(cli_main.app, ["schedule", "run", do_task.id])
    finally:
        lock.release()
    assert refused.exit_code == 1, refused.output
    assert "the API server is running schedules" in refused.output and "Run now" in refused.output
    assert f"POST /v1/schedules/{runner_env.task.id}/run" in refused.output
    assert _fake_wizard_chat.calls == [] and Scheduler().get(runner_env.task.id).run_count == 0
    assert ran.exit_code == 0, ran.output
    assert "Done:" in ran.output and Scheduler().get(do_task.id).run_count == 1


def test_mark_completed_is_still_the_ok_alias():
    """Pre-0.46 callers keep working: ``mark_completed`` is ``mark_run(status="ok")``."""
    sched = Scheduler()
    task = sched.add("alias check", 60)
    sched.mark_completed(task.id)
    again = Scheduler().get(task.id)
    assert again is not None and again.last_status == "ok" and again.run_count == 1
