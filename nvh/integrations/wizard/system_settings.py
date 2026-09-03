"""System settings — the Wizard's privileged tier for the machine it runs on.

A DGX Spark owner's first week is device work: stop the headless greeter
suspending the box, get into the ``docker`` group, enable SSH, scope the
firewall to Tailscale, hold the driver packages so ``apt upgrade`` cannot
strand ``nvidia.ko``. This module gives the Wizard six tools for that
(docs/proposals/SPARK_CONCIERGE_2026-09.md §3.4 and §5 "Sudo reality"):

  ``system_settings_get``    auto        read-only facts about the host
  ``system_settings_plan``   auto        the exact commands a setting would run
  ``system_settings_apply``  privileged  run a catalogue setting
  ``apt_install``            privileged  ``apt-get install -y --no-install-recommends``
  ``snap_install``           privileged  ``snap install``
  ``service_enable``         privileged  ``systemctl enable --now``

Safety posture
==============

  - **Rootless by default, privileged with approval.** The privileged tools
    only run after the user clicks a red card; the card shows the plan this
    module produced (the same code path the apply uses, so what is shown is
    what runs) and carries the approval token the click must bring back
    (:func:`nvh.integrations.wizard.tools.issue_approval`).
    ``NVH_ALLOW_PRIVILEGED=0`` switches the whole tier off
    (:func:`nvh.integrations.wizard.tools.privileged_enabled`).
  - **No password, ever.** :func:`run_host_command` uses ``sudo -n`` only
    where :mod:`nvh.utils.platform_facts` found passwordless sudo (probed
    once with ``sudo -n -k true``). A sudo-group member without it gets
    ``needs_terminal`` and the *exact* command to type themselves; an
    account that cannot elevate is told so. There is no password parameter
    anywhere; stdin is ``DEVNULL`` so nothing can prompt. The one place sudo
    may ask is the user's own terminal: ``nvh playbook install`` calls the
    same runner with ``interactive=True``, which runs plain ``sudo`` with the
    terminal's stdin so sudo prompts *there* — nvHive still never sees,
    stores or passes a password, and the deny list is the same.
  - **Fixed catalogue, fixed deny list.** Every command is an argv list built
    from validated pieces (package names, unit names, hostnames); the model
    never supplies a command string. A package name must start *and end*
    alphanumeric (:data:`PACKAGE_RE`): apt-get reads a trailing ``-`` as
    "remove this package" even under ``install`` and a trailing ``+`` as
    "install" under ``remove``, so neither is a name. Before anything
    spawns, the rendered command passes
    :func:`nvh.core.agent_guardrails.check_command` (shutdown, reboot,
    ``rm -rf /``, ``chown /``, ``format``, …) and this module's own
    patterns: no user or password changes (only ``usermod -aG docker``), no
    disk formatting, no firewall-off, no driver removal, no bare
    ``apt upgrade`` (it has stranded the DGX OS driver), no recursive ``rm``
    outside ``NVH_HOME``, no shutdown, reboot or sleep — including the
    static units behind those targets (``systemd-suspend.service`` and
    friends, :func:`denied_unit`). The deny list is enforced in code,
    independent of the LLM.
  - **Output hygiene.** stdout/stderr are redacted
    (:func:`~nvh.core.agent_guardrails.redact_secrets`) and cut at 1 MB
    (:func:`~nvh.core.agent_guardrails.truncate_output`) here; the registry
    then fits the whole result to the model's 1500-char tool window and
    writes the audit note under the vault's ``Decisions/``.
  - **Everything returns a dict.** Handlers never raise and never prompt.
"""

from __future__ import annotations

import asyncio
import getpass
import json
import logging
import re
import shlex
import shutil
import socket
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from functools import partial
from pathlib import Path
from typing import Any

from nvh.core.agent_guardrails import GuardrailError, check_command, redact_secrets, truncate_output

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_S = 120
INSTALL_TIMEOUT_S = 900
PROBE_TIMEOUT_S = 5
MAX_PACKAGES = 20

NEEDS_TERMINAL_HINT = "run this in a terminal; nvHive never asks for your password"
CANNOT_ELEVATE_ERROR = "this account cannot elevate"
DGX_DRIVER_WARNING = (
    "DGX OS: a bare `apt upgrade` has repeatedly stranded the GPU driver (driver 595 "
    "unsupported, kernels shipped without nvidia.ko). Driver and kernel packages come "
    "through NVIDIA's validated update channel (DGX Dashboard / OTA), never by hand."
)

#: A Debian / snap package name: lowercase, at least two characters, starts
#: and ends alphanumeric. The ends matter — ``apt-get install pkg-`` removes
#: ``pkg`` and ``apt-get remove pkg+`` installs it — so a trailing ``-`` or
#: ``+`` is not a name, and a leading ``-`` can never read as a flag.
PACKAGE_RE = re.compile(r"^[a-z0-9][a-z0-9+.-]*[a-z0-9]$")
PACKAGE_RULE = "^[a-z0-9][a-z0-9+.-]*[a-z0-9]$"
UNIT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.@:\\-]{0,255}$")
HOSTNAME_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")
USERNAME_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
#: Packages ``apt_install`` / ``snap_install`` refuse with :data:`DGX_DRIVER_WARNING`.
DRIVER_PACKAGE_RE = re.compile(
    r"^(nvidia-driver|cuda-drivers|nvidia-dkms|nvidia-kernel|nvidia-open|nvidia-utils|"
    r"nvidia-headless|linux-image|libnvidia-(?!container))",
)
#: Installed packages ``hold_nvidia_driver_packages`` marks held (container bits excluded).
HOLD_PACKAGE_RE = re.compile(r"^(nvidia-|libnvidia-|cuda-drivers)")
HOLD_EXCLUDE_RE = re.compile(r"^(nvidia-container|libnvidia-container|nvidia-docker)")

#: Power-state verbs. Both the target systemd runs for each (``suspend.target``)
#: and the static unit behind it (``systemd-suspend.service``) suspend, halt or
#: reboot the box the moment they start, so ``service_enable`` refuses every
#: spelling: full name, bare stem, with or without ``.service`` / ``.target``.
_POWER_VERBS: tuple[str, ...] = (
    "poweroff", "reboot", "halt", "kexec", "suspend", "hibernate", "hybrid-sleep",
    "suspend-then-hibernate", "sleep", "shutdown",
)
#: systemd units ``service_enable`` refuses (see :func:`denied_unit`).
DENIED_UNITS = frozenset(
    {f"{verb}.target" for verb in _POWER_VERBS}
    | {f"systemd-{verb}.service" for verb in _POWER_VERBS}
    | {"emergency.target", "rescue.target", "final.target", "exit.target", "ctrl-alt-del.target"},
)
#: Any ``systemd-<power verb>`` stem, including template instances (``@``).
_DENIED_UNIT_RE = re.compile(
    r"^systemd-(?:" + "|".join(re.escape(verb) for verb in _POWER_VERBS) + r")(?:[-.@]|$)",
)


def denied_unit(unit: str) -> bool:
    """Is ``unit`` a power-state, rescue or shutdown unit ``service_enable`` must refuse?

    Compares the full name and the stem, with and without ``.service`` and
    ``.target``, against :data:`DENIED_UNITS`, then refuses any stem that
    starts with ``systemd-`` followed by a power verb.
    """
    stem = unit
    for suffix in (".service", ".target"):
        if stem.endswith(suffix):
            stem = stem[: -len(suffix)]
            break
    if {unit, stem, f"{stem}.service", f"{stem}.target"} & DENIED_UNITS:
        return True
    return bool(_DENIED_UNIT_RE.match(stem))

# The module's own deny list, applied to the rendered command after
# ``check_command``. Each entry: (pattern, what the refusal says).
_DENIED: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b(passwd|chpasswd|useradd|userdel|adduser|deluser|gpasswd|visudo|chsh|chfn)\b"),
     "user or password change"),
    (re.compile(r"\busermod\b(?!\s+-aG\s+docker\s)"),
     "user account change (only `usermod -aG docker <user>` is allowed)"),
    (re.compile(r"/etc/(sudoers|shadow|passwd|group)\b"), "edit of the account database"),
    (re.compile(r"\b(mkfs(\.\w+)?|fdisk|sfdisk|parted|gdisk|wipefs|blkdiscard|cryptsetup|mkswap)\b"),
     "disk formatting or partitioning"),
    (re.compile(r"\bdd\s+.*\bof=/dev/"), "raw write to a block device"),
    (re.compile(r"\bufw\s+(disable|reset)\b"), "turning the firewall off"),
    (re.compile(r"\b(iptables|ip6tables)\s+(-F|--flush|-X|-P\s+INPUT\s+ACCEPT)\b"), "flushing firewall rules"),
    (re.compile(r"\bnft\s+flush\b"), "flushing firewall rules"),
    (re.compile(r"\bsystemctl\s+(stop|disable|mask)\s+.*\b(ufw|firewalld|nftables)\b"), "stopping the firewall"),
    (re.compile(r"\b(apt|apt-get|aptitude)\s+.*\b(remove|purge|autoremove)\b.*\b(nvidia|cuda)"), "driver removal"),
    (re.compile(r"\bdpkg\s+(-r|-P|--remove|--purge)\s+.*nvidia"), "driver removal"),
    (re.compile(r"\b(nvidia-uninstall|rmmod\s+nvidia|modprobe\s+-r\s+nvidia)"), "driver removal"),
    (re.compile(r"\b(apt|apt-get)\s+(upgrade|dist-upgrade|full-upgrade)\b"),
     "bare apt upgrade — " + DGX_DRIVER_WARNING),
    (re.compile(r"\b(poweroff|halt|init\s+[06]|telinit\s+[06])\b"), "system shutdown"),
    (re.compile(r"\bsystemctl\s+(poweroff|reboot|halt|kexec|suspend|hibernate)\b"), "system shutdown or reboot"),
    # ``systemctl enable/start <unit>`` where the unit is a power-state target
    # or the static unit behind one (systemd-suspend.service …): the same
    # refusal the planner gives, repeated here so it holds before any spawn
    # whatever built the argv.
    (re.compile(
        r"\bsystemctl\s+(?:\S+\s+)*?(?:systemd-)?(?:poweroff|reboot|halt|kexec|suspend|hibernate|"
        r"hybrid-sleep|suspend-then-hibernate|sleep|shutdown|emergency|rescue|final|exit|ctrl-alt-del)"
        r"(?:\.target|\.service)?(?:\s|$)",
    ), "system shutdown, reboot or sleep"),
]


class PlanError(ValueError):
    """A setting or install request that cannot be turned into a plan (bad input, refused)."""


# ────────────────────────────────────────────────────────────────────────────
# Host facts and small seams (patched by tests)
# ────────────────────────────────────────────────────────────────────────────


def _which(name: str) -> str | None:
    return shutil.which(name)


def _facts() -> Any:
    from nvh.utils.platform_facts import detect_platform_facts

    return detect_platform_facts()


def _home() -> Path:
    from nvh.integrations.workspace.storage import nvh_home

    return nvh_home(None)[0]


def _current_user() -> str:
    try:
        return getpass.getuser()
    except Exception:
        return ""


def _read_text(path: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""


def _clean_output(raw: bytes | str | None) -> str:
    """Decode, cut at 1 MB, redact — in that order so a huge dump never reaches the regexes whole."""
    if raw is None:
        return ""
    text = raw.decode("utf-8", errors="replace") if isinstance(raw, (bytes, bytearray)) else str(raw)
    return redact_secrets(truncate_output(text.replace("\r\n", "\n").strip()))


# ────────────────────────────────────────────────────────────────────────────
# Deny list
# ────────────────────────────────────────────────────────────────────────────


def _rm_outside_home(argv: Sequence[str]) -> str | None:
    """Refuse a recursive ``rm`` unless every target is strictly inside ``NVH_HOME``."""
    words = [str(w) for w in argv]
    index = next((i for i, w in enumerate(words[:4]) if Path(w).name == "rm"), None)
    if index is None:
        return None
    rest = words[index + 1:]
    flags = [w for w in rest if w.startswith("-")]
    recursive = "--recursive" in flags or any(
        not f.startswith("--") and "r" in f.lower() for f in flags
    )
    if not recursive:
        return None
    targets = [w for w in rest if not w.startswith("-")]
    if not targets:
        return "BLOCKED: recursive rm without a target"
    try:
        home = _home().expanduser().resolve()
    except Exception:
        return "BLOCKED: recursive rm (NVH_HOME could not be resolved)"
    for target in targets:
        try:
            resolved = Path(target).expanduser().resolve()
        except Exception:
            return f"BLOCKED: recursive rm of an unresolvable path ({target})"
        if home not in resolved.parents:
            return f"BLOCKED: recursive rm outside NVH_HOME ({target})"
    return None


def denied_reason(argv: Sequence[str]) -> str | None:
    """Why this command must not run, or ``None``. Enforced in code, before any subprocess.

    ``check_command`` (the agent guardrails' blocklist) goes first; then this
    module's patterns; then the ``rm -r`` / ``NVH_HOME`` rule.
    """
    words = [str(w) for w in argv]
    rendered = shlex.join(words)
    try:
        check_command(rendered)
    except GuardrailError as exc:
        return str(exc).splitlines()[0] if str(exc) else "BLOCKED: guardrail"
    for pattern, reason in _DENIED:
        if pattern.search(rendered):
            return f"BLOCKED: {reason}"
    return _rm_outside_home(words)


# ────────────────────────────────────────────────────────────────────────────
# The host command runner
# ────────────────────────────────────────────────────────────────────────────


def _human_argv(argv: Sequence[str], *, sudo: bool, sudo_user: str | None) -> list[str]:
    """The command as the user would type it: ``sudo [-u USER] …`` without ``-n``."""
    words = [str(w) for w in argv]
    if not sudo:
        return words
    prefix = ["sudo"] + (["-u", sudo_user] if sudo_user else [])
    return [*prefix, *words]


def render_command(argv: Sequence[str], *, sudo: bool = False, sudo_user: str | None = None) -> str:
    return shlex.join(_human_argv(argv, sudo=sudo, sudo_user=sudo_user))


def run_host_command(
    argv: Sequence[str],
    *,
    sudo: bool,
    timeout: float = DEFAULT_TIMEOUT_S,
    sudo_user: str | None = None,
    interactive: bool = False,
    echo: bool = False,
) -> dict[str, Any]:
    """Run one host command; never raises, never asks for a password itself.

    The one runner behind the Wizard's privileged tools and playbook jobs
    (``interactive=False``) and the CLI's ``nvh playbook install`` in the
    user's own terminal (``interactive=True``). Order of business:

    1. :func:`denied_reason` on the rendered command → ``{ok: False, denied: True, error}``.
    2. ``sudo=True`` picks the prefix from the platform facts. Root runs the
       command bare (``sudo -u USER`` still applies when ``sudo_user`` is set).

       ``interactive=False``: ``sudo -n`` (plus ``-u USER``) only when the
       facts say ``can_sudo``; a sudo-group member without passwordless sudo
       gets ``{ok: False, needs_terminal: True, command, hint}`` and nothing
       runs; anyone else ``{ok: False, error: "this account cannot elevate"}``.

       ``interactive=True``: plain ``sudo`` — no ``-n`` — whenever the user
       is root, ``can_sudo`` or ``in_sudo_group``, so sudo may ask for the
       password on the terminal's own tty; nvHive never sees it. While the
       privilege probe is still pending the group answer is a placeholder
       (``host_probe_pending``), so sudo itself is left to decide. An account
       with none of those is refused with ``CANNOT_ELEVATE_ERROR`` before
       anything spawns.
    3. ``subprocess.run`` with ``timeout``. ``stdin`` is ``DEVNULL`` unless
       ``interactive`` (then the terminal's own, inherited). stdout/stderr are
       captured, redacted and cut at 1 MB; ``echo=True`` — honoured only with
       ``interactive`` — leaves them on the terminal instead, so the result
       carries them empty.

    Returns ``{ok, command, exit_code, stdout, stderr}`` on a run
    (``ok`` is ``exit_code == 0``), or one of the refusal shapes above.
    """
    words = [str(w) for w in argv]
    if not words:
        return {"ok": False, "error": "empty command"}
    if not sudo:
        sudo_user = None
    human = render_command(words, sudo=sudo, sudo_user=sudo_user)

    reason = denied_reason(_human_argv(words, sudo=sudo, sudo_user=sudo_user))
    if reason:
        return {"ok": False, "denied": True, "error": reason, "command": human}

    full = words
    if sudo:
        facts = _facts()
        as_user = ["-u", sudo_user] if sudo_user else []
        if facts.has_root and not sudo_user:
            full = words
        elif interactive:
            elevates = facts.has_root or facts.can_sudo or facts.in_sudo_group
            if not elevates and not getattr(facts, "host_probe_pending", False):
                return {"ok": False, "error": CANNOT_ELEVATE_ERROR, "command": human}
            full = ["sudo", *as_user, *words]
        elif facts.can_sudo or facts.has_root:
            full = ["sudo", "-n", *as_user, *words]
        elif facts.in_sudo_group:
            return {"ok": False, "needs_terminal": True, "command": human, "hint": NEEDS_TERMINAL_HINT}
        else:
            return {"ok": False, "error": CANNOT_ELEVATE_ERROR, "command": human}

    rendered = shlex.join(full)
    # ``capture_output`` and explicit ``stdout``/``stderr`` are mutually exclusive
    # for subprocess.run, so the streaming (echo) form passes the latter.
    output: dict[str, Any] = {"stdout": None, "stderr": None} if interactive and echo else {"capture_output": True}
    try:
        proc = subprocess.run(
            full,
            stdin=None if interactive else subprocess.DEVNULL,
            timeout=timeout,
            **output,
        )
    except FileNotFoundError:
        return {"ok": False, "error": f"{full[0]}: command not found", "command": rendered}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"timed out after {timeout:g}s", "timed_out": True, "command": rendered}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:200]}", "command": rendered}

    return {
        "ok": proc.returncode == 0,
        "command": rendered,
        "exit_code": proc.returncode,
        "stdout": _clean_output(proc.stdout),
        "stderr": _clean_output(proc.stderr),
    }


def _probe(argv: Sequence[str], *, timeout: float = PROBE_TIMEOUT_S) -> dict[str, Any] | None:
    """A read-only, sudo-free host probe; ``None`` when the binary is missing."""
    if not argv or _which(str(argv[0])) is None:
        return None
    result = run_host_command(argv, sudo=False, timeout=timeout)
    return result if "exit_code" in result else None


# ────────────────────────────────────────────────────────────────────────────
# Plans — what a privileged tool would do, produced by the same code the apply runs
# ────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Step:
    argv: tuple[str, ...]
    sudo: bool = False
    sudo_user: str | None = None
    timeout: float = DEFAULT_TIMEOUT_S

    def human_argv(self) -> list[str]:
        return _human_argv(self.argv, sudo=self.sudo, sudo_user=self.sudo_user)

    def render(self) -> str:
        return shlex.join(self.human_argv())


@dataclass(frozen=True)
class Plan:
    title: str
    changes: str
    steps: tuple[Step, ...]
    undo: tuple[str, ...] = ()
    warning: str = ""
    notes: tuple[str, ...] = ()
    #: The catalogue key or tool name. :func:`build_plan` fills it from the
    #: catalogue key, so a setting's builder never retypes its own name.
    name: str = ""

    @property
    def needs_sudo(self) -> bool:
        return any(step.sudo for step in self.steps)

    def commands(self) -> list[str]:
        return [step.render() for step in self.steps]

    def denied(self) -> str | None:
        for step in self.steps:
            reason = denied_reason(step.human_argv())
            if reason:
                return reason
        return None

    def to_dict(self) -> dict[str, Any]:
        denied = self.denied()
        out: dict[str, Any] = {
            "ok": denied is None,
            "setting": self.name,
            "title": self.title,
            "commands": self.commands(),
            "sudo": self.needs_sudo,
            "changes": self.changes,
            "undo": list(self.undo),
            "notes": list(self.notes),
        }
        if self.warning:
            out["warning"] = self.warning
        if denied:
            out["error"] = denied
        return out


def _plan_disable_headless_suspend(args: dict[str, Any]) -> Plan:
    key = ("org.gnome.settings-daemon.plugins.power", "sleep-inactive-ac-type")
    return Plan(
        title="Stop the GDM greeter suspending a headless machine",
        changes=(
            "Sets sleep-inactive-ac-type to 'nothing' for the gdm greeter account and for your "
            "own desktop session, so an idle Spark with no monitor stays reachable instead of "
            "suspending after about 20 minutes."
        ),
        steps=(
            Step(("dbus-launch", "gsettings", "set", *key, "nothing"), sudo=True, sudo_user="gdm"),
            Step(("dbus-launch", "gsettings", "set", *key, "nothing")),
        ),
        undo=(
            f"sudo -u gdm dbus-launch gsettings set {key[0]} {key[1]} suspend",
            f"dbus-launch gsettings set {key[0]} {key[1]} suspend",
        ),
        notes=("Takes effect the next time the greeter starts; no reboot needed for your session.",),
    )


def _plan_add_user_to_docker_group(args: dict[str, Any]) -> Plan:
    user = _current_user()
    if not USERNAME_RE.match(user):
        raise PlanError("could not determine a valid login name for the current user")
    return Plan(
        title=f"Add {user} to the docker group",
        changes=(
            f"Creates the docker group if missing and adds {user} to it so Docker commands work "
            "without sudo. Note: docker-group members are effectively root on this machine."
        ),
        steps=(
            Step(("groupadd", "-f", "docker"), sudo=True),
            Step(("usermod", "-aG", "docker", user), sudo=True),
        ),
        undo=(f"sudo gpasswd -d {user} docker",),
        notes=("Log out and back in (or run `newgrp docker`) before `docker ps` works without sudo.",),
    )


def _plan_enable_ssh(args: dict[str, Any]) -> Plan:
    return Plan(
        title="Enable and start the OpenSSH server",
        changes="Enables the ssh unit at boot and starts it now, so the machine accepts SSH logins.",
        steps=(Step(("systemctl", "enable", "--now", "ssh"), sudo=True),),
        undo=("sudo systemctl disable --now ssh",),
        notes=("Ubuntu's unit is `ssh`; if it is missing, install openssh-server first (apt_install).",),
    )


def _plan_enable_ufw_tailscale_only(args: dict[str, Any]) -> Plan:
    return Plan(
        title="Firewall: deny incoming except over Tailscale",
        changes=(
            "Sets ufw to deny incoming and allow outgoing, allows everything arriving on "
            "tailscale0, then enables the firewall."
        ),
        steps=(
            Step(("ufw", "default", "deny", "incoming"), sudo=True),
            Step(("ufw", "default", "allow", "outgoing"), sudo=True),
            Step(("ufw", "allow", "in", "on", "tailscale0"), sudo=True),
            Step(("ufw", "--force", "enable"), sudo=True),
        ),
        undo=(
            "sudo ufw delete allow in on tailscale0",
            "sudo ufw disable   # yourself, in a terminal: nvHive never turns a firewall off",
        ),
        warning=(
            "If you are connected over the LAN rather than Tailscale this disconnects you: SSH will "
            "only answer on tailscale0. Check `tailscale status` shows this machine connected first."
        ),
    )


def _plan_set_hostname(args: dict[str, Any]) -> Plan:
    value = args.get("value") or args.get("hostname")
    if not isinstance(value, str) or not HOSTNAME_RE.match(value.strip()):
        raise PlanError("value required: a hostname of letters, digits and hyphens (max 63 chars)")
    value = value.strip()
    current = ""
    try:
        current = socket.gethostname()
    except Exception:
        pass
    return Plan(
        title=f"Set the hostname to {value}",
        changes=f"Changes the static, transient and pretty hostname from {current or 'the current name'} to {value}.",
        steps=(Step(("hostnamectl", "set-hostname", value), sudo=True),),
        undo=(f"sudo hostnamectl set-hostname {current}",) if current else (),
        notes=("If /etc/hosts lists the old name, update that line too.",),
    )


def _installed_packages() -> list[str]:
    result = _probe(["dpkg-query", "-W", "-f=${Package}\\n"])
    if not result or not result.get("ok"):
        return []
    return [line.strip() for line in result.get("stdout", "").splitlines() if line.strip()]


def _plan_hold_nvidia_driver_packages(args: dict[str, Any]) -> Plan:
    held = sorted(
        pkg for pkg in _installed_packages()
        if HOLD_PACKAGE_RE.match(pkg) and not HOLD_EXCLUDE_RE.match(pkg)
    )
    if not held:
        raise PlanError(
            "no installed NVIDIA driver packages found to hold (dpkg-query unavailable or nothing matched)",
        )
    return Plan(
        title=f"Hold {len(held)} NVIDIA driver package(s) against apt upgrade",
        changes=(
            "Marks the installed NVIDIA driver packages held so a plain `apt upgrade` skips them; "
            "container-toolkit packages are left alone. Held: " + ", ".join(held)
        ),
        steps=(Step(("apt-mark", "hold", *held), sudo=True),),
        undo=("sudo apt-mark unhold " + " ".join(held),),
        warning=DGX_DRIVER_WARNING,
    )


@dataclass(frozen=True)
class Setting:
    """One catalogue entry: how it is planned, what it does, how it elevates.

    ``description`` is what the model reads in ``system_settings_plan`` /
    ``system_settings_apply``'s ``setting`` parameter and what
    ``system_settings_get`` lists; ``sudo`` is the one-line privilege note.
    """

    build: Callable[[dict[str, Any]], Plan]
    description: str
    sudo: str = "sudo"


#: The catalogue. The key is the setting's name everywhere — the tool
#: parameter, ``Plan.name`` (:func:`build_plan` fills it), the apply result,
#: the vault note — so nothing else spells it.
SETTINGS: dict[str, Setting] = {
    "disable_headless_suspend": Setting(
        _plan_disable_headless_suspend,
        "stop the GDM greeter suspending an idle headless machine (gsettings for gdm + you)",
        sudo="sudo -u gdm for the greeter; your own session's setting needs none",
    ),
    "add_user_to_docker_group": Setting(
        _plan_add_user_to_docker_group,
        "add the current user to the docker group (usermod -aG docker)",
    ),
    "enable_ssh": Setting(_plan_enable_ssh, "systemctl enable --now ssh"),
    "enable_ufw_tailscale_only": Setting(
        _plan_enable_ufw_tailscale_only,
        "ufw: deny incoming, allow outgoing, allow in on tailscale0, enable",
    ),
    "set_hostname": Setting(_plan_set_hostname, "hostnamectl set-hostname <value> (needs `value`)"),
    "hold_nvidia_driver_packages": Setting(
        _plan_hold_nvidia_driver_packages,
        "apt-mark hold the installed NVIDIA driver packages (DGX OS apt-upgrade guard)",
    ),
}


def catalogue() -> list[dict[str, str]]:
    return [
        {"setting": key, "does": entry.description, "sudo": entry.sudo}
        for key, entry in SETTINGS.items()
    ]


def _setting_name(args: dict[str, Any]) -> str | None:
    value = args.get("setting") or args.get("name")
    return value.strip() if isinstance(value, str) and value.strip() else None


def build_plan(setting: str, args: dict[str, Any]) -> Plan:
    """The plan for a catalogue setting, named after its key; :class:`PlanError` for anything else."""
    entry = SETTINGS.get(setting)
    if entry is None:
        raise PlanError(f"unknown setting '{setting}'. Catalogue: {', '.join(SETTINGS)}")
    return replace(entry.build(args), name=setting)


# ────────────────────────────────────────────────────────────────────────────
# Installs and services
# ────────────────────────────────────────────────────────────────────────────


def _packages_from(args: dict[str, Any]) -> list[str]:
    raw = args.get("packages", args.get("package"))
    if isinstance(raw, str):
        items = [p for p in re.split(r"[\s,]+", raw) if p]
    elif isinstance(raw, (list, tuple)):
        items = [str(p).strip() for p in raw if str(p).strip()]
    else:
        items = []
    if not items:
        raise PlanError("packages required: a list of package names (or one space/comma-separated string)")
    if len(items) > MAX_PACKAGES:
        raise PlanError(f"at most {MAX_PACKAGES} packages per call")
    seen: list[str] = []
    for pkg in items:
        if not PACKAGE_RE.match(pkg):
            raise PlanError(
                f"invalid package name '{pkg[:60]}' (allowed: {PACKAGE_RULE}; a trailing '-' or '+' "
                "would tell apt to remove or install instead)",
            )
        if DRIVER_PACKAGE_RE.match(pkg):
            raise PlanError(f"refusing to install '{pkg}': {DGX_DRIVER_WARNING}")
        if pkg not in seen:
            seen.append(pkg)
    return seen


def _prepare_apt_install(args: dict[str, Any]) -> Plan:
    packages = _packages_from(args)
    return Plan(
        name="apt_install",
        title="Install " + ", ".join(packages) + " with apt-get",
        changes=f"apt-get installs {len(packages)} package(s) without recommends; nothing is upgraded.",
        steps=(
            Step(("apt-get", "install", "-y", "--no-install-recommends", *packages), sudo=True, timeout=INSTALL_TIMEOUT_S),
        ),
        undo=("sudo apt-get remove " + " ".join(packages),),
        notes=("Runs `apt-get install` only — never `apt upgrade`. " + DGX_DRIVER_WARNING,),
    )


def _prepare_snap_install(args: dict[str, Any]) -> Plan:
    packages = _packages_from(args)
    classic = args.get("classic") is True
    flags = ("--classic",) if classic else ()
    return Plan(
        name="snap_install",
        title="Install snap " + ", ".join(packages) + (" (classic confinement)" if classic else ""),
        changes=f"snap installs {len(packages)} package(s)" + (" with classic confinement (no sandbox)." if classic else "."),
        steps=(Step(("snap", "install", *flags, *packages), sudo=True, timeout=INSTALL_TIMEOUT_S),),
        undo=("sudo snap remove " + " ".join(packages),),
    )


def _prepare_service_enable(args: dict[str, Any]) -> Plan:
    unit = args.get("unit") or args.get("service")
    if not isinstance(unit, str) or not UNIT_RE.match(unit.strip()):
        raise PlanError("unit required: a systemd unit name such as ssh, docker or tailscaled")
    unit = unit.strip()
    if denied_unit(unit):
        raise PlanError(
            f"refusing to enable '{unit}': power-state, rescue and shutdown units (and the "
            "systemd-* services behind them) are off limits",
        )
    return Plan(
        name="service_enable",
        title=f"Enable and start {unit}",
        changes=f"Enables {unit} at boot and starts it now.",
        steps=(Step(("systemctl", "enable", "--now", unit), sudo=True),),
        undo=(f"sudo systemctl disable --now {unit}",),
    )


# ────────────────────────────────────────────────────────────────────────────
# Applying a plan
# ────────────────────────────────────────────────────────────────────────────


def apply_plan(plan: Plan) -> dict[str, Any]:
    """Run a plan step by step; stop at the first refusal, hand-off or failure.

    ``steps`` in the result carry each executed command with its exit code
    and (redacted, 1 MB-cut) output. ``applied`` is True once *any* command
    ran — a failing command may have changed the host before exiting
    (``systemctl enable --now`` enables the unit and then fails to start it;
    ``apt-get`` exits 100 after unpacking) — so the registry audits it;
    ``partial`` says later steps never ran.
    """
    denied = plan.denied()
    if denied:
        return {"ok": False, "denied": True, "error": denied, "setting": plan.name, "commands": plan.commands()}
    executed: list[dict[str, Any]] = []
    for index, step in enumerate(plan.steps):
        result = run_host_command(list(step.argv), sudo=step.sudo, sudo_user=step.sudo_user, timeout=step.timeout)
        if result.get("needs_terminal"):
            return {
                "ok": False,
                "needs_terminal": True,
                "setting": plan.name,
                "command": result["command"],
                "commands": [s.render() for s in plan.steps[index:]],
                "hint": NEEDS_TERMINAL_HINT,
                "steps": executed,
                "applied": bool(executed),
                "partial": bool(executed),
                "undo": list(plan.undo),
            }
        if "exit_code" not in result:
            return {
                "ok": False,
                "error": result.get("error", "command did not run"),
                "denied": result.get("denied", False),
                "setting": plan.name,
                "command": result.get("command"),
                "steps": executed,
                "applied": bool(executed),
                "partial": bool(executed),
            }
        executed.append({
            "command": result["command"],
            "exit_code": result["exit_code"],
            "stdout": result["stdout"],
            "stderr": result["stderr"],
        })
        if not result["ok"]:
            return {
                "ok": False,
                "error": f"`{result['command']}` exited {result['exit_code']}",
                "setting": plan.name,
                "summary": plan.title,
                "steps": executed,
                # The failing command ran, so the host may have changed.
                "applied": True,
                "partial": len(executed) < len(plan.steps),
                "undo": list(plan.undo),
            }
    out: dict[str, Any] = {
        "ok": True,
        "applied": True,
        "setting": plan.name,
        "summary": plan.title,
        "steps": executed,
        "undo": list(plan.undo),
        "notes": list(plan.notes),
    }
    if plan.warning:
        out["warning"] = plan.warning
    return out


def plan_setting(args: dict[str, Any]) -> dict[str, Any]:
    setting = _setting_name(args)
    if setting is None:
        return {"ok": False, "error": "setting required", "catalogue": catalogue()}
    try:
        return build_plan(setting, args).to_dict()
    except PlanError as exc:
        return {"ok": False, "error": str(exc), "setting": setting, "commands": [], "catalogue": catalogue()}


def apply_setting(args: dict[str, Any]) -> dict[str, Any]:
    setting = _setting_name(args)
    if setting is None:
        return {"ok": False, "error": "setting required", "catalogue": catalogue()}
    try:
        plan = build_plan(setting, args)
    except PlanError as exc:
        return {"ok": False, "error": str(exc), "setting": setting}
    return apply_plan(plan)


def _plan_dict(prepare: Callable[[dict[str, Any]], Plan], args: dict[str, Any]) -> dict[str, Any]:
    try:
        return prepare(args).to_dict()
    except PlanError as exc:
        return {"ok": False, "error": str(exc), "commands": []}


def _apply_prepared(prepare: Callable[[dict[str, Any]], Plan], args: dict[str, Any]) -> dict[str, Any]:
    try:
        plan = prepare(args)
    except PlanError as exc:
        return {"ok": False, "error": str(exc)}
    return apply_plan(plan)


# ────────────────────────────────────────────────────────────────────────────
# Read-only facts
# ────────────────────────────────────────────────────────────────────────────


def _dgx_release() -> dict[str, str] | None:
    """``/etc/dgx-release`` as key/values (DGX_SWBUILD_VERSION …); ``None`` off DGX OS.

    Platform facts only record that the file exists (``is_dgx_os``); the
    build number inside it is this module's one extra read.
    """
    text = _read_text("/etc/dgx-release")
    if not text.strip():
        return None
    data: dict[str, str] = {}
    for line in text.splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            data[key.strip()] = value.strip().strip('"')
    return data or {"raw": text.strip()[:200]}


def _driver_facts() -> dict[str, Any]:
    result = _probe(["nvidia-smi"])
    if result is None:
        return {"available": False}
    text = result.get("stdout", "")
    driver = re.search(r"Driver Version:\s*([\w.]+)", text)
    cuda = re.search(r"CUDA Version:\s*([\w.]+)", text)
    return {
        "available": result.get("ok", False),
        "driver_version": driver.group(1) if driver else None,
        "cuda_version": cuda.group(1) if cuda else None,
    }


def _docker_group_facts() -> dict[str, Any]:
    user = _current_user()
    result = _probe(["id", "-nG"])
    if result is None or not result.get("ok"):
        return {"user": user, "in_docker_group": None, "note": "group list unavailable"}
    groups = result.get("stdout", "").split()
    return {"user": user, "in_docker_group": "docker" in groups, "groups": groups[:32]}


def _auto_suspend_facts() -> dict[str, Any]:
    result = _probe(["gsettings", "get", "org.gnome.settings-daemon.plugins.power", "sleep-inactive-ac-type"])
    if result is None:
        return {"available": False}
    value = result.get("stdout", "").strip().strip("'") if result.get("ok") else None
    return {
        "available": True,
        "user_setting": value,
        "greeter_setting": "not probed (reading the gdm account's setting needs sudo)",
        "headless_risk": value not in (None, "nothing"),
    }


def _ufw_facts() -> dict[str, Any]:
    result = _probe(["ufw", "status"])
    if result is None:
        return {"installed": False}
    if not result.get("ok"):
        return {"installed": True, "status": "unreadable without sudo"}
    match = re.search(r"Status:\s*(\w+)", result.get("stdout", ""))
    return {"installed": True, "status": match.group(1) if match else "unknown"}


def _tailscale_facts() -> dict[str, Any]:
    result = _probe(["tailscale", "status", "--json"])
    if result is None:
        return {"installed": False}
    if not result.get("ok"):
        return {"installed": True, "backend_state": "unknown", "detail": result.get("stderr", "")[:200]}
    try:
        data = json.loads(result.get("stdout", "") or "{}")
    except ValueError:
        return {"installed": True, "backend_state": "unknown"}
    self_node = data.get("Self") or {}
    return {
        "installed": True,
        "backend_state": data.get("BackendState"),
        "ips": list(self_node.get("TailscaleIPs") or [])[:4],
        "dns_name": self_node.get("DNSName"),
    }


def _unattended_upgrades_facts() -> dict[str, Any]:
    conf = _read_text("/etc/apt/apt.conf.d/20auto-upgrades")
    periodic = re.search(r'Unattended-Upgrade\s+"(\d)"', conf)
    unit = _probe(["systemctl", "is-enabled", "unattended-upgrades"])
    return {
        "installed": _which("unattended-upgrade") is not None,
        "apt_periodic": None if periodic is None else periodic.group(1) == "1",
        "unit": (unit.get("stdout", "").strip() or "unknown") if unit else "unknown",
    }


def _safe(label: str, fn: Callable[[], Any]) -> Any:
    try:
        return fn()
    except Exception as exc:
        logger.debug("system_settings probe %s failed: %s", label, exc)
        return {"error": f"{type(exc).__name__}"}


def collect_system_settings() -> dict[str, Any]:
    """Read-only facts about the host. Every probe optional; nothing needs sudo.

    Distro, kernel and the DGX OS verdict come from the
    :class:`~nvh.utils.platform_facts.PlatformFacts` already in hand (one
    reading of ``/etc/os-release`` per process, seedable in tests); this
    module only adds what those facts do not carry.
    """
    from nvh.integrations.wizard.tools import privileged_enabled

    facts = _facts()
    if facts.has_root:
        sudo_mode = "root"
    elif facts.can_sudo:
        sudo_mode = "passwordless (sudo -n)"
    elif facts.in_sudo_group:
        sudo_mode = "password required — commands are handed to a terminal"
    else:
        sudo_mode = "none — this account cannot elevate"
    return {
        "ok": True,
        "hostname": _safe("hostname", socket.gethostname),
        "kernel": facts.kernel,
        "os": {
            "pretty_name": facts.distro,
            "dgx_release": _safe("dgx-release", _dgx_release),
            "is_dgx_os": facts.is_dgx_os,
        },
        "platform": {
            "device_class": facts.device_class,
            "device_label": facts.device_label,
            "arch": facts.arch,
            "gpu_name": facts.gpu_name,
            "unified_memory": facts.unified_memory,
            "memory_total_gb": facts.memory_total_gb,
            "memory_available_gb": facts.memory_available_gb,
        },
        "driver": _safe("driver", _driver_facts),
        "sudo": {
            "has_root": facts.has_root,
            "can_sudo": facts.can_sudo,
            "in_sudo_group": facts.in_sudo_group,
            "mode": sudo_mode,
        },
        "docker_group": _safe("docker-group", _docker_group_facts),
        "auto_suspend": _safe("auto-suspend", _auto_suspend_facts),
        "ufw": _safe("ufw", _ufw_facts),
        "tailscale": _safe("tailscale", _tailscale_facts),
        "unattended_upgrades": _safe("unattended-upgrades", _unattended_upgrades_facts),
        "privileged_tools_enabled": privileged_enabled(),
        "catalogue": catalogue(),
    }


# ────────────────────────────────────────────────────────────────────────────
# Wizard tool handlers + registration
# ────────────────────────────────────────────────────────────────────────────


def _threaded(
    fn: Callable[[dict[str, Any]], dict[str, Any]], label: str, *, plan: bool = False,
) -> Callable[[dict[str, Any]], Any]:
    """Wrap a blocking handler for the async registry.

    Runs ``fn(args)`` in a worker thread (these spawn ``apt-get`` and friends,
    so they must not sit on the event loop) and turns any exception into
    ``{ok: False, error: "<label> failed: <Type>: <message>"}`` — the message
    kept, cut at 200 chars — so handlers never raise. ``plan=True`` adds the
    empty ``commands`` list a plan dict always carries.
    """
    async def run(args: dict[str, Any]) -> dict[str, Any]:
        try:
            return await asyncio.to_thread(fn, args)
        except Exception as exc:
            failure: dict[str, Any] = {
                "ok": False, "error": f"{label} failed: {type(exc).__name__}: {str(exc)[:200]}",
            }
            if plan:
                failure["commands"] = []
            return failure

    run.__name__ = f"_tool_{label.replace(' ', '_')}"
    return run


_tool_get = _threaded(lambda args: collect_system_settings(), "system_settings_get")
_tool_plan = _threaded(plan_setting, "system_settings_plan", plan=True)
_tool_apply = _threaded(apply_setting, "system_settings_apply")
_tool_apt_install = _threaded(partial(_apply_prepared, _prepare_apt_install), "apt_install")
_tool_snap_install = _threaded(partial(_apply_prepared, _prepare_snap_install), "snap_install")
_tool_service_enable = _threaded(partial(_apply_prepared, _prepare_service_enable), "service_enable")
_plan_apt_install = _threaded(partial(_plan_dict, _prepare_apt_install), "apt_install dry run", plan=True)
_plan_snap_install = _threaded(partial(_plan_dict, _prepare_snap_install), "snap_install dry run", plan=True)
_plan_service_enable = _threaded(
    partial(_plan_dict, _prepare_service_enable), "service_enable dry run", plan=True,
)

_SETTING_PARAM = {
    "type": "string", "required": True,
    "description": "One of: " + ", ".join(f"{key} ({entry.description})" for key, entry in SETTINGS.items()) + ".",
}
_VALUE_PARAM = {
    "type": "string", "required": False,
    "description": "The value for settings that take one (set_hostname: the new hostname).",
}
_PACKAGES_PARAM = {
    "type": "array", "required": True,
    "description": (
        f"Package names (lowercase, {PACKAGE_RULE}; no trailing '-' or '+'). NVIDIA driver, "
        "cuda-drivers and kernel packages are refused: on DGX OS they come through NVIDIA's "
        "validated channel."
    ),
}


def register_wizard_tools(reg: Any) -> None:
    """Register the six system-settings tools on a ``WizardToolRegistry``.

    Registered unconditionally, including with ``NVH_ALLOW_PRIVILEGED=0`` —
    the registry refuses the privileged ones at execute time and the
    catalogue still explains them. Reads and the dry run are ``auto``.
    """
    from nvh.integrations.wizard.tools import WizardTool

    reg.register(WizardTool(
        name="system_settings_get",
        description=(
            "Read-only facts about this machine: hostname, DGX OS / distro version, kernel, "
            "NVIDIA driver and CUDA version, whether sudo is passwordless, docker group "
            "membership, the GNOME auto-suspend setting, ufw status, Tailscale state and "
            "unattended-upgrades. Call this before planning a system change."
        ),
        safety_class="auto",
        parameters={},
        handler=_tool_get,
        summary_template="Read the machine's system settings.",
    ))

    reg.register(WizardTool(
        name="system_settings_plan",
        description=(
            "Dry run of a catalogue system setting: the exact commands it would run, whether "
            "sudo is needed, what changes and how to undo it. Runs nothing. Use it to show the "
            "user before system_settings_apply."
        ),
        safety_class="auto",
        parameters={"setting": _SETTING_PARAM, "value": _VALUE_PARAM},
        handler=_tool_plan,
        summary_template="Plan the system setting {setting}.",
    ))

    reg.register(WizardTool(
        name="system_settings_apply",
        description=(
            "Apply a catalogue system setting on this machine (usually with sudo). The user "
            "approves the exact commands on a red card first; if sudo needs a password the "
            "result hands them the command to run in a terminal. Every apply is recorded in the "
            "vault under Decisions."
        ),
        safety_class="privileged",
        parameters={"setting": _SETTING_PARAM, "value": _VALUE_PARAM},
        handler=_tool_apply,
        planner=_tool_plan,
        summary_template="Apply the system setting {setting}.",
    ))

    reg.register(WizardTool(
        name="apt_install",
        description=(
            "Install Debian packages with `apt-get install -y --no-install-recommends` (sudo). "
            "Never upgrades anything; refuses NVIDIA driver / cuda-drivers / kernel packages."
        ),
        safety_class="privileged",
        parameters={"packages": _PACKAGES_PARAM},
        handler=_tool_apt_install,
        planner=_plan_apt_install,
        summary_template="apt-get install {packages}.",
    ))

    reg.register(WizardTool(
        name="snap_install",
        description="Install snap packages with `snap install` (sudo). Pass classic=true only for snaps that need it.",
        safety_class="privileged",
        parameters={
            "packages": _PACKAGES_PARAM,
            "classic": {"type": "boolean", "required": False, "description": "Use --classic confinement."},
        },
        handler=_tool_snap_install,
        planner=_plan_snap_install,
        summary_template="snap install {packages}.",
    ))

    reg.register(WizardTool(
        name="service_enable",
        description="Enable a systemd unit at boot and start it now: `systemctl enable --now <unit>` (sudo).",
        safety_class="privileged",
        parameters={
            "unit": {"type": "string", "required": True, "description": "Unit name, e.g. ssh, docker, tailscaled."},
        },
        handler=_tool_service_enable,
        planner=_plan_service_enable,
        summary_template="systemctl enable --now {unit}.",
    ))
