"""The schedule runner (nvh/integrations/services/schedule_runner.py) and ``/v1/schedules``.

Design 0.46 §6: a run is a ``schedule-run`` job, ``ask`` is an unattended
Wizard turn that never mints an approval token, ``do`` is refused by the API
runner, the heartbeat lock is stale by age only, and a schedule keeps its last
twenty runs. ``wizard_chat`` is replaced in most tests; one test drives the
real unattended turn against a fake registry and a fake engine, so the token
and ``ask_user`` guarantees are checked end to end. Every file lands in the
hermetic home of tests/conftest.py; ``jobs._TASKS`` is cleared per test. No
network, no model, no real engine.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from nvh.core.engine import BudgetExceededError
from nvh.core.scheduler import Scheduler
from nvh.integrations.services import jobs
from nvh.integrations.services import schedule_runner as runner

_EMPTY_SNAPSHOT = {
    "gpu": {"detected": False}, "storage": {"available": False}, "providers": [],
    "ollama_models": [], "recent_jobs": [], "receipts": {}, "vault": {},
}


@pytest.fixture(autouse=True)
def _clear_job_tasks():
    jobs._TASKS.clear()
    yield
    jobs._TASKS.clear()


def _engine(**overrides: Any) -> SimpleNamespace:
    engine = SimpleNamespace(_check_budget=AsyncMock(), run_council=AsyncMock(), initialize=AsyncMock())
    for key, value in overrides.items():
        setattr(engine, key, value)
    return engine


def _factory(engine: Any):
    async def factory():
        return engine

    return factory


def _envelope(question: str = "", **over: Any) -> dict[str, Any]:
    base = {
        "answer": f"answer to: {question}", "mode": "llm", "used_provider": "ollama", "used_model": "ollama/fake",
        "cost_usd": 0.0, "cost_known": True, "latency_ms": 7, "tool_calls": [], "tool_results": [],
        "deferred_tool_calls": [], "iterations": 1, "used_profile": None, "profile_reason": "",
    }
    base.update(over)
    return base


def _patch_wizard(monkeypatch, *, result: dict[str, Any] | None = None, error: Exception | None = None):
    calls: list[dict[str, Any]] = []

    async def fake(question, **kwargs):
        calls.append({"question": question, **kwargs})
        if error is not None:
            raise error
        return dict(result or _envelope(question))

    monkeypatch.setattr("nvh.integrations.wizard.chat.wizard_chat", fake)
    return calls


def _due(prompt: str = "what changed today?", **kw: Any):
    return Scheduler().add(prompt, kw.pop("every", 3600), **kw)


async def _run(task, **kw: Any) -> dict[str, Any]:
    job_id = await runner.run_schedule_once(task, **kw)
    return await runner.wait_for_run(job_id)


def _complete_event(job: dict[str, Any]) -> dict[str, Any]:
    events = jobs.read_events(job["id"])
    assert events and events[-1]["event"] in {"complete", "error"}, events
    return events[-1]


def _jobs_text() -> str:
    """Every byte under ``$NVH_HOME/jobs/`` as text — what ``GET /v1/jobs/{id}/events`` could serve."""
    return "\n".join(p.read_text(encoding="utf-8") for p in jobs.jobs_root().iterdir() if p.is_file())


# ---------------------------------------------------------------------------
# One run
# ---------------------------------------------------------------------------


async def test_due_task_becomes_one_schedule_run_job_with_one_conversation(monkeypatch, db) -> None:
    """A run is one ``schedule-run`` job whose ``complete`` payload carries ``cost_known``; the schedule
    gets its outcome and one conversation that every later run appends to."""
    from nvh.storage import repository as repo

    calls = _patch_wizard(monkeypatch)
    engine = _engine()
    task = _due(name="Daily check")

    job = await _run(task, engine_factory=_factory(engine))
    assert job["kind"] == runner.SCHEDULE_RUN_KIND and job["status"] == "complete"
    assert job["title"] == "Daily check"
    assert job["request"]["schedule_id"] == task.id and job["request"]["mode"] == "ask"
    payload = _complete_event(job)["payload"]
    assert payload["outcome"] == "ok" and payload["cost_known"] is True and payload["cost_usd"] == 0.0
    assert payload["answer"] == "answer to: what changed today?" and payload["deferred"] == []
    assert payload["provider"] == "ollama" and payload["model"] == "ollama/fake" and payload["latency_ms"] == 7
    engine._check_budget.assert_awaited_once()

    stored = Scheduler().get(task.id)
    assert stored.last_status == "ok" and stored.last_job_id == job["id"] and stored.run_count == 1
    assert stored.last_cost_usd == 0.0 and stored.last_cost_known is True and stored.last_error == ""
    assert stored.conversation_id and payload["conversation_id"] == stored.conversation_id
    assert job["request"]["conversation_id"] == stored.conversation_id
    conv = await repo.get_conversation(stored.conversation_id)
    assert conv is not None and conv.mode == "wizard" and conv.title == "Daily check"

    # The unattended turn, in the schedule's own thread; no advisor, so no provider pin.
    assert calls == [{
        "question": "what changed today?", "profile": None, "provider": None,
        "conversation_id": stored.conversation_id, "unattended": True,
    }]

    second = await _run(Scheduler().get(task.id), engine_factory=_factory(engine))
    assert second["id"] != job["id"]
    assert calls[1]["conversation_id"] == stored.conversation_id, "one conversation per schedule"
    assert Scheduler().get(task.id).run_count == 2
    assert len(jobs.list_jobs(kind=runner.SCHEDULE_RUN_KIND, schedule_id=task.id, limit=50)) == 2


async def test_profile_is_passed_and_a_surfaced_request_marks_the_run_deferred(monkeypatch) -> None:
    calls = _patch_wizard(monkeypatch, result=_envelope(
        "x", tool_calls=[{"name": "save_provider_key", "arguments": {"provider": "groq"}, "privileged": False, "unattended": True}],
        deferred_tool_calls=[{"name": "web_search", "arguments": {"q": "y"}, "reason": "max_iterations"}],
        used_profile="researcher",
    ))
    task = _due(profile="researcher")
    job = await _run(task, engine_factory=_factory(_engine()))
    assert calls[0]["profile"] == "researcher"
    payload = _complete_event(job)["payload"]
    assert payload["outcome"] == "deferred" and payload["used_profile"] == "researcher"
    assert payload["deferred"] == [
        {"name": "save_provider_key", "arguments": {"provider": "groq"}, "privileged": False, "unattended": True},
        {"name": "web_search", "arguments": {"q": "y"}, "reason": "max_iterations"},
    ]
    assert Scheduler().get(task.id).last_status == "deferred"


async def test_a_real_unattended_turn_never_writes_a_token_or_offers_ask_user(monkeypatch) -> None:
    """End to end through ``wizard_chat(unattended=True)``: the model asks for a privileged change, a
    confirm-class save, a question and a daemon restart. Nothing under ``jobs/`` contains
    ``approval_token``, the recorded request has no ``plan``, and the tool schemas the model saw never
    included ``ask_user``."""
    from nvh.integrations.wizard import personality
    from nvh.integrations.wizard.tools import WizardTool, WizardToolRegistry
    from nvh.providers.base import CompletionResponse, FinishReason, Usage

    async def ok(args):
        return {"ran": True}

    async def planner(args):
        raise AssertionError("an unattended turn never plans")

    reg = WizardToolRegistry()
    for name in ("diagnose", "web_search", "refresh_models", "rag_ask_vault"):
        reg.register(WizardTool(name=name, description=name, safety_class="auto", parameters={}, handler=ok))
    reg.register(WizardTool(name="ask_user", description="Ask.", safety_class="confirm", parameters={"question": {}}, handler=ok))
    reg.register(WizardTool(name="save_provider_key", description="Save.", safety_class="confirm", parameters={}, handler=ok))
    reg.register(WizardTool(
        name="system_settings_apply", description="Apply.", safety_class="privileged",
        parameters={"setting": {}}, handler=ok, planner=planner,
    ))
    monkeypatch.setattr("nvh.integrations.wizard.tools.default_registry", lambda **kwargs: reg)

    shown: list[list[str]] = []
    real_build = personality.build_system_prompt

    def spy_build(context, tools=None, **kw):
        shown.append(sorted(t["name"] for t in (tools or [])))
        return real_build(context, tools=tools, **kw)

    monkeypatch.setattr("nvh.integrations.wizard.personality.build_system_prompt", spy_build)

    contents = [
        "Doing it all.\n"
        'TOOL_CALL: {"name": "system_settings_apply", "arguments": {"setting": "enable_ssh"}}\n'
        'TOOL_CALL: {"name": "save_provider_key", "arguments": {"provider": "groq", "api_key": "gsk_x"}}\n'
        'TOOL_CALL: {"name": "ask_user", "arguments": {"question": "Which disk?"}}\n'
        'TOOL_CALL: {"name": "refresh_models", "arguments": {}}\n'
        'TOOL_CALL: {"name": "diagnose", "arguments": {}}\n',
        "Done.",
    ]
    provider = MagicMock()
    provider.complete = AsyncMock(side_effect=[
        CompletionResponse(
            content=c, model="ollama/gemma3:4b", provider="ollama",
            usage=Usage(input_tokens=1, output_tokens=1, total_tokens=2),
            cost_usd=Decimal("0"), latency_ms=1, finish_reason=FinishReason.STOP,
        )
        for c in contents
    ])
    decision = MagicMock(provider="ollama", model="ollama/gemma3:4b")
    engine = MagicMock()
    engine.initialize = AsyncMock()
    engine._check_budget = AsyncMock()
    engine._log_query = AsyncMock()
    engine.router.route = MagicMock(return_value=decision)
    engine.registry.get = MagicMock(return_value=provider)
    engine.config.defaults.temperature = 0.7
    engine.config.defaults.max_tokens = 256
    monkeypatch.setattr("nvh.api.server.get_engine", lambda: engine)
    monkeypatch.setattr("nvh.integrations.wizard.context.wizard_context", lambda *a, **k: _EMPTY_SNAPSHOT)

    task = _due("nightly check")
    job = await _run(task, engine_factory=_factory(engine))

    assert job["status"] == "complete", job
    payload = _complete_event(job)["payload"]
    assert payload["outcome"] == "deferred"
    recorded = {d["name"]: d for d in payload["deferred"]}
    assert recorded["system_settings_apply"] == {
        "name": "system_settings_apply", "arguments": {"setting": "enable_ssh"}, "privileged": True, "unattended": True,
    }
    assert recorded["save_provider_key"]["unattended"] is True and "plan" not in recorded["save_provider_key"]
    assert "ask_user" not in recorded
    for entry in payload["deferred"]:
        assert "plan" not in entry and "approval_token" not in entry and "approval_expires_at" not in entry
    everything = _jobs_text()
    assert "approval_token" not in everything and "approval_expires_at" not in everything
    assert shown and all("ask_user" not in names for names in shown), shown
    assert shown[0] == ["diagnose", "rag_ask_vault", "web_search"]
    assert Scheduler().get(task.id).last_status == "deferred"


async def test_runner_strips_a_token_the_turn_leaks(monkeypatch) -> None:
    """Belt and braces: even an envelope that carries a token lands in ``jobs/`` without it."""
    _patch_wizard(monkeypatch, result=_envelope("x", tool_calls=[{
        "name": "system_settings_apply", "arguments": {"setting": "enable_ssh"}, "privileged": True,
        "approval_token": "tok-123", "approval_expires_at": "2099-01-01T00:00:00+00:00", "plan": {"commands": ["x"]},
    }]))
    job = await _run(_due(), engine_factory=_factory(_engine()))
    payload = _complete_event(job)["payload"]
    assert payload["deferred"] == [{
        "name": "system_settings_apply", "arguments": {"setting": "enable_ssh"}, "privileged": True,
        "plan": {"commands": ["x"]},
    }]
    assert "tok-123" not in _jobs_text() and "approval_token" not in _jobs_text()


async def test_do_is_refused_by_the_api_runner_and_runs_only_with_a_terminal(monkeypatch) -> None:
    calls = _patch_wizard(monkeypatch)
    engine = _engine()
    task = _due(mode="do")

    job = await _run(task, engine_factory=_factory(engine))
    assert job["status"] == "failed" and job["message"] == runner.DO_REFUSED
    event = _complete_event(job)
    assert event["event"] == "error" and event["payload"]["message"] == runner.DO_REFUSED
    stored = Scheduler().get(task.id)
    assert stored.last_status == "error" and stored.last_error == runner.DO_REFUSED and stored.run_count == 1
    assert calls == [] and engine._check_budget.await_count == 0
    assert runner.DO_REFUSED == (
        "mode 'do' runs tools unattended and cannot show an approval card; use mode ask "
        "(the Wizard runs safe tools on its own) or `nvh schedule start` in a terminal"
    )

    # The CLI runner has a terminal: ``allow_do`` runs the agent loop (patched) instead.
    loop_calls: list[dict[str, Any]] = []

    async def fake_loop(**kwargs):
        loop_calls.append(kwargs)
        return SimpleNamespace(final_response="agent did it", total_cost_usd=0.0, cost_known=True)

    monkeypatch.setattr("nvh.core.agent_loop.run_agent_loop", fake_loop)
    job = await _run(Scheduler().get(task.id), engine_factory=_factory(engine), allow_do=True)
    assert job["status"] == "complete" and _complete_event(job)["payload"]["answer"] == "agent did it"
    assert loop_calls and loop_calls[0]["task"] == task.prompt and loop_calls[0]["auto_approve_safe"] is True
    assert Scheduler().get(task.id).last_status == "ok"


async def test_convene_calls_run_council(monkeypatch) -> None:
    calls = _patch_wizard(monkeypatch)
    synthesis = SimpleNamespace(content="the council says hi", provider="groq", model="m")
    engine = _engine(run_council=AsyncMock(return_value=SimpleNamespace(
        synthesis=synthesis, total_cost_usd=Decimal("0.0123"), total_latency_ms=42, member_responses={},
    )))
    task = _due("standup", mode="convene", advisor="groq")
    job = await _run(task, engine_factory=_factory(engine))

    assert job["status"] == "complete"
    engine.run_council.assert_awaited_once()
    args, kwargs = engine.run_council.await_args
    assert args == ("standup",) and kwargs["members"] == ["groq"] and kwargs["conversation_id"]
    payload = _complete_event(job)["payload"]
    assert payload["answer"] == "the council says hi" and payload["provider"] == "groq"
    assert payload["cost_usd"] == pytest.approx(0.0123) and payload["latency_ms"] == 42
    assert calls == []
    stored = Scheduler().get(task.id)
    assert stored.last_status == "ok" and stored.last_cost_usd == pytest.approx(0.0123)


async def test_exception_records_error_and_advances_next_run(monkeypatch) -> None:
    _patch_wizard(monkeypatch, error=RuntimeError("provider exploded"))
    task = _due()
    before = Scheduler().get(task.id).next_run
    job = await _run(task, engine_factory=_factory(_engine()))

    assert job["status"] == "failed" and job["message"] == "provider exploded"
    stored = Scheduler().get(task.id)
    assert stored.last_status == "error" and stored.last_error == "provider exploded"
    assert stored.next_run > before and stored.enabled is True and stored.run_count == 1
    assert stored.last_cost_usd is None and stored.last_cost_known is None
    assert stored.last_job_id == job["id"]


async def test_budget_exceeded_is_recorded_as_skipped(monkeypatch) -> None:
    calls = _patch_wizard(monkeypatch)
    engine = _engine(_check_budget=AsyncMock(side_effect=BudgetExceededError("Daily budget limit reached")))
    task = _due()
    job = await _run(task, engine_factory=_factory(engine))

    assert job["status"] == "complete" and job["message"] == "Skipped: Daily budget limit reached"
    payload = _complete_event(job)["payload"]
    assert payload["outcome"] == "skipped" and payload["cost_known"] is None
    stored = Scheduler().get(task.id)
    assert stored.last_status == "skipped" and "Daily budget" in stored.last_error
    assert stored.run_count == 1 and calls == []


async def test_the_twenty_first_run_prunes_the_oldest_pair(monkeypatch) -> None:
    _patch_wizard(monkeypatch)
    engine = _engine()
    task = _due()
    other = _due("another schedule")
    other_job = await _run(other, engine_factory=_factory(engine))

    ids: list[str] = []
    for _ in range(runner.SCHEDULE_RUN_HISTORY + 1):
        ids.append((await _run(Scheduler().get(task.id), engine_factory=_factory(engine)))["id"])

    root = jobs.jobs_root()
    mine = jobs.list_jobs(kind=runner.SCHEDULE_RUN_KIND, schedule_id=task.id, limit=100)
    assert len(mine) == runner.SCHEDULE_RUN_HISTORY == 20
    oldest = sorted(ids)[0]
    assert not (root / f"{oldest}.json").exists() and not (root / f"{oldest}.jsonl").exists()
    assert {j["id"] for j in mine} == set(sorted(ids)[1:])
    assert (root / f"{other_job['id']}.json").exists(), "another schedule's runs are untouched"
    assert Scheduler().get(task.id).run_count == 21


def test_prune_counts_the_protected_run_and_skips_running_ones(monkeypatch) -> None:
    for i in range(4):
        job = jobs.create_job(kind=runner.SCHEDULE_RUN_KIND, title=f"r{i}", request={"schedule_id": "s1"})
        if i < 3:
            jobs.append_event(job["id"], {"event": "complete", "status": "complete", "message": "done"})
        else:
            jobs._TASKS[job["id"]] = MagicMock(done=lambda: False)  # still in flight
    done = jobs.list_jobs(kind=runner.SCHEDULE_RUN_KIND, schedule_id="s1", status="complete", limit=10)
    newest_done, middle, oldest = done
    # keep=2 counts the protected run: protected + the next newest stay, the oldest pair goes,
    # and the run still in flight is never a candidate.
    removed = runner.prune_schedule_runs("s1", keep=2, protect=newest_done["id"])
    assert removed == 1
    left = jobs.list_jobs(kind=runner.SCHEDULE_RUN_KIND, schedule_id="s1", limit=10)
    assert {j["id"] for j in left} == {newest_done["id"], middle["id"]} | {j for j in jobs._TASKS}
    assert sorted(j["status"] for j in left) == ["complete", "complete", "queued"]  # queued = in flight, never started
    root = jobs.jobs_root()
    assert not (root / f"{oldest['id']}.json").exists() and not (root / f"{oldest['id']}.jsonl").exists()
    assert runner.prune_schedule_runs("s1", keep=2, protect=newest_done["id"]) == 0, "idempotent"


# ---------------------------------------------------------------------------
# The lock
# ---------------------------------------------------------------------------


def _write_lock(path: Path, *, pid: int, age_s: float, poll_s: float = 60.0, kind: str = "api") -> dict[str, Any]:
    beat = (datetime.now(UTC) - timedelta(seconds=age_s)).isoformat()
    info = {"pid": pid, "started_at": beat, "heartbeat": beat, "poll_s": poll_s, "runner": kind}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(info), encoding="utf-8")
    return info


def _forbid_kill(monkeypatch) -> None:
    def boom(*args, **kwargs):
        raise AssertionError("the runner never probes a pid with os.kill")

    monkeypatch.setattr(os, "kill", boom)
    monkeypatch.setattr(sys, "platform", "win32")


def test_runner_status_shape_without_and_with_a_lock() -> None:
    status = runner.runner_status()
    assert status == {
        "active": False, "pid": None, "lock_path": str(runner.lock_path()), "heartbeat": None,
        "started_at": None, "stale": False, "runner": "", "poll_s": None, "unreadable": False,
    }
    assert Path(status["lock_path"]) == runner.lock_path()
    assert runner.lock_path().name == "scheduler.lock" and runner.lock_path().parent.name == "state"

    lock = runner.ScheduleLock(runner.lock_path(), poll_s=30, runner=runner.RUNNER_CLI)
    assert lock.acquire()
    try:
        status = runner.runner_status()
        assert status["active"] is True and status["pid"] == os.getpid() and status["runner"] == "cli"
        assert status["stale"] is False and status["heartbeat"] and status["poll_s"] == 30.0
        assert json.loads(runner.lock_path().read_text(encoding="utf-8"))["pid"] == os.getpid()
    finally:
        lock.release()
    assert not runner.lock_path().exists() and runner.runner_status()["active"] is False


async def test_second_holder_returns_at_once(monkeypatch, caplog) -> None:
    caplog.set_level(logging.INFO, logger="nvh.integrations.services.schedule_runner")
    _forbid_kill(monkeypatch)
    calls = _patch_wizard(monkeypatch)
    _due()
    holder = runner.ScheduleLock(runner.lock_path(), poll_s=60, runner=runner.RUNNER_API)
    assert holder.acquire()
    try:
        stop = asyncio.Event()
        ran = await asyncio.wait_for(runner.run_loop(stop, poll_s=0.01, runner=runner.RUNNER_CLI), timeout=5)
        assert ran is False
        assert calls == [], "the second runner ran nothing"
        assert json.loads(runner.lock_path().read_text(encoding="utf-8"))["runner"] == "api"
        assert any("not started" in r.getMessage() for r in caplog.records)
    finally:
        holder.release()


async def test_stale_heartbeat_is_taken_over_and_released(monkeypatch) -> None:
    _forbid_kill(monkeypatch)
    seen: dict[str, Any] = {}

    async def fake(question, **kwargs):
        seen["lock"] = json.loads(runner.lock_path().read_text(encoding="utf-8"))
        return _envelope(question)

    monkeypatch.setattr("nvh.integrations.wizard.chat.wizard_chat", fake)
    task = _due()
    _write_lock(runner.lock_path(), pid=424242, age_s=10 * 60, poll_s=60)  # 10 minutes > 3 x 60 s

    stop = asyncio.Event()
    asyncio.get_running_loop().call_later(0.2, stop.set)
    ran = await asyncio.wait_for(
        runner.run_loop(stop, poll_s=0.05, runner=runner.RUNNER_CLI, engine_factory=_factory(_engine())), timeout=10,
    )
    assert ran is True
    assert seen["lock"]["pid"] == os.getpid() and seen["lock"]["runner"] == "cli"
    assert seen["lock"]["poll_s"] == 0.05
    assert Scheduler().get(task.id).run_count >= 1
    assert not runner.lock_path().exists(), "released on exit"


async def test_live_heartbeat_with_a_dead_pid_is_not_taken_over(monkeypatch) -> None:
    """Heartbeat age is the only rule: a fresh beat from a pid that no longer exists still holds the lock,
    and nothing probes the pid (``os.kill(pid, 0)`` would terminate it on Windows)."""
    _forbid_kill(monkeypatch)
    calls = _patch_wizard(monkeypatch)
    _due()
    dead_pid = 2**22 + 7
    info = _write_lock(runner.lock_path(), pid=dead_pid, age_s=1, poll_s=60)
    before = runner.lock_path().read_bytes()

    ran = await asyncio.wait_for(runner.run_loop(asyncio.Event(), poll_s=0.01, runner=runner.RUNNER_CLI), timeout=5)
    assert ran is False and calls == []
    assert runner.lock_path().read_bytes() == before
    status = runner.runner_status()
    assert status["active"] is True and status["pid"] == dead_pid and status["stale"] is False
    assert status["heartbeat"] == info["heartbeat"]


def test_staleness_follows_the_holders_poll_interval() -> None:
    fresh = {"pid": 1, "heartbeat": (datetime.now(UTC) - timedelta(seconds=100)).isoformat(), "poll_s": 60}
    assert runner._is_stale(fresh) is False  # 100 s < 180 s
    assert runner._is_stale({**fresh, "poll_s": 10}) is True  # 100 s > 30 s
    assert runner._is_stale({"pid": 1}) is True  # no beat at all
    assert runner._is_stale({}) is True and runner._is_stale(None) is True
    assert runner.STALE_HEARTBEATS == 3


def test_release_leaves_a_takeovers_lock_alone() -> None:
    mine = runner.ScheduleLock(runner.lock_path(), poll_s=60)
    assert mine.acquire()
    _write_lock(runner.lock_path(), pid=777, age_s=0)  # someone else took over
    mine.release()
    assert runner.lock_path().exists() and json.loads(runner.lock_path().read_text(encoding="utf-8"))["pid"] == 777


# ---------------------------------------------------------------------------
# The roster the unattended turn runs under
# ---------------------------------------------------------------------------


def test_schedule_auto_tools_are_auto_class_and_exclude_dialogue_and_probe_invalidating_tools() -> None:
    from nvh.integrations.wizard.chat import (
        DIALOGUE_TOOLS,
        LOCAL_PROBE_INVALIDATING_TOOLS,
        SCHEDULE_AUTO_TOOLS,
    )
    from nvh.integrations.wizard.tools import default_registry

    registry = default_registry()
    classes = {t.name: t.safety_class for t in registry.list_tools()}
    assert SCHEDULE_AUTO_TOOLS and all(classes.get(name) == "auto" for name in SCHEDULE_AUTO_TOOLS), classes
    assert SCHEDULE_AUTO_TOOLS.isdisjoint(LOCAL_PROBE_INVALIDATING_TOOLS)
    assert SCHEDULE_AUTO_TOOLS.isdisjoint(DIALOGUE_TOOLS)
    assert "refresh_models" not in SCHEDULE_AUTO_TOOLS and "repair_workspace" not in SCHEDULE_AUTO_TOOLS


# ---------------------------------------------------------------------------
# /v1/schedules
# ---------------------------------------------------------------------------


@pytest.fixture()
def client(monkeypatch) -> TestClient:
    from nvh.api.routes.schedules import router

    monkeypatch.delenv("HIVE_API_KEY", raising=False)
    app = FastAPI()
    app.include_router(router)
    return TestClient(app, raise_server_exceptions=True)


def _data(resp) -> Any:
    assert resp.status_code in (200, 201, 202), (resp.status_code, resp.text)
    body = resp.json()
    assert body["status"] == "success"
    return body["data"]


def test_list_is_empty_with_the_runner_block(client: TestClient) -> None:
    from nvh.integrations.wizard.chat import SCHEDULE_AUTO_TOOLS

    data = _data(client.get("/v1/schedules"))
    assert data["schedules"] == [] and data["count"] == 0
    assert data["runner"]["active"] is False and data["runner"]["pid"] is None
    assert data["runner"]["tools"] == sorted(SCHEDULE_AUTO_TOOLS)
    assert data["runner"]["history"] == 20 and data["runner"]["lock_path"].endswith("scheduler.lock")


def test_create_read_patch_delete(client: TestClient) -> None:
    created = _data(client.post("/v1/schedules", json={
        "prompt": "  summarise the vault  ", "every": "1h", "name": " Vault  digest ", "profile": "vault-rag",
    }))
    assert created["prompt"] == "summarise the vault" and created["name"] == "Vault digest"
    assert created["interval_seconds"] == 3600 and created["mode"] == "ask" and created["enabled"] is True
    assert created["profile"] == "vault-rag" and len(created["id"]) == 32
    sid = created["id"]

    listed = _data(client.get("/v1/schedules"))
    assert listed["count"] == 1 and listed["schedules"][0]["id"] == sid
    assert _data(client.get(f"/v1/schedules/{sid}"))["id"] == sid

    patched = _data(client.patch(f"/v1/schedules/{sid}", json={"enabled": False, "every": "2h", "prompt": "digest", "advisor": "groq"}))
    assert patched["enabled"] is False and patched["interval_seconds"] == 7200
    assert patched["prompt"] == "digest" and patched["advisor"] == "groq"
    stored = Scheduler().get(sid)
    assert stored.enabled is False and stored.interval_seconds == 7200 and stored.updated_at

    assert client.patch(f"/v1/schedules/{sid}", json={"every": "soon"}).status_code == 422
    assert client.patch(f"/v1/schedules/{sid}", json={"enabled": "maybe"}).status_code == 422
    assert client.patch(f"/v1/schedules/{sid}", json={"profile": "no-such-profile"}).status_code == 422
    assert Scheduler().get(sid).interval_seconds == 7200, "a refused patch changes nothing"

    gone = _data(client.delete(f"/v1/schedules/{sid}"))
    assert gone == {"deleted": True, "id": sid}
    assert client.get(f"/v1/schedules/{sid}").status_code == 404
    assert client.delete(f"/v1/schedules/{sid}").status_code == 404
    assert client.patch(f"/v1/schedules/{sid}", json={"enabled": True}).status_code == 404


def test_create_refuses_do_bad_intervals_and_unknown_profiles(client: TestClient) -> None:
    refused = client.post("/v1/schedules", json={"prompt": "x", "every": "1h", "mode": "do"})
    assert refused.status_code == 422 and refused.json()["detail"] == runner.DO_REFUSED
    assert client.post("/v1/schedules", json={"prompt": "x", "every": "1h", "mode": "fly"}).status_code == 422
    assert client.post("/v1/schedules", json={"prompt": "x", "every": "tomorrow"}).status_code == 422
    assert client.post("/v1/schedules", json={"prompt": "x", "every": "1h", "profile": "nobody"}).status_code == 422
    assert client.post("/v1/schedules", json={"prompt": "", "every": "1h"}).status_code == 422
    assert client.post("/v1/schedules", json={"prompt": "x" * 4001, "every": "1h"}).status_code == 422
    assert Scheduler().list_tasks() == []

    convene = _data(client.post("/v1/schedules", json={"prompt": "x", "every": "30m", "mode": "convene", "enabled": False}))
    assert convene["mode"] == "convene" and convene["enabled"] is False and convene["interval_seconds"] == 1800


def test_run_now_returns_a_job_and_the_run_log_lists_it(monkeypatch) -> None:
    from nvh.api.routes.schedules import router

    calls = _patch_wizard(monkeypatch)
    monkeypatch.setattr(runner, "default_engine", _factory(_engine()))
    monkeypatch.delenv("HIVE_API_KEY", raising=False)
    app = FastAPI()
    app.include_router(router)
    with TestClient(app, raise_server_exceptions=True) as client:
        sid = _data(client.post("/v1/schedules", json={"prompt": "ping", "every": "1h"}))["id"]
        assert client.post("/v1/schedules/nope/run").status_code == 404
        started = _data(client.post(f"/v1/schedules/{sid}/run"))
        assert started["schedule_id"] == sid and started["job_id"]
        job_id = started["job_id"]
        for _ in range(100):
            runs = _data(client.get(f"/v1/schedules/{sid}/runs"))["runs"]
            if runs and runs[0]["status"] in jobs.TERMINAL_STATUSES:
                break
            import time

            time.sleep(0.02)
        assert runs and runs[0]["id"] == job_id and runs[0]["status"] == "complete"
        assert runs[0]["kind"] == runner.SCHEDULE_RUN_KIND and runs[0]["request"]["schedule_id"] == sid
        assert calls and calls[0]["unattended"] is True
        assert _data(client.get(f"/v1/schedules/{sid}/runs", params={"limit": 1}))["count"] == 1
        assert client.get(f"/v1/schedules/{sid}/runs", params={"limit": 0}).status_code == 422
        assert _data(client.get(f"/v1/schedules/{sid}"))["last_status"] == "ok"
        assert _data(client.get("/v1/schedules/other/runs")) == {"runs": [], "count": 0, "schedule_id": "other"}


def test_keyed_deployment_requires_the_key_on_every_route(monkeypatch) -> None:
    from nvh.api.routes.schedules import router

    monkeypatch.setenv("HIVE_API_KEY", "s3cret")
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app, raise_server_exceptions=True)
    assert client.get("/v1/schedules").status_code == 401
    assert client.post("/v1/schedules", json={"prompt": "x", "every": "1h"}).status_code == 401
    assert client.get("/v1/schedules/x").status_code == 401
    assert client.patch("/v1/schedules/x", json={"enabled": True}).status_code == 401
    assert client.delete("/v1/schedules/x").status_code == 401
    assert client.post("/v1/schedules/x/run").status_code == 401
    assert client.get("/v1/schedules/x/runs").status_code == 401
    assert client.get("/v1/schedules", headers={"X-Hive-API-Key": "s3cret"}).status_code == 200
    assert client.get("/v1/schedules", headers={"Authorization": "Bearer s3cret"}).status_code == 200


# ---------------------------------------------------------------------------
# Wave 7b fixes: advisor, no-model runs, one run at a time, the lock under load
# ---------------------------------------------------------------------------


async def test_ask_run_forwards_the_advisor_as_the_turns_provider_pin(monkeypatch) -> None:
    """``nvh schedule add ... --advisor groq`` (mode ask) reaches ``wizard_chat`` as ``provider="groq"``."""
    calls = _patch_wizard(monkeypatch)
    task = _due("Check my server CPU", advisor="groq")
    job = await _run(task, engine_factory=_factory(_engine()))
    assert job["status"] == "complete" and job["request"]["advisor"] == "groq"
    assert calls[0]["provider"] == "groq" and calls[0]["unattended"] is True


@pytest.mark.parametrize("fallback", ["connection refused", "engine not initialized", "profile_local_only_provider_unavailable"])
async def test_a_turn_no_model_answered_is_recorded_as_error_not_ok(monkeypatch, fallback: str) -> None:
    """``wizard_chat`` swallows LLM failures and answers deterministically (the offline helper); the
    runner must not call that ``ok``: the schedule records ``error`` with the ``fallback_reason``."""
    _patch_wizard(monkeypatch, result=_envelope(
        "x", mode="deterministic", fallback_reason=fallback, answer="Best core setup step: ...",
    ))
    task = _due("hourly digest")
    job = await _run(task, engine_factory=_factory(_engine()))
    assert job["status"] == "failed"
    assert job["message"] == f"no model answered the scheduled turn: {fallback}"
    stored = Scheduler().get(task.id)
    assert stored.last_status == "error" and fallback in stored.last_error
    assert stored.run_count == 1 and stored.next_run > 0
    assert "Best core setup step" not in _jobs_text(), "the helper's canned text is not an answer"


async def test_one_run_per_schedule_at_a_time(monkeypatch) -> None:
    """A second ``run_schedule_once`` while the first is in flight raises ``ScheduleRunInFlightError``
    naming the job, and the poll skips the schedule instead of starting a duplicate; once the run
    ends the next one starts normally."""
    gate = asyncio.Event()
    started = asyncio.Event()

    async def slow(question, **kwargs):
        started.set()
        await gate.wait()
        return _envelope(question)

    monkeypatch.setattr("nvh.integrations.wizard.chat.wizard_chat", slow)
    engine = _engine()
    task = _due("slow one")
    first = await runner.run_schedule_once(task, engine_factory=_factory(engine))
    await asyncio.wait_for(started.wait(), timeout=5)
    assert runner.in_flight_job(task.id) == first

    with pytest.raises(runner.ScheduleRunInFlightError) as info:
        await runner.run_schedule_once(Scheduler().get(task.id), engine_factory=_factory(engine))
    assert info.value.job_id == first and info.value.schedule_id == task.id
    assert await runner.run_due_tasks(engine_factory=_factory(engine)) == []  # still due, not re-run
    assert len(jobs.list_jobs(kind=runner.SCHEDULE_RUN_KIND, schedule_id=task.id, limit=10)) == 1

    gate.set()
    job = await runner.wait_for_run(first)
    assert job["status"] == "complete" and runner.in_flight_job(task.id) is None
    assert Scheduler().get(task.id).run_count == 1
    second = await _run(Scheduler().get(task.id), engine_factory=_factory(engine))
    assert second["id"] != first and Scheduler().get(task.id).run_count == 2


def test_run_now_route_answers_409_while_a_run_is_in_flight(client: TestClient, monkeypatch) -> None:
    sid = _data(client.post("/v1/schedules", json={"prompt": "ping", "every": "1h"}))["id"]

    async def busy(task, **kwargs):
        raise runner.ScheduleRunInFlightError(task.id, "20261101120000-feedface")

    monkeypatch.setattr(runner, "run_schedule_once", busy)
    resp = client.post(f"/v1/schedules/{sid}/run")
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["job_id"] == "20261101120000-feedface" and detail["schedule_id"] == sid
    assert "in flight" in detail["message"]


async def test_poll_leaves_a_due_do_schedule_for_the_cli_runner(monkeypatch) -> None:
    """The API runner never runs ``do`` (O5) — and it does not fail it every interval either, which
    would advance ``next_run`` and take the slot the CLI runner needs. ``allow_do`` runs it."""
    calls = _patch_wizard(monkeypatch)
    engine = _engine()
    task = _due("tidy up", mode="do")
    assert await runner.run_due_tasks(engine_factory=_factory(engine)) == []
    stored = Scheduler().get(task.id)
    assert stored.run_count == 0 and stored.last_status == "" and stored in Scheduler().get_due_tasks()
    assert calls == [] and jobs.list_jobs(kind=runner.SCHEDULE_RUN_KIND, schedule_id=task.id, limit=5) == []

    async def fake_loop(**kwargs):
        return SimpleNamespace(final_response="done", total_cost_usd=0.0, cost_known=True)

    monkeypatch.setattr("nvh.core.agent_loop.run_agent_loop", fake_loop)
    finished = await runner.run_due_tasks(engine_factory=_factory(engine), allow_do=True)
    assert len(finished) == 1 and finished[0]["status"] == "complete"
    assert Scheduler().get(task.id).run_count == 1

    # ``only_modes`` narrows a pass: an ask schedule is left alone when only ``do`` is asked for.
    ask = _due("ask me")
    assert await runner.run_due_tasks(engine_factory=_factory(engine), allow_do=True, only_modes={"do"}) == []
    assert Scheduler().get(ask.id).run_count == 0 and calls == []


async def test_the_heartbeat_keeps_beating_while_a_run_is_in_flight(monkeypatch) -> None:
    """A run longer than the poll never lets the lock go stale: a side task beats every ``poll_s``
    while ``wizard_chat`` is awaited, so the beat's age at the end of a 1 s run is well under it."""
    _forbid_kill(monkeypatch)
    ages: list[float] = []

    async def slow(question, **kwargs):
        await asyncio.sleep(1.0)
        ages.append(runner._heartbeat_age_s(runner._read_lock(runner.lock_path())))
        return _envelope(question)

    monkeypatch.setattr("nvh.integrations.wizard.chat.wizard_chat", slow)
    _due()
    stop = asyncio.Event()
    asyncio.get_running_loop().call_later(1.3, stop.set)
    ran = await asyncio.wait_for(
        runner.run_loop(stop, poll_s=0.1, runner=runner.RUNNER_CLI, engine_factory=_factory(_engine())), timeout=10,
    )
    assert ran is True and ages and ages[0] < 0.6, ages
    assert not runner.lock_path().exists()


def test_heartbeat_stands_down_once_another_runner_holds_the_lock(caplog) -> None:
    """A beat re-reads the file first: a foreign pid means a takeover happened, so this runner
    clears ``held``, writes nothing and never overwrites the new holder's record."""
    caplog.set_level(logging.WARNING, logger="nvh.integrations.services.schedule_runner")
    mine = runner.ScheduleLock(runner.lock_path(), poll_s=60)
    assert mine.acquire() and mine.heartbeat() is True
    _write_lock(runner.lock_path(), pid=777, age_s=0, kind="cli")
    assert mine.heartbeat() is False and mine.held is False
    assert json.loads(runner.lock_path().read_text(encoding="utf-8"))["pid"] == 777
    assert any("stands down" in r.getMessage() for r in caplog.records)
    mine.release()
    assert runner.lock_path().exists(), "release never deletes a lock it no longer holds"
    # A missing file is re-created by the holder; one that another runner created in the gap is not.
    again = runner.ScheduleLock(runner.lock_path(), poll_s=60)
    runner.lock_path().unlink()
    assert again.acquire() and again.heartbeat() is True
    runner.lock_path().unlink()
    assert again.heartbeat() is True and json.loads(runner.lock_path().read_text(encoding="utf-8"))["pid"] == os.getpid()
    runner.lock_path().unlink()
    _write_lock(runner.lock_path(), pid=888, age_s=0)  # another runner got there in the gap
    assert again.heartbeat() is False and again.held is False
    again.release()
    assert json.loads(runner.lock_path().read_text(encoding="utf-8"))["pid"] == 888


async def test_an_unreadable_lock_is_live_never_taken_over(monkeypatch, caplog) -> None:
    """A lock that exists but cannot be read (another account's file, a sharing violation that
    outlasts the retries) is not evidence its holder is gone: not stale, not acquired, reported as
    active and ``unreadable``, and a CLI loop returns at once."""
    caplog.set_level(logging.INFO, logger="nvh.integrations.services.schedule_runner")
    _forbid_kill(monkeypatch)
    calls = _patch_wizard(monkeypatch)
    _due()
    _write_lock(runner.lock_path(), pid=4242, age_s=0)
    tries: list[int] = []

    def denied(path):
        tries.append(1)
        raise PermissionError(13, "Permission denied", str(path))

    real_read = runner._read_lock_text
    monkeypatch.setattr(runner, "_read_lock_text", denied)
    monkeypatch.setattr(runner, "LOCK_READ_RETRY_S", 0)
    assert runner._read_lock(runner.lock_path()) == {"unreadable": True}
    assert len(tries) == runner.LOCK_READ_RETRIES
    assert runner._is_stale({"unreadable": True}) is False
    status = runner.runner_status()
    assert status["active"] is True and status["unreadable"] is True
    assert status["pid"] is None and status["runner"] == "" and status["stale"] is False

    lock = runner.ScheduleLock(runner.lock_path(), poll_s=60, runner=runner.RUNNER_CLI)
    assert lock.acquire() is False and lock.held is False
    assert any("cannot be read" in r.getMessage() for r in caplog.records)
    ran = await asyncio.wait_for(runner.run_loop(asyncio.Event(), poll_s=0.01, runner=runner.RUNNER_CLI), timeout=5)
    assert ran is False and calls == []
    monkeypatch.setattr(runner, "_read_lock_text", real_read)
    assert json.loads(runner.lock_path().read_text(encoding="utf-8"))["pid"] == 4242, "the file was never touched"


async def test_api_loop_waits_for_a_held_lock_and_takes_over_when_it_is_released(monkeypatch, caplog) -> None:
    """``wait_for_lock`` (the lifespan): a live CLI runner holds the lock, so the API loop polls
    nothing and takes nothing over — until the CLI releases it, when the API acquires the lock and
    runs the due schedule. Stopped while still waiting, it returns ``False`` and leaves the holder alone."""
    caplog.set_level(logging.INFO, logger="nvh.integrations.services.schedule_runner")
    _forbid_kill(monkeypatch)
    calls = _patch_wizard(monkeypatch)
    _due()
    holder = runner.ScheduleLock(runner.lock_path(), poll_s=60, runner=runner.RUNNER_CLI)
    assert holder.acquire()

    stop = asyncio.Event()
    waiting = asyncio.create_task(runner.run_loop(
        stop, poll_s=0.05, runner=runner.RUNNER_API, engine_factory=_factory(_engine()), wait_for_lock=True,
    ))
    await asyncio.sleep(0.3)
    assert not waiting.done() and calls == []
    assert json.loads(runner.lock_path().read_text(encoding="utf-8"))["runner"] == "cli"
    assert any("waiting" in r.getMessage() for r in caplog.records)

    holder.release()
    for _ in range(100):
        await asyncio.sleep(0.05)
        info = runner._read_lock(runner.lock_path())
        if info and info.get("runner") == "api" and calls:
            break
    assert runner._read_lock(runner.lock_path())["runner"] == "api" and calls, "took over and ran the due schedule"
    assert any("took over polling" in r.getMessage() for r in caplog.records)
    stop.set()
    assert await asyncio.wait_for(waiting, timeout=5) is True
    assert not runner.lock_path().exists()

    # Stopped before the holder ever let go: nothing ran, the holder's lock is untouched.
    assert holder.acquire()
    stop = asyncio.Event()
    waiting = asyncio.create_task(runner.run_loop(stop, poll_s=0.05, runner=runner.RUNNER_API, wait_for_lock=True))
    await asyncio.sleep(0.15)
    stop.set()
    assert await asyncio.wait_for(waiting, timeout=5) is False
    assert json.loads(runner.lock_path().read_text(encoding="utf-8"))["runner"] == "cli"
    holder.release()


async def test_recorded_arguments_are_redacted_before_they_reach_the_job_file(monkeypatch) -> None:
    """An unattended ``save_provider_key`` request carries the model's key; the run record and the job
    file carry the redaction marker, never the key."""
    key = "gsk_" + "Ab12Cd34" * 4
    _patch_wizard(monkeypatch, result=_envelope("x", tool_calls=[{
        "name": "save_provider_key", "arguments": {"provider": "groq", "api_key": key},
        "privileged": False, "unattended": True,
    }]))
    job = await _run(_due(), engine_factory=_factory(_engine()))
    payload = _complete_event(job)["payload"]
    assert payload["deferred"][0]["arguments"] == {"provider": "groq", "api_key": "[REDACTED:groq_key]"}
    assert key not in _jobs_text() and "[REDACTED:groq_key]" in _jobs_text()


async def test_route_saves_and_mark_run_hold_the_store_lock(client: TestClient, monkeypatch) -> None:
    """Every ``Scheduler._save`` the routes and the runner make runs with ``STORE_LOCK`` owned, so a
    route on a worker thread and a run finishing on the loop never interleave their load-modify-save."""
    from nvh.core import scheduler as scheduler_mod

    owned: list[bool] = []
    real_save = scheduler_mod.Scheduler._save

    def spy(self):
        owned.append(scheduler_mod.STORE_LOCK._is_owned())
        return real_save(self)

    monkeypatch.setattr(scheduler_mod.Scheduler, "_save", spy)
    sid = _data(client.post("/v1/schedules", json={"prompt": "ping", "every": "1h", "enabled": False}))["id"]
    _data(client.patch(f"/v1/schedules/{sid}", json={"enabled": True, "name": "renamed"}))
    _patch_wizard(monkeypatch)
    await _run(Scheduler().get(sid), engine_factory=_factory(_engine()))  # mark_run
    _data(client.delete(f"/v1/schedules/{sid}"))
    assert len(owned) >= 5 and all(owned), owned  # add, set_enabled, update, mark_run, remove
