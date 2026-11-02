"""`nvh launch <tool>` (0.46): bundled targets, rendering, refusal and launch.

Hermetic: nothing here reaches the network or a child process. The tool runner
(``launch._run_tool``), the health probe, ``nvh.cli.services`` and the Claude
registrar are patched on the modules that bind them, and every environment change
goes through ``monkeypatch`` so CI's single-process run sees nothing left behind.
"""

from __future__ import annotations

import os
from types import SimpleNamespace

import httpx
import pytest
from typer.testing import CliRunner

import nvh.cli.main as cli_main
import nvh.data
from nvh.cli import services
from nvh.integrations import launch as launch_mod

SHIPPED = ("claude", "codex", "opencode", "continue", "cursor")
BASE = "http://127.0.0.1:8000/v1"
LOOPBACK_OPEN = {"bind_host": "127.0.0.1", "auth_mode": "open", "key_required": False}
# Secrets a launch must never forward, whatever the environment holds.
PROVIDER_SECRETS = {
    "OPENAI_API_KEY": "sk-openai-must-not-leak",
    "ANTHROPIC_API_KEY": "sk-ant-must-not-leak",
    "GROQ_API_KEY": "gsk-must-not-leak",
    "NVAPI_KEY": "nvapi-must-not-leak",
}


def _fake_env_target(**overrides) -> launch_mod.LaunchTarget:
    """A kind-``env`` row: no shipped row uses the kind yet, so the code path gets a fake."""
    fields = dict(
        name="fake-tool",
        display_name="Fake Tool",
        kind="env",
        env={"OPENAI_BASE_URL": "{base}", "OPENAI_API_KEY": "{key}"},
        snippet="",
        docs_url="https://example.invalid/fake",
        verified_on=None,
        notes=(),
        command=("fake-tool",),
    )
    fields.update(overrides)
    return launch_mod.LaunchTarget(**fields)


@pytest.fixture()
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture()
def offline(monkeypatch):
    """Any probe, spawn or launch is a test failure unless the test patches it itself."""
    def _boom(*_args, **_kwargs):
        raise AssertionError("nvh launch reached a side effect the test did not allow")

    monkeypatch.setattr(launch_mod, "_run_tool", _boom)
    monkeypatch.setattr(launch_mod, "_http_get_json", _boom)
    monkeypatch.setattr(services, "api_healthy", _boom)
    monkeypatch.setattr(services, "port_listening", _boom)
    monkeypatch.setattr(services, "start_api", _boom)
    monkeypatch.setattr(launch_mod, "register_claude_code", _boom)
    monkeypatch.delenv("HIVE_API_KEY", raising=False)


@pytest.fixture()
def spawn(monkeypatch):
    """Records ``launch._run_tool`` instead of running it; ``spawn.code`` is what it returns."""
    class Recorder:
        def __init__(self) -> None:
            self.calls: list[tuple[list[str], dict]] = []
            self.code = 0

        def __call__(self, argv, **kwargs):
            self.calls.append((list(argv), kwargs))
            return self.code

    recorder = Recorder()
    monkeypatch.setattr(launch_mod, "_run_tool", recorder)
    return recorder


# ---------------------------------------------------------------------------
# The bundled table
# ---------------------------------------------------------------------------


def test_every_shipped_row_has_the_required_keys_and_verified_on() -> None:
    payload = nvh.data.read_json("data", "launch_targets.json")
    assert isinstance(payload, dict)
    rows = payload["targets"]
    assert [row["name"] for row in rows] == list(SHIPPED)
    for row in rows:
        missing = launch_mod.REQUIRED_KEYS - set(row)
        assert not missing, (row["name"], missing)
        assert "verified_on" in row, row["name"]
        assert row["verified_on"] is None or isinstance(row["verified_on"], str)
        assert row["kind"] in launch_mod.KINDS, row["name"]
        assert isinstance(row["env"], dict) and isinstance(row["notes"], list)
        assert row["docs_url"].startswith("https://"), row["name"]


def test_load_targets_parses_every_row_and_labels_its_verification() -> None:
    """Every row ships ``verified_on: null`` today, but INTEGRATIONS.md invites a maintainer to
    record a date after a real run — the labels follow the value, so a dated row stays green."""
    targets = launch_mod.load_targets()
    assert launch_mod.target_names(targets) == list(SHIPPED)
    for target in targets:
        assert target.verified_on is None or isinstance(target.verified_on, str)
        if target.verified_on is None:
            assert launch_mod.verification_label(target) == "unverified"
            assert launch_mod.verification_line(target).startswith("unverified against this tool")
            assert "`--print` shows exactly what nvHive sets" in launch_mod.verification_line(target)
        else:
            assert launch_mod.verification_label(target) == target.verified_on
            assert launch_mod.verification_line(target) == (
                f"verified against {target.display_name} on {target.verified_on}"
            )
        if target.launches:
            assert target.command, target.name


def test_a_recorded_verification_date_is_the_label_and_the_line() -> None:
    dated = _fake_env_target(verified_on="2026-11-15")
    assert launch_mod.verification_label(dated) == "2026-11-15"
    assert launch_mod.verification_line(dated) == "verified against Fake Tool on 2026-11-15"


def test_shipped_kinds_follow_the_design() -> None:
    by_name = {t.name: t for t in launch_mod.load_targets()}
    assert by_name["claude"].kind == "mcp"
    assert by_name["claude"].register == "claude_code"
    assert by_name["claude"].env == {}, "inference stays with Anthropic: no ANTHROPIC_* variable"
    assert by_name["claude"].inference_proxy is not None
    for name in ("codex", "opencode", "continue", "cursor"):
        assert by_name[name].kind == "snippet", name
        assert not by_name[name].launches
    assert 'wire_api = "chat"' in by_name["codex"].snippet
    assert "[model_providers.nvhive]" in by_name["codex"].snippet
    assert "baseURL" in by_name["opencode"].snippet
    assert "apiBase" in by_name["continue"].snippet
    assert "Override OpenAI Base URL" in by_name["cursor"].snippet


def test_get_target_is_case_insensitive_and_unknown_lists_the_names() -> None:
    assert launch_mod.get_target("Codex").name == "codex"
    with pytest.raises(launch_mod.UnknownTargetError) as excinfo:
        launch_mod.get_target("nope")
    message = str(excinfo.value)
    assert "nope" in message
    for name in SHIPPED:
        assert name in message


def test_a_malformed_row_is_a_value_error() -> None:
    with pytest.raises(ValueError, match="lacks"):
        launch_mod._target_from_row({"name": "x", "kind": "env"})
    with pytest.raises(ValueError, match="kind"):
        launch_mod._target_from_row({
            "name": "x", "display_name": "X", "kind": "magic", "env": {}, "snippet": "",
            "docs_url": "", "verified_on": None, "notes": [],
        })
    with pytest.raises(ValueError, match="names no command"):
        launch_mod._target_from_row({
            "name": "x", "display_name": "X", "kind": "env", "env": {}, "snippet": "",
            "docs_url": "", "verified_on": None, "notes": [],
        })


# ---------------------------------------------------------------------------
# Rendering: only {base} and {key}, only the row's own variables, never a secret
# ---------------------------------------------------------------------------


def test_rendered_env_contains_only_the_rows_own_variables(monkeypatch) -> None:
    for name, value in PROVIDER_SECRETS.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("HIVE_API_KEY", raising=False)
    key = launch_mod.api_key_for_launch()
    for target in [*launch_mod.load_targets(), _fake_env_target()]:
        rendered = launch_mod.render(target, api_base=BASE, api_key=key)
        assert set(rendered["env"]) == set(target.env), target.name
        texts = [*rendered["env"].values(), rendered["snippet"]]
        proxy = launch_mod.render_inference_proxy(target, api_base=BASE, api_key=key)
        if proxy is not None:
            texts.append(proxy)
        for text in texts:
            for secret in PROVIDER_SECRETS.values():
                assert secret not in text, target.name
            assert "{base}" not in text and "{key}" not in text, target.name


def test_key_is_hive_api_key_when_set_else_the_placeholder(monkeypatch) -> None:
    monkeypatch.delenv("HIVE_API_KEY", raising=False)
    assert launch_mod.api_key_for_launch() == launch_mod.PLACEHOLDER_KEY == "nvhive"
    rendered = launch_mod.render(_fake_env_target(), api_base=BASE, api_key=launch_mod.api_key_for_launch())
    assert rendered["env"] == {"OPENAI_BASE_URL": BASE, "OPENAI_API_KEY": "nvhive"}

    monkeypatch.setenv("HIVE_API_KEY", "hive-secret")
    assert launch_mod.api_key_for_launch() == "hive-secret"
    rendered = launch_mod.render(_fake_env_target(), api_base=BASE, api_key=launch_mod.api_key_for_launch())
    assert rendered["env"]["OPENAI_API_KEY"] == "hive-secret"
    assert launch_mod.api_key_for_launch({"HIVE_API_KEY": ""}) == "nvhive"


def test_render_template_touches_nothing_but_base_and_key() -> None:
    out = launch_mod.render_template(
        '{base} {key} {"json": {other}} {}', api_base="B", api_key="K",
    )
    assert out == 'B K {"json": {other}} {}'


def test_api_base_follows_the_port() -> None:
    assert launch_mod.api_base() == BASE
    assert launch_mod.api_base(8123) == "http://127.0.0.1:8123/v1"


def test_describe_lists_env_lines_then_the_snippet() -> None:
    target = _fake_env_target(snippet="# paste me\nfoo = {base}")
    lines = launch_mod.describe(target, api_base=BASE, api_key="nvhive")
    assert lines == [f"OPENAI_BASE_URL={BASE}", "OPENAI_API_KEY=nvhive", "", "# paste me", f"foo = {BASE}"]


# ---------------------------------------------------------------------------
# Refusal mirrors the server
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("health, has_token, fragment", [
    ({"bind_host": "0.0.0.0", "auth_mode": "open", "key_required": False}, False, "open mode"),
    ({"bind_host": "0.0.0.0", "auth_mode": "open", "key_required": False}, True, "open mode"),
    ({"bind_host": None, "auth_mode": "open", "key_required": False}, False, "cannot vouch"),
    ({"bind_host": "192.168.1.5:8000", "auth_mode": "users", "key_required": False}, True, "open mode"),
    ({"bind_host": "127.0.0.1", "auth_mode": "users", "key_required": False}, False, "user token"),
    ({"bind_host": "127.0.0.1", "auth_mode": "users", "key_required": True}, False, "user token"),
    # A keyed API with no HIVE_API_KEY here: the placeholder would only earn a 401.
    ({"bind_host": "127.0.0.1", "auth_mode": "key", "key_required": True}, False, "401"),
    ({"bind_host": "0.0.0.0", "auth_mode": "key", "key_required": True}, False, "placeholder `nvhive`"),
    (None, True, "/v1/health"),
])
def test_refuse_if_unsafe_refuses(health, has_token, fragment) -> None:
    message = launch_mod.refuse_if_unsafe(health, has_token=has_token)
    assert message is not None and message.startswith("refusing")
    assert fragment in message


@pytest.mark.parametrize("health, has_token", [
    (LOOPBACK_OPEN, False),
    ({"bind_host": "localhost:8000", "auth_mode": "open", "key_required": False}, False),
    ({"bind_host": "[::1]:8000", "auth_mode": "open", "key_required": False}, False),
    ({"bind_host": "LOCALHOST", "auth_mode": "open", "key_required": False}, False),
    ({"bind_host": "0.0.0.0", "auth_mode": "key", "key_required": True}, True),
    ({"bind_host": "127.0.0.1", "auth_mode": "key", "key_required": True}, True),
    ({"bind_host": "127.0.0.1", "auth_mode": "users", "key_required": True}, True),
    ({"bind_host": "127.0.0.1", "auth_mode": "users", "key_required": False}, True),
])
def test_refuse_if_unsafe_allows(health, has_token) -> None:
    assert launch_mod.refuse_if_unsafe(health, has_token=has_token) is None


def test_loopback_test_mirrors_the_servers() -> None:
    from nvh.api import server as server_module

    assert launch_mod.LOOPBACK_HOSTS == server_module._LOOPBACK_HOSTS
    for value in ("127.0.0.1:8000", "[::1]:8000", "Localhost", "0.0.0.0", "[fe80::1]"):
        assert launch_mod.bare_host(value) == server_module._bare_host(value), value
    assert launch_mod.is_loopback(None) is False
    assert launch_mod.is_loopback("") is False


def test_health_for_launch_unwraps_the_envelope_or_returns_none(monkeypatch) -> None:
    seen: list[tuple[str, float]] = []

    def _ok(url: str, timeout: float):
        seen.append((url, timeout))
        return {"status": "success", "data": LOOPBACK_OPEN}

    monkeypatch.setattr(launch_mod, "_http_get_json", _ok)
    assert launch_mod.health_for_launch(8123) == LOOPBACK_OPEN
    assert seen == [("http://127.0.0.1:8123/v1/health", launch_mod.HEALTH_TIMEOUT_S)]

    def _down(url: str, timeout: float):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(launch_mod, "_http_get_json", _down)
    assert launch_mod.health_for_launch() is None
    monkeypatch.setattr(launch_mod, "_http_get_json", lambda url, timeout: {"status": "error"})
    assert launch_mod.health_for_launch() is None
    monkeypatch.setattr(launch_mod, "_http_get_json", lambda url, timeout: ["not", "a", "dict"])
    assert launch_mod.health_for_launch() is None


def test_health_probe_never_goes_through_a_proxy(monkeypatch) -> None:
    """``services.api_healthy`` (urllib) bypasses local addresses from the registry/env; httpx
    would route 127.0.0.1 through HTTP(S)_PROXY / the system proxy and the two probes would
    disagree ("API healthy" then "could not read /v1/health"). ``trust_env=False`` settles it."""
    seen: dict = {}

    class _Resp:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {"status": "success", "data": dict(LOOPBACK_OPEN)}

    def fake_get(url: str, **kwargs):
        seen.update(kwargs, url=url)
        return _Resp()

    monkeypatch.setattr(launch_mod.httpx, "get", fake_get)
    assert launch_mod.health_for_launch(8000) == LOOPBACK_OPEN
    assert seen["url"] == "http://127.0.0.1:8000/v1/health"
    assert seen["trust_env"] is False and seen["timeout"] == launch_mod.HEALTH_TIMEOUT_S


def test_ensure_api_running(monkeypatch) -> None:
    started: list[int] = []
    monkeypatch.setattr(services, "port_listening", lambda port, **kwargs: False)
    monkeypatch.setattr(services, "api_healthy", lambda port, timeout=2.0: (True, "engine_initialized"))
    monkeypatch.setattr(services, "start_api", lambda port, log_dir=None: started.append(port) or (True, "x"))
    assert launch_mod.ensure_api_running(8000, start=False) == (True, "API healthy on port 8000 (engine_initialized)")
    assert started == []

    monkeypatch.setattr(services, "api_healthy", lambda port, timeout=2.0: (False, "unreachable (refused)"))
    ok, reason = launch_mod.ensure_api_running(8000, start=False)
    assert ok is False
    assert "--start-api" in reason and "nvh serve --port 8000" in reason
    assert started == []

    monkeypatch.setattr(
        services, "start_api",
        lambda port, log_dir=None: started.append(port) or (True, "healthy after wait (engine_initialized)"),
    )
    ok, reason = launch_mod.ensure_api_running(8000, start=True)
    assert ok is True and "healthy after wait" in reason
    assert started == [8000]

    monkeypatch.setattr(services, "start_api", lambda port, log_dir=None: (False, "did not become healthy in 20s"))
    ok, reason = launch_mod.ensure_api_running(8000, start=True)
    assert ok is False and "did not become healthy" in reason


def test_ensure_api_running_never_frees_a_port_something_else_holds(monkeypatch) -> None:
    """``services.start_api`` recovers a stale nvHive instance by killing whatever listens on the
    port (``fuser -k``, ``lsof`` + ``kill``, the /proc walk) — right behind ``nvh webui``, never
    for ``nvh launch -y --port N`` when N is a dev server or a database. The port is refused."""
    touched: list = []
    monkeypatch.setattr(
        services, "api_healthy", lambda port, timeout=2.0: (False, "unreachable (HTTP Error 404: Not Found)"),
    )
    monkeypatch.setattr(services, "port_listening", lambda port, **kwargs: True)
    monkeypatch.setattr(services, "kill_stale_api", lambda port: touched.append(("kill", port)))
    monkeypatch.setattr(services, "start_api", lambda port, log_dir=None: touched.append(("start", port)) or (True, "x"))
    ok, reason = launch_mod.ensure_api_running(3000, start=True)
    assert ok is False and touched == []
    assert "port 3000 is held" in reason and "will not stop it" in reason
    assert "nvh services restart" in reason and "HTTP Error 404" in reason

    ok, reason = launch_mod.ensure_api_running(3000, start=False)
    assert ok is False and touched == [] and "--start-api" in reason


# ---------------------------------------------------------------------------
# launch(): the child environment, no shell, the tool's exit code
# ---------------------------------------------------------------------------


def test_launch_env_kind_merges_the_rendered_env_over_os_environ_without_a_shell(monkeypatch, spawn) -> None:
    monkeypatch.setenv("NVH_LAUNCH_TEST_KEEP", "kept")
    monkeypatch.setenv("OPENAI_API_KEY", PROVIDER_SECRETS["OPENAI_API_KEY"])
    monkeypatch.delenv("HIVE_API_KEY", raising=False)
    said: list[str] = []
    code = launch_mod.launch(
        _fake_env_target(), api_base=BASE, api_key="nvhive", extra_args=["--flag", "x"], emit=said.append,
    )
    assert code == 0
    assert len(spawn.calls) == 1
    argv, kwargs = spawn.calls[0]
    assert argv == ["fake-tool", "--flag", "x"]
    assert not kwargs.get("shell")
    env = kwargs["env"]
    assert env["OPENAI_BASE_URL"] == BASE
    assert env["OPENAI_API_KEY"] == "nvhive", "the row's variable wins over the inherited secret"
    assert env["NVH_LAUNCH_TEST_KEEP"] == "kept", "the rest of os.environ is inherited"
    assert said == []


def test_launch_returns_the_tools_exit_code_and_127_when_missing(monkeypatch, spawn) -> None:
    spawn.code = 3
    assert launch_mod.launch(_fake_env_target(), api_base=BASE, api_key="k", emit=lambda _s: None) == 3

    def _missing(argv, **kwargs):
        raise FileNotFoundError(argv[0])

    said: list[str] = []
    monkeypatch.setattr(launch_mod, "_run_tool", _missing)
    assert launch_mod.launch(_fake_env_target(), api_base=BASE, api_key="k", emit=said.append) == 127
    assert said and "fake-tool is not on PATH" in said[0]


def test_run_tool_waits_through_ctrl_c_and_returns_the_tools_own_exit_code(monkeypatch) -> None:
    """A console Ctrl-C reaches both processes; ``subprocess.call`` would kill the child after
    0.25 s and re-raise (Claude Code exits only on a double press). The runner spawns first,
    then ignores SIGINT in the parent (after the spawn, so the child does not inherit it), keeps
    waiting through the KeyboardInterrupt, returns the child's code and restores the handler."""
    events: list[str] = []

    class FakeProcess:
        def __init__(self) -> None:
            self.waits = 0

        def wait(self, timeout=None):
            self.waits += 1
            if self.waits == 1:
                raise KeyboardInterrupt
            return 7

        def kill(self) -> None:
            events.append("kill")

    spawned: list[tuple[list[str], dict]] = []

    def fake_popen(argv, **kwargs):
        spawned.append((list(argv), kwargs))
        events.append("spawn")
        return FakeProcess()

    handlers: list[tuple] = []

    def fake_signal(signum, handler):
        handlers.append((signum, handler))
        events.append("signal")
        return "previous-handler"

    monkeypatch.setattr(launch_mod.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(launch_mod.signal, "signal", fake_signal)
    monkeypatch.setattr(launch_mod.shutil, "which", lambda name: None)
    assert launch_mod._run_tool(["fake-tool", "--x"], env={"A": "1"}) == 7
    assert events == ["spawn", "signal", "signal"]
    assert "kill" not in events
    assert handlers[0] == (launch_mod.signal.SIGINT, launch_mod.signal.SIG_IGN)
    assert handlers[1] == (launch_mod.signal.SIGINT, "previous-handler")
    assert spawned == [(["fake-tool", "--x"], {"env": {"A": "1"}})]


def test_run_tool_resolves_the_command_the_way_the_shell_does(monkeypatch) -> None:
    """``shutil.which`` honours PATHEXT: an npm install leaves ``claude.cmd`` on Windows, which
    ``CreateProcess`` alone never finds although ``claude`` works in the user's shell."""
    spawned: list[list[str]] = []

    class Done:
        def wait(self, timeout=None) -> int:
            return 0

    monkeypatch.setattr(launch_mod.subprocess, "Popen", lambda argv, **kw: spawned.append(list(argv)) or Done())
    monkeypatch.setattr(launch_mod.signal, "signal", lambda *args: None)
    monkeypatch.setattr(launch_mod.shutil, "which", lambda name: r"C:\npm\claude.CMD" if name == "claude" else None)
    assert launch_mod._run_tool(["claude", "--resume"], env={}) == 0
    assert spawned == [[r"C:\npm\claude.CMD", "--resume"]]


def test_registrar_resolves_the_claude_shim_too(monkeypatch) -> None:
    from nvh.integrations.diagnostics import detector

    seen: list[list[str]] = []
    monkeypatch.setattr(detector.shutil, "which", lambda name: r"C:\npm\claude.CMD" if name == "claude" else None)
    monkeypatch.setattr(
        detector.subprocess, "run",
        lambda argv, **kw: seen.append(list(argv)) or SimpleNamespace(returncode=0, stderr=""),
    )
    assert detector.register_claude_code() == (True, "Registered nvhive MCP server with Claude Code")
    assert seen == [[r"C:\npm\claude.CMD", "mcp", "add", "nvhive", "--", "nvhive-mcp"]]


def test_launch_dry_run_prints_and_runs_nothing(offline) -> None:
    said: list[str] = []
    code = launch_mod.launch(
        _fake_env_target(), api_base=BASE, api_key="nvhive", extra_args=["--a"], dry_run=True, emit=said.append,
    )
    assert code == 0
    assert said[0] == "would run: fake-tool --a"
    assert f"  OPENAI_BASE_URL={BASE}" in said and "  OPENAI_API_KEY=nvhive" in said


def test_launch_snippet_kind_prints_the_snippet_and_spawns_nothing(offline) -> None:
    codex = launch_mod.get_target("codex")
    said: list[str] = []
    assert launch_mod.launch(codex, api_base=BASE, api_key="nvhive", emit=said.append) == 0
    text = "\n".join(said)
    assert 'base_url = "http://127.0.0.1:8000/v1"' in text
    assert 'wire_api = "chat"' in text
    assert "export HIVE_API_KEY=nvhive" in text


def test_launch_claude_registers_the_mcp_server_before_launching(monkeypatch, spawn) -> None:
    order: list[str] = []

    def _register():
        order.append("register")
        return True, "Registered nvhive MCP server with Claude Code"

    monkeypatch.setattr(launch_mod, "register_claude_code", _register)
    monkeypatch.setattr(launch_mod, "_run_tool", lambda argv, **kw: order.append("call") or spawn(argv, **kw))
    said: list[str] = []
    claude = launch_mod.get_target("claude")
    code = launch_mod.launch(claude, api_base=BASE, api_key="nvhive", extra_args=["--resume"], emit=said.append)
    assert code == 0
    assert order == ["register", "call"]
    argv, kwargs = spawn.calls[0]
    assert argv == ["claude", "--resume"]
    assert kwargs["env"] == dict(os.environ), "claude gets exactly the inherited environment: no ANTHROPIC_* is added"
    assert said == ["registered: Registered nvhive MCP server with Claude Code"]


def test_launch_claude_reports_a_failed_registration_and_still_launches(monkeypatch, spawn) -> None:
    monkeypatch.setattr(launch_mod, "register_claude_code", lambda: (False, "Registration failed: boom"))
    said: list[str] = []
    assert launch_mod.launch(launch_mod.get_target("claude"), api_base=BASE, api_key="k", emit=said.append) == 0
    assert said == ["registration did not complete: Registration failed: boom"]
    assert spawn.calls[0][0] == ["claude"]


# ---------------------------------------------------------------------------
# The CLI command
# ---------------------------------------------------------------------------


def test_cli_list_shows_the_five_targets_unverified(runner, offline) -> None:
    result = runner.invoke(cli_main.app, ["launch", "--list"])
    assert result.exit_code == 0, result.output
    for name in SHIPPED:
        assert name in result.output
    assert "unverified" in result.output
    assert "Claude Code" in result.output


def test_cli_without_a_target_shows_the_table_and_exits_1(runner, offline) -> None:
    result = runner.invoke(cli_main.app, ["launch"])
    assert result.exit_code == 1, result.output
    for name in SHIPPED:
        assert name in result.output
    assert "Name a target" in result.output


def test_cli_unknown_target_exits_1_and_lists_the_names(runner, offline) -> None:
    result = runner.invoke(cli_main.app, ["launch", "nope"])
    assert result.exit_code == 1, result.output
    assert "unknown launch target 'nope'" in result.output
    for name in SHIPPED:
        assert name in result.output


@pytest.mark.parametrize("name", SHIPPED)
def test_cli_print_renders_every_target_offline(runner, offline, name) -> None:
    result = runner.invoke(cli_main.app, ["launch", name, "--print"])
    assert result.exit_code == 0, result.output
    target = launch_mod.get_target(name)
    assert launch_mod.verification_line(target) in result.output
    if target.kind == "snippet":
        assert BASE in result.output
        assert "nvhive" in result.output
    else:
        assert "claude mcp add nvhive -- nvhive-mcp" in result.output
    assert "ANTHROPIC_BASE_URL" not in result.output
    assert "{base}" not in result.output and "{key}" not in result.output


def test_cli_print_honours_the_port_and_hive_api_key(runner, offline, monkeypatch) -> None:
    monkeypatch.setenv("HIVE_API_KEY", "hive-secret")
    result = runner.invoke(cli_main.app, ["launch", "continue", "--print", "--port", "8123"])
    assert result.exit_code == 0, result.output
    assert "apiBase: http://127.0.0.1:8123/v1" in result.output
    assert "apiKey: hive-secret" in result.output


def test_cli_inference_proxy_prints_the_anthropic_snippet_marked_unverified(runner, offline) -> None:
    result = runner.invoke(cli_main.app, ["launch", "claude", "--inference-proxy"])
    assert result.exit_code == 0, result.output
    assert "ANTHROPIC_BASE_URL=http://127.0.0.1:8000/v1/anthropic" in result.output
    assert "ANTHROPIC_AUTH_TOKEN=nvhive" in result.output
    assert "tool_use" in result.output
    assert launch_mod.verification_line(launch_mod.get_target("claude")) in result.output

    result = runner.invoke(cli_main.app, ["launch", "codex", "--inference-proxy"])
    assert result.exit_code == 1, result.output
    assert "no inference-proxy snippet" in result.output


def test_cli_refuses_open_mode_off_loopback_with_exit_2(runner, offline, monkeypatch) -> None:
    monkeypatch.setattr(services, "api_healthy", lambda port, timeout=2.0: (True, "engine_initialized"))
    monkeypatch.setattr(
        launch_mod, "health_for_launch",
        lambda port=8000, timeout=2.0: {"bind_host": "0.0.0.0", "auth_mode": "open", "key_required": False},
    )
    result = runner.invoke(cli_main.app, ["launch", "codex"])
    assert result.exit_code == 2, result.output
    assert "refusing" in result.output and "open mode" in result.output
    assert "Set HIVE_API_KEY" in result.output


def test_cli_refuses_users_mode_without_a_token_with_exit_2(runner, offline, monkeypatch) -> None:
    monkeypatch.setattr(services, "api_healthy", lambda port, timeout=2.0: (True, "engine_initialized"))
    monkeypatch.setattr(
        launch_mod, "health_for_launch",
        lambda port=8000, timeout=2.0: {"bind_host": "127.0.0.1", "auth_mode": "users", "key_required": False},
    )
    result = runner.invoke(cli_main.app, ["launch", "claude"])
    assert result.exit_code == 2, result.output
    assert "user token" in result.output and "nvh auth create-token" in result.output


def test_cli_refuses_a_keyed_api_without_hive_api_key_with_exit_2(runner, offline, monkeypatch) -> None:
    """Server started with HIVE_API_KEY, a fresh shell without it: the only credential the launch
    could render is the placeholder, which that API answers with 401 — so refuse, never print
    `export HIVE_API_KEY=nvhive` after a green health line."""
    monkeypatch.setattr(services, "api_healthy", lambda port, timeout=2.0: (True, "engine_initialized"))
    monkeypatch.setattr(
        launch_mod, "health_for_launch",
        lambda port=8000, timeout=2.0: {"bind_host": "127.0.0.1", "auth_mode": "key", "key_required": True},
    )
    result = runner.invoke(cli_main.app, ["launch", "codex"])
    assert result.exit_code == 2, result.output
    assert "refusing" in result.output and "401" in result.output and "HIVE_API_KEY" in result.output
    assert "export HIVE_API_KEY=nvhive" not in result.output

    monkeypatch.setenv("HIVE_API_KEY", "hive-secret")
    result = runner.invoke(cli_main.app, ["launch", "codex"])
    assert result.exit_code == 0, result.output
    assert "export HIVE_API_KEY=hive-secret" in result.output


def test_cli_refuses_without_an_api_unless_start_api_is_passed(runner, offline, monkeypatch) -> None:
    monkeypatch.setattr(services, "api_healthy", lambda port, timeout=2.0: (False, "unreachable (refused)"))
    result = runner.invoke(cli_main.app, ["launch", "codex"])
    assert result.exit_code == 2, result.output
    assert "--start-api" in result.output and "nvh serve --port 8000" in result.output


def test_cli_start_api_flag_starts_the_api_then_launches(runner, offline, monkeypatch, spawn) -> None:
    started: list[int] = []
    monkeypatch.setattr(services, "api_healthy", lambda port, timeout=2.0: (False, "unreachable (refused)"))
    monkeypatch.setattr(services, "port_listening", lambda port, **kwargs: False)
    monkeypatch.setattr(
        services, "start_api",
        lambda port, log_dir=None: started.append(port) or (True, "healthy after wait (engine_initialized)"),
    )
    monkeypatch.setattr(launch_mod, "health_for_launch", lambda port=8000, timeout=2.0: dict(LOOPBACK_OPEN))
    monkeypatch.setattr(launch_mod, "register_claude_code", lambda: (True, "Registered nvhive MCP server with Claude Code"))
    result = runner.invoke(cli_main.app, ["launch", "claude", "-y", "--port", "8123", "--", "--resume"])
    assert result.exit_code == 0, result.output
    assert started == [8123]
    assert spawn.calls[0][0] == ["claude", "--resume"]
    assert "registered:" in result.output
    assert launch_mod.verification_line(launch_mod.get_target("claude")) in result.output


def test_cli_start_api_refuses_a_held_port_instead_of_freeing_it(runner, offline, monkeypatch, spawn) -> None:
    monkeypatch.setattr(services, "api_healthy", lambda port, timeout=2.0: (False, "unreachable (HTTP Error 404)"))
    monkeypatch.setattr(services, "port_listening", lambda port, **kwargs: True)
    result = runner.invoke(cli_main.app, ["launch", "codex", "-y", "--port", "3000"])
    assert result.exit_code == 2, result.output
    assert "port 3000 is held" in result.output and "will not stop it" in result.output
    assert spawn.calls == []


def test_cli_passes_unknown_options_through_to_the_tool_and_returns_its_exit_code(
    runner, offline, monkeypatch, spawn,
) -> None:
    monkeypatch.setattr(services, "api_healthy", lambda port, timeout=2.0: (True, "engine_initialized"))
    monkeypatch.setattr(launch_mod, "health_for_launch", lambda port=8000, timeout=2.0: dict(LOOPBACK_OPEN))
    monkeypatch.setattr(launch_mod, "register_claude_code", lambda: (True, "ok"))
    spawn.code = 3
    result = runner.invoke(cli_main.app, ["launch", "claude", "--resume", "-p", "hello"])
    assert result.exit_code == 3, result.output
    assert spawn.calls[0][0] == ["claude", "--resume", "-p", "hello"]


def test_cli_snippet_target_launch_prints_the_snippet_and_spawns_nothing(runner, offline, monkeypatch) -> None:
    monkeypatch.setattr(services, "api_healthy", lambda port, timeout=2.0: (True, "engine_initialized"))
    monkeypatch.setattr(launch_mod, "health_for_launch", lambda port=8000, timeout=2.0: dict(LOOPBACK_OPEN))
    result = runner.invoke(cli_main.app, ["launch", "opencode"])
    assert result.exit_code == 0, result.output
    assert f'"baseURL": "{BASE}"' in result.output
    assert "writes no third-party config" in result.output
    assert launch_mod.verification_line(launch_mod.get_target("opencode")) in result.output
