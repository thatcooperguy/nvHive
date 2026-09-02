"""`nvh mcp` registry shape after the 0.41.1 shadowing fix.

Before 0.41.1 ``@app.command`` ``mcp`` (the MCP *server*) and
``app.add_typer(mcp_app, name="mcp")`` (external tool *servers*) registered
the same name; Click kept whichever landed last, so ``nvh mcp`` never started
the server. The group now owns the name with ``invoke_without_command`` and
the tool-server verbs live under ``nvh mcp servers``.
"""

from __future__ import annotations

import sys
import types

import pytest
import typer
from typer.main import get_command, get_command_name
from typer.testing import CliRunner

import nvh.cli.main as cli_main


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
        assert "Model Context Protocol" in result.output
        assert "--transport" in result.output
        assert "servers" in result.output
        # Old spellings are hidden aliases — not advertised.
        assert "refresh" not in result.output

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
        assert runs[-1] == {"transport": "streamable-http", "host": "0.0.0.0", "port": 9001}
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
