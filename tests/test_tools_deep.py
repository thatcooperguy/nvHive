"""Deep tests for nvh.core.tools — ToolRegistry execution, guardrails, path resolution.

All filesystem and subprocess calls are mocked. No real file I/O.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, mock_open, patch

import pytest

from nvh.core.tools import Tool, ToolRegistry

# ---------------------------------------------------------------------------
# ToolRegistry basics (extending the shallow tests in test_tools.py)
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# ToolRegistry.execute — async tests
# ---------------------------------------------------------------------------


class TestToolExecute:

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


# ---------------------------------------------------------------------------
# Built-in tool handlers (via execute)
# ---------------------------------------------------------------------------


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
