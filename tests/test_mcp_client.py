"""Tests for the MCP client (nvh/integrations/mcp_client.py).

Covers the three layers separately so failures localize:
  1. Config + cache plumbing (pure JSON, no SDK needed).
  2. Registry integration — cached tools become namespaced WizardTools
     with the right safety classes.
  3. The mcp SDK 1.x / 2.x attribute shim and a fake-SDK round trip through
     ``refresh_all_tools`` / ``call_mcp_tool`` (hermetic, no SDK needed).
  4. One end-to-end stdio call against a real subprocess MCP server
     (MCPServer on mcp 2.x, FastMCP on 1.x), skipped when the `mcp` extra
     isn't installed.
  5. ``sdk_version()`` and the one `mcp <version>` line `nvh status` prints.
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
import textwrap
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

from nvh.integrations import mcp_client
from nvh.integrations.mcp_client import (
    load_mcp_config,
    namespaced_tool_name,
    read_tools_cache,
    register_mcp_tools,
    servers_status,
)
from nvh.integrations.wizard.tools import WizardToolRegistry


def _write_config(home: Path, servers: dict) -> None:
    cfg_dir = home / "config"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "mcp-servers.json").write_text(
        json.dumps({"mcpServers": servers}), encoding="utf-8"
    )


def _write_cache(home: Path, cache: dict) -> None:
    state = home / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "mcp-tools-cache.json").write_text(json.dumps(cache), encoding="utf-8")


# ── 1. Config + cache plumbing ──────────────────────────────────────────────


def test_missing_config_means_feature_off(tmp_path) -> None:
    assert load_mcp_config(home_dir=tmp_path) == {}
    assert read_tools_cache(home_dir=tmp_path) == {}
    assert servers_status(home_dir=tmp_path) == []


def test_malformed_config_returns_empty_not_raise(tmp_path) -> None:
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "mcp-servers.json").write_text("{not json", encoding="utf-8")
    assert load_mcp_config(home_dir=tmp_path) == {}


def test_config_skips_disabled_and_commandless_servers(tmp_path) -> None:
    _write_config(tmp_path, {
        "good": {"command": "echo"},
        "off": {"command": "echo", "enabled": False},
        "broken": {"args": ["no-command"]},
    })
    config = load_mcp_config(home_dir=tmp_path)
    assert set(config) == {"good"}


def test_namespaced_tool_name_sanitizes() -> None:
    assert namespaced_tool_name("My-Server", "read.file") == "mcp_my_server_read_file"
    assert namespaced_tool_name("fs", "list") == "mcp_fs_list"


# ── 2. Registry integration ─────────────────────────────────────────────────


def _seed(tmp_path: Path) -> None:
    _write_config(tmp_path, {
        "fs": {
            "command": "echo",
            "auto_approve": ["read_file"],
        },
    })
    _write_cache(tmp_path, {
        "fs": {
            "ok": True,
            "refreshed_at": "2026-08-05T00:00:00Z",
            "tools": [
                {
                    "name": "read_file",
                    "description": "Read a file",
                    "input_schema": {
                        "type": "object",
                        "properties": {"path": {"type": "string", "description": "file path"}},
                        "required": ["path"],
                    },
                },
                {
                    "name": "write_file",
                    "description": "Write a file",
                    "input_schema": {},
                },
            ],
        },
    })


def test_register_mcp_tools_namespaces_and_safety_classes(tmp_path) -> None:
    _seed(tmp_path)
    reg = WizardToolRegistry()
    register_mcp_tools(reg, home_dir=tmp_path)

    read = reg.get("mcp_fs_read_file")
    write = reg.get("mcp_fs_write_file")
    assert read is not None and write is not None
    # auto_approve promotes read_file; everything else defaults to confirm —
    # external MCP servers run arbitrary code, confirm is the safe default.
    assert read.safety_class == "auto"
    assert write.safety_class == "confirm"
    # Schema mapped through with required flag.
    assert read.parameters["path"]["required"] is True
    # Server provenance is visible to the LLM + confirm cards.
    assert "[MCP:fs]" in read.description


def test_register_mcp_tools_keeps_the_servers_schema_whole(tmp_path) -> None:
    """The MCP inputSchema is the tool's JSON Schema as-is (nested shapes and
    all, for native function calling); the Wizard view is derived from it by
    the one translation, not re-typed in the adapter."""
    from nvh.core.tools import translate_parameters

    _write_config(tmp_path, {"fs": {"command": "echo"}})
    schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "file path"},
            "opts": {"type": "object", "properties": {"recursive": {"type": "boolean"}}},
            "mode": {"type": ["string", "null"], "enum": ["r", "w", None]},
        },
        "required": ["path"],
        "additionalProperties": False,
    }
    _write_cache(tmp_path, {"fs": {"ok": True, "tools": [{"name": "read", "description": "Read", "input_schema": schema}]}})
    reg = WizardToolRegistry()
    register_mcp_tools(reg, home_dir=tmp_path)
    tool = reg.get("mcp_fs_read")
    assert tool.input_schema == schema
    assert tool.as_openai_tool()["function"]["parameters"] == schema
    assert tool.parameters == translate_parameters(schema)
    assert tool.parameters["mode"] == {"type": "string", "description": "", "required": False}
    assert tool.parameters["opts"]["type"] == "object"


def test_register_mcp_tools_skips_failed_and_unrefreshed_servers(tmp_path) -> None:
    _write_config(tmp_path, {"a": {"command": "echo"}, "b": {"command": "echo"}})
    _write_cache(tmp_path, {"a": {"ok": False, "error": "boom", "tools": []}})
    reg = WizardToolRegistry()
    register_mcp_tools(reg, home_dir=tmp_path)
    assert not [t for t in reg.list_tools() if t.name.startswith("mcp_")]


def test_confirm_class_mcp_tool_requires_confirmation(tmp_path) -> None:
    _seed(tmp_path)
    reg = WizardToolRegistry()
    register_mcp_tools(reg, home_dir=tmp_path)

    import asyncio

    result = asyncio.run(reg.execute("mcp_fs_write_file", arguments={}))
    assert result.get("needs_confirmation") or result.get("ok") is False


def test_default_registry_includes_mcp_tools(tmp_path, monkeypatch) -> None:
    """The chat-turn registry build must pick up cached MCP tools."""
    _seed(tmp_path)
    import nvh.integrations.mcp_client as mc

    monkeypatch.setattr(mc, "mcp_config_path", lambda home_dir=None: tmp_path / "config" / "mcp-servers.json")
    monkeypatch.setattr(mc, "tools_cache_path", lambda home_dir=None: tmp_path / "state" / "mcp-tools-cache.json")

    from nvh.integrations.wizard.tools import default_registry

    names = {t.name for t in default_registry().list_tools()}
    assert "mcp_fs_read_file" in names


# ── 3. SDK 1.x / 2.x attribute shim + fake-SDK round trip ───────────────────


class TestSdkAttributeShim:
    """mcp 2.x models are snake_case only (``input_schema``, ``is_error``);
    1.x models are camelCase (``inputSchema``, ``isError``). The client reads
    the 2.x name first and falls back, with no SDK version sniffing."""

    def test_input_schema_reads_v2_then_v1(self) -> None:
        schema = {"type": "object", "properties": {"p": {"type": "string"}}}
        assert mcp_client._tool_input_schema(SimpleNamespace(input_schema=schema)) == schema
        assert mcp_client._tool_input_schema(SimpleNamespace(inputSchema=schema)) == schema
        # Both present (a shimmed model): the 2.x name wins.
        both = SimpleNamespace(input_schema=schema, inputSchema={"stale": True})
        assert mcp_client._tool_input_schema(both) == schema
        assert mcp_client._tool_input_schema(SimpleNamespace(input_schema=None)) == {}
        assert mcp_client._tool_input_schema(SimpleNamespace()) == {}

    def test_is_error_reads_v2_then_v1(self) -> None:
        assert mcp_client._result_is_error(SimpleNamespace(is_error=True)) is True
        assert mcp_client._result_is_error(SimpleNamespace(isError=True)) is True
        # An explicit 2.x False is authoritative beside a stale camelCase flag.
        assert mcp_client._result_is_error(SimpleNamespace(is_error=False, isError=True)) is False
        assert mcp_client._result_is_error(SimpleNamespace()) is False

    def test_content_text_joins_text_blocks_and_labels_the_rest(self) -> None:
        result = SimpleNamespace(content=[
            SimpleNamespace(type="text", text="one"),
            SimpleNamespace(type="image", data="...", mimeType="image/png"),
            SimpleNamespace(type="text", text="two"),
        ])
        assert mcp_client._content_text(result) == "one\n[image]\ntwo"
        assert mcp_client._content_text(SimpleNamespace(content=[])) == ""
        assert mcp_client._content_text(SimpleNamespace()) == ""


def _install_fake_sdk(monkeypatch, *, tools: list, result) -> list:
    """Bind ``mcp`` / ``mcp.client.stdio`` to in-process fakes so the client's
    stdio path runs without a subprocess or the real SDK. Returns the list
    the fake session appends ``(tool, arguments)`` to on every call."""
    calls: list = []

    class FakeSession:
        def __init__(self, read, write) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc) -> bool:
            return False

        async def initialize(self) -> None:
            pass

        async def list_tools(self):
            return SimpleNamespace(tools=tools)

        async def call_tool(self, name, arguments):
            calls.append((name, arguments))
            return result

    @contextlib.asynccontextmanager
    async def stdio_client(params):
        yield (None, None)

    mcp_mod = types.ModuleType("mcp")
    mcp_mod.ClientSession = FakeSession  # type: ignore[attr-defined]
    client_mod = types.ModuleType("mcp.client")
    stdio_mod = types.ModuleType("mcp.client.stdio")
    stdio_mod.stdio_client = stdio_client  # type: ignore[attr-defined]
    stdio_mod.StdioServerParameters = lambda **kw: kw  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "mcp", mcp_mod)
    monkeypatch.setitem(sys.modules, "mcp.client", client_mod)
    monkeypatch.setitem(sys.modules, "mcp.client.stdio", stdio_mod)
    return calls


class TestFakeSdkRoundTrip:
    """The refresh and call paths read whichever attribute shape the SDK hands
    back; one fake per major, no version branches in the code under test."""

    def test_refresh_reads_v2_tool_shape(self, tmp_path, monkeypatch) -> None:
        import asyncio

        schema = {"type": "object", "properties": {"message": {"type": "string"}}}
        _install_fake_sdk(
            monkeypatch,
            tools=[SimpleNamespace(name="ping", description="Echo", input_schema=schema)],
            result=None,
        )
        _write_config(tmp_path, {"fake": {"command": "echo"}})
        cache = asyncio.run(mcp_client.refresh_all_tools(home_dir=tmp_path))
        assert cache["fake"]["ok"] is True
        assert cache["fake"]["tools"] == [
            {"name": "ping", "description": "Echo", "input_schema": schema}
        ]

    def test_refresh_reads_v1_tool_shape(self, tmp_path, monkeypatch) -> None:
        import asyncio

        schema = {"type": "object", "properties": {"message": {"type": "string"}}}
        _install_fake_sdk(
            monkeypatch,
            tools=[SimpleNamespace(name="ping", description=None, inputSchema=schema)],
            result=None,
        )
        _write_config(tmp_path, {"fake": {"command": "echo"}})
        cache = asyncio.run(mcp_client.refresh_all_tools(home_dir=tmp_path))
        assert cache["fake"]["tools"] == [{"name": "ping", "description": "", "input_schema": schema}]

    def test_call_reads_v2_result_shape(self, tmp_path, monkeypatch) -> None:
        import asyncio

        calls = _install_fake_sdk(
            monkeypatch,
            tools=[],
            result=SimpleNamespace(
                content=[SimpleNamespace(type="text", text="pong: hi")], is_error=False
            ),
        )
        _write_config(tmp_path, {"fake": {"command": "echo"}})
        out = asyncio.run(mcp_client.call_mcp_tool("fake", "ping", {"message": "hi"}, home_dir=tmp_path))
        assert calls == [("ping", {"message": "hi"})]
        assert out == {
            "ok": True, "content": "pong: hi", "is_error": False, "server": "fake", "tool": "ping",
        }

    def test_call_reads_v1_error_flag(self, tmp_path, monkeypatch) -> None:
        import asyncio

        _install_fake_sdk(
            monkeypatch,
            tools=[],
            result=SimpleNamespace(
                content=[SimpleNamespace(type="text", text="boom")], isError=True
            ),
        )
        _write_config(tmp_path, {"fake": {"command": "echo"}})
        out = asyncio.run(mcp_client.call_mcp_tool("fake", "ping", {}, home_dir=tmp_path))
        assert out["ok"] is False
        assert out["is_error"] is True
        assert out["content"] == "boom"


# ── 4. End-to-end stdio call (needs the mcp extra) ──────────────────────────


SERVER_SCRIPT = textwrap.dedent(
    '''
    try:
        from mcp.server.mcpserver import MCPServer as FastMCP  # mcp 2.x
    except ImportError:
        from mcp.server.fastmcp import FastMCP  # mcp 1.x

    mcp = FastMCP("pingpong")

    @mcp.tool()
    def ping(message: str) -> str:
        """Echo the message back with a pong prefix."""
        return f"pong: {message}"

    if __name__ == "__main__":
        mcp.run()
    '''
)


@pytest.mark.slow
@pytest.mark.timeout(90)
def test_end_to_end_stdio_call(tmp_path) -> None:
    pytest.importorskip("mcp")
    server_py = tmp_path / "server.py"
    server_py.write_text(SERVER_SCRIPT, encoding="utf-8")
    _write_config(tmp_path, {
        "pingpong": {"command": sys.executable, "args": [str(server_py)]},
    })

    import asyncio

    cache = asyncio.run(mcp_client.refresh_all_tools(home_dir=tmp_path))
    assert cache["pingpong"]["ok"], cache["pingpong"].get("error")
    tool_names = [t["name"] for t in cache["pingpong"]["tools"]]
    assert "ping" in tool_names

    result = asyncio.run(
        mcp_client.call_mcp_tool("pingpong", "ping", {"message": "hi"}, home_dir=tmp_path)
    )
    assert result["ok"], result
    assert "pong: hi" in result["content"]


# ── 5. SDK version reporting (decision 21b) ─────────────────────────────────


class TestSdkVersion:
    def test_reports_the_installed_distribution(self) -> None:
        pytest.importorskip("mcp")
        from importlib.metadata import version

        assert mcp_client.sdk_version() == version("mcp")

    def test_none_when_the_extra_is_absent(self, monkeypatch) -> None:
        monkeypatch.setitem(sys.modules, "mcp", None)
        assert mcp_client.sdk_version() is None


class TestStatusReportsSdk:
    """`nvh status` (glance tier) prints one `mcp <version>` line when the SDK
    imports and nothing about mcp when it does not."""

    @staticmethod
    def _glance(monkeypatch) -> str:
        from rich.console import Console

        import nvh.cli.main as cli_main
        from nvh.integrations.diagnostics import checks as diag

        async def _no_checks(*_args, **_kwargs):
            return []

        monkeypatch.setattr(diag, "run_checks", _no_checks)
        buf = io.StringIO()
        monkeypatch.setattr(cli_main, "console", Console(file=buf, force_terminal=False, width=200))
        cli_main._status_glance(SimpleNamespace(config=None))
        return buf.getvalue()

    def test_prints_the_sdk_version_when_installed(self, monkeypatch) -> None:
        monkeypatch.setattr(mcp_client, "sdk_version", lambda: "9.9.9")
        out = self._glance(monkeypatch)
        assert "NVHive v" in out
        assert "mcp 9.9.9" in out

    def test_prints_nothing_about_mcp_when_absent(self, monkeypatch) -> None:
        monkeypatch.setattr(mcp_client, "sdk_version", lambda: None)
        out = self._glance(monkeypatch)
        assert "NVHive v" in out
        assert "mcp" not in out.lower()
