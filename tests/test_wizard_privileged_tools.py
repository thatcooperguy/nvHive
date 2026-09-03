"""The Wizard's ``privileged`` tool class and the system-settings module.

Hermetic throughout: ``NVH_HOME`` is ``tmp_path``, platform facts are seeded
(never probed), ``subprocess`` inside :mod:`nvh.integrations.wizard.system_settings`
is a recording fake, binaries "exist" via a patched ``_which`` and /etc files
via a patched ``_read_text``. Nothing here runs ``sudo``, ``apt`` or
``systemctl`` for real — a fake that sees an unexpected command fails the test.

Invariants pinned (docs/proposals/SPARK_CONCIERGE_2026-09.md §3.4, §5):

  - ``WizardToolRegistry.execute()`` is the only enforcement point: the kill
    switch (``NVH_ALLOW_PRIVILEGED``) is checked there on the card path and
    the confirmed path; registration is unaffected.
  - Unconfirmed → the confirm card plus ``privileged: True``, ``plan`` and an
    ``approval_token`` bound to that exact call; confirmed → only with that
    token (15 min, single use), then the handler runs, an apply that touched
    the host (complete, partial or failed) writes a vault note under
    ``Decisions/``; the result is fitted to the 1500-char window.
  - The HTTP layer adds the one check only it can make — where a confirmed
    privileged call came from (open mode on the network, foreign Host /
    Origin) — and otherwise returns the registry's answer unchanged.
  - ``never`` still raises at registration; ``list_tools()`` orders
    auto, confirm, privileged by an explicit key.
  - The sudo matrix: ``can_sudo`` → ``sudo -n``; group-only →
    ``needs_terminal`` with the exact command and no subprocess; neither →
    "this account cannot elevate". There is no password anywhere.
  - The deny list (``check_command`` + the module's own) fires before any
    subprocess; ``apt_install`` refuses driver packages with the DGX warning.
  - ``chat.py`` buckets a privileged call as needing confirmation (see also
    tests/test_wizard_profile_enforcement.py).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

import nvh.integrations.wizard.system_settings as ss
from nvh.integrations.wizard.tools import (
    APPROVAL_REQUIRED_ERROR,
    APPROVAL_TTL_S,
    PRIVILEGED_DISABLED_ERROR,
    PRIVILEGED_ENV,
    TOOL_RESULT_CHARS,
    WizardTool,
    WizardToolRegistry,
    default_registry,
    fit_tool_window,
    issue_approval,
    privileged_enabled,
    verify_approval,
)
from nvh.utils import platform_facts as pf

SPARK_LABEL = "NVIDIA DGX Spark (GB10, 128 GB unified)"
PRIVILEGED_TOOLS = ("system_settings_apply", "apt_install", "snap_install", "service_enable")


# ───────────────────────────────────────────────────────────────────────────
# Fixtures and doubles
# ───────────────────────────────────────────────────────────────────────────


def seed(*, can_sudo: bool = False, in_sudo_group: bool = False, has_root: bool = False) -> None:
    """Seed platform facts for a DGX Spark with the given privilege answer (no probes)."""
    pf.seed_platform_facts(pf.PlatformFacts(
        os="linux", arch="arm64", machine="aarch64", distro="DGX OS 7.2", kernel="6.11.0-1016-nvidia",
        is_dgx_os=True, gpu_name="NVIDIA GB10", unified_memory=True, memory_total_gb=128.0,
        memory_available_gb=100.0, device_class="dgx-spark", device_label=SPARK_LABEL,
        has_root=has_root, can_sudo=can_sudo, in_sudo_group=in_sudo_group or can_sudo or has_root,
    ))


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("NVH_HOME", str(tmp_path))
    monkeypatch.delenv(PRIVILEGED_ENV, raising=False)
    monkeypatch.setattr(ss, "_current_user", lambda: "alice")
    monkeypatch.setattr(ss, "_which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(ss, "_read_text", lambda path: "")
    seed()


class FakeSubprocess:
    """Stands in for the ``subprocess`` module inside system_settings.

    ``run`` records every argv and answers from ``responder(argv)`` →
    ``(returncode, stdout, stderr)``; the default is exit 0 with "done".
    """

    DEVNULL = -3

    class TimeoutExpired(Exception):  # noqa: N818 — must match subprocess's name
        def __init__(self, cmd, timeout):
            super().__init__(f"timeout {timeout}")

    def __init__(self, responder=None) -> None:
        self.calls: list[list[str]] = []
        self.kwargs: list[dict[str, Any]] = []
        self.responder = responder or (lambda argv: (0, "done\n", ""))

    def run(self, argv, **kwargs):
        self.calls.append(list(argv))
        self.kwargs.append(kwargs)
        answer = self.responder(list(argv))
        if isinstance(answer, BaseException):
            raise answer
        code, out, err = answer
        return SimpleNamespace(returncode=code, stdout=out.encode(), stderr=err.encode())


@pytest.fixture()
def fake_run(monkeypatch) -> FakeSubprocess:
    fake = FakeSubprocess()
    monkeypatch.setattr(ss, "subprocess", fake)
    return fake


class _Counter:
    def __init__(self, result: dict[str, Any] | None = None, raises: Exception | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.result = result if result is not None else {"ok": True, "summary": "stub applied"}
        self.raises = raises

    async def __call__(self, args: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(dict(args))
        if self.raises is not None:
            raise self.raises
        return dict(self.result)


def _stub_registry(
    *, result: dict[str, Any] | None = None, plan: dict[str, Any] | None = None, planner_raises: bool = False,
) -> tuple[WizardToolRegistry, _Counter, _Counter]:
    handler = _Counter(result)
    planner = _Counter(
        plan if plan is not None else {"ok": True, "commands": ["sudo systemctl enable --now ssh"], "sudo": True},
        raises=RuntimeError("planner boom") if planner_raises else None,
    )
    reg = WizardToolRegistry()
    reg.register(WizardTool(
        name="stub_priv", description="Stub privileged tool", safety_class="privileged",
        parameters={"setting": {"type": "string", "required": True}}, handler=handler, planner=planner,
        summary_template="Apply {setting}.",
    ))
    reg.register(WizardTool(
        name="stub_auto", description="Stub auto", safety_class="auto", parameters={}, handler=_Counter({"ran": True}),
    ))
    reg.register(WizardTool(
        name="stub_confirm", description="Stub confirm", safety_class="confirm", parameters={},
        handler=_Counter({"ran": True}),
    ))
    return reg, handler, planner


def _decisions(tmp_path: Path) -> list[Path]:
    """The privileged-change notes under the vault's Decisions/ (init_vault seeds
    product notes there too, so filter on the tag the audit sink writes)."""
    folder = tmp_path / "vault" / "Decisions"
    if not folder.is_dir():
        return []
    return sorted(p for p in folder.glob("*.md") if "#privileged" in p.read_text(encoding="utf-8"))


async def _approve(reg: WizardToolRegistry, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    """The red-card flow: fetch the card (plan + token), then confirm with that token."""
    card = await reg.execute(name, arguments=arguments)
    assert card["needs_confirmation"] is True and card["privileged"] is True
    return await reg.execute(name, arguments=arguments, confirmed=True, approval_token=card["approval_token"])


def _loopback_client() -> TestClient:
    """A client whose requests carry a loopback ``Host`` (the default ``testserver`` is not one)."""
    from nvh.api.server import app

    return TestClient(app, base_url="http://127.0.0.1:8000")


# ───────────────────────────────────────────────────────────────────────────
# The class: registration, ordering, the kill switch
# ───────────────────────────────────────────────────────────────────────────


def test_register_accepts_privileged_and_never_still_raises() -> None:
    reg = WizardToolRegistry()
    reg.register(WizardTool(
        name="p", description="p", safety_class="privileged", parameters={}, handler=_Counter(),
    ))
    assert reg.get("p") is not None and reg.get("p").safety_class == "privileged"
    with pytest.raises(ValueError, match="never"):
        reg.register(WizardTool(name="n", description="n", safety_class="never", parameters={}, handler=_Counter()))
    with pytest.raises(ValueError, match="safety_class"):
        reg.register(WizardTool(name="s", description="s", safety_class="sudo", parameters={}, handler=_Counter()))


def test_list_tools_orders_auto_confirm_privileged_by_explicit_key() -> None:
    """Names chosen so alphabetical order would be the reverse of the class order."""
    reg = WizardToolRegistry()
    for name, cls in (("aaa_priv", "privileged"), ("bbb_conf", "confirm"), ("zzz_auto", "auto"), ("aab_priv", "privileged")):
        reg.register(WizardTool(name=name, description=name, safety_class=cls, parameters={}, handler=_Counter()))
    assert [t.name for t in reg.list_tools()] == ["zzz_auto", "bbb_conf", "aaa_priv", "aab_priv"]


@pytest.mark.parametrize("raw, expected", [
    (None, True), ("", True), ("1", True), ("true", True), ("yes", True),
    ("0", False), ("false", False), ("No", False), ("OFF", False), ("  off ", False),
])
def test_privileged_enabled_reads_the_full_falsy_vocabulary(monkeypatch, raw, expected) -> None:
    if raw is None:
        monkeypatch.delenv(PRIVILEGED_ENV, raising=False)
    else:
        monkeypatch.setenv(PRIVILEGED_ENV, raw)
    assert privileged_enabled() is expected


def test_public_dict_carries_enabled_which_follows_the_kill_switch(monkeypatch) -> None:
    reg, _handler, _planner = _stub_registry()
    assert reg.get("stub_priv").as_public_dict()["enabled"] is True
    assert reg.get("stub_auto").as_public_dict()["enabled"] is True
    monkeypatch.setenv(PRIVILEGED_ENV, "0")
    assert reg.get("stub_priv").as_public_dict()["enabled"] is False
    # Only the privileged class is switched off.
    assert reg.get("stub_auto").as_public_dict()["enabled"] is True
    assert reg.get("stub_confirm").as_public_dict()["enabled"] is True
    assert reg.get("stub_priv").as_public_dict()["safety_class"] == "privileged"


@pytest.mark.asyncio
async def test_kill_switch_off_refuses_on_both_paths_but_keeps_the_tool_registered(monkeypatch, tmp_path) -> None:
    reg, handler, planner = _stub_registry()
    monkeypatch.setenv(PRIVILEGED_ENV, "0")
    assert reg.get("stub_priv") is not None  # still in the catalogue
    for confirmed in (False, True):
        result = await reg.execute("stub_priv", arguments={"setting": "x"}, confirmed=confirmed)
        assert result == {
            "ok": False,
            "error": PRIVILEGED_DISABLED_ERROR,
            "disabled": True,
            "tool": "stub_priv",
            "safety_class": "privileged",
        }
        assert "NVH_ALLOW_PRIVILEGED=0" in result["error"]
    assert handler.calls == [] and planner.calls == []
    assert _decisions(tmp_path) == []
    # auto / confirm tools are untouched by the switch.
    assert (await reg.execute("stub_auto"))["ok"] is True
    assert (await reg.execute("stub_confirm", confirmed=True))["ok"] is True


@pytest.mark.asyncio
async def test_kill_switch_is_read_per_call_not_at_registration(monkeypatch) -> None:
    """Flipping the variable after the registry was built changes execute()'s answer."""
    reg, handler, _planner = _stub_registry()
    monkeypatch.setenv(PRIVILEGED_ENV, "0")
    assert (await reg.execute("stub_priv", arguments={"setting": "x"}, confirmed=True))["disabled"] is True
    monkeypatch.setenv(PRIVILEGED_ENV, "1")
    assert (await _approve(reg, "stub_priv", {"setting": "x"}))["ok"] is True
    assert len(handler.calls) == 1


# ───────────────────────────────────────────────────────────────────────────
# execute(): the card with a plan, the confirmed run, the audit note
# ───────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_unconfirmed_privileged_call_returns_card_with_plan_and_runs_nothing() -> None:
    reg, handler, planner = _stub_registry()
    card = await reg.execute("stub_priv", arguments={"setting": "enable_ssh"})
    assert card["ok"] is False
    assert card["needs_confirmation"] is True
    assert card["privileged"] is True
    assert card["plan"]["commands"] == ["sudo systemctl enable --now ssh"]
    assert card["plan"]["sudo"] is True
    assert card["summary"] == "Apply enable_ssh."
    assert card["arguments"] == {"setting": "enable_ssh"}
    assert card["tool"]["name"] == "stub_priv" and card["tool"]["enabled"] is True
    assert "handler" not in card["tool"]
    # The token the click must bring back, bound to this exact call.
    assert isinstance(card["approval_token"], str) and "." in card["approval_token"]
    assert card["approval_expires_at"] > 0
    assert verify_approval("stub_priv", {"setting": "enable_ssh"}, card["approval_token"]) is True
    assert planner.calls == [{"setting": "enable_ssh"}]
    assert handler.calls == []


@pytest.mark.asyncio
async def test_card_survives_a_planner_that_raises_and_a_tool_without_planner() -> None:
    reg, handler, _planner = _stub_registry(planner_raises=True)
    card = await reg.execute("stub_priv", arguments={"setting": "x"})
    assert card["needs_confirmation"] is True and card["privileged"] is True
    assert card["plan"]["ok"] is False and "dry run failed" in card["plan"]["error"]
    assert card["plan"]["commands"] == []
    assert handler.calls == []

    bare = WizardToolRegistry()
    bare.register(WizardTool(
        name="no_plan", description="no planner", safety_class="privileged", parameters={}, handler=_Counter(),
    ))
    card = await bare.execute("no_plan")
    assert card["needs_confirmation"] is True and card["privileged"] is True and card["plan"] is None


@pytest.mark.asyncio
async def test_confirmed_privileged_call_runs_and_writes_a_decisions_note(tmp_path: Path) -> None:
    result_from_handler = {
        "ok": True,
        "applied": True,
        "summary": "Enable and start the OpenSSH server",
        "steps": [
            {"command": "sudo -n systemctl enable --now ssh", "exit_code": 0,
             "stdout": "Created symlink /etc/systemd/system/multi-user.target.wants/ssh.service", "stderr": ""},
        ],
    }
    reg, handler, planner = _stub_registry(result=result_from_handler)
    out = await _approve(reg, "stub_priv", {"setting": "enable_ssh"})

    assert out["ok"] is True and out["tool"] == "stub_priv" and out["safety_class"] == "privileged"
    assert out["result"]["summary"] == "Enable and start the OpenSSH server"
    assert handler.calls == [{"setting": "enable_ssh"}]
    assert planner.calls == [{"setting": "enable_ssh"}]  # the card planned once; the confirmed path does not re-plan
    assert out["audit"]["saved"] is True and out["audit"]["category"] == "Decisions"

    notes = _decisions(tmp_path)
    assert len(notes) == 1 and Path(out["audit"]["path"]) == notes[0]
    text = notes[0].read_text(encoding="utf-8")
    assert text.startswith("# Privileged change: Enable and start the OpenSSH server")
    assert "Category: Decisions" in text
    assert "Outcome: applied" in text
    assert "#privileged" in text and "#stub_priv" in text
    assert "`sudo -n systemctl enable --now ssh` — exit 0" in text
    assert "Created symlink" in text
    assert SPARK_LABEL in text
    assert '"setting": "enable_ssh"' in text


@pytest.mark.asyncio
@pytest.mark.parametrize("result", [
    {"ok": False, "needs_terminal": True, "command": "sudo systemctl enable --now ssh", "hint": ss.NEEDS_TERMINAL_HINT},
    {"ok": False, "error": ss.CANNOT_ELEVATE_ERROR},
    {"ok": False, "denied": True, "error": "BLOCKED: system shutdown"},
])
async def test_refusals_and_terminal_handoffs_get_no_vault_note(tmp_path: Path, result) -> None:
    reg, _handler, _planner = _stub_registry(result=result)
    out = await _approve(reg, "stub_priv", {"setting": "x"})
    assert out["ok"] is True  # the tool answered …
    assert out["result"]["ok"] is False  # … with a refusal
    assert "audit" not in out
    assert _decisions(tmp_path) == []


@pytest.mark.asyncio
async def test_partial_apply_is_still_audited(tmp_path: Path) -> None:
    reg, _handler, _planner = _stub_registry(result={
        "ok": False, "applied": True, "partial": True, "error": "`sudo -n ufw --force enable` exited 1",
        "summary": "Firewall: deny incoming except over Tailscale",
        "steps": [{"command": "sudo -n ufw default deny incoming", "exit_code": 0, "stdout": "", "stderr": ""}],
    })
    out = await _approve(reg, "stub_priv", {"setting": "fw"})
    assert out["audit"]["saved"] is True
    notes = _decisions(tmp_path)
    assert len(notes) == 1
    text = notes[0].read_text(encoding="utf-8")
    assert text.startswith("# Privileged change (partial): Firewall: deny incoming except over Tailscale")
    assert "Outcome: partial — `sudo -n ufw --force enable` exited 1" in text
    assert "`sudo -n ufw default deny incoming` — exit 0" in text


@pytest.mark.asyncio
async def test_single_step_failure_that_ran_is_audited_as_failed(tmp_path: Path) -> None:
    """``systemctl enable --now foo`` exiting 1 still enabled the unit; the note says failed, with the exit code."""
    reg, _handler, _planner = _stub_registry(result={
        "ok": False, "applied": True, "partial": False, "error": "`sudo -n systemctl enable --now foo` exited 1",
        "summary": "Enable and start foo",
        "steps": [{"command": "sudo -n systemctl enable --now foo", "exit_code": 1, "stdout": "", "stderr": "Job failed"}],
    })
    out = await _approve(reg, "stub_priv", {"setting": "foo"})
    assert out["audit"]["saved"] is True
    text = _decisions(tmp_path)[0].read_text(encoding="utf-8")
    assert text.startswith("# Privileged change (failed): Enable and start foo")
    assert "Outcome: failed — `sudo -n systemctl enable --now foo` exited 1" in text
    assert "`sudo -n systemctl enable --now foo` — exit 1" in text and "Job failed" in text


@pytest.mark.asyncio
async def test_handler_exception_never_escapes_execute(tmp_path: Path) -> None:
    reg = WizardToolRegistry()
    reg.register(WizardTool(
        name="boom", description="boom", safety_class="privileged", parameters={},
        handler=_Counter(raises=RuntimeError("kaboom")),
    ))
    out = await _approve(reg, "boom")
    assert out == {"ok": False, "error": "kaboom", "tool": "boom"}
    assert _decisions(tmp_path) == []


@pytest.mark.asyncio
async def test_audit_failure_is_reported_not_raised(monkeypatch, tmp_path: Path) -> None:
    import nvh.integrations.workspace.vault as vault_mod

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(vault_mod, "append_vault_memory", boom)
    reg, _handler, _planner = _stub_registry()
    out = await _approve(reg, "stub_priv", {"setting": "x"})
    assert out["ok"] is True
    assert out["audit"]["saved"] is False and "disk full" in out["audit"]["error"]


@pytest.mark.asyncio
async def test_privileged_result_is_fitted_to_the_tool_window_and_the_note_keeps_more(tmp_path: Path) -> None:
    big = "x" * 20_000
    reg, _handler, _planner = _stub_registry(result={
        "ok": True, "summary": "big output", "steps": [{"command": "sudo -n apt-get install -y htop", "exit_code": 0, "stdout": big, "stderr": ""}],
    })
    out = await _approve(reg, "stub_priv", {"setting": "x"})
    payload = json.dumps(out["result"], default=str)
    assert len(payload) <= TOOL_RESULT_CHARS
    assert out["result"]["truncated"] is True and "vault" in out["result"]["note"]
    assert out["result"]["steps"][0]["exit_code"] == 0
    note_text = _decisions(tmp_path)[0].read_text(encoding="utf-8")
    assert "x" * 4000 in note_text  # the note keeps up to AUDIT_OUTPUT_CHARS per stream
    assert "x" * 4001 not in note_text
    assert "[cut at 4000 chars]" in note_text


def test_fit_tool_window_unit() -> None:
    small = {"ok": True, "stdout": "fine"}
    assert fit_tool_window(small) is small
    fitted = fit_tool_window({"ok": True, "stdout": "y" * 5000, "steps": [{"stdout": "z" * 5000, "exit_code": 0}]}, limit=600)
    assert len(json.dumps(fitted)) <= 600
    assert fitted["truncated"] is True and fitted["ok"] is True
    assert fitted["steps"][0]["exit_code"] == 0
    # Something un-shrinkable still yields a verdict, never an exception.
    stubborn = fit_tool_window({"ok": True, "blob": "w" * 5000, "summary": "s"}, limit=200)
    assert stubborn["ok"] is True and stubborn["summary"] == "s" and "blob" in stubborn["dropped_keys"]


def test_fit_tool_window_keeps_the_handoff_contract_for_a_21_package_hold() -> None:
    """The DGX OS hold with 21 driver packages, group-only sudo: no stdout to
    shrink, yet the fields the hand-off depends on — ``needs_terminal``, the
    exact ``command``, ``hint`` — must survive, and the command is never cut."""
    held = sorted(
        [f"libnvidia-{c}-580" for c in ("cfg1", "compute", "decode", "encode", "extra", "fbc", "gl", "common")]
        + [f"nvidia-{c}-580" for c in ("compute-utils", "dkms", "driver", "firmware", "kernel-common", "kernel-source", "utils")]
        + [f"nvidia-{c}-580-open" for c in ("driver", "kernel", "dkms")]
        + ["cuda-drivers-580", "cuda-drivers", "nvidia-settings"],
    )
    assert len(held) == 21
    command = "sudo apt-mark hold " + " ".join(held)
    handoff = {
        "ok": False, "needs_terminal": True, "setting": "hold_nvidia_driver_packages", "command": command,
        "commands": [command], "hint": ss.NEEDS_TERMINAL_HINT, "steps": [], "applied": False, "partial": False,
        "undo": ["sudo apt-mark unhold " + " ".join(held)],
    }
    assert len(json.dumps(handoff)) > TOOL_RESULT_CHARS, "the example must exceed the window to mean anything"
    fitted = fit_tool_window(handoff)
    assert len(json.dumps(fitted)) <= TOOL_RESULT_CHARS
    assert fitted["needs_terminal"] is True and fitted["ok"] is False
    assert fitted["command"] == command  # exact, uncut
    assert fitted["hint"] == ss.NEEDS_TERMINAL_HINT
    assert fitted["setting"] == "hold_nvidia_driver_packages"
    assert fitted["applied"] is False and fitted["partial"] is False
    assert fitted["truncated"] is True
    # A short hand-off is left alone entirely.
    small = {"ok": False, "needs_terminal": True, "command": "sudo systemctl enable --now ssh", "hint": ss.NEEDS_TERMINAL_HINT}
    assert fit_tool_window(small) is small
    # Lists are shortened before anything is dropped.
    listy = {"ok": True, "summary": "s", "notes": [f"note {i} " + "n" * 40 for i in range(60)]}
    fitted = fit_tool_window(listy, limit=700)
    assert len(json.dumps(fitted)) <= 700 and fitted["summary"] == "s"
    assert fitted["notes"][-1].startswith("… ") and fitted["notes"][-1].endswith(" more")


# ───────────────────────────────────────────────────────────────────────────
# run_host_command: the sudo matrix and the deny list
# ───────────────────────────────────────────────────────────────────────────


def test_can_sudo_prefixes_sudo_n_and_never_opens_stdin(fake_run: FakeSubprocess) -> None:
    seed(can_sudo=True)
    out = ss.run_host_command(["systemctl", "enable", "--now", "ssh"], sudo=True, timeout=42)
    assert out["ok"] is True and out["exit_code"] == 0 and out["stdout"] == "done"
    assert out["command"] == "sudo -n systemctl enable --now ssh"
    assert fake_run.calls == [["sudo", "-n", "systemctl", "enable", "--now", "ssh"]]
    kw = fake_run.kwargs[0]
    assert kw["stdin"] == FakeSubprocess.DEVNULL and kw["capture_output"] is True and kw["timeout"] == 42
    assert "input" not in kw and "shell" not in kw


def test_group_only_hands_the_exact_command_to_a_terminal_and_runs_nothing(fake_run: FakeSubprocess) -> None:
    seed(in_sudo_group=True)
    out = ss.run_host_command(["systemctl", "enable", "--now", "ssh"], sudo=True)
    assert out == {
        "ok": False,
        "needs_terminal": True,
        "command": "sudo systemctl enable --now ssh",
        "hint": "run this in a terminal; nvHive never asks for your password",
    }
    assert fake_run.calls == []


def test_no_privilege_is_refused_without_a_subprocess(fake_run: FakeSubprocess) -> None:
    seed()
    out = ss.run_host_command(["systemctl", "enable", "--now", "ssh"], sudo=True)
    assert out["ok"] is False and out["error"] == "this account cannot elevate"
    assert out["command"] == "sudo systemctl enable --now ssh"
    assert fake_run.calls == []


def test_root_runs_without_sudo_and_sudo_user_uses_sudo_n_u(fake_run: FakeSubprocess) -> None:
    seed(has_root=True)
    ss.run_host_command(["apt-mark", "hold", "nvidia-driver-580-open"], sudo=True)
    assert fake_run.calls[-1] == ["apt-mark", "hold", "nvidia-driver-580-open"]
    seed(can_sudo=True)
    out = ss.run_host_command(["dbus-launch", "gsettings", "get", "a.b", "c"], sudo=True, sudo_user="gdm")
    assert fake_run.calls[-1] == ["sudo", "-n", "-u", "gdm", "dbus-launch", "gsettings", "get", "a.b", "c"]
    assert out["command"] == "sudo -n -u gdm dbus-launch gsettings get a.b c"


def test_sudo_false_runs_directly_whatever_the_facts_say(fake_run: FakeSubprocess) -> None:
    seed()
    out = ss.run_host_command(["id", "-nG"], sudo=False)
    assert out["ok"] is True and fake_run.calls == [["id", "-nG"]]


def test_spawn_failures_are_in_band(monkeypatch) -> None:
    seed(can_sudo=True)
    fake = FakeSubprocess(responder=lambda argv: FileNotFoundError("nope"))
    monkeypatch.setattr(ss, "subprocess", fake)
    out = ss.run_host_command(["ufw", "status"], sudo=False)
    assert out["ok"] is False and "command not found" in out["error"]
    fake.responder = lambda argv: FakeSubprocess.TimeoutExpired(argv, 5)
    out = ss.run_host_command(["ufw", "status"], sudo=False, timeout=5)
    assert out["ok"] is False and out["timed_out"] is True
    fake.responder = lambda argv: PermissionError("denied")
    out = ss.run_host_command(["ufw", "status"], sudo=False)
    assert out["ok"] is False and "PermissionError" in out["error"]
    assert ss.run_host_command([], sudo=False) == {"ok": False, "error": "empty command"}


@pytest.mark.parametrize("argv, fragment", [
    (["shutdown", "-h", "now"], "shutdown"),
    (["reboot"], "reboot"),
    (["systemctl", "poweroff"], "shutdown"),
    (["rm", "-rf", "/"], "root"),
    (["chown", "-R", "alice", "/"], "chown"),
    (["mkfs.ext4", "/dev/nvme0n1p2"], "disk formatting"),
    (["dd", "if=/dev/zero", "of=/dev/nvme0n1"], "disk"),
    (["passwd", "alice"], "password"),
    (["usermod", "-aG", "sudo", "alice"], "usermod -aG docker"),
    (["ufw", "disable"], "firewall"),
    (["iptables", "-F"], "firewall"),
    (["apt-get", "upgrade"], "DGX OS"),
    (["apt", "full-upgrade", "-y"], "DGX OS"),
    (["apt-get", "purge", "-y", "nvidia-driver-580-open"], "driver removal"),
    (["modprobe", "-r", "nvidia"], "driver removal"),
    (["systemctl", "enable", "--now", "systemd-suspend.service"], "sleep"),
    (["systemctl", "start", "suspend.target"], "sleep"),
    (["systemctl", "enable", "--now", "systemd-poweroff.service"], "shutdown"),
])
def test_deny_list_fires_before_any_subprocess(fake_run: FakeSubprocess, argv, fragment) -> None:
    seed(can_sudo=True)
    out = ss.run_host_command(argv, sudo=True)
    assert out["ok"] is False and out["denied"] is True
    assert out["error"].startswith("BLOCKED")
    assert fragment.lower() in out["error"].lower()
    assert fake_run.calls == []


def test_recursive_rm_is_allowed_only_strictly_inside_nvh_home(tmp_path: Path) -> None:
    inside = tmp_path / "cache" / "old"
    assert ss.denied_reason(["rm", "-rf", str(inside)]) is None
    assert ss.denied_reason(["rm", "-r", str(tmp_path.parent / "elsewhere")]) is not None
    assert "outside NVH_HOME" in ss.denied_reason(["rm", "-rf", str(tmp_path.parent / "elsewhere")])
    assert ss.denied_reason(["rm", "-rf", str(tmp_path)]) is not None  # NVH_HOME itself is off limits
    assert ss.denied_reason(["rm", "-rf"]) == "BLOCKED: recursive rm without a target"
    assert ss.denied_reason(["rm", str(inside / "one-file")]) is None  # non-recursive: guardrails only
    assert ss.denied_reason(["sudo", "usermod", "-aG", "docker", "alice"]) is None


def test_output_is_redacted_and_cut_at_one_megabyte(monkeypatch) -> None:
    seed()
    secret_out = "TOKEN=abcdefghijklmnop12345\nBearer abcdefghijklmnopqrstuvwxyz0123456789\nAKIAABCDEFGHIJKLMNOP\n"
    fake = FakeSubprocess(responder=lambda argv: (0, secret_out, "key sk-abcdefghijklmnopqrstuvwxyz0123456789 rejected"))
    monkeypatch.setattr(ss, "subprocess", fake)
    out = ss.run_host_command(["id", "-nG"], sudo=False)
    assert "abcdefghijklmnop12345" not in out["stdout"] and "[REDACTED:env_secret]" in out["stdout"]
    assert "AKIAABCDEFGHIJKLMNOP" not in out["stdout"] and "[REDACTED:aws_key]" in out["stdout"]
    assert "[REDACTED:bearer_token]" in out["stdout"]
    assert "sk-abcdef" not in out["stderr"] and "[REDACTED:api_key]" in out["stderr"]

    fake.responder = lambda argv: (0, "y" * (2 * 1024 * 1024), "")
    out = ss.run_host_command(["id", "-nG"], sudo=False)
    assert len(out["stdout"]) < 1024 * 1024 + 200
    assert "[TRUNCATED" in out["stdout"]


# ───────────────────────────────────────────────────────────────────────────
# The catalogue: plans, applies, validation
# ───────────────────────────────────────────────────────────────────────────


def test_catalogue_plans_render_exact_commands_and_run_nothing(fake_run: FakeSubprocess, monkeypatch) -> None:
    fake_run.responder = lambda argv: (0, "nvidia-driver-580-open\nlibnvidia-compute-580\nnvidia-container-toolkit\nlibnvidia-container1\nhtop\ncuda-drivers-580\n", "") if argv[0] == "dpkg-query" else (0, "", "")
    monkeypatch.setattr(ss.socket, "gethostname", lambda: "spark-old")
    expected = {
        "disable_headless_suspend": [
            "sudo -u gdm dbus-launch gsettings set org.gnome.settings-daemon.plugins.power sleep-inactive-ac-type nothing",
            "dbus-launch gsettings set org.gnome.settings-daemon.plugins.power sleep-inactive-ac-type nothing",
        ],
        "add_user_to_docker_group": ["sudo groupadd -f docker", "sudo usermod -aG docker alice"],
        "enable_ssh": ["sudo systemctl enable --now ssh"],
        "enable_ufw_tailscale_only": [
            "sudo ufw default deny incoming", "sudo ufw default allow outgoing",
            "sudo ufw allow in on tailscale0", "sudo ufw --force enable",
        ],
        "set_hostname": ["sudo hostnamectl set-hostname spark-01"],
        "hold_nvidia_driver_packages": ["sudo apt-mark hold cuda-drivers-580 libnvidia-compute-580 nvidia-driver-580-open"],
    }
    assert set(expected) == set(ss.SETTINGS)
    for setting, commands in expected.items():
        plan = ss.plan_setting({"setting": setting, "value": "spark-01"})
        assert plan["ok"] is True, plan
        assert plan["setting"] == setting  # named after the catalogue key, not retyped
        assert plan["commands"] == commands
        assert plan["sudo"] is True
        assert plan["changes"] and plan["undo"]
    # The only subprocess the planning phase spawned is the read-only package query.
    assert fake_run.calls == [["dpkg-query", "-W", "-f=${Package}\\n"]]
    hold = ss.plan_setting({"setting": "hold_nvidia_driver_packages"})
    assert hold["warning"] == ss.DGX_DRIVER_WARNING
    assert "nvidia-container-toolkit" not in hold["commands"][0] and "libnvidia-container1" not in hold["commands"][0]
    ufw = ss.plan_setting({"setting": "enable_ufw_tailscale_only"})
    assert "disconnects you" in ufw["warning"]
    host = ss.plan_setting({"setting": "set_hostname", "value": "spark-01"})
    assert host["undo"] == ["sudo hostnamectl set-hostname spark-old"]


def test_plan_validation_errors_are_in_band(fake_run: FakeSubprocess) -> None:
    assert ss.plan_setting({})["error"] == "setting required"
    unknown = ss.plan_setting({"setting": "format_disk"})
    assert unknown["ok"] is False and "unknown setting" in unknown["error"] and unknown["commands"] == []
    assert [c["setting"] for c in unknown["catalogue"]] == list(ss.SETTINGS)
    assert "value required" in ss.plan_setting({"setting": "set_hostname"})["error"]
    assert "value required" in ss.plan_setting({"setting": "set_hostname", "value": "bad name; rm -rf /"})["error"]
    fake_run.responder = lambda argv: (1, "", "dpkg-query: not found")
    assert "no installed NVIDIA driver packages" in ss.plan_setting({"setting": "hold_nvidia_driver_packages"})["error"]
    assert ss.apply_setting({"setting": "set_hostname"})["ok"] is False
    assert ss.apply_setting({})["error"] == "setting required"
    assert [c for c in fake_run.calls if c[0] != "dpkg-query"] == []


def test_apply_enable_ssh_with_passwordless_sudo_runs_and_reports_steps(fake_run: FakeSubprocess) -> None:
    seed(can_sudo=True)
    out = ss.apply_setting({"setting": "enable_ssh"})
    assert out["ok"] is True and out["applied"] is True and out["setting"] == "enable_ssh"
    assert out["summary"] == "Enable and start the OpenSSH server"
    assert out["steps"] == [{"command": "sudo -n systemctl enable --now ssh", "exit_code": 0, "stdout": "done", "stderr": ""}]
    assert out["undo"] == ["sudo systemctl disable --now ssh"]
    assert fake_run.calls == [["sudo", "-n", "systemctl", "enable", "--now", "ssh"]]


def test_apply_group_only_hands_off_the_first_sudo_step_and_runs_nothing(fake_run: FakeSubprocess, tmp_path) -> None:
    seed(in_sudo_group=True)
    out = ss.apply_setting({"setting": "enable_ufw_tailscale_only"})
    assert out["ok"] is False and out["needs_terminal"] is True
    assert out["command"] == "sudo ufw default deny incoming"
    assert out["commands"] == [
        "sudo ufw default deny incoming", "sudo ufw default allow outgoing",
        "sudo ufw allow in on tailscale0", "sudo ufw --force enable",
    ]
    assert out["hint"] == ss.NEEDS_TERMINAL_HINT
    assert out["applied"] is False and out["steps"] == []
    assert fake_run.calls == []


def test_apply_disable_headless_suspend_runs_both_steps_in_order(fake_run: FakeSubprocess) -> None:
    seed(can_sudo=True)
    out = ss.apply_setting({"setting": "disable_headless_suspend"})
    assert out["ok"] is True
    assert fake_run.calls == [
        ["sudo", "-n", "-u", "gdm", "dbus-launch", "gsettings", "set",
         "org.gnome.settings-daemon.plugins.power", "sleep-inactive-ac-type", "nothing"],
        ["dbus-launch", "gsettings", "set", "org.gnome.settings-daemon.plugins.power", "sleep-inactive-ac-type", "nothing"],
    ]


def test_apply_stops_at_the_first_failing_step_and_marks_a_partial_apply(fake_run: FakeSubprocess) -> None:
    seed(can_sudo=True)
    fake_run.responder = lambda argv: (1, "", "ERROR: tailscale0 unknown") if "tailscale0" in argv else (0, "ok", "")
    out = ss.apply_setting({"setting": "enable_ufw_tailscale_only"})
    assert out["ok"] is False and out["applied"] is True and out["partial"] is True
    assert out["error"] == "`sudo -n ufw allow in on tailscale0` exited 1"
    assert [s["exit_code"] for s in out["steps"]] == [0, 0, 1]
    assert out["steps"][-1]["stderr"] == "ERROR: tailscale0 unknown"
    assert len(fake_run.calls) == 3  # the final `ufw --force enable` never ran


@pytest.mark.parametrize("packages", [
    ["nvidia-driver-580-open"], ["htop", "cuda-drivers"], "nvidia-dkms-580", ["linux-image-6.11.0-1016-nvidia"],
    ["libnvidia-compute-580"],
])
def test_apt_install_refuses_driver_packages_with_the_dgx_warning(fake_run: FakeSubprocess, packages) -> None:
    seed(can_sudo=True)
    out = ss._apply_prepared(ss._prepare_apt_install, {"packages": packages})
    assert out["ok"] is False and "DGX OS" in out["error"] and "refusing to install" in out["error"]
    plan = ss._plan_dict(ss._prepare_apt_install, {"packages": packages})
    assert plan["ok"] is False and plan["commands"] == [] and "DGX OS" in plan["error"]
    assert fake_run.calls == []


@pytest.mark.parametrize("packages, fragment", [
    ([], "packages required"), (None, "packages required"), (["Htop"], "invalid package name"),
    (["htop; rm -rf /"], "invalid package name"), (["-y"], "invalid package name"),
    # apt-get reads a trailing '-' as "remove" even under `install` (and '+'
    # as "install" under `remove`): neither is a package name.
    (["htop", "linux-modules-nvidia-580-open-6.11.0-1016-nvidia-"], "invalid package name"),
    (["pkg-"], "invalid package name"), (["pkg+"], "invalid package name"), (["-o"], "invalid package name"),
    (["a"], "invalid package name"),
    ([f"p{i}" for i in range(25)], "at most 20"),
])
def test_apt_install_validates_package_names(fake_run: FakeSubprocess, packages, fragment) -> None:
    seed(can_sudo=True)
    out = ss._apply_prepared(ss._prepare_apt_install, {"packages": packages})
    assert out["ok"] is False and fragment in out["error"]
    assert fake_run.calls == []


def test_apt_install_runs_apt_get_install_only_and_container_toolkit_is_allowed(fake_run: FakeSubprocess) -> None:
    seed(can_sudo=True)
    out = ss._apply_prepared(ss._prepare_apt_install, {"packages": "htop, nvidia-container-toolkit libnvidia-container1"})
    assert out["ok"] is True and out["applied"] is True
    assert fake_run.calls == [[
        "sudo", "-n", "apt-get", "install", "-y", "--no-install-recommends", "htop", "nvidia-container-toolkit", "libnvidia-container1",
    ]]
    assert fake_run.kwargs[0]["timeout"] == ss.INSTALL_TIMEOUT_S
    assert out["undo"] == ["sudo apt-get remove htop nvidia-container-toolkit libnvidia-container1"]
    assert "never `apt upgrade`" in out["notes"][0]


def test_snap_install_and_service_enable_plans(fake_run: FakeSubprocess) -> None:
    seed(can_sudo=True)
    plan = ss._plan_dict(ss._prepare_snap_install, {"packages": ["code"], "classic": True})
    assert plan["commands"] == ["sudo snap install --classic code"]
    out = ss._apply_prepared(ss._prepare_snap_install, {"packages": ["code"]})
    assert out["ok"] is True and fake_run.calls[-1] == ["sudo", "-n", "snap", "install", "code"]

    plan = ss._plan_dict(ss._prepare_service_enable, {"unit": "docker"})
    assert plan["commands"] == ["sudo systemctl enable --now docker"] and plan["undo"] == ["sudo systemctl disable --now docker"]
    out = ss._apply_prepared(ss._prepare_service_enable, {"unit": "tailscaled"})
    assert out["ok"] is True and fake_run.calls[-1] == ["sudo", "-n", "systemctl", "enable", "--now", "tailscaled"]

    before = len(fake_run.calls)
    for unit in (
        "reboot.target", "poweroff", "halt.target", "suspend.target", "rescue.target",
        # The static units behind the power targets suspend / halt the box the
        # moment they start, with or without their suffix, template instances too.
        "systemd-suspend.service", "systemd-suspend", "systemd-hibernate.service", "systemd-hybrid-sleep",
        "systemd-suspend-then-hibernate.service", "systemd-poweroff.service", "systemd-reboot",
        "systemd-halt.service", "systemd-kexec.service", "systemd-suspend@lid.service", "sleep.target",
        "ctrl-alt-del.target", "final.target",
    ):
        out = ss._apply_prepared(ss._prepare_service_enable, {"unit": unit})
        assert out["ok"] is False, unit
        assert "refusing to enable" in out["error"], unit
    for unit in ("../evil", "a b"):  # not unit names at all
        out = ss._apply_prepared(ss._prepare_service_enable, {"unit": unit})
        assert out["ok"] is False and out["error"].startswith("unit required"), unit
    assert ss._apply_prepared(ss._prepare_service_enable, {})["error"].startswith("unit required")
    assert len(fake_run.calls) == before
    # Ordinary units, including systemd-* ones that are not power verbs, still plan.
    for unit in ("ssh", "docker", "tailscaled", "systemd-timesyncd", "systemd-resolved.service", "sleep-monitor.service"):
        assert ss.denied_unit(unit) is False, unit
        assert ss._plan_dict(ss._prepare_service_enable, {"unit": unit})["ok"] is True, unit


# ───────────────────────────────────────────────────────────────────────────
# system_settings_get
# ───────────────────────────────────────────────────────────────────────────


def test_collect_system_settings_reads_optional_probes(fake_run: FakeSubprocess, monkeypatch) -> None:
    seed(in_sudo_group=True)
    files = {
        "/etc/os-release": 'PRETTY_NAME="Ubuntu 24.04.3 LTS"\nVERSION_ID="24.04"\n',
        "/etc/dgx-release": 'DGX_NAME="DGX Spark"\nDGX_SWBUILD_VERSION="7.2.3"\n',
        "/etc/apt/apt.conf.d/20auto-upgrades": 'APT::Periodic::Update-Package-Lists "1";\nAPT::Periodic::Unattended-Upgrade "1";\n',
    }
    monkeypatch.setattr(ss, "_read_text", lambda path: files.get(path, ""))
    monkeypatch.setattr(ss.socket, "gethostname", lambda: "spark-01")

    def responder(argv):
        head = argv[0]
        if head == "nvidia-smi":
            return 0, "| NVIDIA-SMI 580.95   Driver Version: 580.95.05   CUDA Version: 13.0 |\n", ""
        if head == "id":
            return 0, "alice adm docker sudo\n", ""
        if head == "gsettings":
            return 0, "'suspend'\n", ""
        if head == "ufw":
            return 1, "", "ERROR: You need to be root to run this script"
        if head == "tailscale":
            return 0, json.dumps({"BackendState": "Running", "Self": {"TailscaleIPs": ["100.64.0.7"], "DNSName": "spark-01.tail.ts.net."}}), ""
        if head == "systemctl":
            return 0, "enabled\n", ""
        raise AssertionError(f"unexpected probe {argv}")

    fake_run.responder = responder
    out = ss.collect_system_settings()
    assert out["ok"] is True and out["hostname"] == "spark-01"
    # Distro, kernel and the DGX verdict are the platform facts' (seeded), not
    # a second reading of /etc/os-release: one payload names one OS.
    assert out["os"]["pretty_name"] == "DGX OS 7.2" and out["os"]["is_dgx_os"] is True
    assert out["kernel"] == "6.11.0-1016-nvidia"
    assert "Ubuntu" not in json.dumps(out)
    assert out["os"]["dgx_release"] == {"DGX_NAME": "DGX Spark", "DGX_SWBUILD_VERSION": "7.2.3"}
    assert out["platform"]["device_label"] == SPARK_LABEL and out["platform"]["unified_memory"] is True
    assert out["driver"] == {"available": True, "driver_version": "580.95.05", "cuda_version": "13.0"}
    assert out["sudo"]["can_sudo"] is False and out["sudo"]["in_sudo_group"] is True
    assert "terminal" in out["sudo"]["mode"]
    assert out["docker_group"]["in_docker_group"] is True and out["docker_group"]["user"] == "alice"
    assert out["auto_suspend"] == {
        "available": True, "user_setting": "suspend",
        "greeter_setting": "not probed (reading the gdm account's setting needs sudo)", "headless_risk": True,
    }
    assert out["ufw"] == {"installed": True, "status": "unreadable without sudo"}
    assert out["tailscale"] == {"installed": True, "backend_state": "Running", "ips": ["100.64.0.7"], "dns_name": "spark-01.tail.ts.net."}
    assert out["unattended_upgrades"] == {"installed": True, "apt_periodic": True, "unit": "enabled"}
    assert out["privileged_tools_enabled"] is True
    assert [c["setting"] for c in out["catalogue"]] == list(ss.SETTINGS)
    # Every probe ran without sudo.
    assert all(call[0] != "sudo" for call in fake_run.calls)


def test_settings_catalogue_is_one_table() -> None:
    """Every entry has a builder, a description and a sudo note; the key is the
    name everywhere (tool parameter, plan, apply result); nothing retypes it."""
    assert ss.SETTINGS
    for key, entry in ss.SETTINGS.items():
        assert callable(entry.build), key
        assert entry.description.strip(), key
        assert entry.sudo.strip(), key
        assert f"{key} ({entry.description})" in ss._SETTING_PARAM["description"], key
    assert [c["setting"] for c in ss.catalogue()] == list(ss.SETTINGS)
    assert all(c["does"] and c["sudo"] for c in ss.catalogue())
    # A builder that names nothing gets the key; build_plan is where the name comes from.
    plan = ss.build_plan("enable_ssh", {})
    assert plan.name == "enable_ssh"
    assert ss._plan_enable_ssh({}).name == ""  # the builder itself never spells it
    assert not hasattr(ss, "SETTINGS_CATALOGUE") and not hasattr(ss, "CATALOGUE_DESCRIPTIONS")


def test_collect_system_settings_without_binaries_spawns_nothing(fake_run: FakeSubprocess, monkeypatch) -> None:
    monkeypatch.setattr(ss, "_which", lambda name: None)
    out = ss.collect_system_settings()
    assert out["ok"] is True
    assert out["driver"] == {"available": False}
    assert out["docker_group"]["in_docker_group"] is None
    assert out["auto_suspend"] == {"available": False}
    assert out["ufw"] == {"installed": False} and out["tailscale"] == {"installed": False}
    assert out["unattended_upgrades"] == {"installed": False, "apt_periodic": None, "unit": "unknown"}
    assert out["os"]["dgx_release"] is None
    assert out["sudo"]["mode"].startswith("none")
    assert fake_run.calls == []


def test_collect_system_settings_survives_a_broken_probe(fake_run: FakeSubprocess, monkeypatch) -> None:
    def boom():
        raise RuntimeError("no dbus")

    monkeypatch.setattr(ss, "_auto_suspend_facts", boom)
    out = ss.collect_system_settings()
    assert out["ok"] is True and out["auto_suspend"] == {"error": "RuntimeError"}


# ───────────────────────────────────────────────────────────────────────────
# Through the registry (the real tools) and the HTTP layer
# ───────────────────────────────────────────────────────────────────────────


def test_default_registry_has_the_six_tools_with_planners_on_the_privileged_ones() -> None:
    reg = default_registry()
    by_name = {t.name: t for t in reg.list_tools()}
    assert by_name["system_settings_get"].safety_class == "auto"
    assert by_name["system_settings_plan"].safety_class == "auto"
    for name in PRIVILEGED_TOOLS:
        assert by_name[name].safety_class == "privileged", name
        assert by_name[name].planner is not None, name
    assert by_name["system_settings_get"].planner is None
    # No password parameter anywhere in the catalogue.
    for tool in reg.list_tools():
        assert not any("passw" in key.lower() for key in tool.parameters), tool.name


@pytest.mark.asyncio
async def test_real_privileged_tools_through_execute(fake_run: FakeSubprocess, tmp_path: Path) -> None:
    seed(can_sudo=True)
    reg = default_registry()

    card = await reg.execute("apt_install", arguments={"packages": ["htop"]})
    assert card["needs_confirmation"] is True and card["privileged"] is True
    assert card["plan"]["commands"] == ["sudo apt-get install -y --no-install-recommends htop"]
    assert card["summary"] == "apt-get install ['htop']."
    assert isinstance(card["approval_token"], str)
    assert fake_run.calls == []

    # The token is bound to the card's arguments: confirming a different
    # package list with it is refused before the handler is reached.
    swapped = await reg.execute(
        "apt_install", arguments={"packages": ["nmap"]}, confirmed=True, approval_token=card["approval_token"],
    )
    assert swapped == {
        "ok": False, "error": APPROVAL_REQUIRED_ERROR, "approval_required": True,
        "tool": "apt_install", "safety_class": "privileged",
    }
    assert fake_run.calls == []

    refused = await _approve(reg, "apt_install", {"packages": ["nvidia-driver-580-open"]})
    assert refused["ok"] is True and refused["result"]["ok"] is False and "DGX OS" in refused["result"]["error"]
    assert "audit" not in refused and fake_run.calls == []

    done = await reg.execute("apt_install", arguments={"packages": ["htop"]}, confirmed=True, approval_token=card["approval_token"])
    assert done["ok"] is True and done["result"]["applied"] is True
    assert done["audit"]["saved"] is True
    assert fake_run.calls == [["sudo", "-n", "apt-get", "install", "-y", "--no-install-recommends", "htop"]]
    note = _decisions(tmp_path)[0].read_text(encoding="utf-8")
    assert "# Privileged change: Install htop with apt-get" in note
    assert "#apt_install" in note and "`sudo -n apt-get install -y --no-install-recommends htop` — exit 0" in note

    # Single use: the same token cannot run the install twice.
    again = await reg.execute("apt_install", arguments={"packages": ["htop"]}, confirmed=True, approval_token=card["approval_token"])
    assert again["approval_required"] is True and len(fake_run.calls) == 1

    seed(in_sudo_group=True)
    handoff = await _approve(reg, "system_settings_apply", {"setting": "enable_ssh"})
    assert handoff["result"]["needs_terminal"] is True
    assert handoff["result"]["command"] == "sudo systemctl enable --now ssh"
    assert "audit" not in handoff
    assert len(fake_run.calls) == 1  # nothing new ran

    plan = await reg.execute("system_settings_plan", arguments={"setting": "enable_ssh"})
    assert plan["ok"] is True and plan["result"]["commands"] == ["sudo systemctl enable --now ssh"]
    facts = await reg.execute("system_settings_get")
    assert facts["ok"] is True and facts["result"]["hostname"]


@pytest.mark.asyncio
async def test_real_tool_handlers_never_raise_and_keep_the_message(monkeypatch, fake_run: FakeSubprocess) -> None:
    """One ``_threaded`` wrapper for every handler and planner: an exception
    becomes ``{ok: False, error: "<label> failed: <Type>: <message>"}`` — the
    message kept — and planners add the empty ``commands`` a plan carries."""
    seed(can_sudo=True)

    def boom(*a, **k):
        raise RuntimeError("unexpected: no facts")

    monkeypatch.setattr(ss, "_facts", boom)  # collect_system_settings and every sudo step
    monkeypatch.setattr(ss, "build_plan", boom)  # plan_setting / apply_setting
    monkeypatch.setattr(ss, "denied_reason", boom)  # Plan.denied() → the install planners and applies
    assert (await ss._tool_apply({"setting": "enable_ssh"})) == {
        "ok": False, "error": "system_settings_apply failed: RuntimeError: unexpected: no facts",
    }
    assert (await ss._tool_plan({"setting": "enable_ssh"})) == {
        "ok": False, "error": "system_settings_plan failed: RuntimeError: unexpected: no facts", "commands": [],
    }
    assert (await ss._tool_get({}))["error"] == "system_settings_get failed: RuntimeError: unexpected: no facts"
    assert (await ss._tool_apt_install({"packages": ["htop"]}))["error"] == "apt_install failed: RuntimeError: unexpected: no facts"
    assert (await ss._plan_apt_install({"packages": ["htop"]})) == {
        "ok": False, "error": "apt_install dry run failed: RuntimeError: unexpected: no facts", "commands": [],
    }
    assert (await ss._tool_service_enable({"unit": "ssh"}))["error"].startswith("service_enable failed: RuntimeError")
    assert (await ss._plan_snap_install({"packages": ["code"]}))["commands"] == []
    assert fake_run.calls == []
    # The message is cut, never dropped.
    monkeypatch.setattr(ss, "_facts", lambda: (_ for _ in ()).throw(ValueError("m" * 500)))
    error = (await ss._tool_get({}))["error"]
    assert error.startswith("system_settings_get failed: ValueError: mmm") and len(error) < 300


def test_wizard_tools_endpoint_reports_privileged_counts_and_the_switch(monkeypatch) -> None:
    from nvh.api import server as server_module

    monkeypatch.setattr(server_module, "_wizard_tool_registry", None)
    client = _loopback_client()
    body = client.get("/v1/wizard/tools").json()["data"]
    assert body["privileged_enabled"] is True
    assert body["privileged_count"] == 4
    assert body["auto_count"] >= 1 and body["confirm_count"] >= 1
    names = {t["name"]: t for t in body["tools"]}
    for name in PRIVILEGED_TOOLS:
        assert names[name]["safety_class"] == "privileged" and names[name]["enabled"] is True
    classes = [t["safety_class"] for t in body["tools"]]
    assert classes == sorted(classes, key=["auto", "confirm", "privileged"].index)

    monkeypatch.setenv(PRIVILEGED_ENV, "0")
    body = client.get("/v1/wizard/tools").json()["data"]
    assert body["privileged_enabled"] is False and body["privileged_count"] == 4
    assert all(t["enabled"] is False for t in body["tools"] if t["safety_class"] == "privileged")
    assert all(t["enabled"] is True for t in body["tools"] if t["safety_class"] != "privileged")

    resp = client.post("/v1/wizard/tools/execute", json={"name": "apt_install", "arguments": {"packages": ["htop"]}, "confirmed": True})
    assert resp.status_code == 200
    assert resp.json()["data"] == {
        "ok": False, "error": PRIVILEGED_DISABLED_ERROR, "disabled": True, "tool": "apt_install", "safety_class": "privileged",
    }


def test_wizard_tools_execute_endpoint_returns_the_privileged_card_unchanged(fake_run: FakeSubprocess, monkeypatch) -> None:
    from nvh.api import server as server_module

    monkeypatch.setattr(server_module, "_wizard_tool_registry", None)
    client = TestClient(server_module.app)  # the card path has no Host check: Host "testserver" is fine
    resp = client.post("/v1/wizard/tools/execute", json={"name": "service_enable", "arguments": {"unit": "docker"}})
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["ok"] is False and data["needs_confirmation"] is True and data["privileged"] is True
    assert data["plan"]["commands"] == ["sudo systemctl enable --now docker"]
    assert data["summary"] == "systemctl enable --now docker."
    assert isinstance(data["approval_token"], str) and isinstance(data["approval_expires_at"], int)
    assert fake_run.calls == []


def test_wizard_tools_execute_endpoint_confirmed_privileged_needs_token_and_a_local_origin(
    fake_run: FakeSubprocess, monkeypatch,
) -> None:
    """The HTTP layer's one addition: where a *confirmed* privileged call came from."""
    from nvh.api import server as server_module

    seed(can_sudo=True)
    monkeypatch.setattr(server_module, "_wizard_tool_registry", None)
    monkeypatch.delenv("HIVE_API_KEY", raising=False)
    monkeypatch.delenv("NVH_API_BIND_HOST", raising=False)
    client = _loopback_client()
    body = {"name": "service_enable", "arguments": {"unit": "docker"}, "confirmed": True}

    # Loopback Host, no token: the registry refuses (nothing ran).
    data = client.post("/v1/wizard/tools/execute", json=body).json()["data"]
    assert data == {
        "ok": False, "error": APPROVAL_REQUIRED_ERROR, "approval_required": True,
        "tool": "service_enable", "safety_class": "privileged",
    }
    # A forged / foreign token is the same refusal.
    data = client.post("/v1/wizard/tools/execute", json={**body, "approval_token": "AAAA.1"}).json()["data"]
    assert data["approval_required"] is True

    # A DNS-rebound page talks to 127.0.0.1 under its own name: refused on Host.
    card = client.post("/v1/wizard/tools/execute", json={"name": "service_enable", "arguments": {"unit": "docker"}}).json()["data"]
    token = card["approval_token"]
    data = client.post(
        "/v1/wizard/tools/execute", json={**body, "approval_token": token}, headers={"Host": "evil.example:8000"},
    ).json()["data"]
    assert data["ok"] is False and data["refused"] is True and "Host 'evil.example:8000'" in data["error"]
    assert data["tool"] == "service_enable" and data["safety_class"] == "privileged"
    # A cross-site page: refused on Origin (CORS would only hide the answer).
    data = client.post(
        "/v1/wizard/tools/execute", json={**body, "approval_token": token}, headers={"Origin": "http://evil.example"},
    ).json()["data"]
    assert data["refused"] is True and "Origin" in data["error"]
    # The WebUI's own origin and hosts CORS already trusts pass.
    for headers in ({"Origin": "http://localhost:3000"}, {"Host": "nvhive:8000"}, {"Host": "[::1]:8000"}, {"Host": "localhost"}):
        data = client.post("/v1/wizard/tools/execute", json={**body, "approval_token": "AAAA.1"}, headers=headers).json()["data"]
        assert data.get("refused") is None and data["approval_required"] is True, headers

    # Open mode on a non-loopback bind: refused, naming HIVE_API_KEY …
    monkeypatch.setenv("NVH_API_BIND_HOST", "0.0.0.0")
    data = client.post("/v1/wizard/tools/execute", json={**body, "approval_token": token}).json()["data"]
    assert data["refused"] is True and "HIVE_API_KEY" in data["error"] and "0.0.0.0" in data["error"]
    # … and with a key configured the same bind is fine (auth now gates the endpoint).
    monkeypatch.setenv("HIVE_API_KEY", "k-for-test")
    data = client.post(
        "/v1/wizard/tools/execute", json={**body, "approval_token": token}, headers={"X-Hive-API-Key": "k-for-test"},
    ).json()["data"]
    assert data.get("refused") is None
    assert data["ok"] is True and data["result"]["applied"] is True
    assert fake_run.calls == [["sudo", "-n", "systemctl", "enable", "--now", "docker"]]
    # Loopback binds never trip the open-mode check.
    monkeypatch.delenv("HIVE_API_KEY", raising=False)
    for bind in ("127.0.0.1", "localhost", "::1", "[::1]"):
        monkeypatch.setenv("NVH_API_BIND_HOST", bind)
        data = client.post("/v1/wizard/tools/execute", json={**body, "approval_token": "AAAA.1"}).json()["data"]
        assert data.get("refused") is None and data["approval_required"] is True, bind
    assert len(fake_run.calls) == 1


# ───────────────────────────────────────────────────────────────────────────
# chat.py: a privileged call is a confirm-bucket call, never auto-run
# ───────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_chat_helpers_bucket_privileged_as_confirm_and_never_auto_run() -> None:
    from nvh.integrations.wizard.chat import _run_auto_tool, _split_by_safety_class

    reg, handler, planner = _stub_registry()
    call = {"name": "stub_priv", "arguments": {"setting": "enable_ssh"}}
    confirm, auto = _split_by_safety_class([call, {"name": "stub_auto", "arguments": {}}], reg)
    assert confirm == [call] and auto == [{"name": "stub_auto", "arguments": {}}]

    deferred = await _run_auto_tool("stub_priv", {"setting": "enable_ssh"}, registry=reg)
    assert deferred["deferred_to_user"] is True and deferred["safety_class"] == "privileged"
    assert deferred["ok"] is False
    assert handler.calls == [] and planner.calls == []


@pytest.mark.asyncio
async def test_chat_surfaces_a_privileged_call_with_its_card(monkeypatch, tmp_path: Path) -> None:
    """The confirm bucket carries the red card's payload for privileged calls
    — ``privileged``, the registry's ``plan`` and the ``approval_token`` —
    read off the same unconfirmed ``execute()`` card the HTTP layer returns;
    confirm-class calls pass through untouched; a switched-off tier surfaces
    ``disabled`` instead of a plan. The model never sees any of it."""
    from nvh.integrations.wizard.chat import _surface_confirm_calls, _surfaced_call

    reg, handler, planner = _stub_registry()
    call = {"name": "stub_priv", "arguments": {"setting": "enable_ssh"}}
    surfaced = await _surfaced_call(call, reg)
    assert surfaced["name"] == "stub_priv" and surfaced["arguments"] == {"setting": "enable_ssh"}
    assert surfaced["privileged"] is True
    assert surfaced["plan"]["commands"] == ["sudo systemctl enable --now ssh"]
    assert verify_approval("stub_priv", {"setting": "enable_ssh"}, surfaced["approval_token"]) is True
    assert surfaced["approval_expires_at"] > 0
    assert planner.calls == [{"setting": "enable_ssh"}] and handler.calls == []

    confirm = {"name": "stub_confirm", "arguments": {}}
    assert await _surfaced_call(confirm, reg) is confirm
    assert await _surfaced_call({"name": "nope", "arguments": {}}, reg) == {"name": "nope", "arguments": {}}
    assert await _surfaced_call(call, None) is call

    # Dedup is by name + arguments, whatever else a surfaced entry carries.
    pending: list[dict[str, Any]] = []
    await _surface_confirm_calls(pending, [call, confirm], reg)
    await _surface_confirm_calls(pending, [dict(call), {"name": "stub_priv", "arguments": {"setting": "other"}}], reg)
    assert [(p["name"], p["arguments"]) for p in pending] == [
        ("stub_priv", {"setting": "enable_ssh"}), ("stub_confirm", {}), ("stub_priv", {"setting": "other"}),
    ]
    assert len({p["approval_token"] for p in pending if "approval_token" in p}) == 2

    monkeypatch.setenv(PRIVILEGED_ENV, "0")
    off = await _surfaced_call(call, reg)
    assert off["disabled"] is True and "NVH_ALLOW_PRIVILEGED=0" in off["error"]
    assert "plan" not in off and "approval_token" not in off


def test_persisted_metadata_drops_approval_tokens() -> None:
    from nvh.integrations.wizard.chat import _persistable_calls

    calls = [{"name": "stub_priv", "arguments": {}, "privileged": True, "plan": {"ok": True}, "approval_token": "x.1", "approval_expires_at": 9}]
    assert _persistable_calls(calls) == [{"name": "stub_priv", "arguments": {}, "privileged": True, "plan": {"ok": True}}]
    assert calls[0]["approval_token"] == "x.1"  # the live list is untouched


def test_history_tool_results_reach_the_model_as_tool_result_lines() -> None:
    """The WebUI runs the cards after the turn ends; the next turn's history
    carries ``tool_results`` and ``_build_messages`` renders them, redacted and
    cut, right after the assistant turn — so a ``needs a terminal:`` outcome
    is something the model can act on."""
    from dataclasses import fields

    from nvh.integrations.wizard.chat import (
        HISTORY_TOOL_SUMMARY_CHARS,
        ProfileOverrides,
        _build_messages,
        _history_tool_results_message,
        _TurnSetup,
    )

    entry = {
        "role": "assistant",
        "content": "Enabling SSH.",
        "tool_results": [
            {"name": "system_settings_apply", "ok": False, "summary": "needs a terminal: sudo systemctl enable --now ssh"},
            {"name": "save_provider_key", "ok": True, "summary": "saved key sk-ant-api03-" + "A" * 40 + " to config"},
            {"name": "apt_install", "ok": False, "summary": "y" * 1000},
            {"name": "", "ok": True, "summary": "no name"},
            "junk",
        ],
    }
    block = _history_tool_results_message(entry)
    lines = block.splitlines()
    assert lines[0] == 'TOOL_RESULT system_settings_apply: {"ok": false, "summary": "needs a terminal: sudo systemctl enable --now ssh"}'
    assert lines[1].startswith("TOOL_RESULT save_provider_key: ") and "sk-ant" not in lines[1] and "[REDACTED:api_key]" in lines[1]
    assert json.loads(lines[2].split(": ", 1)[1])["summary"] == "y" * HISTORY_TOOL_SUMMARY_CHARS
    assert len(lines) == 3
    assert _history_tool_results_message({"role": "assistant", "content": "x"}) is None
    assert _history_tool_results_message({"role": "assistant", "tool_results": []}) is None
    assert _history_tool_results_message({"role": "assistant", "tool_results": ["junk", {"ok": True}]}) is None

    blank = {f.name: None for f in fields(_TurnSetup)}
    turn = _TurnSetup(**{
        **blank, "prof": ProfileOverrides(), "system_prompt": "SYS", "user_message": "did it work?",
        "history": [{"role": "user", "content": "enable ssh"}, entry, {"role": "user", "content": "thanks"}, {"role": "assistant", "content": "sure"}],
    })
    messages = _build_messages(turn)
    assert [(m.role, m.content[:22]) for m in messages] == [
        ("system", "SYS"), ("user", "enable ssh"), ("assistant", "Enabling SSH."), ("system", "TOOL_RESULT system_set"),
        ("user", "thanks"), ("assistant", "sure"), ("user", "did it work?"),
    ]


def test_tool_result_message_redacts_before_it_cuts() -> None:
    """A key straddling the 1500-char cut must not survive as a half-key the pattern no longer matches."""
    from nvh.integrations.wizard.chat import _format_tool_result_message

    key = "sk-ant-api03-" + "B" * 60
    # The key starts ~35 chars before the cut: truncate-then-redact would keep
    # `sk-ant-api03-BBBBBBBBB…` (too short for the pattern) in the clear.
    result = {"pad": "x" * (TOOL_RESULT_CHARS - 60), "api_key": key}
    naive = json.dumps(result)[:TOOL_RESULT_CHARS]
    assert "sk-ant-api03-BBBB" in naive, "the fixture must straddle the cut"
    message = _format_tool_result_message("save_provider_key", result)
    assert message.startswith("TOOL_RESULT save_provider_key: ")
    assert len(message) <= len("TOOL_RESULT save_provider_key: ") + TOOL_RESULT_CHARS
    assert "sk-ant" not in message and "BBBB" not in message
    assert "[REDACTED:api_key]" in message


@pytest.mark.asyncio
async def test_record_privileged_change_ignores_a_model_supplied_home_dir(tmp_path: Path) -> None:
    """The vault is ``NVH_HOME``'s; ``home_dir`` in the model's arguments (no privileged tool declares one) is inert."""
    elsewhere = tmp_path / "elsewhere"
    reg, _handler, _planner = _stub_registry()
    out = await _approve(reg, "stub_priv", {"setting": "x", "home_dir": str(elsewhere)})
    assert out["audit"]["saved"] is True
    assert Path(out["audit"]["path"]).is_relative_to(tmp_path / "vault")
    assert not elsewhere.exists()
    assert len(_decisions(tmp_path)) == 1


def test_issue_and_verify_approval() -> None:
    now = 1_800_000_000.0
    args = {"packages": ["htop"], "classic": False}
    issued = issue_approval("apt_install", args, now=now)
    token = issued["approval_token"]
    assert issued["approval_expires_at"] == int(now) + APPROVAL_TTL_S
    mac, stamp, nonce = token.split(".")
    assert stamp == str(int(now)) and len(mac) == 43 and "=" not in mac and nonce
    # Canonical arguments: key order does not matter to verification …
    assert verify_approval("apt_install", {"classic": False, "packages": ["htop"]}, token, now=now) is True
    # … and the same call in the same second is still a second, separately spent token.
    twin = issue_approval("apt_install", args, now=now)["approval_token"]
    assert twin != token and verify_approval("apt_install", args, twin, now=now) is True
    token = issue_approval("apt_install", args, now=now)["approval_token"]
    mac, stamp, nonce = token.split(".")
    # Wrong name, wrong arguments, tampered MAC, tampered nonce, malformed, expired, from the future: all refused …
    assert verify_approval("snap_install", args, token, now=now) is False
    assert verify_approval("apt_install", {"packages": ["nmap"]}, token, now=now) is False
    assert verify_approval("apt_install", args, ("A" if mac[0] != "A" else "B") + mac[1:] + f".{stamp}.{nonce}", now=now) is False
    assert verify_approval("apt_install", args, f"{mac}.{stamp}.{nonce}x", now=now) is False
    for bad in ("", ".", "..", "abc", f"{mac}.{stamp}", f"{mac}..{nonce}", f".{stamp}.{nonce}", f"{mac}.notanumber.{nonce}", None, 42, b"x"):
        assert verify_approval("apt_install", args, bad, now=now) is False, bad
    assert verify_approval("apt_install", args, token, now=now + APPROVAL_TTL_S + 1) is False
    assert verify_approval("apt_install", args, token, now=now - 60) is False
    # … the right one passes exactly once.
    assert verify_approval("apt_install", args, token, now=now + APPROVAL_TTL_S - 1) is True
    assert verify_approval("apt_install", args, token, now=now + APPROVAL_TTL_S - 1) is False, "single use"
    # A fresh issue a second later is a different token and passes on its own.
    later = issue_approval("apt_install", args, now=now + 1)["approval_token"]
    assert later != token and verify_approval("apt_install", args, later, now=now + 1) is True


@pytest.mark.asyncio
async def test_confirmed_privileged_call_without_a_valid_token_runs_nothing(tmp_path: Path) -> None:
    reg, handler, planner = _stub_registry()
    refusal = {
        "ok": False, "error": APPROVAL_REQUIRED_ERROR, "approval_required": True,
        "tool": "stub_priv", "safety_class": "privileged",
    }
    assert await reg.execute("stub_priv", arguments={"setting": "x"}, confirmed=True) == refusal
    assert await reg.execute("stub_priv", arguments={"setting": "x"}, confirmed=True, approval_token="forged.1") == refusal
    card = await reg.execute("stub_priv", arguments={"setting": "x"})
    # Same token, different arguments: refused.
    assert (await reg.execute("stub_priv", arguments={"setting": "y"}, confirmed=True, approval_token=card["approval_token"])) == refusal
    assert handler.calls == [] and _decisions(tmp_path) == []
    # Confirm-class tools are unaffected: no token needed.
    assert (await reg.execute("stub_confirm", confirmed=True))["ok"] is True
    # The exact call, with its token, runs — once.
    ok = await reg.execute("stub_priv", arguments={"setting": "x"}, confirmed=True, approval_token=card["approval_token"])
    assert ok["ok"] is True and handler.calls == [{"setting": "x"}]
    assert (await reg.execute("stub_priv", arguments={"setting": "x"}, confirmed=True, approval_token=card["approval_token"])) == refusal
    assert handler.calls == [{"setting": "x"}]
    assert planner.calls == [{"setting": "x"}]


@pytest.mark.asyncio
async def test_registry_plan_is_the_public_dry_run() -> None:
    reg, handler, planner = _stub_registry()
    assert (await reg.plan("stub_priv", {"setting": "enable_ssh"}))["commands"] == ["sudo systemctl enable --now ssh"]
    assert await reg.plan("nope") is None
    assert await reg.plan("stub_auto") is None  # no planner
    assert planner.calls == [{"setting": "enable_ssh"}] and handler.calls == []


def test_apply_plan_single_step_failure_is_applied_not_partial(fake_run: FakeSubprocess) -> None:
    """The failing command ran and may have changed the host: ``applied`` True; nothing remained, so not ``partial``."""
    seed(can_sudo=True)
    fake_run.responder = lambda argv: (1, "", "Job for foo.service failed")
    out = ss._apply_prepared(ss._prepare_service_enable, {"unit": "foo"})
    assert out["ok"] is False and out["applied"] is True and out["partial"] is False
    assert out["error"] == "`sudo -n systemctl enable --now foo` exited 1"
    assert out["summary"] == "Enable and start foo"
    assert out["steps"][0]["exit_code"] == 1 and out["steps"][0]["stderr"] == "Job for foo.service failed"
    # A hand-off or a spawn failure after an earlier step also says partial.
    seed(can_sudo=True)
    calls = {"n": 0}

    def flaky(argv):
        calls["n"] += 1
        if calls["n"] == 2:
            return FileNotFoundError("ufw vanished")
        return (0, "ok", "")

    fake_run.responder = flaky
    out = ss.apply_setting({"setting": "enable_ufw_tailscale_only"})
    assert out["ok"] is False and out["applied"] is True and out["partial"] is True
    assert "command not found" in out["error"] and len(out["steps"]) == 1


def test_no_password_parameter_or_prompt_anywhere() -> None:
    source = Path(ss.__file__).read_text(encoding="utf-8")
    assert "getpass.getpass" not in source
    assert "input(" not in source
    assert os.environ.get("SUDO_ASKPASS") is None or "SUDO_ASKPASS" not in source
