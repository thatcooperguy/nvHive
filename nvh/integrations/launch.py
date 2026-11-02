"""``nvh launch <tool>``: start a coding tool against the local nvHive API.

The targets are data, ``nvh/data/launch_targets.json`` read through
``nvh.data``. Each row names a tool, how nvHive hands it the API — ``env``
variables in the child's environment, a ``snippet`` to paste into the tool's
own config, or ``mcp`` registration of nvHive's MCP server — the template text
with ``{base}`` and ``{key}`` placeholders, a ``docs_url`` and ``verified_on``:
``null`` until a maintainer records a run against the tool's current release,
and every ``null`` prints as "unverified". Targets are external facts the repo
cannot verify, so the column is honest rather than hopeful.

Rules this module keeps:

- Only ``{base}`` (the API base URL) and ``{key}`` are ever substituted. The
  key is ``HIVE_API_KEY`` from the environment when set, else the documented
  placeholder ``nvhive``; no provider secret is read or rendered, and a
  rendered environment never carries a variable the row did not name.
- Variables reach the tool through its child environment only (``shell=False``);
  nvHive writes no third-party config file — snippet kinds print the text.
  No shipped row is kind ``env`` yet: ``claude`` is an ``mcp`` registration
  (inference stays with Anthropic, so its env is empty) and the other four are
  snippets, so today nothing is launched with an nvHive variable set.
- Before a launch the API's own ``GET /v1/health`` facts decide: open mode
  bound off-loopback, a keyed API (``key_required``) with no ``HIVE_API_KEY``
  here to forward (the placeholder would only earn a 401), or ``users`` mode
  with no token, is refused client-side, mirroring the server's
  ``_privileged_network_refusal`` (``bind_host: null`` counts as not-loopback,
  as the health docstring says).
- ``--start-api`` starts ``nvh serve`` only on a free port. A port held by
  something that does not answer nvHive's health check is refused, never
  killed — ``nvh services restart`` is the tool for a stale nvHive API.
- The launched tool owns Ctrl-C: the parent ignores SIGINT while the child runs
  and returns the child's own exit code (``subprocess.call`` would kill it on
  the first interrupt). The command is resolved with ``shutil.which`` so an npm
  shim (``claude.cmd`` on Windows) is found the way the shell finds it.
- ``--print`` renders offline: no probe, no refusal, nothing launched — and it
  shows the live ``HIVE_API_KEY`` value when one is set, by design.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import httpx

from nvh.data import read_json
from nvh.integrations.diagnostics.detector import register_claude_code

KINDS = frozenset({"env", "snippet", "mcp"})
REQUIRED_KEYS = frozenset(
    {"name", "display_name", "kind", "env", "snippet", "docs_url", "verified_on", "notes"}
)
PLACEHOLDER_KEY = "nvhive"
DEFAULT_PORT = 8000
HEALTH_TIMEOUT_S = 2.0
UNVERIFIED = "unverified"
# Mirror of nvh.api.server._LOOPBACK_HOSTS: the server's own loopback test.
LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})

EXIT_OK = 0
EXIT_UNKNOWN_TARGET = 1
EXIT_REFUSED = 2
EXIT_TOOL_MISSING = 127


class UnknownTargetError(LookupError):
    """``nvh launch <name>`` named a tool the bundled table does not list."""

    def __init__(self, name: str, names: Sequence[str]) -> None:
        self.name = name
        self.names = list(names)
        super().__init__(f"unknown launch target {name!r}; targets: {', '.join(self.names)}")

    def __str__(self) -> str:
        return self.args[0]


@dataclass(frozen=True)
class LaunchTarget:
    """One row of ``launch_targets.json``; ``command`` is the argv for env/mcp kinds."""

    name: str
    display_name: str
    kind: str
    env: dict[str, str]
    snippet: str
    docs_url: str
    verified_on: str | None
    notes: tuple[str, ...] = ()
    command: tuple[str, ...] = ()
    register: str | None = None
    inference_proxy: str | None = None

    @property
    def launches(self) -> bool:
        """Whether ``launch`` runs a process (env and mcp kinds) or only prints (snippet)."""
        return self.kind in ("env", "mcp")


# ---------------------------------------------------------------------------
# Targets
# ---------------------------------------------------------------------------


def _target_from_row(row: Any) -> LaunchTarget:
    if not isinstance(row, dict):
        raise ValueError(f"launch target rows must be objects, got {type(row).__name__}")
    missing = sorted(REQUIRED_KEYS - set(row))
    if missing:
        raise ValueError(f"launch target {row.get('name')!r} lacks {missing}")
    name = row["name"]
    kind = row["kind"]
    if kind not in KINDS:
        raise ValueError(f"launch target {name!r}: kind {kind!r} is not one of {sorted(KINDS)}")
    env = row["env"]
    if not isinstance(env, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in env.items()
    ):
        raise ValueError(f"launch target {name!r}: env must map variable names to templates")
    command = tuple(str(part) for part in (row.get("command") or ()))
    if kind != "snippet" and not command:
        raise ValueError(f"launch target {name!r} is kind {kind!r} but names no command")
    verified_on = row["verified_on"]
    if verified_on is not None and not isinstance(verified_on, str):
        raise ValueError(f"launch target {name!r}: verified_on must be a date string or null")
    return LaunchTarget(
        name=str(name),
        display_name=str(row["display_name"]),
        kind=kind,
        env=dict(env),
        snippet=str(row["snippet"] or ""),
        docs_url=str(row["docs_url"] or ""),
        verified_on=verified_on,
        notes=tuple(str(note) for note in (row["notes"] or ())),
        command=command,
        register=row.get("register"),
        inference_proxy=row.get("inference_proxy"),
    )


def load_targets() -> list[LaunchTarget]:
    """Every target in the bundled table, in file order (a malformed table is a ValueError)."""
    payload = read_json("data", "launch_targets.json")
    rows = payload.get("targets") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise ValueError('nvh/data/launch_targets.json: expected {"targets": [...]}')
    targets = [_target_from_row(row) for row in rows]
    names = [target.name for target in targets]
    duplicates = sorted({n for n in names if names.count(n) > 1})
    if duplicates:
        raise ValueError(f"launch targets named twice: {duplicates}")
    return targets


def target_names(targets: Sequence[LaunchTarget] | None = None) -> list[str]:
    return [target.name for target in (load_targets() if targets is None else targets)]


def get_target(name: str, targets: Sequence[LaunchTarget] | None = None) -> LaunchTarget:
    """The target called ``name`` (case-insensitive) or :class:`UnknownTargetError`."""
    rows = load_targets() if targets is None else list(targets)
    wanted = name.strip().lower()
    for target in rows:
        if target.name == wanted:
            return target
    raise UnknownTargetError(name, [target.name for target in rows])


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def api_base(port: int = DEFAULT_PORT) -> str:
    """The documented standard-path base URL for the local API."""
    return f"http://127.0.0.1:{port}/v1"


def api_key_for_launch(environ: Mapping[str, str] | None = None) -> str:
    """``HIVE_API_KEY`` when set, else the documented placeholder — never any other secret."""
    env = os.environ if environ is None else environ
    return env.get("HIVE_API_KEY") or PLACEHOLDER_KEY


def render_template(template: str, *, api_base: str, api_key: str) -> str:
    """Substitute ``{base}`` and ``{key}`` and nothing else.

    ``str.replace`` rather than ``str.format``: the snippets are TOML, JSON and
    YAML with braces of their own, and no other placeholder may ever expand.
    """
    return template.replace("{base}", api_base).replace("{key}", api_key)


def render(target: LaunchTarget, *, api_base: str, api_key: str) -> dict[str, Any]:
    """``{"env": {...}, "snippet": str}`` with the templates filled in.

    The ``env`` dict holds exactly the variables the row names, so a tool is
    never handed a variable (or a secret) the table did not ask for.
    """
    env = {
        name: render_template(value, api_base=api_base, api_key=api_key)
        for name, value in target.env.items()
    }
    snippet = render_template(target.snippet, api_base=api_base, api_key=api_key)
    return {"env": env, "snippet": snippet}


def render_inference_proxy(target: LaunchTarget, *, api_base: str, api_key: str) -> str | None:
    """The row's ``--inference-proxy`` snippet (claude: the Anthropic variables), or None."""
    if target.inference_proxy is None:
        return None
    return render_template(target.inference_proxy, api_base=api_base, api_key=api_key)


def describe(target: LaunchTarget, *, api_base: str, api_key: str) -> list[str]:
    """The lines ``--print`` shows: ``VAR=value`` for each env entry, then the snippet."""
    rendered = render(target, api_base=api_base, api_key=api_key)
    lines = [f"{name}={value}" for name, value in rendered["env"].items()]
    if rendered["snippet"]:
        if lines:
            lines.append("")
        lines.extend(rendered["snippet"].splitlines())
    return lines


def verification_label(target: LaunchTarget) -> str:
    """The table cell: the recorded date or ``unverified``."""
    return target.verified_on or UNVERIFIED


def verification_line(target: LaunchTarget) -> str:
    if target.verified_on is None:
        return (
            f"{UNVERIFIED} against this tool's current version — "
            "`--print` shows exactly what nvHive sets"
        )
    return f"verified against {target.display_name} on {target.verified_on}"


# ---------------------------------------------------------------------------
# Health, refusal, API lifecycle
# ---------------------------------------------------------------------------


def _http_get_json(url: str, timeout: float) -> Any:
    # trust_env=False: a loopback probe never goes through HTTP(S)_PROXY or the
    # system proxy, so it agrees with services.api_healthy (urllib bypasses local
    # addresses from the registry/env; httpx would not).
    response = httpx.get(url, timeout=timeout, trust_env=False)
    response.raise_for_status()
    return response.json()


def health_for_launch(
    port: int = DEFAULT_PORT, timeout: float = HEALTH_TIMEOUT_S
) -> dict[str, Any] | None:
    """The ``data`` object of ``GET /v1/health`` on the loopback API, or None when unreadable."""
    url = f"http://127.0.0.1:{port}/v1/health"
    try:
        body = _http_get_json(url, timeout)
    except (httpx.HTTPError, ValueError):
        return None
    if not isinstance(body, dict) or body.get("status") != "success":
        return None
    data = body.get("data")
    return data if isinstance(data, dict) else None


def bare_host(value: str) -> str:
    """Mirror of ``nvh.api.server._bare_host``: no port, no IPv6 brackets, lower-cased."""
    host = value.strip().lower()
    if host.startswith("["):
        host = host[1 : host.index("]")] if "]" in host else host[1:]
    elif host.count(":") == 1:
        host = host.rsplit(":", 1)[0]
    return host


def is_loopback(bind_host: str | None) -> bool:
    """Whether the API vouches for a loopback bind; unknown (``None``) is not loopback."""
    if not bind_host:
        return False
    return bare_host(str(bind_host)) in LOOPBACK_HOSTS


def refuse_if_unsafe(health: dict[str, Any] | None, *, has_token: bool) -> str | None:
    """A refusal message, or None when the API may be handed to a tool.

    Open mode (``key_required`` false — ``auth_mode: users`` without the key is
    still open, as SECURITY.md says) bound anywhere but loopback is refused;
    ``auth_mode: users`` with no ``HIVE_API_KEY`` here to forward is refused
    with the user-token hint; a keyed API (``key_required`` true) with no
    ``HIVE_API_KEY`` here is refused too, since the only credential the launch
    could render is the placeholder, which that API rejects with 401. A missing
    or unreadable health payload refuses as well: the client cannot vouch for
    what it cannot read.
    """
    if health is None:
        return (
            "refusing: could not read GET /v1/health from the API; start it with "
            "`nvh serve` (or pass --start-api) and retry"
        )
    bind_host = health.get("bind_host")
    auth_mode = health.get("auth_mode")
    key_required = health.get("key_required")
    open_mode = (not key_required) if isinstance(key_required, bool) else auth_mode == "open"
    if open_mode and not is_loopback(bind_host):
        where = (
            f"bound to {bind_host}"
            if bind_host
            else "bound to an interface it cannot vouch for (not started by `nvh serve`)"
        )
        return (
            f"refusing: the API is in open mode (no HIVE_API_KEY) and {where}. "
            "Set HIVE_API_KEY before `nvh serve`, or bind it to 127.0.0.1, then retry."
        )
    if auth_mode == "users" and not has_token:
        return (
            "refusing: the API checks per-user tokens first (auth_mode users) and HIVE_API_KEY "
            "is unset here, so the tool would be sent without a credential. Put a user token "
            "(`nvh auth create-token`) or the API key in HIVE_API_KEY and retry."
        )
    if not open_mode and not has_token:
        return (
            f"refusing: the API requires a key (auth_mode {auth_mode or 'key'}) and HIVE_API_KEY is "
            f"unset here, so the tool would be handed the placeholder `{PLACEHOLDER_KEY}`, which that "
            "API answers with 401. Export the same HIVE_API_KEY the server was started with and retry."
        )
    return None


def ensure_api_running(port: int = DEFAULT_PORT, *, start: bool) -> tuple[bool, str]:
    """``(ok, reason)``: a healthy API on ``port``, started on request.

    Reuses ``nvh.cli.services``: ``api_healthy`` for the probe and ``start_api``
    (``nvh serve --port <port>``, loopback, waits for health) when ``start`` is
    set and nothing listens on the port. Without ``start`` an absent API is a
    refusal that names ``--start-api``. A port that is listening but does not
    answer the health check is refused rather than handed to ``start_api``,
    whose stale-instance recovery frees the port by killing whatever holds it
    — the right move behind ``nvh webui``, never for a flag whose help says
    "start".
    """
    from nvh.cli import services

    ok, reason = services.api_healthy(port)
    if ok:
        return True, f"API healthy on port {port} ({reason})"
    if not start:
        return False, (
            f"no healthy nvHive API on port {port} ({reason}); start one with "
            f"`nvh serve --port {port}`, or pass --start-api (-y) to let nvh launch start it"
        )
    if services.port_listening(port):
        return False, (
            f"port {port} is held by something that does not answer nvHive's GET /v1/health "
            f"({reason}); nvh launch will not stop it. Pick another --port, or if that is an nvHive "
            f"API that is still starting or stale, wait or run `nvh services restart`, then retry."
        )
    ok, reason = services.start_api(port)
    if ok:
        return True, f"API on port {port}: {reason}"
    return False, f"could not start the API on port {port}: {reason}"


# ---------------------------------------------------------------------------
# Launch
# ---------------------------------------------------------------------------


def _registrar(name: str) -> Callable[[], tuple[bool, str]]:
    # Looked up at call time so a test can patch register_claude_code on this module.
    registrars: dict[str, Callable[[], tuple[bool, str]]] = {"claude_code": register_claude_code}
    try:
        return registrars[name]
    except KeyError:
        raise ValueError(f"launch target names unknown registrar {name!r}") from None


def _run_tool(argv: Sequence[str], *, env: Mapping[str, str]) -> int:
    """Run the tool and return ITS exit code, leaving Ctrl-C to the tool.

    ``subprocess.call`` catches the KeyboardInterrupt a shared console Ctrl-C
    raises in the parent, kills the child after 0.25 s and re-raises — a single
    Ctrl-C inside Claude Code (which uses it to interrupt a response and exits
    on a double press) would take the whole session down. So: spawn with
    ``Popen``, then ignore SIGINT in this process (after the spawn, so the child
    does not inherit the ignore) and keep waiting until the child exits by
    itself. The command is resolved with ``shutil.which``, which honours
    ``PATHEXT`` — ``CreateProcess`` alone never finds the ``claude.cmd`` shim an
    npm install leaves on Windows, although ``claude`` works in the shell.
    """
    resolved = shutil.which(argv[0]) or argv[0]
    process = subprocess.Popen([resolved, *argv[1:]], env=dict(env))
    try:
        previous: Any = signal.signal(signal.SIGINT, signal.SIG_IGN)
    except ValueError:  # not the main thread: the interrupt cannot reach us anyway
        previous = None
    try:
        while True:
            try:
                return int(process.wait())
            except KeyboardInterrupt:
                continue  # the tool handles its own Ctrl-C; keep waiting for its exit
    finally:
        if previous is not None:
            signal.signal(signal.SIGINT, previous)


def launch(
    target: LaunchTarget,
    *,
    api_base: str,
    api_key: str,
    extra_args: Sequence[str] = (),
    dry_run: bool = False,
    emit: Callable[[str], None] = print,
) -> int:
    """Launch ``target`` and return the exit code ``nvh launch`` should use.

    Snippet kinds print the rendered snippet (nvHive writes no third-party
    config) and return 0. An ``mcp`` kind runs its registrar first (claude:
    ``detector.register_claude_code``), a failure there is reported and the
    launch goes ahead. Env and mcp kinds then run the row's command plus
    ``extra_args`` through :func:`_run_tool` with the rendered variables merged
    over ``os.environ`` — never through a shell — and return the tool's own
    exit code, or 127 when the tool is not on PATH. ``dry_run`` prints the
    argv and the variables instead of running anything.
    """
    rendered = render(target, api_base=api_base, api_key=api_key)
    if not target.launches:
        for line in describe(target, api_base=api_base, api_key=api_key):
            emit(line)
        return EXIT_OK
    if target.register is not None:
        ok, message = _registrar(target.register)()
        emit(("registered: " if ok else "registration did not complete: ") + message)
    argv = [*target.command, *extra_args]
    env = {**os.environ, **rendered["env"]}
    if dry_run:
        emit("would run: " + " ".join(argv))
        for name, value in rendered["env"].items():
            emit(f"  {name}={value}")
        return EXIT_OK
    try:
        return _run_tool(argv, env=env)
    except FileNotFoundError:
        emit(
            f"{target.command[0]} is not on PATH; install {target.display_name} "
            f"(see {target.docs_url}) and retry"
        )
        return EXIT_TOOL_MISSING
