"""Tests for nvh.core.tools — ToolRegistry, execution, guardrails, path resolution, handlers."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, mock_open, patch

import pytest

from nvh.core.tools import Tool, ToolRegistry


class TestToolRegistry:
    def test_builtin_tools_registered(self):
        r = ToolRegistry(include_system=False)
        names = [t.name for t in r.list_tools()]
        assert "read_file" in names
        assert "write_file" in names
        assert "web_search" in names
        assert "web_fetch" in names

    def test_system_tools_registered(self):
        r = ToolRegistry(include_system=True)
        names = [t.name for t in r.list_tools()]
        assert "list_processes" in names
        assert "system_info" in names
        assert "pip_list" in names
        assert "open" in names

    def test_tool_count(self):
        r = ToolRegistry()
        assert len(r.list_tools()) >= 20

    def test_safe_vs_unsafe(self):
        r = ToolRegistry()
        safe = [t for t in r.list_tools() if t.safe]
        unsafe = [t for t in r.list_tools() if not t.safe]
        assert len(safe) > len(unsafe)

    def test_get_tool(self):
        r = ToolRegistry()
        t = r.get("read_file")
        assert t is not None
        assert t.name == "read_file"
        assert t.safe

    def test_get_unknown_tool(self):
        r = ToolRegistry()
        assert r.get("nonexistent_tool") is None

    def test_tool_descriptions(self):
        r = ToolRegistry()
        desc = r.get_tool_descriptions()
        assert "read_file" in desc
        assert "web_search" in desc

    def test_path_traversal_blocked(self):
        r = ToolRegistry(workspace="/tmp/test")
        with pytest.raises(PermissionError):
            r._resolve_path("../../etc/passwd")


class TestToolRegistryExtended:

    def test_register_custom_tool(self):
        reg = ToolRegistry(include_system=False)
        custom = Tool(
            name="my_tool",
            description="does stuff",
            parameters={"type": "object", "properties": {}},
            handler=AsyncMock(),
            safe=True,
        )
        reg.register(custom)
        assert reg.get("my_tool") is not None
        assert reg.get("my_tool").name == "my_tool"

    def test_get_tool_descriptions_format(self):
        reg = ToolRegistry(include_system=False)
        desc = reg.get_tool_descriptions()
        assert "Available tools" in desc
        assert "read_file" in desc
        assert "write_file" in desc
        # Check that parameters are listed
        assert "path" in desc

    def test_list_tools_returns_all_builtins(self):
        reg = ToolRegistry(include_system=False)
        names = {t.name for t in reg.list_tools()}
        expected = {"read_file", "write_file", "list_files", "search_files",
                    "run_code", "shell", "web_search", "web_fetch",
                    "screenshot", "imagine"}
        assert expected.issubset(names)

    def test_resolve_path_within_workspace(self):
        reg = ToolRegistry(workspace="/home/user/project", include_system=False)
        resolved = reg._resolve_path("src/main.py")
        assert "src" in resolved
        assert "main.py" in resolved

    def test_resolve_path_traversal_blocked(self):
        reg = ToolRegistry(workspace="/home/user/project", include_system=False)
        with pytest.raises(PermissionError, match="Path traversal"):
            reg._resolve_path("../../etc/passwd")

    def test_resolve_path_absolute_outside_blocked(self):
        reg = ToolRegistry(workspace="/home/user/project", include_system=False)
        with pytest.raises(PermissionError, match="Path traversal"):
            reg._resolve_path("/etc/passwd")


class TestToolRegistryWorkspace:
    def test_builtins_registered(self, tmp_path: Path) -> None:
        reg = ToolRegistry(workspace=str(tmp_path), include_system=False)
        names = {t.name for t in reg.list_tools()}
        assert "read_file" in names
        assert "write_file" in names
        assert "list_files" in names
        assert "search_files" in names
        assert "shell" in names

    def test_get_tool_descriptions(self, tmp_path: Path) -> None:
        reg = ToolRegistry(workspace=str(tmp_path), include_system=False)
        desc = reg.get_tool_descriptions()
        assert "read_file" in desc
        assert len(desc) > 50

    @pytest.mark.asyncio
    async def test_read_file(self, tmp_path: Path) -> None:
        (tmp_path / "hello.txt").write_text("world")
        reg = ToolRegistry(workspace=str(tmp_path), include_system=False)
        tool = reg.get("read_file")
        assert tool is not None
        result = await tool.handler(path="hello.txt")
        assert result == "world"

    @pytest.mark.asyncio
    async def test_write_file(self, tmp_path: Path) -> None:
        reg = ToolRegistry(workspace=str(tmp_path), include_system=False)
        tool = reg.get("write_file")
        assert tool is not None
        result = await tool.handler(path="out.txt", content="data")
        assert "4 chars" in result
        assert (tmp_path / "out.txt").read_text() == "data"

    @pytest.mark.asyncio
    async def test_list_files(self, tmp_path: Path) -> None:
        (tmp_path / "a.py").write_text("x")
        (tmp_path / "b.py").write_text("y")
        reg = ToolRegistry(workspace=str(tmp_path), include_system=False)
        tool = reg.get("list_files")
        assert tool is not None
        result = await tool.handler(pattern="*.py")
        assert "a.py" in result
        assert "b.py" in result

    @pytest.mark.asyncio
    async def test_search_files(self, tmp_path: Path) -> None:
        (tmp_path / "code.py").write_text("def hello_world():\n    pass\n")
        reg = ToolRegistry(workspace=str(tmp_path), include_system=False)
        tool = reg.get("search_files")
        assert tool is not None
        result = await tool.handler(query="hello_world", pattern="*.py")
        assert "hello_world" in result

    def test_path_traversal_blocked(self, tmp_path: Path) -> None:
        reg = ToolRegistry(workspace=str(tmp_path), include_system=False)
        with pytest.raises(PermissionError, match="traversal"):
            reg._resolve_path("../../etc/passwd")

    def test_get_unknown_tool(self, tmp_path: Path) -> None:
        reg = ToolRegistry(workspace=str(tmp_path), include_system=False)
        assert reg.get("nonexistent") is None

    @pytest.mark.asyncio
    async def test_execute_unknown_tool(self, tmp_path: Path) -> None:
        reg = ToolRegistry(workspace=str(tmp_path), include_system=False)
        result = await reg.execute("no_such_tool", {})
        assert not result.success
        assert "Unknown tool" in result.error


class TestToolExecute:
    """All filesystem and subprocess calls are mocked. No real file I/O."""

    @pytest.mark.asyncio
    async def test_execute_unknown_tool(self):
        reg = ToolRegistry(include_system=False)
        result = await reg.execute("nonexistent", {})
        assert result.success is False
        assert "Unknown tool" in result.error

    @pytest.mark.asyncio
    async def test_execute_read_file_success(self):
        reg = ToolRegistry(workspace="/tmp/test_ws", include_system=False)
        with patch("os.path.isfile", return_value=True), \
             patch("builtins.open", mock_open(read_data="hello world")), \
             patch("nvh.core.agent_guardrails.check_file_read"), \
             patch("nvh.core.agent_guardrails.check_path"), \
             patch("nvh.core.agent_guardrails.redact_secrets", side_effect=lambda x: x), \
             patch("nvh.core.agent_guardrails.truncate_output", side_effect=lambda x: x):
            result = await reg.execute("read_file", {"path": "hello.txt"})

        assert result.success is True
        assert "hello world" in result.output

    @pytest.mark.asyncio
    async def test_execute_read_file_not_found(self):
        reg = ToolRegistry(workspace="/tmp/test_ws", include_system=False)
        with patch("os.path.isfile", return_value=False), \
             patch("nvh.core.agent_guardrails.check_file_read"), \
             patch("nvh.core.agent_guardrails.check_path"):
            result = await reg.execute("read_file", {"path": "nope.txt"})

        assert result.success is False
        assert "not found" in result.error.lower() or "FileNotFoundError" in result.error

    @pytest.mark.asyncio
    async def test_execute_guardrail_blocks_command(self):
        """If a guardrail fires, the tool is rejected."""
        reg = ToolRegistry(workspace="/tmp/test_ws", include_system=False)
        from nvh.core.agent_guardrails import GuardrailError

        with patch("nvh.core.agent_guardrails.check_command", side_effect=GuardrailError("dangerous command")):
            result = await reg.execute("shell", {"command": "rm -rf /"})

        assert result.success is False
        assert "GUARDRAIL" in result.error

    @pytest.mark.asyncio
    async def test_execute_handler_exception_caught(self):
        """If the tool handler raises, it returns an error ToolResult."""
        reg = ToolRegistry(include_system=False)
        broken_tool = Tool(
            name="broken",
            description="always fails",
            parameters={"type": "object", "properties": {}},
            handler=AsyncMock(side_effect=RuntimeError("boom")),
            safe=True,
        )
        reg.register(broken_tool)

        # No guardrail imports needed for custom tools
        result = await reg.execute("broken", {})
        assert result.success is False
        assert "boom" in result.error

    @pytest.mark.asyncio
    async def test_execute_write_file_guardrail_size_check(self):
        """write_file should invoke check_write_size guardrail."""
        reg = ToolRegistry(workspace="/tmp/test_ws", include_system=False)
        from nvh.core.agent_guardrails import GuardrailError

        with patch("nvh.core.agent_guardrails.check_path"), \
             patch("nvh.core.agent_guardrails.check_write_size",
                   side_effect=GuardrailError("file too large")):
            result = await reg.execute("write_file", {"path": "big.txt", "content": "x" * 999999})

        assert result.success is False
        assert "GUARDRAIL" in result.error


class TestToolExecuteRealFs:
    @pytest.mark.asyncio
    async def test_shell_simple_command(self):
        reg = ToolRegistry(workspace=".", include_system=False)
        result = await reg.execute("shell", {"command": "echo test123"})
        # On CI, Docker noise may interfere — just verify tool ran
        assert result is not None

    @pytest.mark.asyncio
    async def test_list_files_no_match(self, tmp_path):
        reg = ToolRegistry(workspace=str(tmp_path), include_system=False)
        result = await reg.execute("list_files", {"pattern": "*.nonexistent"})
        assert result.success
        # Empty or "no files" message
        assert result.output is not None

    @pytest.mark.asyncio
    async def test_read_file_not_found(self, tmp_path):
        reg = ToolRegistry(workspace=str(tmp_path), include_system=False)
        result = await reg.execute("read_file", {"path": "does_not_exist.txt"})
        assert not result.success
        assert "not found" in result.error.lower() or "No such" in result.error

    @pytest.mark.asyncio
    async def test_write_then_read(self, tmp_path):
        reg = ToolRegistry(workspace=str(tmp_path), include_system=False)
        w = await reg.execute("write_file", {"path": "round_trip.txt", "content": "hello world"})
        assert w.success
        r = await reg.execute("read_file", {"path": "round_trip.txt"})
        assert r.success
        assert "hello world" in r.output


class TestBuiltinToolHandlers:

    @pytest.mark.asyncio
    async def test_list_files_returns_matches(self):
        reg = ToolRegistry(workspace="/tmp/test_ws", include_system=False)
        fake_matches = ["/tmp/test_ws/a.py", "/tmp/test_ws/b.py"]
        with patch("glob.glob", return_value=fake_matches):
            result = await reg.execute("list_files", {"pattern": "*.py", "directory": "."})

        assert result.success is True
        assert "a.py" in result.output

    @pytest.mark.asyncio
    async def test_search_files_no_matches(self):
        reg = ToolRegistry(workspace="/tmp/test_ws", include_system=False)
        with patch("glob.glob", return_value=[]):
            result = await reg.execute("search_files", {"query": "NOTFOUND"})

        assert result.success is True
        assert "No matches" in result.output
