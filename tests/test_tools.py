"""Tests for the tool registry."""
from nvh.core.tools import ToolRegistry


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
        import pytest
        with pytest.raises(PermissionError):
            r._resolve_path("../../etc/passwd")
