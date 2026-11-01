"""Process-group kill of an in-flight playbook command (0.46).

Two layers, both hermetic on Windows and Linux:

  - :func:`nvh.integrations.wizard.system_settings.run_host_command_killable`
    -- the job path's step runner -- against a fake ``subprocess`` whose
    ``Popen`` returns a fake process: its own process group, piped stdout and
    stderr, ``stdin=DEVNULL``, the cancel flag polled between ``communicate``
    timeouts, SIGTERM then SIGKILL after the grace, the ``timed_out`` refusal
    on the deadline, and the deny list and the off-Linux refusal before any
    spawn -- the same steps 1 and 2 as ``run_host_command``. Plus the honest
    edges: a command that exited on its own between the poll and the stop is
    a plain exit (never ``killed``); one the signals cannot be shown to have
    stopped (a root-owned command behind ``sudo``) is ``kill_failed``, after
    the ``sudo -n kill`` escalation a sudo step gets.
  - the engine's cancel decision (:func:`nvh.integrations.installs.playbooks._engine`)
    with asyncio fakes and no threads: a kill the runner reports after the
    task was cancelled, a command that finished first, a runner that never
    reports, a task cancelled without the job's flag, a kill reported on
    the ordinary path, a kill the runner cannot vouch for, a step whose own
    deadline fired as the cancel landed, and a repeat cancel during the
    bounded wait -- each with the receipt wording it must leave. The
    between-steps wording (``stopped after step N``) is pinned unchanged.

Plus one POSIX-only integration test that runs ``sleep 30`` (and a ``bash``
that ignores SIGTERM) through the real runner, flips the flag and asserts the
group is gone within the grace, the pipes are closed and the child is reaped.
The job-path tests with the real jobs module live in ``tests/test_playbooks.py``.
"""

from __future__ import annotations

import asyncio
import io
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import nvh.integrations.installs.playbooks as pb
import nvh.integrations.wizard.system_settings as ss
from nvh.integrations.services import receipts
from nvh.integrations.wizard.tools import PRIVILEGED_ENV
from nvh.utils import platform_facts as pf

SPARK_LABEL = "NVIDIA DGX Spark (GB10, 128 GB unified)"
TOTAL_TAILSCALE_STEPS = 11


def seed(*, can_sudo: bool = False, in_sudo_group: bool = False, os: str = "linux") -> None:
    pf.seed_platform_facts(pf.PlatformFacts(
        os=os, arch="arm64", machine="aarch64", distro="DGX OS 7.2", kernel="6.11.0-1016-nvidia",
        is_dgx_os=True, gpu_name="NVIDIA GB10", unified_memory=True, memory_total_gb=128.0,
        memory_available_gb=100.0, device_class="dgx-spark", device_label=SPARK_LABEL,
        has_root=False, can_sudo=can_sudo, in_sudo_group=in_sudo_group or can_sudo,
    ))


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("NVH_HOME", str(tmp_path))
    monkeypatch.delenv(PRIVILEGED_ENV, raising=False)
    monkeypatch.setattr(ss, "_current_user", lambda: "alice")
    monkeypatch.setattr(pb, "_user_home", lambda: "/home/alice")
    seed()


# ───────────────────────────────────────────────────────────────────────────
# The fake subprocess (Windows-safe: the group-signal seam is patched, so no real pid is ever signalled)
# ───────────────────────────────────────────────────────────────────────────


@dataclass
class Hang:
    """A command that runs until signalled: dies on SIGTERM, or only on SIGKILL when ``stubborn``.

    ``immortal``: no signal reaches it (an unprivileged nvHive versus a
    root-owned child: EPERM, swallowed), so it never exits. ``holds_pipes``:
    the child dies on SIGKILL but something it started keeps the pipes open,
    so ``communicate`` keeps timing out and only ``wait`` reaps it.
    ``exits_after_first_poll``: it finishes on its own right after the
    runner's first wait timed out -- the race a cancel can land in.
    """

    partial: str = ""
    stubborn: bool = False
    immortal: bool = False
    holds_pipes: bool = False
    exits_after_first_poll: bool = False
    polls: int = 0

    def dies_on(self, signals: list[str]) -> bool:
        if self.immortal:
            return False
        return "SIGKILL" in signals or (not self.stubborn and "SIGTERM" in signals)

    def exited(self) -> bool:
        return self.exits_after_first_poll and self.polls >= 1


class FakeProc:
    def __init__(self, fake: FakeSubprocess, argv: list[str], answer: Any) -> None:
        self.fake, self.args, self.pid = fake, list(argv), 424242
        self.returncode: int | None = None
        self.signals: list[str] = []
        self.stdout, self.stderr = io.BytesIO(), io.BytesIO()
        self._answer = answer

    def communicate(self, timeout: float | None = None) -> tuple[bytes, bytes]:
        answer = self._answer
        if isinstance(answer, Hang):
            if answer.exited():
                code, out, err = 0, answer.partial, ""  # finished on its own; this reaps it
            elif not answer.dies_on(self.signals) or (answer.holds_pipes and "SIGKILL" in self.signals):
                answer.polls += 1
                time.sleep(min(timeout, 0.01) if timeout else 0.01)
                raise self.fake.TimeoutExpired(self.args, timeout)
            else:
                code, out, err = (-9 if "SIGKILL" in self.signals else -15), answer.partial, ""
        else:
            code, out, err = answer
        self.returncode = code
        self.stdout.close()
        self.stderr.close()
        return out.encode(), err.encode()

    def poll(self) -> int | None:
        answer = self._answer
        if isinstance(answer, Hang) and answer.exited() and self.returncode is None:
            self.returncode = 0  # a real poll() reaps an exited child
        return self.returncode

    def signal_group(self, *, hard: bool) -> None:
        self.signals.append("SIGKILL" if hard else "SIGTERM")

    def terminate(self) -> None:
        self.signal_group(hard=False)

    def kill(self) -> None:
        self.signal_group(hard=True)

    def wait(self, timeout: float | None = None) -> int | None:
        answer = self._answer
        if isinstance(answer, Hang) and answer.holds_pipes and "SIGKILL" in self.signals and self.returncode is None:
            self.returncode = -9  # the zombie is reaped; whatever holds the pipes is not ours
        return self.returncode


class FakeSubprocess:
    DEVNULL = -3
    PIPE = -1

    class TimeoutExpired(Exception):  # noqa: N818 — must match subprocess's name
        def __init__(self, cmd, timeout):
            super().__init__(f"timeout {timeout}")

    def __init__(self, responder=None) -> None:
        self.calls: list[list[str]] = []
        self.kwargs: list[dict[str, Any]] = []
        self.procs: list[FakeProc] = []
        self.responder = responder or (lambda argv: (0, "done\n", ""))

    def _answer(self, argv, kwargs):
        self.calls.append(list(argv))
        self.kwargs.append(kwargs)
        answer = self.responder(list(argv))
        if isinstance(answer, BaseException):
            raise answer
        return answer

    def run(self, argv, **kwargs):
        code, out, err = self._answer(argv, kwargs)
        return SimpleNamespace(returncode=code, stdout=out.encode(), stderr=err.encode())

    def Popen(self, argv, **kwargs):  # noqa: N802 — must match subprocess's name
        proc = FakeProc(self, argv, self._answer(argv, kwargs))
        self.procs.append(proc)
        return proc


@pytest.fixture()
def fake(monkeypatch) -> FakeSubprocess:
    fake = FakeSubprocess()
    monkeypatch.setattr(ss, "subprocess", fake)
    monkeypatch.setattr(ss, "_signal_group", lambda proc, *, hard: proc.signal_group(hard=hard))
    return fake


# ───────────────────────────────────────────────────────────────────────────
# The killable runner
# ───────────────────────────────────────────────────────────────────────────


def test_runner_spawns_in_its_own_process_group_with_pipes_and_no_stdin(fake: FakeSubprocess) -> None:
    """``Popen`` (not ``run``): ``stdin=DEVNULL`` so nothing can prompt, stdout/stderr piped so the receipt and the
    note get the output, and the group keyword for this OS so a kill reaches the command's children too. A command
    that finishes answers exactly as ``run_host_command`` would -- no ``killed`` key -- and the flag is never asked."""
    asked: list[bool] = []
    fake.responder = lambda argv: (0, "ok\n", "warn\n")
    out = ss.run_host_command_killable(["echo", "ok"], sudo=False, timeout=5, should_stop=lambda: asked.append(True) or True)
    assert out == {"ok": True, "command": "echo ok", "exit_code": 0, "stdout": "ok", "stderr": "warn"}
    assert asked == []  # the command finished at the first wait
    assert fake.calls == [["echo", "ok"]]
    spawn = fake.kwargs[0]
    assert spawn["stdin"] == FakeSubprocess.DEVNULL and spawn["stdout"] == FakeSubprocess.PIPE and spawn["stderr"] == FakeSubprocess.PIPE
    if os.name == "nt":
        assert "creationflags" in spawn and "start_new_session" not in spawn
    else:
        assert spawn["start_new_session"] is True and "creationflags" not in spawn
    assert "timeout" not in spawn and "capture_output" not in spawn and "shell" not in spawn and "input" not in spawn
    proc = fake.procs[0]
    assert proc.stdout.closed and proc.stderr.closed and proc.returncode == 0


def test_cancel_flag_sigterms_the_group_and_reports_the_kill(fake: FakeSubprocess) -> None:
    """The flag is read between ``communicate`` timeouts (one poll per ``poll_s``, never a spin); the first True
    SIGTERMs the group, the command dies, and the result says so with the signal's exit code and the output that
    had arrived."""
    hang = Hang(partial="Unpacking...\n")
    fake.responder = lambda argv: hang
    flag = {"up": False}

    def should_stop() -> bool:
        if hang.polls >= 3:
            flag["up"] = True
        return flag["up"]

    out = ss.run_host_command_killable(["sleep", "30"], sudo=False, timeout=60, should_stop=should_stop, poll_s=0.01, grace_s=5)
    proc = fake.procs[0]
    assert proc.signals == ["SIGTERM"] and proc.returncode == -15 and hang.polls == 3
    assert out == {"ok": False, "command": "sleep 30", "exit_code": -15, "stdout": "Unpacking...", "stderr": "", "killed": "SIGTERM"}
    assert proc.stdout.closed and proc.stderr.closed


def test_sigkill_follows_when_the_group_ignores_sigterm_past_the_grace(fake: FakeSubprocess) -> None:
    hang = Hang(stubborn=True)
    fake.responder = lambda argv: hang
    out = ss.run_host_command_killable(["sleep", "30"], sudo=False, timeout=60, should_stop=lambda: True, poll_s=0.01, grace_s=0.05)
    proc = fake.procs[0]
    assert proc.signals == ["SIGTERM", "SIGKILL"] and proc.returncode == -9
    assert out["killed"] == "SIGKILL" and out["exit_code"] == -9 and out["ok"] is False
    assert proc.stdout.closed and proc.stderr.closed


def test_timeout_kills_the_group_and_returns_the_timed_out_refusal(fake: FakeSubprocess) -> None:
    """The deadline is the runner's own: the group is killed and the refusal is the one ``run_host_command`` gives."""
    hang = Hang()
    fake.responder = lambda argv: hang
    out = ss.run_host_command_killable(["sleep", "30"], sudo=False, timeout=0.05, should_stop=lambda: False, poll_s=0.01)
    assert out == {"ok": False, "error": "timed out after 0.05s", "timed_out": True, "command": "sleep 30"}
    proc = fake.procs[0]
    assert proc.signals == ["SIGTERM"] and proc.returncode == -15 and proc.stdout.closed


def test_a_flag_that_cannot_be_read_is_not_a_cancel(fake: FakeSubprocess) -> None:
    hang = Hang()
    fake.responder = lambda argv: hang

    def flaky() -> bool:
        if hang.polls >= 2:
            return True
        raise OSError("job record unreadable")

    out = ss.run_host_command_killable(["sleep", "30"], sudo=False, timeout=60, should_stop=flaky, poll_s=0.01)
    assert out["killed"] == "SIGTERM" and hang.polls == 2  # the raising reads were False; the first True killed


def test_refusals_hold_before_any_spawn_and_match_the_plain_runner(fake: FakeSubprocess) -> None:
    """Steps 1 and 2 are shared (``_resolve_host_command``): the deny list, the off-Linux refusal, ``needs_terminal``
    for a sudo-group member and the ``sudo -n`` prefix come out identical from both runners, and nothing spawns."""
    denied = ss.run_host_command_killable(["rm", "-rf", "/"], sudo=True, timeout=5)
    assert denied["ok"] is False and denied["denied"] is True and denied["error"].startswith("BLOCKED")
    assert denied == ss.run_host_command(["rm", "-rf", "/"], sudo=True)
    seed(in_sudo_group=True)
    group_only = ss.run_host_command_killable(["systemctl", "enable", "--now", "ssh"], sudo=True)
    assert group_only["needs_terminal"] is True and group_only == ss.run_host_command(["systemctl", "enable", "--now", "ssh"], sudo=True)
    seed(os="windows")
    windows = ss.run_host_command_killable(["systemctl", "enable", "--now", "ssh"], sudo=True)
    assert windows["unsupported_host"] is True and windows["error"] == "System changes are not available on Windows yet"
    assert windows == ss.run_host_command(["systemctl", "enable", "--now", "ssh"], sudo=True)
    assert ss.run_host_command_killable([], sudo=False) == {"ok": False, "error": "empty command"}
    assert fake.calls == [] and fake.procs == []
    seed(can_sudo=True)
    fake.responder = lambda argv: (0, "", "")
    out = ss.run_host_command_killable(["systemctl", "enable", "--now", "ssh"], sudo=True)
    assert out["command"] == "sudo -n systemctl enable --now ssh" and fake.calls == [["sudo", "-n", "systemctl", "enable", "--now", "ssh"]]


def test_missing_binary_is_the_plain_refusal(fake: FakeSubprocess) -> None:
    fake.responder = lambda argv: FileNotFoundError("nope")
    assert ss.run_host_command_killable(["nope", "x"], sudo=False) == {"ok": False, "error": "nope: command not found", "command": "nope x"}


def test_a_command_that_exited_between_the_poll_and_the_stop_is_a_plain_exit_not_a_kill(fake: FakeSubprocess) -> None:
    """The race: the wait timed out, the flag read True, and the command finished on its own in between. The stop
    polls before it signals, reaps the natural exit and reports it as the plain exit it was -- no signal is sent and
    no ``killed`` is claimed (the engine then records ``stopped after step N``, not a kill that never happened)."""
    hang = Hang(partial="Reading package lists... Done\n", exits_after_first_poll=True)
    fake.responder = lambda argv: hang
    out = ss.run_host_command_killable(["apt", "update"], sudo=False, timeout=60, should_stop=lambda: True, poll_s=0.01)
    proc = fake.procs[0]
    assert out == {"ok": True, "command": "apt update", "exit_code": 0, "stdout": "Reading package lists... Done", "stderr": ""}
    assert proc.signals == [] and proc.returncode == 0 and proc.stdout.closed and proc.stderr.closed


def test_a_kill_that_does_not_reach_the_command_is_reported_as_kill_failed_not_killed(fake: FakeSubprocess) -> None:
    """Both signals swallowed (EPERM: the child is not ours to signal) and the command still alive after the grace:
    the runner closes the pipes, says ``kill_failed`` with why, and never ``killed``. No root escalation for a
    plain step."""
    hang = Hang(immortal=True)
    fake.responder = lambda argv: hang
    out = ss.run_host_command_killable(["sleep", "30"], sudo=False, timeout=60, should_stop=lambda: True, poll_s=0.01, grace_s=0.02)
    proc = fake.procs[0]
    assert proc.signals == ["SIGTERM", "SIGKILL"] and proc.returncode is None
    assert out == {
        "ok": False, "command": "sleep 30", "exit_code": None, "stdout": "", "stderr": "",
        "kill_failed": "the command did not stop",
    }
    assert "killed" not in out and proc.stdout.closed and proc.stderr.closed
    assert fake.calls == [["sleep", "30"]]  # nothing else spawned


def test_a_sudo_step_whose_command_outlives_sigkill_is_killed_as_root(fake: FakeSubprocess, monkeypatch) -> None:
    """The command behind ``sudo -n`` runs as root, so nvHive's own SIGKILL cannot reach it; the same passwordless
    sudo that started the step kills its group (``sudo -n kill -KILL -- -<pgid>``), bounded, and the result is an
    honest ``killed: SIGKILL``."""
    seed(can_sudo=True)
    monkeypatch.setattr(ss, "_root_kill_supported", lambda: True)
    hang = Hang(immortal=True)

    def responder(argv: list[str]):
        if argv[:3] == ["sudo", "-n", "kill"]:
            hang.immortal = False  # root's SIGKILL lands
            return (0, "", "")
        return hang

    fake.responder = responder
    out = ss.run_host_command_killable(["apt", "install", "-y", "x"], sudo=True, timeout=60, should_stop=lambda: True, poll_s=0.01, grace_s=0.02)
    proc = fake.procs[0]
    assert proc.args == ["sudo", "-n", "apt", "install", "-y", "x"] and proc.signals == ["SIGTERM", "SIGKILL"]
    assert fake.calls[-1] == ["sudo", "-n", "kill", "-KILL", "--", f"-{proc.pid}"]
    escalation = fake.kwargs[-1]
    assert escalation["stdin"] == FakeSubprocess.DEVNULL and escalation["capture_output"] is True
    assert escalation["timeout"] == ss.KILL_ESCALATE_S
    assert out["killed"] == "SIGKILL" and out["exit_code"] == -9 and "kill_failed" not in out
    assert proc.returncode == -9 and proc.stdout.closed and proc.stderr.closed


def test_a_root_survivor_that_keeps_the_pipes_is_kill_failed_with_the_sudo_wording(fake: FakeSubprocess, monkeypatch) -> None:
    """``sudo`` itself dies of SIGKILL but the root command it ran keeps the pipes (and the escalation is refused):
    the zombie is reaped, the pipes are closed, and the result is ``kill_failed`` naming the root command -- not
    ``killed: SIGKILL`` for a command that is still modifying the host."""
    seed(can_sudo=True)
    monkeypatch.setattr(ss, "_root_kill_supported", lambda: True)
    monkeypatch.setattr(ss, "KILL_PIPE_WAIT_S", 0.02)
    hang = Hang(stubborn=True, holds_pipes=True)
    fake.responder = lambda argv: (1, "", "sudo: a password is required\n") if argv[:3] == ["sudo", "-n", "kill"] else hang
    out = ss.run_host_command_killable(["apt", "install", "-y", "x"], sudo=True, timeout=60, should_stop=lambda: True, poll_s=0.01, grace_s=0.02)
    proc = fake.procs[0]
    assert proc.signals == ["SIGTERM", "SIGKILL"] and proc.returncode == -9
    assert fake.calls[-1][:3] == ["sudo", "-n", "kill"]
    assert "killed" not in out and out["exit_code"] == -9 and out["ok"] is False
    assert out["kill_failed"].startswith("`sudo` died but the command it ran as root kept going")
    assert proc.stdout.closed and proc.stderr.closed


def test_the_runners_worst_case_is_one_formula_and_the_engine_waits_longer_than_it() -> None:
    """The cancel handler's bound is the runner's worst case plus real slack, computed from the same constants."""
    worst = ss.stop_worst_case_s(poll_s=pb.CANCEL_POLL_S, grace_s=pb.KILL_GRACE_S)
    assert worst == ss.CANCEL_POLL_S + 3 * ss.KILL_GRACE_S + ss.KILL_ESCALATE_S + ss.KILL_PIPE_WAIT_S
    assert pb._settle_bound() - worst >= 2.0


# ───────────────────────────────────────────────────────────────────────────
# The engine's cancel decision (asyncio fakes, no threads)
# ───────────────────────────────────────────────────────────────────────────


def _plan():
    playbook = pb.get_playbook("tailscale")
    ctx = pb._context(playbook)
    return playbook, ctx, pb.compile_plan(playbook, ctx=ctx)


async def _check_cmd(argv) -> dict[str, Any]:
    return {"ok": False, "exit_code": 1}


def _ran(step, *, code: int = 0) -> dict[str, Any]:
    return {"ok": code == 0, "exit_code": code, "command": step.render(), "stdout": "", "stderr": ""}


def _killed(step, how: str) -> dict[str, Any]:
    code = -9 if how == "SIGKILL" else -15
    return {"ok": False, "exit_code": code, "command": step.render(), "stdout": "partial\n", "stderr": "", "killed": how}


class SecondStep:
    """A ``run_cmd`` fake: step 1 finishes at once; step 2 waits until the test releases it, then answers ``report``."""

    def __init__(self, report) -> None:
        self.report = report
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.seen: list[Any] = []

    async def __call__(self, step) -> dict[str, Any]:
        self.seen.append(step)
        if len(self.seen) == 1:
            return _ran(step)
        self.started.set()
        await self.release.wait()
        return self.report(step)


async def _drive(runner, flag: dict[str, bool], *, mode: str = "job") -> tuple[asyncio.Task, list[dict[str, Any]]]:
    playbook, ctx, plan = _plan()
    events: list[dict[str, Any]] = []

    async def consume() -> None:
        async for event in pb._engine(
            playbook, ctx, plan, run_cmd=runner, check_cmd=_check_cmd, home_dir=None, mode=mode,
            cancel_requested=lambda: flag["up"],
        ):
            events.append(event)

    task = asyncio.create_task(consume())
    await asyncio.wait_for(runner.started.wait(), 5)
    return task, events


def _decisions(tmp_path: Path) -> list[Path]:
    folder = tmp_path / "vault" / "Decisions"
    if not folder.is_dir():
        return []
    return sorted(p for p in folder.glob("*.md") if "#privileged" in p.read_text(encoding="utf-8"))


@pytest.mark.asyncio
async def test_engine_records_the_kill_the_runner_reports_after_the_task_was_cancelled(tmp_path: Path) -> None:
    """cancel_job's order -- flag first, then task.cancel() -- lands the CancelledError at the shielded await while the
    runner is killing the command. The handler waits for the report, records ``stopped during step 2 (command
    killed)`` with the signal's exit code, and yields the ``canceled`` event itself (no CancelledError escapes)."""
    flag = {"up": False}
    runner = SecondStep(lambda step: _killed(step, "SIGTERM"))
    task, events = await _drive(runner, flag)
    flag["up"] = True
    task.cancel()
    await asyncio.sleep(0)  # the CancelledError lands; the handler now waits for the runner's report
    runner.release.set()
    await asyncio.wait_for(task, 5)

    final = events[-1]
    assert final["event"] == "canceled" and final["status"] == "canceled" and final["canceled"] is True
    assert final["message"] == pb._killed_message(2, final["in_flight"]["command"], "SIGTERM")
    assert "stopped during step 2 (command killed)" in final["message"] and final["message"] == final["error"]
    assert final["stopped_after_step"] == 1 and final["steps_run"] == 2 and final["applied"] is True and final["partial"] is True
    assert final["in_flight"]["step"] == 1 and final["in_flight"]["sudo"] is True
    assert final["in_flight"]["killed"] == "SIGTERM" and final["in_flight"]["exit_code"] == -15
    assert final["no_root"] is False  # the killed sudo command had been spawned

    receipt = receipts.load_receipt("playbook:tailscale")
    assert receipt["status"] == "partial" and receipt["metadata"]["outcome"] == "canceled"
    steps = receipt["metadata"]["steps"]
    assert [s["exit_code"] for s in steps] == [0, -15] and steps[1]["canceled"] is True and steps[1]["killed"] == "SIGTERM"
    assert "canceled" not in steps[0] and "killed" not in steps[0]
    assert receipt["metadata"]["stopped_after_step"] == 1 and receipt["metadata"]["in_flight"]["killed"] == "SIGTERM"
    notes = _decisions(tmp_path)
    assert len(notes) == 1
    text = notes[0].read_text(encoding="utf-8")
    assert "Outcome: partial — canceled by the user — stopped during step 2 (command killed)" in text
    assert "— exit -15" in text and "partial" in text and "may have completed" not in text


@pytest.mark.asyncio
async def test_engine_records_a_command_that_finished_before_the_kill_landed(tmp_path: Path) -> None:
    """The runner reports a plain exit (no ``killed``): the command was done before the signal. That is the record a
    flag read after the step leaves -- ``stopped after step 2``, nothing in flight -- never a kill that did not happen."""
    flag = {"up": False}
    runner = SecondStep(lambda step: _ran(step, code=0))
    task, events = await _drive(runner, flag)
    flag["up"] = True
    task.cancel()
    runner.release.set()
    await asyncio.wait_for(task, 5)
    final = events[-1]
    assert final["event"] == "canceled" and final["message"] == pb._stopped_message(2, TOTAL_TAILSCALE_STEPS)
    assert final["stopped_after_step"] == 2 and final["in_flight"] is None and final["steps_run"] == 2
    receipt = receipts.load_receipt("playbook:tailscale")
    steps = receipt["metadata"]["steps"]
    assert [s["exit_code"] for s in steps] == [0, 0] and "canceled" not in steps[1] and "killed" not in steps[1]
    assert receipt["metadata"]["stopped_after_step"] == 2 and receipt["metadata"]["in_flight"] is None

    # A failed exit keeps the post-step wording's exit-code tail.
    receipts.receipt_path("playbook", "tailscale").unlink()
    flag = {"up": False}
    runner = SecondStep(lambda step: _ran(step, code=100))
    task, events = await _drive(runner, flag)
    flag["up"] = True
    task.cancel()
    runner.release.set()
    await asyncio.wait_for(task, 5)
    final = events[-1]
    assert final["message"].startswith(pb._stopped_message(2, TOTAL_TAILSCALE_STEPS)) and "had exited 100" in final["message"]


@pytest.mark.asyncio
async def test_engine_keeps_the_in_flight_record_when_the_runner_never_reports(monkeypatch, tmp_path: Path) -> None:
    """The bounded wait runs out (a runner that cannot stop its command): the honest pre-0.46 record -- in flight, exit
    unknown, "may have completed on the host" -- with no ``killed`` claim, and still the engine's own event."""
    monkeypatch.setattr(pb, "KILL_GRACE_S", 0.02)
    monkeypatch.setattr(pb, "CANCEL_POLL_S", 0.01)
    monkeypatch.setattr(ss, "KILL_ESCALATE_S", 0.03)
    monkeypatch.setattr(ss, "KILL_PIPE_WAIT_S", 0.04)
    # the runner's worst case (0.01 + 2*0.02 + 0.03 + 0.02 + 0.04 = 0.14) plus the slack (2*0.01 + 2.0)
    assert pb._settle_bound() == pytest.approx(2.16)
    flag = {"up": False}
    runner = SecondStep(lambda step: _ran(step))
    task, events = await _drive(runner, flag)
    flag["up"] = True
    task.cancel()
    started = time.monotonic()
    await asyncio.wait_for(task, 8)
    assert 2.0 <= time.monotonic() - started < 6.0  # waited the bound, not forever
    final = events[-1]
    assert final["event"] == "canceled" and "may have completed on the host" in final["message"]
    assert "command killed" not in final["message"] and final["stopped_after_step"] == 1
    assert final["in_flight"] == {"step": 1, "title": final["in_flight"]["title"], "command": final["in_flight"]["command"], "sudo": True}
    receipt = receipts.load_receipt("playbook:tailscale")
    steps = receipt["metadata"]["steps"]
    assert [s["exit_code"] for s in steps] == [0, None] and steps[1]["canceled"] is True and "killed" not in steps[1]
    assert "killed" not in receipt["metadata"]["in_flight"]
    runner.release.set()  # let the fake finish so no task is left pending
    await asyncio.sleep(0)
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_task_cancelled_without_the_job_flag_leaves_the_command_alone_and_re_raises(tmp_path: Path) -> None:
    """A server shutdown cancels the task but never sets the job's flag: no wait, no kill, the command is recorded as in
    flight (it may finish on the host) and the CancelledError continues to the job, exactly as before 0.46."""
    flag = {"up": False}
    runner = SecondStep(lambda step: _ran(step))
    task, events = await _drive(runner, flag)
    started = time.monotonic()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 5)
    assert time.monotonic() - started < 1.0  # no bounded wait without the flag
    assert events[-1]["event"] == "step" and events[-1]["status"] == "running"  # no canceled event: the job writes its own
    receipt = receipts.load_receipt("playbook:tailscale")
    steps = receipt["metadata"]["steps"]
    assert [s["exit_code"] for s in steps] == [0, None] and steps[1]["canceled"] is True and "killed" not in steps[1]
    assert receipt["metadata"]["in_flight"]["command"] == steps[1]["command"] and "killed" not in receipt["metadata"]["in_flight"]
    assert "may have completed on the host" in _decisions(tmp_path)[0].read_text(encoding="utf-8")
    runner.release.set()
    await asyncio.sleep(0)
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_a_kill_reported_on_the_ordinary_path_is_a_cancel_stopped_during_the_step(tmp_path: Path) -> None:
    """The runner saw the flag and killed the command without the task being cancelled (the flag was written to the job
    record directly): the ordinary ``await`` returns a ``killed`` result and the run is the same cancel."""
    playbook, ctx, plan = _plan()
    spawned: list[Any] = []

    async def run_cmd(step):
        spawned.append(step)
        return _killed(step, "SIGKILL") if len(spawned) == 2 else _ran(step)

    events = [e async for e in pb._engine(playbook, ctx, plan, run_cmd=run_cmd, check_cmd=_check_cmd, home_dir=None, mode="job")]
    final = events[-1]
    assert len(spawned) == 2 and final["event"] == "canceled" and final["canceled"] is True
    assert final["message"] == pb._killed_message(2, spawned[1].render(), "SIGKILL")
    assert "stopped during step 2 (command killed)" in final["message"] and f"{pb.KILL_GRACE_S:g}s" in final["message"]
    assert final["stopped_after_step"] == 1 and final["in_flight"]["killed"] == "SIGKILL" and final["in_flight"]["exit_code"] == -9
    receipt = receipts.load_receipt("playbook:tailscale")
    assert [s["exit_code"] for s in receipt["metadata"]["steps"]] == [0, -9] and receipt["metadata"]["steps"][1]["killed"] == "SIGKILL"


@pytest.mark.asyncio
async def test_engine_records_a_command_the_kill_did_not_reach_as_possibly_still_running(tmp_path: Path) -> None:
    """The runner signalled the command but cannot vouch that it stopped (``kill_failed``): the record says ``may
    still be running on the host`` with the reason, carries ``kill_failed`` and never ``killed``, and the sudo
    command still counts as spawned (``no_root`` False)."""
    reason = "`sudo` died but the command it ran as root kept going (nvHive cannot signal a root process)"
    flag = {"up": False}
    runner = SecondStep(lambda step: {
        "ok": False, "exit_code": -9, "command": step.render(), "stdout": "", "stderr": "", "kill_failed": reason,
    })
    task, events = await _drive(runner, flag)
    flag["up"] = True
    task.cancel()
    await asyncio.sleep(0)
    runner.release.set()
    await asyncio.wait_for(task, 5)
    final = events[-1]
    assert final["event"] == "canceled" and final["canceled"] is True
    assert final["message"] == pb._kill_failed_message(2, final["in_flight"]["title"], final["in_flight"]["command"], reason)
    assert "may still be running on the host" in final["message"] and "command killed" not in final["message"]
    assert final["stopped_after_step"] == 1 and final["steps_run"] == 2 and final["no_root"] is False
    assert final["in_flight"]["kill_failed"] == reason and "killed" not in final["in_flight"]
    receipt = receipts.load_receipt("playbook:tailscale")
    steps = receipt["metadata"]["steps"]
    assert [s["exit_code"] for s in steps] == [0, -9] and steps[1]["canceled"] is True
    assert steps[1]["kill_failed"] == reason and "killed" not in steps[1]
    assert receipt["metadata"]["in_flight"]["kill_failed"] == reason and "killed" not in receipt["metadata"]["in_flight"]
    text = _decisions(tmp_path)[0].read_text(encoding="utf-8")
    assert "may still be running on the host" in text and "command killed" not in text


@pytest.mark.asyncio
async def test_engine_records_a_step_whose_deadline_fired_as_the_cancel_landed(tmp_path: Path) -> None:
    """The runner's report is the ``timed_out`` refusal (no ``exit_code``, no ``killed``): the command ran until its
    own deadline and was killed by it. That is a step that ran -- recorded as such, with the sudo spawn counted and
    the receipt written -- not "nothing was in flight"."""
    flag = {"up": False}
    runner = SecondStep(lambda step: {"ok": False, "error": "timed out after 900s", "timed_out": True, "command": step.render()})
    task, events = await _drive(runner, flag)
    flag["up"] = True
    task.cancel()
    await asyncio.sleep(0)
    runner.release.set()
    await asyncio.wait_for(task, 5)
    final = events[-1]
    assert final["event"] == "canceled" and final["canceled"] is True
    assert final["message"] == pb._timed_out_message(2, final["in_flight"]["title"], final["in_flight"]["command"], "timed out after 900s")
    assert "no command was in flight" not in final["message"]
    assert final["stopped_after_step"] == 1 and final["steps_run"] == 2 and final["applied"] is True
    assert final["no_root"] is False and final["in_flight"]["timed_out"] is True and "killed" not in final["in_flight"]
    receipt = receipts.load_receipt("playbook:tailscale")
    steps = receipt["metadata"]["steps"]
    assert [s["exit_code"] for s in steps] == [0, None] and steps[1]["canceled"] is True and steps[1]["timed_out"] is True
    assert receipt["no_root"] is False and receipt["metadata"]["in_flight"]["timed_out"] is True
    assert "timed out after 900s" in _decisions(tmp_path)[0].read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_a_repeat_cancel_during_the_bounded_wait_keeps_the_runners_report(tmp_path: Path) -> None:
    """The Cancel button pressed twice (or the endpoint called twice) while the handler waits for the runner: the
    second CancelledError is absorbed, the wait goes on, the kill is recorded as a kill, and the task ends with
    every cancel accounted for (``cancelling() == 0``)."""
    flag = {"up": False}
    runner = SecondStep(lambda step: _killed(step, "SIGTERM"))
    task, events = await _drive(runner, flag)
    flag["up"] = True
    task.cancel()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    task.cancel()  # again, mid-wait
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    runner.release.set()
    await asyncio.wait_for(task, 5)
    final = events[-1]
    assert final["event"] == "canceled" and "stopped during step 2 (command killed)" in final["message"]
    assert final["in_flight"]["killed"] == "SIGTERM" and final["in_flight"]["exit_code"] == -15
    assert "may have completed" not in final["message"] and task.cancelling() == 0
    steps = receipts.load_receipt("playbook:tailscale")["metadata"]["steps"]
    assert [s["exit_code"] for s in steps] == [0, -15] and steps[1]["killed"] == "SIGTERM"


@pytest.mark.asyncio
async def test_a_kill_failed_report_on_the_ordinary_path_is_a_cancel_possibly_still_running(tmp_path: Path) -> None:
    playbook, ctx, plan = _plan()
    spawned: list[Any] = []

    async def run_cmd(step):
        spawned.append(step)
        if len(spawned) == 2:
            return {"ok": False, "exit_code": None, "command": step.render(), "stdout": "", "stderr": "", "kill_failed": "the command did not stop"}
        return _ran(step)

    events = [e async for e in pb._engine(playbook, ctx, plan, run_cmd=run_cmd, check_cmd=_check_cmd, home_dir=None, mode="job")]
    final = events[-1]
    assert len(spawned) == 2 and final["event"] == "canceled" and final["canceled"] is True
    assert final["message"].endswith("but the command did not stop; it may still be running on the host")
    assert final["stopped_after_step"] == 1 and final["in_flight"]["kill_failed"] == "the command did not stop"
    assert "killed" not in final["in_flight"] and final["no_root"] is False
    steps = receipts.load_receipt("playbook:tailscale")["metadata"]["steps"]
    assert [s["exit_code"] for s in steps] == [0, None] and steps[1]["kill_failed"] == "the command did not stop"


def test_wording_pins() -> None:
    """The cancel sentences a receipt or a note can carry: between steps (unchanged), during a step (killed), and the
    two honest edges (the kill the runner cannot vouch for, the step its own deadline killed)."""
    assert pb._stopped_message(1, 11) == "canceled by the user — stopped after step 1 of 11; no command was in flight"
    assert pb._stopped_message(0, 11) == "canceled by the user — stopped before the first of 11 step(s); no command was in flight"
    term = pb._killed_message(2, "sudo -n apt install -y openssh-server", "SIGTERM")
    assert term == "canceled by the user — stopped during step 2 (command killed); `sudo -n apt install -y openssh-server` was stopped with SIGTERM"
    kill = pb._killed_message(3, "docker pull x", "SIGKILL")
    assert kill.startswith("canceled by the user — stopped during step 3 (command killed); `docker pull x` ignored SIGTERM for ")
    assert kill.endswith("and was killed with SIGKILL") and f"{pb.KILL_GRACE_S:g}s" in kill
    failed = pb._kill_failed_message(2, "Install the OpenSSH server", "sudo -n apt install -y openssh-server", "the command did not stop")
    assert failed == (
        "canceled by the user during step 2 (Install the OpenSSH server) — `sudo -n apt install -y openssh-server` was "
        "signalled (SIGTERM, then SIGKILL) but the command did not stop; it may still be running on the host"
    )
    deadline = pb._timed_out_message(3, "Pull the image", "docker pull x", "timed out after 3600s")
    assert deadline == "canceled by the user during step 3 (Pull the image) — `docker pull x` timed out after 3600s and was killed by its deadline"
    assert pb.CANCEL_POLL_S == ss.CANCEL_POLL_S and pb.KILL_GRACE_S == ss.KILL_GRACE_S
    assert 0 < pb.CANCEL_POLL_S <= 1.0 and 1.0 <= pb.KILL_GRACE_S <= 10.0
    assert not hasattr(pb, "subprocess")  # the spawner stays in system_settings


# ───────────────────────────────────────────────────────────────────────────
# POSIX integration: the real runner, a real process group
# ───────────────────────────────────────────────────────────────────────────


def _group_gone(pgid: int, *, within: float = 3.0) -> bool:
    """True once no process of the group exists (a reparented child may sit as a zombie for a moment)."""
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.05)
    return False


@pytest.mark.skipif(os.name == "nt", reason="process groups, SIGTERM/SIGKILL and `sleep` are POSIX; the engine refuses on Windows anyway")
def test_posix_sleep_30_is_killed_with_its_group_within_the_grace(monkeypatch) -> None:
    """``sleep 30`` through the real runner: the flag flips after it started; the group is gone within the grace, the
    exit code is SIGTERM's, the pipes are closed and the child is reaped (no zombie). A ``bash`` that ignores SIGTERM
    is SIGKILLed with its ``sleep`` after the grace."""
    seen: list[tuple[Any, bool]] = []
    real = ss._signal_group

    def spy(proc, *, hard: bool) -> None:
        seen.append((proc, hard))
        real(proc, hard=hard)

    monkeypatch.setattr(ss, "_signal_group", spy)

    flag = threading.Event()
    threading.Timer(0.3, flag.set).start()
    started = time.monotonic()
    out = ss.run_host_command_killable(["sleep", "30"], sudo=False, timeout=60, should_stop=flag.is_set, poll_s=0.1, grace_s=5.0)
    elapsed = time.monotonic() - started
    assert elapsed < 5.0 + 2.0, elapsed
    assert out == {"ok": False, "command": "sleep 30", "exit_code": -15, "stdout": "", "stderr": "", "killed": "SIGTERM"}
    assert [hard for _, hard in seen] == [False]
    proc = seen[0][0]
    assert proc.returncode == -15 and proc.stdout.closed and proc.stderr.closed
    assert _group_gone(proc.pid)
    with pytest.raises(ChildProcessError):
        os.waitpid(proc.pid, os.WNOHANG)  # already reaped by communicate: no zombie

    seen.clear()
    flag = threading.Event()
    threading.Timer(0.3, flag.set).start()
    started = time.monotonic()
    out = ss.run_host_command_killable(
        ["bash", "-c", "trap '' TERM; sleep 30"], sudo=False, timeout=60, should_stop=flag.is_set, poll_s=0.1, grace_s=0.5,
    )
    elapsed = time.monotonic() - started
    assert elapsed < 0.5 * 2 + 3.0, elapsed
    assert out["killed"] == "SIGKILL" and out["exit_code"] == -9 and out["ok"] is False
    assert [hard for _, hard in seen] == [False, True]
    proc = seen[0][0]
    assert proc.returncode == -9 and proc.stdout.closed and proc.stderr.closed
    assert _group_gone(proc.pid)  # bash and its sleep both
    with pytest.raises(ChildProcessError):
        os.waitpid(proc.pid, os.WNOHANG)
