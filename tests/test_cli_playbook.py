"""``nvh playbook list | plan <id> | install <id> [-y]`` (phase 2b, design brief §6).

Hermetic throughout: ``NVH_HOME`` is ``tmp_path``, the platform facts are the
seeded neutral ones from tests/conftest.py (no sudo, no root), the login name
is ``alice`` and ``playbooks.run_in_terminal`` is a recording fake — nothing
here runs ``sudo``, ``apt``, ``docker`` or ``curl``. A guard replaces the
``subprocess`` module seen by system_settings — the one runner both the job
and the terminal path spawn through — so a slip past the fake fails loudly
instead of spawning anything.

``list`` and ``plan`` drive the real ``catalogue()`` / ``plan_dict()`` (pure:
they only compile and read receipts) so the CLI renders the shape the module
really returns; ``install`` always goes through the fake runner.
"""

from __future__ import annotations

import re
import subprocess
import types
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

import nvh.cli.main as cli_main
import nvh.integrations.installs.playbooks as pb
import nvh.integrations.wizard.system_settings as ss
from nvh.integrations.services import receipts
from nvh.utils import platform_facts as pf

_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _plain(text: str) -> str:
    # Rich colours output on CI (FORCE_COLOR) and styles words with escape
    # codes between; substring checks need the de-styled text.
    return _ANSI.sub("", text)


def _one_line(text: str) -> str:
    return " ".join(_plain(text).split())


def _cells(out: str, playbook_id: str) -> list[str]:
    """The stripped cells of the table row whose first cell is ``playbook_id``."""
    for line in _plain(out).splitlines():
        cells = [cell.strip() for cell in re.split(r"[│┃|]", line)][1:-1]
        if cells and cells[0] == playbook_id:
            return cells
    raise AssertionError(f"no table row for {playbook_id!r}:\n{out}")


class _NoSpawn:
    """Stands in for ``subprocess``: any ``run`` is a test failure."""

    DEVNULL = subprocess.DEVNULL
    PIPE = subprocess.PIPE
    TimeoutExpired = subprocess.TimeoutExpired

    def run(self, argv, **kwargs):
        raise AssertionError(f"the CLI must never spawn a host command in tests: {argv}")


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch, tmp_path: Path) -> Path:
    monkeypatch.setenv("NVH_HOME", str(tmp_path))
    for var in ("NVHIVE_HOME", "HIVE_CONFIG_HOME", "NVH_STATE"):
        monkeypatch.delenv(var, raising=False)
    # Wide console so Rich never folds an id or a command across lines (tmp_path makes commands long).
    monkeypatch.setenv("COLUMNS", "400")
    monkeypatch.setattr(ss, "_current_user", lambda: "alice")
    monkeypatch.setattr(pb, "_user_home", lambda: "/home/alice")
    assert not hasattr(pb, "subprocess")  # playbooks has no spawner of its own; system_settings is the one seam
    monkeypatch.setattr(ss, "subprocess", _NoSpawn())
    return tmp_path


@pytest.fixture()
def runner() -> CliRunner:
    return CliRunner()


def _seed(**overrides: Any) -> None:
    facts: dict[str, Any] = {
        "os": "linux", "arch": "x86_64", "machine": "x86_64", "device_class": "workstation",
        "device_label": "Workstation (linux/x86_64; no NVIDIA GPU)",
        "has_root": False, "can_sudo": False, "in_sudo_group": False, "is_cloud": False,
    }
    facts.update(overrides)
    pf.seed_platform_facts(pf.PlatformFacts(**facts))


class FakeTerminalRun:
    """Records every ``run_in_terminal`` call, replays canned events through ``emit`` and returns a result."""

    def __init__(self, events: list[dict[str, Any]] | None = None, result: dict[str, Any] | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.events = list(events or [])
        self.result = result

    def __call__(self, playbook_id: str, *, assume_yes: bool, echo: bool = True, home_dir=None, confirm=None, emit=None):
        self.calls.append({
            "playbook_id": playbook_id, "assume_yes": assume_yes, "echo": echo, "home_dir": home_dir,
            "confirm": confirm, "emit": emit,
        })
        if not assume_yes:
            # Like the real runner: the library owns the question and asks it once through ``confirm``.
            playbook = pb.get_playbook(playbook_id)
            question = pb._confirm_question(playbook, pb.compile_plan(playbook, home_dir=home_dir))
            if confirm is None or not confirm(question):
                return {"ok": False, "applied": False, "canceled": True, "error": "canceled", "playbook": playbook_id, "commands": []}
        for event in self.events:
            if emit is not None:
                emit(event)
        if self.result is not None:
            out = dict(self.result)
            out.setdefault("events", list(self.events))
            return out
        final = dict(self.events[-1]) if self.events else {"event": "error", "error": "no events"}
        final["ok"] = final.get("event") == "complete"
        final["events"] = list(self.events)
        return final


def _vscode_events(tmp_path: Path) -> list[dict[str, Any]]:
    """The event stream of a run that skipped the download (already done), installed and verified."""
    receipt = (tmp_path / "receipts" / "playbook_vscode.json").as_posix()
    audit = (tmp_path / "vault" / "Decisions" / "2026-09-03-privileged-change-install-the-vscode-playbook.md").as_posix()
    base = {"playbook": "vscode"}
    deb = (tmp_path / "playbooks" / "vscode" / "vscode-arm64.deb").as_posix()
    step0 = {**base, "step": 0, "steps_total": 2, "title": "Download the VS Code .deb", "sudo": False,
             "command": f"wget 'https://code.visualstudio.com/sha/download?build=stable&os=linux-deb-arm64' -O {deb}"}
    step1 = {**base, "step": 1, "steps_total": 2, "title": "Install VS Code", "sudo": True, "command": f"sudo apt-get install -y {deb}"}
    return [
        {**base, "event": "plan", "status": "running", "message": "Visual Studio Code: 2 command(s), 2 manual step(s)", "steps_total": 2},
        {**step0, "event": "step", "status": "running", "message": "Step 1/2: Download the VS Code .deb"},
        {**step0, "event": "log", "status": "running", "message": "already done — skipped: Download the VS Code .deb", "skipped": True},
        {**step0, "event": "step", "status": "complete", "skipped": True, "message": "Step 1/2 skipped (already done)"},
        {**step1, "event": "step", "status": "running", "message": "Step 2/2: Install VS Code"},
        {**step1, "event": "log", "status": "running", "message": "Unpacking code (1.99.0)", "exit_code": 0},
        {**step1, "event": "step", "status": "complete", "message": "Step 2/2 done: Install VS Code", "exit_code": 0},
        {**base, "event": "log", "status": "running", "verify": True, "message": "verify: code --version → ok", "command": "code --version", "exit_code": 0},
        {
            **base, "event": "complete", "status": "complete", "applied": True, "partial": False, "outcome": "complete",
            "no_root": False, "steps_run": 1, "steps_total": 2, "verify": [{"command": "code --version", "ok": True, "exit_code": 0}],
            "receipt_path": receipt, "audit": {"saved": True, "path": audit, "category": "Decisions"},
            "manual_steps": [], "undo": ["sudo apt-get remove -y code"],
            "message": "Visual Studio Code: 1 command(s) ran, 1 already done",
        },
    ]


@pytest.fixture()
def fake_run(monkeypatch, tmp_path: Path) -> FakeTerminalRun:
    fake = FakeTerminalRun(events=_vscode_events(tmp_path))
    monkeypatch.setattr(pb, "run_in_terminal", fake)
    return fake


# ───────────────────────────────────────────────────────────────────────────
# nvh playbook list
# ───────────────────────────────────────────────────────────────────────────


class TestList:
    def test_table_has_every_playbook_with_sudo_manual_time_and_installed(self, runner: CliRunner):
        result = runner.invoke(cli_main.app, ["playbook", "list"])
        assert result.exit_code == 0, result.output
        out = _plain(result.output)
        for header in ("Playbook", "Title", "Sudo steps", "Manual steps", "Est. time", "Installed"):
            assert header in out, header
        rows = pb.catalogue()
        assert len(rows) == len(pb.PLAYBOOKS)
        for row in rows:
            cells = _cells(out, row["id"])
            assert cells[1] == row["title"], cells
            assert cells[2] == f"{row['sudo_steps']} of {row['steps_total']}", cells
            assert cells[3] == str(row["manual_steps"]), cells
            assert cells[4] == f"~{row['estimated_minutes']} min", cells
            assert cells[5] == "no", cells  # nothing installed yet
        # Deferred playbooks are named with the reason, never rendered as runnable rows.
        flat = _one_line(out)
        for entry in pb.deferred():
            assert f"- {entry['id']}: {entry['reason'][:30]}" in flat, entry["id"]
            with pytest.raises(AssertionError):
                _cells(out, entry["id"])
        assert "Not yet shipped" in out
        # The next two verbs, and where sudo will ask.
        assert "nvh playbook plan <id>" in out and "nvh playbook install <id>" in out
        assert "password in this terminal" in out
        assert pb.UPSTREAM_TREE in out

    def test_installed_comes_from_the_receipt(self, runner: CliRunner, tmp_path: Path):
        receipts.write_receipt(
            kind=pb.RECEIPT_KIND, item_id="vscode", title="Visual Studio Code",
            install_path=str(tmp_path / "playbooks" / "vscode"), status="installed",
            source_urls=[pb.UPSTREAM_TREE + "vscode"], no_root=False, home_dir=tmp_path,
        )
        receipts.write_receipt(
            kind=pb.RECEIPT_KIND, item_id="tailscale", title="Tailscale",
            install_path=str(tmp_path / "playbooks" / "tailscale"), status="partial",
            source_urls=[pb.UPSTREAM_TREE + "tailscale"], no_root=False, home_dir=tmp_path,
        )
        result = runner.invoke(cli_main.app, ["playbook", "list"])
        assert result.exit_code == 0, result.output
        assert _cells(result.output, "vscode")[5] == "yes"
        assert _cells(result.output, "tailscale")[5] == "partial"
        assert _cells(result.output, "ollama")[5] == "no"

    def test_home_dir_reaches_the_catalogue(self, runner: CliRunner, monkeypatch, tmp_path: Path):
        seen: list[Any] = []

        def fake_catalogue(home_dir=None):
            seen.append(home_dir)
            return []

        monkeypatch.setattr(pb, "catalogue", fake_catalogue)
        other = str(tmp_path / "other-home")
        assert runner.invoke(cli_main.app, ["playbook", "list", "--home-dir", other]).exit_code == 0
        assert runner.invoke(cli_main.app, ["playbook", "list", "--home", other]).exit_code == 0
        assert runner.invoke(cli_main.app, ["playbook", "list"]).exit_code == 0
        assert seen == [other, other, None]


# ───────────────────────────────────────────────────────────────────────────
# nvh playbook plan <id>
# ───────────────────────────────────────────────────────────────────────────


class TestPlan:
    def test_every_step_is_tagged_with_its_exact_command_then_verify_and_undo(self, runner: CliRunner):
        plan = pb.plan_dict("vscode")
        assert plan["ok"] and plan["steps"] and plan["verify"] and plan["undo"]
        result = runner.invoke(cli_main.app, ["playbook", "plan", "vscode"])
        assert result.exit_code == 0, result.output
        out = _plain(result.output)
        flat = _one_line(out)
        assert plan["title"] in out and "(vscode)" in out
        assert plan["source_urls"][0] in out
        squash = _one_line  # the plan's own text, whitespace-collapsed the same way as the output
        for step in plan["steps"]:
            tag = "[sudo]" if step["sudo"] else "[user]"
            assert f"{step['index'] + 1}. {tag} {step['title']}" in flat, step["title"]
            assert f"$ {squash(step['command'])}" in flat, step["command"]
        assert "[sudo]" in out and "[user]" in out
        verify_at = flat.index("Verify")
        undo_at = flat.index("Undo")
        assert verify_at < undo_at
        for command in plan["verify"]:
            assert f"$ {squash(command)}" in flat[verify_at:undo_at], command
        for line in plan["undo"]:
            assert f"$ {squash(line)}" in flat[undo_at:], line
        assert "preview only" in out and "never runs these" in out
        assert f"~{plan['estimates']['minutes']} min" in out and "Risk:" in out
        assert "estimated_minutes" not in plan  # plan_dict ships the one `estimates` shape
        # The sudo count is the playbook's own, not re-derived from the step list.
        assert f"({len(plan['steps'])} command(s), {plan['sudo_steps']} with sudo," in flat
        for offset, text in enumerate(plan["manual_steps"], start=len(plan["steps"]) + 1):
            assert f"{offset}. [manual] {squash(text)}" in flat, text
        # Neutral facts: no sudo at all → the plan says so instead of pretending.
        assert "cannot use sudo" in out

    def test_manual_steps_and_the_unpinned_note_are_shown(self, runner: CliRunner):
        plan = pb.plan_dict("ollama")
        assert plan["manual_steps"] and plan["unpinned"]
        result = runner.invoke(cli_main.app, ["playbook", "plan", "ollama"])
        assert result.exit_code == 0, result.output
        out = _plain(result.output)
        assert "[manual]" in out
        for text in plan["manual_steps"]:
            assert text.split(" — ", 1)[0] in out, text
        assert "pipe-to-shell: unpinned" in out
        assert "curl -fsSL https://ollama.com/install.sh | sh" in out  # the upstream one-liner, verbatim
        assert plan["rootless_alternative"] and f"nvh studio --install {plan['rootless_alternative']} -y" in out

    def test_unpinned_vendor_download_is_flagged_without_the_pipe_wording(self, runner: CliRunner):
        plan = pb.plan_dict("vscode")
        assert plan["unpinned"] and all(step["unpinned"] for step in plan["steps"])
        out = _one_line(runner.invoke(cli_main.app, ["playbook", "plan", "vscode"]).output)
        assert "unpinned download" in out and "no version pin or checksum" in out
        assert "pipe-to-shell" not in out  # a .deb is not a script piped into a shell
        assert "sudo dpkg -i vscode-arm64.deb" in out  # the upstream commands, verbatim
        assert out.count("unpinned download") == 2  # once per step, and not repeated as a plan note

    def test_docker_playbook_names_the_usermod_step_and_the_stop(self, runner: CliRunner):
        result = runner.invoke(cli_main.app, ["playbook", "plan", "open-webui"])
        assert result.exit_code == 0, result.output
        out = _plain(result.output)
        assert "$ sudo usermod -aG docker alice" in out
        assert "newgrp" not in out  # policy (b): the re-login is a note, never a command
        assert "run stops after this step" in _one_line(out)

    def test_password_prompt_is_announced_when_sudo_needs_one(self, runner: CliRunner):
        _seed(in_sudo_group=True)  # sudo works, but asks
        out = _plain(runner.invoke(cli_main.app, ["playbook", "plan", "vscode"]).output)
        assert "ask for your password in this terminal" in out
        assert "cannot use sudo" not in out

    def test_unknown_id_exits_1_and_points_at_the_catalogue(self, runner: CliRunner):
        result = runner.invoke(cli_main.app, ["playbook", "plan", "nope"])
        assert result.exit_code == 1
        out = _plain(result.output)
        assert "unknown playbook 'nope'" in out and "nvh playbook list" in out

    def test_ids_are_case_insensitive(self, runner: CliRunner):
        assert runner.invoke(cli_main.app, ["playbook", "plan", "VSCode"]).exit_code == 0


# ───────────────────────────────────────────────────────────────────────────
# nvh playbook install <id> [-y]
# ───────────────────────────────────────────────────────────────────────────


class TestInstall:
    def test_yes_runs_in_the_terminal_streams_steps_and_prints_receipt_and_audit(self, runner: CliRunner, fake_run: FakeTerminalRun):
        result = runner.invoke(cli_main.app, ["playbook", "install", "vscode", "-y"])
        assert result.exit_code == 0, result.output
        out = _plain(result.output)
        flat = _one_line(out)
        # The plan is printed first (the same plan_dict the Wizard card shows).
        plan = pb.plan_dict("vscode")
        for step in plan["steps"]:
            assert f"$ {step['command']}" in flat, step["command"]
        # One call, confirmation already given here → the runner must not ask again.
        assert len(fake_run.calls) == 1
        call = fake_run.calls[0]
        assert call["playbook_id"] == "vscode" and call["assume_yes"] is True and call["echo"] is True
        assert call["home_dir"] is None and callable(call["emit"])
        # Streamed step lines.
        assert "STEP 1/2 [user] Download the VS Code .deb" in flat
        assert "STEP 2/2 [sudo] Install VS Code" in flat
        assert "$ sudo apt-get install -y" in out
        assert "Unpacking code (1.99.0)" in out
        assert "already done — skipped" in out
        assert "verify: code --version → ok" in out
        assert "Done." in out and "1 command(s) ran, 1 already done" in out
        # Ends with the receipt and the vault audit paths.
        final = fake_run.events[-1]
        assert f"Receipt: {final['receipt_path']}" in flat
        assert f"Audit: {final['audit']['path']}" in flat
        assert out.rstrip().splitlines()[-1].strip().startswith("Audit:")

    def test_declined_prompt_runs_nothing_and_exits_1(self, runner: CliRunner, fake_run: FakeTerminalRun):
        result = runner.invoke(cli_main.app, ["playbook", "install", "vscode"], input="n\n")
        assert result.exit_code == 1
        out = _plain(result.output)
        # The library's question (one copy, `_confirm_question`), asked through typer.confirm.
        assert "Run 2 command(s) (1 with sudo) for the vscode playbook?" in _one_line(out)
        assert "Cancelled." in out
        assert len(fake_run.calls) == 1 and fake_run.calls[0]["assume_yes"] is False
        assert "STEP" not in out and "Receipt:" not in out  # the runner returned canceled before any step

    def test_declined_prompt_spawns_nothing_with_the_real_runner(self, runner: CliRunner, tmp_path: Path):
        # No fake runner: the real ``run_in_terminal`` asks, hears no, and never reaches subprocess (the _NoSpawn guard would raise).
        result = runner.invoke(cli_main.app, ["playbook", "install", "vscode"], input="n\n")
        assert result.exit_code == 1, result.output
        out = _plain(result.output)
        assert "Run 2 command(s) (1 with sudo) for the vscode playbook?" in _one_line(out) and "Cancelled." in out
        assert not (tmp_path / "receipts" / "playbook_vscode.json").exists()  # nothing ran, nothing recorded
        assert not (tmp_path / "vault" / "Decisions").exists()

    def test_accepted_prompt_hands_the_question_to_the_runner_once(self, runner: CliRunner, fake_run: FakeTerminalRun):
        result = runner.invoke(cli_main.app, ["playbook", "install", "vscode"], input="y\n")
        assert result.exit_code == 0, result.output
        call = fake_run.calls[0]
        assert len(fake_run.calls) == 1 and call["assume_yes"] is False and callable(call["confirm"])
        assert _plain(result.output).count("for the vscode playbook?") == 1  # asked exactly once

    def test_home_dir_flows_to_the_plan_and_the_runner(self, runner: CliRunner, fake_run: FakeTerminalRun, tmp_path: Path):
        other = tmp_path / "persist"
        result = runner.invoke(cli_main.app, ["playbook", "install", "vscode", "-y", "--home-dir", str(other)])
        assert result.exit_code == 0, result.output
        assert fake_run.calls[0]["home_dir"] == str(other)
        assert pb.plan_dict("vscode", home_dir=other)["install_path"] in _plain(result.output)  # compiled for that home

    def test_failed_step_exits_1_after_the_streamed_error(self, runner: CliRunner, monkeypatch, tmp_path: Path):
        events = _vscode_events(tmp_path)[:4]
        command = events[3]["command"]
        events += [
            {**events[3], "event": "step", "status": "failed", "message": f"`{command}` exited 1", "error": f"`{command}` exited 1", "exit_code": 1},
            {
                "playbook": "vscode", "event": "error", "status": "failed", "applied": True, "partial": True, "outcome": "failed",
                "no_root": False, "steps_run": 2, "steps_total": 3, "verify": [], "manual_steps": [], "undo": [],
                "receipt_path": (tmp_path / "receipts" / "playbook_vscode.json").as_posix(),
                "audit": {"saved": True, "path": (tmp_path / "vault" / "Decisions" / "failed.md").as_posix(), "category": "Decisions"},
                "message": f"`{command}` exited 1", "error": f"`{command}` exited 1",
            },
        ]
        fake = FakeTerminalRun(events=events)
        monkeypatch.setattr(pb, "run_in_terminal", fake)
        result = runner.invoke(cli_main.app, ["playbook", "install", "vscode", "-y"])
        assert result.exit_code == 1
        out = _plain(result.output)
        assert "FAILED" in out and "exited 1" in out and "Failed:" in out
        # A partial run still has a receipt and an audit note — both printed.
        assert "playbook_vscode.json" in out and "failed.md" in out
        assert out.count("exited 1") == 2  # the failed step and the final event; never repeated a third time

    def test_refusal_before_the_first_step_prints_the_reason_and_exits_1(self, runner: CliRunner, monkeypatch):
        fake = FakeTerminalRun(result={
            "ok": False, "denied": True, "error": "refused: rm -rf / is on the deny list", "playbook": "vscode", "commands": [],
        })
        monkeypatch.setattr(pb, "run_in_terminal", fake)
        result = runner.invoke(cli_main.app, ["playbook", "install", "vscode", "-y"])
        assert result.exit_code == 1
        out = _plain(result.output)
        assert "refused: rm -rf / is on the deny list" in out
        assert "Receipt: none" in out

    def test_docker_relogin_halt_is_a_hand_off_not_a_failure(self, runner: CliRunner, monkeypatch, tmp_path: Path):
        base = {"playbook": "open-webui"}
        head = {**base, "step": 0, "steps_total": 4, "title": "Join the docker group", "command": "sudo usermod -aG docker alice", "sudo": True}
        events = [
            {**base, "event": "plan", "status": "running", "message": "Open WebUI: 4 command(s), 3 manual step(s)"},
            {**head, "event": "step", "status": "running", "message": "Step 1/4: Join the docker group"},
            {**head, "event": "step", "status": "complete", "message": "Step 1/4 done: Join the docker group", "exit_code": 0},
            {**head, "event": "log", "status": "running", "message": f"MANUAL: {pb.RELOGIN_NOTE}"},
            {
                **base, "event": "complete", "status": "complete", "halted": True, "applied": True, "partial": True, "outcome": "halted",
                "no_root": False, "steps_run": 1, "steps_total": 4, "verify": [],
                "receipt_path": (tmp_path / "receipts" / "playbook_open-webui.json").as_posix(),
                "audit": {"saved": True, "path": (tmp_path / "vault" / "Decisions" / "halted.md").as_posix(), "category": "Decisions"},
                "manual_steps": ["Open the UI — http://localhost:3000 in a browser"], "undo": [], "message": pb.RELOGIN_NOTE,
            },
        ]
        fake = FakeTerminalRun(events=events)
        monkeypatch.setattr(pb, "run_in_terminal", fake)
        result = runner.invoke(cli_main.app, ["playbook", "install", "open-webui", "-y"])
        assert result.exit_code == 0, result.output
        out = _one_line(result.output)
        assert "MANUAL:" in out and "Log out and back in" in out
        assert "Failed" not in out
        assert "Left to you" in out and "http://localhost:3000" in out
        assert "playbook_open-webui.json" in out and "halted.md" in out

    def test_unknown_id_exits_1_before_running_anything(self, runner: CliRunner, fake_run: FakeTerminalRun):
        result = runner.invoke(cli_main.app, ["playbook", "install", "nope", "-y"])
        assert result.exit_code == 1
        assert "unknown playbook 'nope'" in _plain(result.output)
        assert fake_run.calls == []

    def test_manual_only_playbook_asks_a_different_question(self, runner: CliRunner, monkeypatch):
        plan = pb.plan_dict("dgx-dashboard")
        assert plan["steps_total"] == 0 and plan["manual_steps"]
        fake = FakeTerminalRun(result={
            "ok": True, "event": "complete", "applied": False, "partial": False, "outcome": "complete", "playbook": "dgx-dashboard",
            "receipt_path": None, "audit": None, "manual_steps": plan["manual_steps"], "message": "nothing to run",
        })
        monkeypatch.setattr(pb, "run_in_terminal", fake)
        result = runner.invoke(cli_main.app, ["playbook", "install", "dgx-dashboard"], input="y\n")
        assert result.exit_code == 0, result.output
        out = _one_line(result.output)
        assert "no commands to run" in out and f"{len(plan['manual_steps'])} manual step(s)" in out
        assert "Left to you" in out
        assert len(fake.calls) == 1


# ───────────────────────────────────────────────────────────────────────────
# Dispatch and help — `playbook` is a command, bare questions stay questions
# ───────────────────────────────────────────────────────────────────────────


class _AppProxy:
    def __init__(self, real, events):
        self._real, self._events = real, events

    def __getattr__(self, name):
        return getattr(self._real, name)

    def __call__(self, *args, **kwargs):
        self._events.append("app")


@pytest.fixture()
def dispatch(monkeypatch):
    """Run main() with argv; a real `app()` or LLM path is replaced by a marker."""
    events: list[str] = []

    def fake_run(coro):
        if hasattr(coro, "close"):
            coro.close()
            events.append("smart_default")

    monkeypatch.setattr(cli_main, "app", _AppProxy(cli_main.app, events))
    monkeypatch.setattr(cli_main, "_run", fake_run)
    monkeypatch.setattr(cli_main, "_launch_default_repl", lambda: events.append("repl"))
    monkeypatch.setattr(cli_main.sys, "stdin", types.SimpleNamespace(isatty=lambda: True))

    def run(*argv: str) -> tuple[list[str], int | None]:
        monkeypatch.setattr(cli_main.sys, "argv", ["nvh", *argv])
        events.clear()
        try:
            cli_main.main()
        except SystemExit as exc:
            return list(events), exc.code
        return list(events), None

    return run


class TestDispatch:
    def test_playbook_is_a_registered_command(self):
        assert "playbook" in cli_main._known_commands()
        assert cli_main._suggest_commands(["playbok", "list"]) == ["playbook"]

    def test_playbook_verbs_go_to_typer(self, dispatch):
        assert dispatch("playbook", "list") == (["app"], None)
        assert dispatch("playbook", "install", "ollama", "-y") == (["app"], None)
        assert dispatch("PLAYBOOK", "plan", "vllm") == (["app"], None)

    def test_a_question_about_playbooks_is_still_a_question(self, dispatch):
        assert dispatch("which", "playbook", "installs", "ollama?") == (["smart_default"], None)

    def test_group_help_lists_the_three_verbs(self, runner: CliRunner):
        result = runner.invoke(cli_main.app, ["playbook", "--help"])
        assert result.exit_code == 0
        out = _plain(result.output)
        for verb in ("list", "plan", "install"):
            assert re.search(rf"\b{verb}\b", out), verb
        assert "sudo" in out

    def test_install_help_names_the_flags(self, runner: CliRunner):
        out = _plain(runner.invoke(cli_main.app, ["playbook", "install", "--help"]).output)
        assert "--yes" in out and "--home-dir" in out
        assert "password" in out

    def test_generated_commands_doc_lists_the_group(self):
        doc = (Path(__file__).resolve().parents[1] / "docs" / "COMMANDS.md").read_text(encoding="utf-8")
        assert "`nvh playbook <command>`" in doc
        assert "### `nvh playbook`" in doc
        for verb in ("list", "plan PLAYBOOK_ID", "install PLAYBOOK_ID"):
            assert f"`nvh playbook {verb}`" in doc, verb


def test_docs_describe_the_approval_model_without_hand_typed_counts():
    root = Path(__file__).resolve().parents[1]
    getting_started = (root / "docs" / "GETTING_STARTED.md").read_text(encoding="utf-8")
    assert "## Spark playbooks" in getting_started
    for phrase in ("nvh playbook list", "nvh playbook plan", "nvh playbook install", "never executes", "Decisions/", "never `newgrp`"):
        assert phrase in getting_started, phrase
    configuration = (root / "docs" / "CONFIGURATION.md").read_text(encoding="utf-8")
    row = next(ln for ln in configuration.splitlines() if "`NVH_ALLOW_PRIVILEGED`" in ln)
    assert "playbook_install" in row and "nvh playbook install <id>" in row
    # No "N playbooks"-style inventory count in either doc (tests/test_marketing_parity.py scans the other nouns).
    assert not re.search(r"\b\d+\s+playbooks\b", getting_started + configuration)
