"""The REPL's ``/do`` asks before a confirm-class tool runs (0.46, O3).

``imagine`` became confirm-class in 0.46 because the hosted image providers
bill the user's key. ``nvh do`` already handed the agent loop a
``confirm_unsafe`` prompt; the REPL's ``/do`` handed it none, so the loop
refused the tool with ``Tool requires approval: imagine`` instead of asking.
The REPL now passes the same ``Allow? [y/N]`` prompt, read with the plain
``input()`` the REPL reads every line with. No model, no network: the agent
loop is replaced and the prompt's ``input`` is scripted.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from nvh.cli import repl


def _session() -> repl.ReplSession:
    return repl.ReplSession(
        provider=None, model=None, council_mode=False, auto_agents=False, preset=None, system_prompt=None,
    )


def _captured_loop(captured: dict):
    async def fake_run_agent_loop(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            final_response="done", completed=True, total_iterations=1, total_tool_calls=0,
            total_cost_usd=0.0, error=None,
        )

    return fake_run_agent_loop


def test_do_hands_the_loop_a_confirm_prompt_and_still_auto_approves_safe_tools(monkeypatch):
    captured: dict = {}
    monkeypatch.setattr("nvh.core.agent_loop.run_agent_loop", _captured_loop(captured))
    asyncio.run(repl._run_do_command_async("draw a cat", _session(), engine=object()))
    assert captured["auto_approve_safe"] is True
    assert callable(captured["confirm_unsafe"]), "the REPL's /do must be able to ask, like `nvh do`"


def test_the_prompt_reads_yes_no_and_treats_eof_as_no(monkeypatch):
    captured: dict = {}
    monkeypatch.setattr("nvh.core.agent_loop.run_agent_loop", _captured_loop(captured))
    asyncio.run(repl._run_do_command_async("draw a cat", _session(), engine=object()))
    confirm = captured["confirm_unsafe"]

    answers = iter(["y", " YES ", "n", ""])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    assert confirm("imagine", {"prompt": "a cat"}) is True
    assert confirm("imagine", {"prompt": "a cat"}) is True
    assert confirm("imagine", {"prompt": "a cat"}) is False
    assert confirm("imagine", {"prompt": "a cat"}) is False

    def _eof(prompt=""):
        raise EOFError

    monkeypatch.setattr("builtins.input", _eof)
    assert confirm("imagine", {"prompt": "a cat"}) is False

    def _interrupt(prompt=""):
        raise KeyboardInterrupt

    monkeypatch.setattr("builtins.input", _interrupt)
    assert confirm("imagine", {"prompt": "a cat"}) is False
