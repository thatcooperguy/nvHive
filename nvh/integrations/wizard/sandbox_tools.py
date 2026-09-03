"""The sandbox bridge: ``shell`` and ``run_code`` as Wizard tools.

Phase 3 of docs/proposals/SPARK_CONCIERGE_2026-09.md ("the bridge"): the
agentic sandbox tools the core :class:`nvh.core.tools.ToolRegistry` already
has reach the Wizard chat behind approval cards. A narrow, explicit bridge of
two tools with their own safety classes — not a sweep of the core registry.

  - ``shell`` is ``privileged``. Isolation is decided per call by
    :meth:`nvh.sandbox.executor.SandboxExecutor._select_mode` (Docker when
    ``docker info`` answers, otherwise a plain subprocess, fail-open by
    default), so the class cannot depend on the environment at registration.
    The planner is a dry run that renders the exact command, the isolation
    the run *will* get (it asks the executor's own Docker probe without
    running anything), the working directory and the timeout, with a warning
    line when the run would not be isolated. The isolation it rendered is
    **pinned into the approved arguments** (``plan.pinned_arguments`` →
    ``arguments.isolation``, signed by the approval token), and the handler
    re-probes and refuses in band when Docker's availability has changed
    since the card — a card approved as "Docker sandbox" can never run on the
    host, and one approved as "on this machine" never silently moves into a
    container. The handler applies BOTH deny lists before spawning —
    :func:`nvh.core.agent_guardrails.check_command` on the raw command and
    :func:`nvh.integrations.wizard.system_settings.denied_reason` (the
    host-hitting list) on every simple command in it (``&&`` / ``;`` / ``|``
    chains split, ``env`` / ``nohup`` / ``timeout`` prefixes stripped,
    ``sh -c`` and ``eval`` payloads recursed), refuses ``sudo`` / ``su`` /
    ``doas`` outright, then runs through :meth:`SandboxExecutor.run_shell`
    exactly as the core ``shell`` tool does — with a stricter subprocess
    fallback that closes stdin and strips key/token-looking variables from
    the environment — and answers in the system-settings apply shape (``ok``,
    ``applied``, ``summary``, ``steps=[{command, exit_code, stdout, stderr}]``,
    ``isolation``, ``timed_out``) so ``WizardToolRegistry.execute()`` writes
    the vault ``Decisions/`` note and fits the result to the tool window
    untouched. Output is redacted before it is cut; ``command`` is never cut.
    The deny lists are a backstop for the well-known destructive shapes, not
    a sandbox: the red card the user reads is the gate.
  - ``run_code`` is ``confirm`` — honest only because the handler forces
    ``SandboxConfig(require_docker=True)``: without Docker it returns an
    in-band refusal (``ok: False, refused: True, error``) naming ``docker``
    and pointing at the playbooks that install it, and executes nothing. The
    guardrail blocklist is applied to the ``code`` argument.

Parameters are translated from the core tools' JSON Schema into the
``WizardTool`` shape (``{name: {type, description, required}}``) by
:func:`nvh.integrations.wizard.tools.parameters_from_json_schema`, so there
is no second hand-typed schema; the two bridge-only ``shell`` parameters
(``cwd``, ``timeout_s``) are added on top.

The workspace both tools see is the rootless layout's ``projects/`` directory
(``NVH_PROJECTS``, default ``$NVH_HOME/projects`` — "agent workspaces" in
docs/CONFIGURATION.md): mounted read-write at ``/workspace`` under Docker,
the subprocess fallback's cwd otherwise. ``cwd`` must stay inside it. The
vision bridge's allowlist imports :func:`workspace_dir` so both tools agree
on where "the workspace" is.
"""

from __future__ import annotations

import asyncio
import getpass
import logging
import os
import re
import shlex
import tempfile
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from nvh.core.agent_guardrails import (
    GuardrailError,
    check_command,
    check_path,
    redact_secrets,
    truncate_output,
)
from nvh.sandbox.executor import ExecutionResult, SandboxConfig, SandboxExecutor

logger = logging.getLogger(__name__)

#: ``shell``'s default and ceiling for ``timeout_s``.
SHELL_DEFAULT_TIMEOUT_S = 60
SHELL_MAX_TIMEOUT_S = 300
#: ``run_code``'s fixed timeout (the core tool uses the executor default, 30 s).
RUN_CODE_TIMEOUT_S = 60
#: How long the planner AND the handler wait for ``docker info`` before calling
#: Docker unavailable — the same bound on both sides, so a slow daemon cannot
#: make the card and the run disagree.
DOCKER_PROBE_TIMEOUT_S = 5.0

RUN_CODE_LANGUAGES: tuple[str, ...] = ("python", "javascript", "bash")

#: What ``arguments.isolation`` may hold once the planner has pinned it:
#: ``"docker"``, ``"subprocess"`` or ``""`` (the card said the run would be
#: refused — isolation required, Docker absent).
ISOLATION_MODES: tuple[str, ...] = ("docker", "subprocess", "")

ISOLATION_DOCKER = "Docker sandbox (no network, read-only image, /workspace mounted)"
#: ``{user}`` is the account the nvHive process runs as.
ISOLATION_SUBPROCESS = (
    "directly on this machine as {user}, no Docker isolation "
    "(stdin closed; API keys and tokens removed from its environment)"
)
ISOLATION_REFUSED = (
    "refused — Docker is unavailable and isolation is required "
    "(NVH_SANDBOX_REQUIRE_DOCKER); the run will execute nothing"
)
NOT_ISOLATED_WARNING = (
    "Not isolated: Docker was not found (`docker info` failed), so this command runs directly on "
    "this machine as {user}, with your permissions, your files and your network. Its stdin is closed "
    "and variables that look like API keys or tokens are removed from its environment; everything "
    "else it can reach, it can change. Read it before approving."
)
ISOLATION_PINNED_NOTE = (
    "Approval is bound to this isolation: if Docker's availability changes before the run, "
    "nothing runs and a fresh card is needed."
)
#: The in-band refusal ``run_code`` returns without Docker. Names ``docker``,
#: the playbooks that install it, and the terminal.
RUN_CODE_NEEDS_DOCKER_ERROR = (
    "run_code needs Docker and `docker info` failed (docker is not installed, not running, or this "
    "account is not in the docker group). Nothing was executed. Install Docker with the open-webui or "
    "vllm playbook (playbook_plan, then playbook_install), or run the code yourself in a terminal."
)
#: ``shell`` confirmed with a mode the card did not approve.
ISOLATION_CHANGED_ERROR = (
    "the card approved a {approved} run but Docker is now {now}; nothing ran — "
    "ask the Wizard again so a fresh card can show what the run would get"
)
#: ``shell`` confirmed without the planner's pin (never through a card).
UNPLANNED_ERROR = (
    "no approved isolation on this call: `isolation` is pinned by the red card's dry run, so shell "
    "only runs from the card the Wizard surfaced; nothing ran"
)
#: ``shell`` confirmed on a card that already said the run would be refused.
REQUIRED_ISOLATION_ERROR = (
    "Docker is unavailable and isolation is required (NVH_SANDBOX_REQUIRE_DOCKER) — the card said "
    "the run would be refused; nothing was executed"
)
ESCALATION_ERROR = (
    "BLOCKED: privilege escalation ({word}) — the Wizard's shell runs as the nvHive user; for root, "
    "use apt_install, service_enable or system_settings_apply, which run their own sudo behind a red card"
)

#: Descriptions for the parameters the core schema leaves undocumented, keyed
#: by tool then parameter. The types and the required flags come from the
#: core schema itself (:func:`nvh.integrations.wizard.tools.parameters_from_json_schema`).
_PARAMETER_DESCRIPTIONS: dict[str, dict[str, str]] = {
    "shell": {
        "command": "The shell command, exactly as it will run (bash -c under Docker; the system shell otherwise).",
    },
    "run_code": {
        "code": "The source to run. Written to main.<ext> in a throwaway read-only mount and executed.",
        "language": "python (default), javascript or bash.",
    },
}
#: ``shell`` parameters the core tool does not have; added after translation.
#: ``isolation`` is deliberately NOT listed: the planner pins it, the model
#: never chooses it.
_SHELL_EXTRA_PARAMETERS: dict[str, dict[str, Any]] = {
    "cwd": {
        "type": "string",
        "description": (
            "Working directory, relative to the workspace (or an absolute path inside it). Defaults to the "
            "workspace root. Under Docker this directory is what /workspace is."
        ),
        "required": False,
    },
    "timeout_s": {
        "type": "integer",
        "description": f"Seconds before the command is killed (1-{SHELL_MAX_TIMEOUT_S}, default {SHELL_DEFAULT_TIMEOUT_S}).",
        "required": False,
    },
}


# ────────────────────────────────────────────────────────────────────────────
# Small seams (patched by tests) and helpers
# ────────────────────────────────────────────────────────────────────────────


def _current_user() -> str:
    try:
        return getpass.getuser() or "the nvHive user"
    except Exception:
        return "the nvHive user"


def workspace_dir() -> Path:
    """The directory ``shell`` mounts / runs in: the rootless layout's ``projects/``.

    ``NVH_PROJECTS`` when set, otherwise ``$NVH_HOME/projects`` — the "agent
    workspaces" entry of the storage layout. Resolved; not created here (the
    handler creates it right before a run, the planner never does). The
    vision bridge's allowlist uses the same function.
    """
    from nvh.integrations.workspace.storage import storage_layout

    return Path(storage_layout().projects_dir).resolve()


def _core_schema(name: str) -> dict[str, Any]:
    """The core registry's JSON Schema for ``name`` (built-ins only; no system tools)."""
    from nvh.core.tools import ToolRegistry

    tool = ToolRegistry(include_system=False).get(name)
    if tool is None:  # pragma: no cover — the core tool set is ours
        raise LookupError(f"core tool {name!r} is not registered")
    return tool.parameters


def _clean(text: str | None) -> str:
    """Redact, then cut at the 1 MB output window — in that order."""
    return truncate_output(redact_secrets((text or "").replace("\r\n", "\n"))).strip()


def _blocked_reason(exc: BaseException, default: str = "BLOCKED: guardrail") -> str:
    """The first line of a ``GuardrailError`` — what the card shows — or ``default``."""
    text = str(exc).strip()
    first = text.splitlines()[0].strip() if text else ""
    return first or default


def _whole_number(value: Any, low: int, high: int) -> bool:
    """Is ``value`` an int (or an integral float, not a bool) within ``[low, high]``?"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return value == int(value) and low <= value <= high
    except (OverflowError, ValueError):  # inf / nan
        return False


# ────────────────────────────────────────────────────────────────────────────
# The deny lists, applied to every simple command
# ────────────────────────────────────────────────────────────────────────────

#: Shells whose ``-c`` payload is a command line of its own.
_SHELLS = frozenset({"sh", "bash", "zsh", "dash", "ksh", "ash", "fish"})
#: ``sudo`` and friends: refused outright (see :data:`ESCALATION_ERROR`).
_ESCALATORS = frozenset({"sudo", "su", "doas", "pkexec", "runuser"})
#: Prefix wrappers that run whatever follows them; stripped so the real
#: command is the first word the rules look at.
_WRAPPERS = frozenset({
    "env", "nohup", "nice", "ionice", "time", "timeout", "command", "exec", "builtin",
    "setsid", "xargs", "busybox", "stdbuf", "chronic",
})
#: Options of those wrappers that take a value (so ``nice -n 10 rm`` skips ``10``).
_WRAPPER_VALUE_OPTIONS: dict[str, frozenset[str]] = {
    "env": frozenset({"-u", "--unset", "-C", "--chdir", "-S", "--split-string"}),
    "nice": frozenset({"-n", "--adjustment"}),
    "ionice": frozenset({"-c", "--class", "-n", "--classdata", "-p", "--pid"}),
    "timeout": frozenset({"-k", "--kill-after", "-s", "--signal"}),
    "xargs": frozenset({
        "-I", "-i", "-n", "-P", "-L", "-d", "-a", "-E", "-s",
        "--max-args", "--max-procs", "--delimiter", "--arg-file", "--max-lines", "--replace",
    }),
    "stdbuf": frozenset({"-i", "-o", "-e"}),
}
#: Positional words a wrapper consumes before the command (``timeout 30 cmd``).
_WRAPPER_POSITIONALS: dict[str, int] = {"timeout": 1}
#: Tokens that end a simple command.
_OPERATORS = frozenset({";", ";;", "&&", "||", "|", "|&", "&", "(", ")"})
_MAX_UNWRAP_DEPTH = 6

#: The bridge's own patterns on the raw command — shapes neither list covers
#: that a chat-authored command line can carry.
_BRIDGE_DENIED: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\|\s*(?:sudo\s+)?(?:busybox\s+)?(?:\S*/)?(?:ba|z|da|k|a|fi)?sh(?:\s|$)"),
     "piping into a shell (obfuscated or remote code)"),
    (re.compile(r"\bchmod\s+(?:-\S+\s+)*[0-7]*777\s+/(?:\s|$)"), "world-writable filesystem root"),
    (re.compile(r">\s*/dev/(?:sd|hd|vd|xvd|nvme|mmcblk|md|dm-)"), "raw write to a block device"),
]


def _tokens(line: str) -> list[str] | None:
    """Shell words with ``;`` ``&&`` ``||`` ``|`` ``&`` and parentheses as their own tokens; ``None`` on unbalanced quotes."""
    lex = shlex.shlex(line, posix=True, punctuation_chars=True)
    lex.whitespace_split = True
    try:
        return list(lex)
    except ValueError:
        return None


def _unwrap(words: list[str]) -> list[str]:
    """Strip prefix wrappers (``env VAR=x``, ``nohup``, ``nice -n 5``, ``timeout 30``, ``xargs`` …)."""
    while words:
        head = Path(words[0]).name
        if head not in _WRAPPERS:
            return words
        rest = words[1:]
        value_options = _WRAPPER_VALUE_OPTIONS.get(head, frozenset())
        while rest:
            if rest[0] in value_options and len(rest) > 1:
                rest = rest[2:]
            elif rest[0].startswith("-") and rest[0] != "-":
                rest = rest[1:]
            elif head == "env" and "=" in rest[0] and not rest[0].startswith("="):
                rest = rest[1:]
            else:
                break
        rest = rest[_WRAPPER_POSITIONALS.get(head, 0):]
        words = rest
    return words


def _dash_c_payload(words: list[str]) -> str | None:
    """The string ``sh -c`` / ``bash -lc`` runs, or ``None`` (a script file, or no ``-c``)."""
    for index in range(1, len(words)):
        word = words[index]
        if word == "--" or not word.startswith("-"):
            return None
        if not word.startswith("--") and "c" in word[1:]:
            return words[index + 1] if index + 1 < len(words) else None
    return None


def _expand(words: list[str], depth: int) -> list[list[str]]:
    """``words`` unwrapped, plus every simple command inside its ``sh -c`` / ``eval`` payload."""
    words = _unwrap(words)
    if not words:
        return []
    head = Path(words[0]).name
    if head in _SHELLS:
        payload = _dash_c_payload(words)
        if payload is not None:
            return [words, *simple_commands(payload, depth + 1)]
    if head == "eval" and len(words) > 1:
        return [words, *simple_commands(" ".join(words[1:]), depth + 1)]
    return [words]


def simple_commands(command: str, depth: int = 0) -> list[list[str]]:
    """Every simple command in ``command``, as argv lists the deny rules can read.

    Lines are split at ``;`` ``&&`` ``||`` ``|`` ``&`` and parentheses
    (``$(…)`` included), prefix wrappers are stripped, and the payload of
    ``sh -c '…'`` / ``bash -lc '…'`` / ``eval …`` is expanded recursively (to
    :data:`_MAX_UNWRAP_DEPTH`). A line shlex cannot split (unbalanced quotes)
    falls back to whitespace words. Best effort by design — the card is the
    gate; this is what keeps the well-known shapes from reaching it.
    """
    out: list[list[str]] = []
    if depth > _MAX_UNWRAP_DEPTH:
        return out
    for line in command.splitlines():
        tokens = _tokens(line)
        if tokens is None:
            tokens = line.split()
        segment: list[str] = []
        for token in [*tokens, ";"]:
            if token in _OPERATORS:
                if segment:
                    out.extend(_expand(segment, depth))
                segment = []
            else:
                segment.append(token)
    return out


def _find_delete_outside_home(words: list[str]) -> str | None:
    """``find <roots> … -delete`` / ``-exec rm`` is a recursive rm of ``<roots>``: the same NVH_HOME rule."""
    from nvh.integrations.wizard.system_settings import denied_reason

    if Path(words[0]).name != "find":
        return None
    deletes = "-delete" in words or any(
        word in ("-exec", "-execdir", "-ok", "-okdir") and index + 1 < len(words)
        and Path(words[index + 1]).name in ("rm", "unlink", "shred")
        for index, word in enumerate(words)
    )
    if not deletes:
        return None
    targets: list[str] = []
    for word in words[1:]:
        if word.startswith("-") or word in ("!", "(", ")"):
            break
        targets.append(word)
    reason = denied_reason(["rm", "-r", *(targets or ["."])])
    return reason.replace("recursive rm", "find -delete", 1) if reason else None


def _escalation(words: list[str]) -> str | None:
    head = Path(words[0]).name
    return ESCALATION_ERROR.format(word=head) if head in _ESCALATORS else None


def _denied(command: str) -> str | None:
    """Why ``command`` must not run, from both deny lists, or ``None``.

    :func:`check_command` on the raw string first (quoting can hide a pattern
    from a re-rendered form), the bridge's own raw patterns next, then for
    every simple command (:func:`simple_commands`): the host-hitting list
    :func:`nvh.integrations.wizard.system_settings.denied_reason` (its own
    patterns plus the recursive-``rm``/``NVH_HOME`` rule), the same rule for
    ``find -delete``, and the ``sudo`` / ``su`` / ``doas`` refusal. Enforced
    in code before any subprocess, on the card and on the confirmed path.
    """
    from nvh.integrations.wizard.system_settings import denied_reason

    try:
        check_command(command)
    except GuardrailError as exc:
        return _blocked_reason(exc)
    for pattern, reason in _BRIDGE_DENIED:
        if pattern.search(command):
            return f"BLOCKED: {reason}"
    for words in simple_commands(command):
        reason = denied_reason(words) or _find_delete_outside_home(words) or _escalation(words)
        if reason is not None:
            return reason
    return None


# ────────────────────────────────────────────────────────────────────────────
# Argument validation
# ────────────────────────────────────────────────────────────────────────────


def _shell_request(args: dict[str, Any]) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Validate ``shell``'s arguments; ``(error_result, request)``.

    ``request`` carries ``command``, the resolved ``cwd`` (inside the
    workspace), ``workspace``, ``timeout_s`` and ``isolation`` (the planner's
    pin — ``None`` when the call was never planned). Refusals are in-band:
    ``denied: True`` for a command or a working directory the deny lists /
    the workspace boundary refuse, plain ``ok: False`` for bad input. Nothing
    here touches the filesystem beyond resolving paths.
    """
    command = args.get("command")
    if not isinstance(command, str) or not command.strip():
        return {"ok": False, "applied": False, "error": "command required (string)"}, {}
    command = command.strip()

    timeout_raw = args.get("timeout_s")
    if timeout_raw is None:
        timeout_raw = SHELL_DEFAULT_TIMEOUT_S
    if not _whole_number(timeout_raw, 1, SHELL_MAX_TIMEOUT_S):
        return {
            "ok": False, "applied": False, "command": command,
            "error": f"timeout_s must be an integer between 1 and {SHELL_MAX_TIMEOUT_S}",
        }, {}
    timeout_s = int(timeout_raw)

    isolation = args.get("isolation")
    if isolation is not None and isolation not in ISOLATION_MODES:
        return {
            "ok": False, "applied": False, "command": command,
            "error": "isolation is pinned by the red card's dry run (docker, subprocess or '') and cannot be chosen by the caller",
        }, {}

    reason = _denied(command)
    if reason is not None:
        return {"ok": False, "denied": True, "applied": False, "command": command, "error": reason}, {}

    workspace = workspace_dir()
    cwd_raw = args.get("cwd")
    if cwd_raw is None or (isinstance(cwd_raw, str) and not cwd_raw.strip()):
        cwd = workspace
    elif not isinstance(cwd_raw, str):
        return {"ok": False, "applied": False, "command": command, "error": "cwd must be a string"}, {}
    else:
        cwd = (workspace / cwd_raw.strip().replace("\\", "/")).resolve()
        try:
            check_path(str(cwd), workspace)
        except GuardrailError as exc:
            return {
                "ok": False, "denied": True, "applied": False, "command": command,
                "error": f"{_blocked_reason(exc, 'BLOCKED: cwd outside the workspace')} — cwd must stay inside the workspace {workspace}",
            }, {}

    return None, {
        "command": command, "cwd": cwd, "workspace": workspace, "timeout_s": timeout_s, "isolation": isolation,
    }


async def _docker_available(executor: Any) -> bool:
    """The executor's own probe, bounded so a hung daemon cannot stall a card or a run.

    The executor caches the answer, so the ``run_shell`` that follows reuses
    it instead of probing again.
    """
    try:
        return bool(await asyncio.wait_for(executor._check_docker(), timeout=DOCKER_PROBE_TIMEOUT_S))
    except Exception as exc:
        logger.debug("docker probe failed: %s", exc)
        return False


def _isolation_line(docker: bool, require_docker: bool) -> tuple[str, str | None]:
    """``(isolation note, warning or None)`` for the mode a run will get."""
    if docker:
        return ISOLATION_DOCKER, None
    if require_docker:
        return ISOLATION_REFUSED, ISOLATION_REFUSED
    user = _current_user()
    return ISOLATION_SUBPROCESS.format(user=user), NOT_ISOLATED_WARNING.format(user=user)


# ────────────────────────────────────────────────────────────────────────────
# The subprocess fallback the bridge uses: stdin closed, secrets out of env
# ────────────────────────────────────────────────────────────────────────────

#: Environment variables a host-run command must not inherit, by name shape:
#: ``HIVE_API_KEY``, ``OPENAI_API_KEY``, ``GITHUB_TOKEN``, ``AWS_SECRET_ACCESS_KEY``,
#: ``HF_TOKEN``, anything ``*PASSWORD*`` / ``*CREDENTIAL*`` / ``*PRIVATE*``.
_SECRET_ENV_RE = re.compile(r"KEY|TOKEN|SECRET|PASSW|CREDENTIAL|PRIVATE", re.IGNORECASE)

#: Seam for tests; the real spawn otherwise.
_spawn_shell = asyncio.create_subprocess_shell


def scrubbed_environment(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """``environ`` (default ``os.environ``) without the variables whose names look like secrets."""
    source = os.environ if environ is None else environ
    return {key: value for key, value in source.items() if not _SECRET_ENV_RE.search(key)}


async def run_host_shell(command: str, *, cwd: str | None, timeout_s: int, max_output_bytes: int) -> ExecutionResult:
    """Run ``command`` through the system shell on this machine, the bridge's way.

    Same contract as ``SandboxExecutor._run_process`` (timeout, output cap,
    exceptions as a result), with two things the executor's fallback does not
    do: stdin is ``DEVNULL`` (nothing can prompt or read what an operator
    types into the server's terminal) and the environment is
    :func:`scrubbed_environment` (the server's API keys never reach the
    command). Docker mode passes no host environment at all, so both modes
    now keep the keys.
    """
    start = time.monotonic()
    try:
        proc = await _spawn_shell(
            command,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            env=scrubbed_environment(),
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            return ExecutionResult(
                stdout="", stderr="Execution timed out", exit_code=-1,
                execution_time_ms=int((time.monotonic() - start) * 1000),
                timed_out=True, error=f"Timed out after {timeout_s}s",
            )
        return ExecutionResult(
            stdout=stdout.decode(errors="replace")[:max_output_bytes],
            stderr=stderr.decode(errors="replace")[:max_output_bytes],
            exit_code=proc.returncode or 0,
            execution_time_ms=int((time.monotonic() - start) * 1000),
        )
    except Exception as exc:
        return ExecutionResult(
            stdout="", stderr=str(exc), exit_code=-1,
            execution_time_ms=int((time.monotonic() - start) * 1000), error=str(exc),
        )


class _WizardShellExecutor(SandboxExecutor):
    """The sandbox executor with :func:`run_host_shell` as its subprocess fallback.

    Docker mode, the probe, the fail-closed refusal and the isolation label
    are the base class's; only the host spawn differs.
    """

    async def _run_shell_subprocess(self, command: str, mount: Path | None) -> ExecutionResult:
        if mount is not None:
            return await run_host_shell(
                command, cwd=str(mount), timeout_s=self.config.timeout_seconds,
                max_output_bytes=self.config.max_output_bytes,
            )
        with tempfile.TemporaryDirectory() as tmpdir:
            return await run_host_shell(
                command, cwd=tmpdir, timeout_s=self.config.timeout_seconds,
                max_output_bytes=self.config.max_output_bytes,
            )


# ────────────────────────────────────────────────────────────────────────────
# shell — privileged: planner (dry run) and handler
# ────────────────────────────────────────────────────────────────────────────


async def _plan_shell(args: dict[str, Any]) -> dict[str, Any]:
    """The red card's dry run: the command, the isolation it will get, cwd, timeout. Runs nothing.

    ``pinned_arguments.isolation`` is what the registry folds into the
    approved arguments (and the token signs), so the handler enforces the
    very mode this card showed.
    """
    error, request = _shell_request(args)
    if error is not None:
        plan = {"ok": False, "error": error["error"], "commands": []}
        if error.get("denied"):
            plan["denied"] = True
        return plan

    config = SandboxConfig(mount_dir=request["cwd"], timeout_seconds=request["timeout_s"])
    docker = await _docker_available(_WizardShellExecutor(config))
    isolation, warning = _isolation_line(docker, config.require_docker)
    mode = "docker" if docker else ("" if config.require_docker else "subprocess")
    plan: dict[str, Any] = {
        "ok": True,
        "commands": [request["command"]],
        "sudo": False,
        "isolation": mode,
        "pinned_arguments": {"isolation": mode},
        "changes": (
            f"Runs `{request['command']}` in {request['cwd']} with a {request['timeout_s']} s timeout; "
            f"isolation: {isolation}."
        ),
        "undo": [],
        "notes": [
            f"Isolation: {isolation}",
            f"Working directory: {request['cwd']}",
            f"Timeout: {request['timeout_s']} s",
            ISOLATION_PINNED_NOTE,
            "Recorded in the vault under Decisions when it runs.",
        ],
    }
    if warning:
        plan["warning"] = warning
    return plan


async def _tool_shell(args: dict[str, Any]) -> dict[str, Any]:
    """Run one shell command in the workspace through the sandbox executor.

    Both deny lists first; then the approved isolation is re-checked against
    a fresh (bounded) Docker probe and the run is refused in band when it
    would not get the mode the card showed; then
    :meth:`SandboxExecutor.run_shell` with the workspace (or ``cwd``) as the
    mount dir, the requested timeout and ``require_docker`` forced for a
    Docker-approved run. The answer is the apply shape the registry audits:
    ``applied`` is True whenever something executed (the executor reports the
    isolation it actually used), False for a refusal.
    """
    error, request = _shell_request(args)
    if error is not None:
        return error
    command: str = request["command"]
    cwd: Path = request["cwd"]
    planned: str | None = request["isolation"]

    def refused(message: str, **extra: Any) -> dict[str, Any]:
        return {
            "ok": False, "refused": True, "applied": False, "command": command, "cwd": str(cwd),
            "isolation": "", "timed_out": False, "steps": [], "error": message, **extra,
        }

    if planned is None:
        return refused(UNPLANNED_ERROR)
    if planned == "":
        return refused(REQUIRED_ISOLATION_ERROR)

    config = SandboxConfig(mount_dir=cwd, timeout_seconds=request["timeout_s"])
    if planned == "docker":
        # Belt and braces: even if the probe below flips between the check and
        # the run, the executor itself may not fall back to the host.
        config.require_docker = True
    executor = _WizardShellExecutor(config)
    docker_now = await _docker_available(executor)
    if docker_now != (planned == "docker"):
        return refused(
            ISOLATION_CHANGED_ERROR.format(
                approved=ISOLATION_DOCKER if planned == "docker" else "host (no Docker)",
                now="available" if docker_now else "unavailable",
            ),
            isolation_changed=True, approved_isolation=planned,
        )

    try:
        cwd.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return {
            "ok": False, "applied": False, "command": command,
            "error": f"cwd could not be created: {type(exc).__name__}: {str(exc)[:200]}",
        }

    result = await executor.run_shell(command)

    if not result.isolation:
        # The fail-closed refusal (require_docker without Docker) or a spawn
        # that never happened: nothing ran, nothing to audit.
        return refused(result.error or result.stderr or "nothing was executed")

    stdout = _clean(result.stdout)
    stderr = _clean(result.stderr)
    ok = result.exit_code == 0 and not result.timed_out and not result.error
    out: dict[str, Any] = {
        "ok": ok,
        "applied": True,
        "partial": False,
        # The note's title and the card's one-liner; ``command`` below carries the
        # whole thing, so a long command is not written twice into the window.
        "summary": f"Shell command: {command}" if len(command) <= 120 else f"Shell command: {command[:120]}…",
        "command": command,
        "cwd": str(cwd),
        "steps": [{"command": command, "exit_code": result.exit_code, "stdout": stdout, "stderr": stderr}],
        "exit_code": result.exit_code,
        "isolation": result.isolation,
        "timed_out": bool(result.timed_out),
    }
    if result.timed_out:
        out["error"] = f"`{command}` timed out after {request['timeout_s']} s"
    elif result.error:
        out["error"] = redact_secrets(result.error)[:300]
    elif result.exit_code != 0:
        out["error"] = f"`{command}` exited {result.exit_code}"
    if result.isolation == "subprocess":
        out["note"] = (
            "ran as a plain subprocess — Docker unavailable, no network/memory/user isolation "
            "(stdin closed, API keys and tokens removed from its environment)"
        )
    return out


# ────────────────────────────────────────────────────────────────────────────
# run_code — confirm, Docker required
# ────────────────────────────────────────────────────────────────────────────


async def _tool_run_code(args: dict[str, Any]) -> dict[str, Any]:
    """Execute a snippet in the Docker sandbox; refuse in-band without Docker.

    ``check_command`` on ``code`` first; then ``SandboxConfig(require_docker=True)``
    so the executor itself can never fall back to a subprocess, with the
    Docker probe answered up front so the refusal can name what to do.
    Output is redacted, then cut, then fitted to the tool window.
    """
    from nvh.integrations.wizard.tools import fit_tool_window

    code = args.get("code")
    if not isinstance(code, str) or not code.strip():
        return {"ok": False, "error": "code required (string)"}
    language = args.get("language", "python")
    if language is None or (isinstance(language, str) and not language.strip()):
        language = "python"
    if not isinstance(language, str) or language.strip().lower() not in RUN_CODE_LANGUAGES:
        return {"ok": False, "error": f"language must be one of {', '.join(RUN_CODE_LANGUAGES)}", "language": str(language)}
    language = language.strip().lower()

    try:
        check_command(code)
    except GuardrailError as exc:
        return {"ok": False, "denied": True, "error": _blocked_reason(exc), "language": language}

    executor = SandboxExecutor(SandboxConfig(require_docker=True, timeout_seconds=RUN_CODE_TIMEOUT_S))
    if not await _docker_available(executor):
        return {"ok": False, "refused": True, "error": RUN_CODE_NEEDS_DOCKER_ERROR, "language": language, "isolation": ""}

    result = await executor.execute(code=code, language=language)
    if not result.isolation:
        # The executor's own fail-closed answer (the probe flipped between the
        # check and the run): still a refusal, still nothing executed.
        return {
            "ok": False, "refused": True, "language": language, "isolation": "",
            "error": result.error or result.stderr or RUN_CODE_NEEDS_DOCKER_ERROR,
        }

    out: dict[str, Any] = {
        "ok": result.exit_code == 0 and not result.timed_out and not result.error,
        "stdout": _clean(result.stdout),
        "stderr": _clean(result.stderr),
        "exit_code": result.exit_code,
        "isolation": result.isolation,
        "timed_out": bool(result.timed_out),
        "language": language,
    }
    if result.timed_out:
        out["error"] = f"timed out after {RUN_CODE_TIMEOUT_S} s"
    elif result.error:
        out["error"] = redact_secrets(result.error)[:300]
    elif result.exit_code != 0:
        out["error"] = f"exited {result.exit_code}"
    return fit_tool_window(out)


# ────────────────────────────────────────────────────────────────────────────
# Registration
# ────────────────────────────────────────────────────────────────────────────


def register_wizard_tools(reg: Any) -> None:
    """Register ``shell`` (privileged, with planner) and ``run_code`` (confirm)."""
    from nvh.integrations.wizard.tools import WizardTool, parameters_from_json_schema

    shell_parameters = parameters_from_json_schema(_core_schema("shell"), _PARAMETER_DESCRIPTIONS["shell"])
    shell_parameters.update(_SHELL_EXTRA_PARAMETERS)
    reg.register(WizardTool(
        name="shell",
        description=(
            "Run one shell command in the agent workspace (NVH_PROJECTS, default $NVH_HOME/projects). "
            "The user approves the exact command on a red card that also says how it will run: in a Docker "
            "sandbox (no network, read-only image, workspace mounted at /workspace) when Docker is available, "
            "otherwise directly on this machine with the user's permissions (stdin closed, API keys and tokens "
            "stripped from its environment); the approval is bound to that isolation. sudo/su/doas are refused — "
            "use apt_install, service_enable or system_settings_apply for root. Well-known destructive shapes "
            "(recursive rm or find -delete outside NVH_HOME, shutdown, driver removal, firewall or account "
            "changes, piping into a shell) are refused before anything runs, inside `sh -c` strings and "
            "&&/;/| chains too — a backstop, not a sandbox: the user reading the card is the gate. Every run "
            "is recorded in the vault under Decisions."
        ),
        safety_class="privileged",
        parameters=shell_parameters,
        handler=_tool_shell,
        planner=_plan_shell,
        summary_template="Run shell command: {command}",
    ))

    reg.register(WizardTool(
        name="run_code",
        description=(
            "Execute a code snippet (python, javascript or bash) in the Docker sandbox: no network, read-only "
            "image, memory and time limits. Docker is REQUIRED — without it the call is refused and nothing runs "
            "(install Docker with the open-webui or vllm playbook, or run the snippet in a terminal). The user "
            "confirms before it runs."
        ),
        safety_class="confirm",
        parameters=parameters_from_json_schema(_core_schema("run_code"), _PARAMETER_DESCRIPTIONS["run_code"]),
        handler=_tool_run_code,
        summary_template="Run a code snippet in the Docker sandbox.",
    ))
