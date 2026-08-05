"""Tests for the MCP client (nvh/integrations/mcp_client.py).

Covers the three layers separately so failures localize:
  1. Config + cache plumbing (pure JSON, no SDK needed).
  2. Registry integration — cached tools become namespaced WizardTools
     with the right safety classes.
  3. One end-to-end stdio call against a real subprocess MCP server
     (FastMCP script), skipped when the `mcp` extra isn't installed.
"""

from __future__ import annotations

import json
import sys
import textwrap
from pathlib import Path

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


# ── 3. End-to-end stdio call (needs the mcp extra) ──────────────────────────


SERVER_SCRIPT = textwrap.dedent(
    '''
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP("pingpong")

    @mcp.tool()
    def ping(message: str) -> str:
        """Echo the message back with a pong prefix."""
        return f"pong: {message}"

    if __name__ == "__main__":
        mcp.run()
    '''
)


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
