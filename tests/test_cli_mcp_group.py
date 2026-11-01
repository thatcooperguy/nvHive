"""`nvh mcp` registry shape after the 0.41.1 shadowing fix.

Before 0.41.1 ``@app.command`` ``mcp`` (the MCP *server*) and
``app.add_typer(mcp_app, name="mcp")`` (external tool *servers*) registered
the same name; Click kept whichever landed last, so ``nvh mcp`` never started
the server. The group now owns the name with ``invoke_without_command`` and
the tool-server verbs live under ``nvh mcp servers``.
"""

from __future__ import annotations

import asyncio
import re
import sys
import types

import pytest
import typer
from typer.main import get_command, get_command_name
from typer.testing import CliRunner

import nvh.cli.main as cli_main

_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _plain(text: str) -> str:
    # Rich colours help output on CI (FORCE_COLOR) and styles `--flag` as
    # `-` + `-flag` with escape codes between, so substring checks must run
    # on the de-styled text.
    return _ANSI.sub("", text)


@pytest.fixture()
def runner() -> CliRunner:
    return CliRunner()


def _command_names(app: typer.Typer) -> list[str]:
    return [
        info.name or get_command_name(info.callback.__name__)
        for info in app.registered_commands
        if info.callback is not None
    ]


def _group_names(app: typer.Typer) -> list[str]:
    names = []
    for info in app.registered_groups:
        name = info.name
        if name is None and info.typer_instance is not None:
            name = info.typer_instance.info.name
        if name is None and info.typer_instance is not None and info.typer_instance.info.callback:
            name = get_command_name(info.typer_instance.info.callback.__name__)
        names.append(name)
    return names


def _assert_no_collisions(app: typer.Typer, path: str) -> None:
    commands = _command_names(app)
    groups = _group_names(app)
    dup_commands = {n for n in commands if commands.count(n) > 1}
    dup_groups = {n for n in groups if groups.count(n) > 1}
    both = set(commands) & set(groups)
    assert not dup_commands, f"{path}: command registered twice: {dup_commands}"
    assert not dup_groups, f"{path}: group registered twice: {dup_groups}"
    assert not both, f"{path}: registered as both command and group: {both}"
    for info in app.registered_groups:
        if info.typer_instance is not None:
            _assert_no_collisions(info.typer_instance, f"{path} {info.name}")


class TestRegistryCollisionGuard:
    def test_no_name_is_both_command_and_group(self):
        _assert_no_collisions(cli_main.app, "nvh")

    def test_click_tree_keeps_every_registration(self):
        """If two registrations shared a name, Click would silently keep one."""
        click_group = get_command(cli_main.app)
        expected = set(_command_names(cli_main.app)) | set(_group_names(cli_main.app))
        assert set(click_group.commands) == expected

    def test_mcp_is_a_group_not_a_command(self):
        assert "mcp" not in _command_names(cli_main.app)
        assert "mcp" in _group_names(cli_main.app)

    def test_previously_shadowed_commands_are_reachable(self):
        """`nvidia` (advisor cmd vs dashboard) and `agent` (coding agent vs
        persona group) were the other two silent collisions the guard found."""
        click_group = get_command(cli_main.app)
        assert "run" in click_group.commands["agent"].commands
        assert click_group.commands["nvidia"].callback.__name__ == "nvidia"


class TestMcpGroup:
    def test_help_describes_the_server(self, runner: CliRunner):
        result = runner.invoke(cli_main.app, ["mcp", "--help"])
        assert result.exit_code == 0, result.output
        output = _plain(result.output)
        assert "Model Context Protocol" in output
        assert "--transport" in output
        assert "servers" in output
        # Old spellings are hidden aliases — not advertised.
        assert "refresh" not in output

    def test_servers_subgroup_and_hidden_aliases(self):
        mcp_group = get_command(cli_main.app).commands["mcp"]
        servers = mcp_group.commands["servers"]
        assert {"list", "refresh"} <= set(servers.commands)
        assert mcp_group.commands["list"].hidden is True
        assert mcp_group.commands["refresh"].hidden is True
        # Typer wraps each registration separately; both wrap the same function.
        assert mcp_group.commands["list"].callback.__name__ == "mcp_list"
        assert servers.commands["list"].callback.__name__ == "mcp_list"

    def test_bare_mcp_starts_the_server(self, runner: CliRunner, monkeypatch):
        runs: list[dict] = []

        class FakeServer:
            def run(self, **kwargs):
                runs.append(kwargs)

        fake = types.ModuleType("nvh.mcp_server")
        fake.create_server = lambda: FakeServer()  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "nvh.mcp_server", fake)

        result = runner.invoke(cli_main.app, ["mcp"])
        assert result.exit_code == 0, result.output
        assert runs == [{"transport": "stdio"}]
        # stdout is the JSON-RPC channel under stdio; the banner must not touch it.
        assert result.stdout == ""
        assert "NVHive MCP Server" in result.stderr
        assert "claude mcp add nvhive nvh mcp" in result.stderr

        result = runner.invoke(cli_main.app, ["mcp", "-t", "streamable-http", "--port", "9001"])
        assert result.exit_code == 0, result.output
        assert runs[-1] == {"transport": "streamable-http", "host": "127.0.0.1", "port": 9001}
        assert result.stdout == ""

    @pytest.mark.parametrize("argv", [["mcp", "servers", "list"], ["mcp", "list"]])
    def test_servers_list_does_not_start_the_server(
        self, runner: CliRunner, monkeypatch, tmp_path, argv,
    ):
        fake = types.ModuleType("nvh.mcp_server")
        fake.create_server = lambda: pytest.fail("server started for a subcommand")  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "nvh.mcp_server", fake)
        monkeypatch.setenv("NVH_HOME", str(tmp_path))
        for var in ("NVHIVE_HOME", "HIVE_CONFIG_HOME", "NVH_STATE"):
            monkeypatch.delenv(var, raising=False)

        result = runner.invoke(cli_main.app, argv)
        assert result.exit_code == 0, result.output
        assert "No MCP servers configured" in result.output
        assert "nvh mcp servers refresh" in result.output


# ───────────────────────────────────────────────────────────────────────────
# HTTP transport bind (0.44.1): loopback by default, both MCP SDK majors
# ───────────────────────────────────────────────────────────────────────────


class _KwargsServer:
    """mcp 2.x shape: ``MCPServer.run(transport, **kwargs)`` forwards host/port."""

    def __init__(self, runs: list) -> None:
        self.runs = runs

    def run(self, transport: str = "stdio", **kwargs) -> None:
        self.runs.append({"transport": transport, **kwargs})


class _V1Server:
    """mcp 1.x shape: ``FastMCP.run(transport, mount_path)`` reads ``settings.host/port``
    and raises ``TypeError`` on a ``host=`` keyword."""

    def __init__(self, runs: list) -> None:
        self.runs = runs
        self.settings = types.SimpleNamespace(host="127.0.0.1", port=8000)

    def run(self, transport: str = "stdio", mount_path: str | None = None) -> None:
        self.runs.append({"transport": transport, "mount_path": mount_path})


def _install_fake_server(monkeypatch, server) -> None:
    fake = types.ModuleType("nvh.mcp_server")
    fake.create_server = lambda: server  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "nvh.mcp_server", fake)


def _installed_sdk_server():
    """A real server object from whichever MCP SDK major is installed."""
    try:
        from mcp.server.mcpserver import MCPServer as _Server  # mcp 2.x
    except ImportError:
        try:
            from mcp.server.fastmcp import FastMCP as _Server  # mcp 1.x
        except ImportError:
            pytest.skip("MCP SDK not installed")
    return _Server("nvhive-bind-test")


class TestMcpHttpBind:
    """The HTTP transport binds loopback unless ``--host`` says otherwise, and
    starts on both SDK majors (mcp 1.x ``run()`` rejected host/port before)."""

    def test_default_host_is_loopback(self):
        assert cli_main.MCP_HTTP_DEFAULT_HOST == "127.0.0.1"
        for command in ("mcp", "openclaw"):
            params = {p.name: p for p in get_command(cli_main.app).commands[command].params}
            assert params["host"].default == "127.0.0.1", command

    def test_help_advertises_host(self, runner: CliRunner):
        result = runner.invoke(cli_main.app, ["mcp", "--help"])
        assert result.exit_code == 0, result.output
        assert "--host" in _plain(result.output)

    def test_mcp_2x_gets_loopback_through_run_kwargs(self, runner: CliRunner, monkeypatch):
        runs: list[dict] = []
        _install_fake_server(monkeypatch, _KwargsServer(runs))
        result = runner.invoke(cli_main.app, ["mcp", "-t", "streamable-http", "--port", "9001"])
        assert result.exit_code == 0, result.output
        assert runs == [{"transport": "streamable-http", "host": "127.0.0.1", "port": 9001}]
        assert "127.0.0.1:9001" in result.stderr
        assert result.stdout == ""

    def test_mcp_2x_forwards_an_explicit_host(self, runner: CliRunner, monkeypatch):
        runs: list[dict] = []
        _install_fake_server(monkeypatch, _KwargsServer(runs))
        result = runner.invoke(
            cli_main.app, ["mcp", "-t", "streamable-http", "--host", "0.0.0.0", "--port", "9001"],
        )
        assert result.exit_code == 0, result.output
        assert runs == [{"transport": "streamable-http", "host": "0.0.0.0", "port": 9001}]
        assert "0.0.0.0:9001" in result.stderr

    @pytest.mark.parametrize(
        "argv, expected_host", [([], "127.0.0.1"), (["--host", "0.0.0.0"], "0.0.0.0")],
    )
    def test_mcp_1x_gets_the_bind_through_settings(
        self, runner: CliRunner, monkeypatch, argv, expected_host,
    ):
        runs: list[dict] = []
        server = _V1Server(runs)
        _install_fake_server(monkeypatch, server)
        result = runner.invoke(
            cli_main.app, ["mcp", "-t", "streamable-http", "--port", "9002", *argv],
        )
        assert result.exit_code == 0, result.output
        # No host/port keywords: FastMCP.run() would raise TypeError on them.
        assert runs == [{"transport": "streamable-http", "mount_path": None}]
        assert (server.settings.host, server.settings.port) == (expected_host, 9002)

    def test_stdio_ignores_the_host_option(self, runner: CliRunner, monkeypatch):
        runs: list[dict] = []
        _install_fake_server(monkeypatch, _V1Server(runs))
        result = runner.invoke(cli_main.app, ["mcp", "--host", "0.0.0.0"])
        assert result.exit_code == 0, result.output
        assert runs == [{"transport": "stdio", "mount_path": None}]

    def test_server_with_neither_bind_path_fails_loudly(self):
        class _Bare:
            def run(self, transport: str = "stdio", mount_path: str | None = None) -> None:
                pytest.fail("run() must not start a server whose bind cannot be applied")

        with pytest.raises(TypeError, match="settings.host/port"):
            cli_main._run_mcp_streamable_http(_Bare(), "127.0.0.1", 9003)

    @pytest.mark.parametrize("server_cls", [_KwargsServer, _V1Server])
    def test_openclaw_start_http_binds_loopback_by_default(
        self, runner: CliRunner, monkeypatch, server_cls,
    ):
        runs: list[dict] = []
        server = server_cls(runs)
        _install_fake_server(monkeypatch, server)
        result = runner.invoke(cli_main.app, ["openclaw", "--start", "--http", "--port", "9004"])
        assert result.exit_code == 0, result.output
        assert "127.0.0.1:9004" in _plain(result.output)
        if server_cls is _KwargsServer:
            assert runs == [{"transport": "streamable-http", "host": "127.0.0.1", "port": 9004}]
        else:
            assert runs == [{"transport": "streamable-http", "mount_path": None}]
            assert (server.settings.host, server.settings.port) == ("127.0.0.1", 9004)

    def test_openclaw_start_http_forwards_an_explicit_host(self, runner: CliRunner, monkeypatch):
        runs: list[dict] = []
        _install_fake_server(monkeypatch, _KwargsServer(runs))
        result = runner.invoke(
            cli_main.app, ["openclaw", "--start", "--http", "--host", "0.0.0.0", "--port", "9004"],
        )
        assert result.exit_code == 0, result.output
        assert runs == [{"transport": "streamable-http", "host": "0.0.0.0", "port": 9004}]

    def test_installed_sdk_server_takes_the_bind_without_a_type_error(self, monkeypatch):
        """Against the installed SDK (either major): ``run()`` is reached with a
        bind it accepts. The transport coroutine is replaced and ``anyio.run``
        intercepted, so nothing listens on a socket; on 2.x the *real*
        coroutine's signature is checked for the keywords the helper forwards,
        since the fake would swallow a renamed ``host``/``port`` silently."""
        import inspect

        import anyio

        server = _installed_sdk_server()
        seen: dict = {}
        # Read from the class before the instance attribute is replaced.
        real_params = inspect.signature(type(server).run_streamable_http_async).parameters

        async def fake_transport(**kwargs):
            seen.update(kwargs)

        monkeypatch.setattr(server, "run_streamable_http_async", fake_transport)
        monkeypatch.setattr(anyio, "run", lambda fn, *args, **_kw: asyncio.run(fn(*args)))
        v1 = hasattr(getattr(server, "settings", None), "host")
        if v1:
            server.settings.host = "0.0.0.0"  # the helper must overwrite, not inherit, it

        cli_main._run_mcp_streamable_http(server, "127.0.0.1", 9005)

        if v1:
            assert (server.settings.host, server.settings.port) == ("127.0.0.1", 9005)
            assert seen == {}
        else:
            assert seen == {"host": "127.0.0.1", "port": 9005}
            # ``MCPServer.run(transport, **kwargs)`` hands these straight to the
            # transport coroutine, so the keywords must exist there by name or
            # `nvh mcp -t streamable-http` raises TypeError at startup.
            keyword_kinds = {inspect.Parameter.KEYWORD_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD}
            for name in ("host", "port"):
                assert name in real_params, (
                    f"run_streamable_http_async() has no {name!r} keyword any more: "
                    f"{list(real_params)}"
                )
                assert real_params[name].kind in keyword_kinds
