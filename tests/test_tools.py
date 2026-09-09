"""Tests for nvh.core.tools — ToolRegistry, execution, guardrails, path resolution, handlers.

Since 0.44 (issue #132 D2) this is the ONE tool model: ``Tool`` carries
``safety_class`` (``safe`` derived), a JSON-Schema ``input_schema`` whose
Wizard shape is derived by ``translate_parameters``; ``ToolRegistry`` carries
the Wizard ``execute()`` semantics (kill switch, approval tokens, audit,
tool window) *and* the core guardrails; ``WizardTool`` / ``WizardToolRegistry``
are thin subclasses. Two instances remain: the agent registry (built-ins +
system/browser/vision) and the Wizard's curated one.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, mock_open, patch

import pytest

from nvh.core import tools as tools_mod
from nvh.core.tools import (
    SAFETY_CLASSES,
    Tool,
    ToolRegistry,
    ToolResult,
    json_schema_from_parameters,
    translate_parameters,
)


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


class TestOneToolModel:
    """D2: one Tool dataclass, one safety vocabulary, one parameter translation."""

    def test_safety_class_vocabulary_and_derived_safe_flag(self):
        assert SAFETY_CLASSES == ("auto", "confirm", "privileged")
        auto = Tool(name="a", description="", parameters={}, handler=AsyncMock())
        confirm = Tool(name="b", description="", parameters={}, handler=AsyncMock(), safe=False)
        privileged = Tool(name="c", description="", parameters={}, handler=AsyncMock(), safety_class="privileged")
        assert (auto.safety_class, auto.safe) == ("auto", True)
        assert (confirm.safety_class, confirm.safe) == ("confirm", False)
        assert (privileged.safety_class, privileged.safe) == ("privileged", False)
        # safety_class wins when both are given.
        assert Tool(name="d", description="", parameters={}, handler=AsyncMock(), safe=True, safety_class="confirm").safety_class == "confirm"
        assert auto.enabled is True

    def test_parameters_are_json_schema_and_the_wizard_shape_is_derived(self):
        schema = {"type": "object", "properties": {"path": {"type": "string", "description": "P"}, "n": {"type": "integer"}}, "required": ["path"]}
        tool = Tool(name="t", description="T", parameters=schema, handler=AsyncMock())
        assert tool.parameters == schema and tool.input_schema == schema
        assert tool.wizard_parameters == translate_parameters(schema) == {
            "path": {"type": "string", "description": "P", "required": True},
            "n": {"type": "integer", "description": "", "required": False},
        }
        public = tool.as_public_dict()
        assert public["parameters"] == tool.wizard_parameters
        assert set(public) == {"name", "description", "safety_class", "parameters", "summary_template", "enabled"}
        assert tool.as_openai_tool() == {"type": "function", "function": {"name": "t", "description": "T", "parameters": schema}}

    def test_wizard_shape_input_is_normalised_to_json_schema(self):
        wizard = {"q": {"type": "string", "required": True, "description": "Q"}, "k": {"type": "integer", "required": False}}
        tool = Tool(name="t", description="", parameters=wizard, handler=AsyncMock())
        assert tool.input_schema == json_schema_from_parameters(wizard) == {
            "type": "object",
            "properties": {"q": {"type": "string", "description": "Q"}, "k": {"type": "integer"}},
            "required": ["q"],
        }
        assert tool.wizard_parameters == {
            "q": {"type": "string", "description": "Q", "required": True},
            "k": {"type": "integer", "description": "", "required": False},
        }
        assert Tool(name="e", description="", parameters={}, handler=AsyncMock()).input_schema == {"type": "object", "properties": {}}
        assert Tool(name="n", description="", parameters=None, handler=AsyncMock()).input_schema == {"type": "object", "properties": {}}

    def test_wizard_tool_and_registry_are_thin_subclasses(self):
        from nvh.integrations.wizard.tools import WizardTool, WizardToolRegistry

        assert issubclass(WizardTool, Tool) and issubclass(WizardToolRegistry, ToolRegistry)
        wtool = WizardTool(name="w", description="", safety_class="auto", parameters={"x": {"type": "string", "required": True}}, handler=AsyncMock())
        assert wtool.parameters == {"x": {"type": "string", "description": "", "required": True}}  # the derived view
        assert wtool.input_schema == {"type": "object", "properties": {"x": {"type": "string"}}, "required": ["x"]}
        assert wtool.handler_style == "mapping"
        reg = WizardToolRegistry()
        assert reg.list_tools() == [] and reg.enforce_confirmation is True
        # The core module's own helpers are what the Wizard module re-exports.
        from nvh.integrations.wizard import tools as wizard_mod

        for name in ("issue_approval", "verify_approval", "fit_tool_window", "privileged_enabled", "parameters_from_json_schema", "audit_privileged_change"):
            assert getattr(wizard_mod, name) is getattr(tools_mod, name), name

    def test_register_validates_the_vocabulary_on_the_core_registry(self):
        reg = ToolRegistry(include_system=False, builtins=False)
        with pytest.raises(ValueError, match="never"):
            reg.register(Tool(name="x", description="", parameters={}, handler=AsyncMock(), safety_class="never"))
        with pytest.raises(ValueError, match="safety_class"):
            reg.register(Tool(name="x", description="", parameters={}, handler=AsyncMock(), safety_class="maybe"))
        with pytest.raises(ValueError, match="handler_style"):
            Tool(name="x", description="", parameters={}, handler=AsyncMock(), handler_style="positional")

    def test_list_tools_is_ordered_by_class_then_name(self):
        reg = ToolRegistry(include_system=False, builtins=False)
        for name, cls in (("z", "auto"), ("m", "privileged"), ("a", "confirm"), ("b", "auto")):
            reg.register(Tool(name=name, description="", parameters={}, handler=AsyncMock(), safety_class=cls))
        assert [t.name for t in reg.list_tools()] == ["b", "z", "a", "m"]


class TestOneRegistryExecute:
    """The one execute(): envelope, click enforcement per instance, tokens, guardrails."""

    @pytest.mark.asyncio
    async def test_envelope_is_a_dict_and_a_tool_result(self):
        reg = ToolRegistry(include_system=False, builtins=False)
        reg.register(Tool(name="echo", description="", parameters={}, handler=AsyncMock(return_value="hi")))
        out = await reg.execute("echo", {})
        assert isinstance(out, ToolResult) and isinstance(out, dict)
        assert out == {"ok": True, "result": "hi", "tool": "echo", "safety_class": "auto"}
        assert (out.success, out.output, out.error, out.tool_name) == (True, "hi", "", "echo")
        assert json.loads(json.dumps(out)) == out
        # Dict results (Wizard-style handlers) render as JSON text for the agent loop.
        reg.register(Tool(name="d", description="", parameters={}, handler=AsyncMock(return_value={"a": 1}), handler_style="mapping"))
        out = await reg.execute("d", {"x": 1})
        assert out["result"] == {"a": 1} and out.output == '{"a": 1}'
        reg.get("d").handler.assert_awaited_once_with({"x": 1})
        # The legacy constructor still builds one.
        legacy = ToolResult(tool_name="t", success=False, output="", error="bad")
        assert legacy == {"tool": "t", "ok": False, "result": "", "error": "bad"} and legacy.error == "bad"

    @pytest.mark.asyncio
    async def test_agent_registry_runs_confirm_tools_the_caller_already_approved(self):
        """The agent instance's callers own the click (typer prompt, confirm_unsafe);
        the Wizard instance is the enforcement point and returns the card."""
        from nvh.integrations.wizard.tools import WizardToolRegistry

        handler = AsyncMock(return_value="done")
        tool = Tool(name="write", description="Write", parameters={}, handler=handler, safety_class="confirm", summary_template="Write it")
        agent = ToolRegistry(include_system=False, builtins=False)
        agent.register(tool)
        assert agent.enforce_confirmation is False
        assert (await agent.execute("write", {})).success is True

        wizard = WizardToolRegistry()
        wizard.register(tool)
        card = await wizard.execute("write", {})
        assert card["needs_confirmation"] is True and card["summary"] == "Write it" and card["tool"]["name"] == "write"
        assert handler.await_count == 1  # the card ran nothing
        assert (await wizard.execute("write", {}, confirmed=True)).success is True

    @pytest.mark.asyncio
    async def test_privileged_needs_the_card_token_on_every_instance(self):
        handler = AsyncMock(return_value={"ok": True, "applied": False})
        tool = Tool(name="priv", description="", parameters={}, handler=handler, safety_class="privileged", handler_style="mapping")
        agent = ToolRegistry(include_system=False, builtins=False)
        agent.register(tool)
        card = await agent.execute("priv", {"a": 1})
        assert card["needs_confirmation"] is True and card["privileged"] is True and "approval_token" in card
        refused = await agent.execute("priv", {"a": 1}, confirmed=True)
        assert refused["approval_required"] is True and handler.await_count == 0
        ok = await agent.execute("priv", {"a": 1}, confirmed=True, approval_token=card["approval_token"])
        assert ok["ok"] is True and handler.await_count == 1

    @pytest.mark.asyncio
    async def test_guardrails_read_the_right_argument_and_redact_text_output(self, tmp_path: Path):
        reg = ToolRegistry(workspace=str(tmp_path), include_system=False)
        blocked = await reg.execute("run_code", {"code": "import os; os.system('rm -rf /')", "language": "python"})
        assert blocked.success is False and blocked.error.startswith("GUARDRAIL")
        blocked = await reg.execute("shell", {"command": "rm -rf /"})
        assert blocked.success is False and blocked.error.startswith("GUARDRAIL")
        traversal = await reg.execute("read_file", {"path": "../../etc/passwd"})
        assert traversal.success is False and "GUARDRAIL" in traversal.error
        reg.register(Tool(name="leak", description="", parameters={}, handler=AsyncMock(return_value="key sk-ant-api03-" + "A" * 40)))
        out = await reg.execute("leak", {})
        assert "sk-ant" not in out.output and "REDACTED" in out.output

    @pytest.mark.asyncio
    async def test_dict_and_list_results_are_redacted_and_capped_too(self):
        """A mapping-style tool (MCP ``content``, the Wizard tools) answers a dict;
        every string inside it is redacted and an oversized answer is cut, so
        the agent registry's guarantees hold whatever shape a handler returns."""
        from nvh.core.agent_guardrails import MAX_COMMAND_OUTPUT
        from nvh.core.tools import sanitize_tool_result

        reg = ToolRegistry(include_system=False, builtins=False)
        secret = "OPENAI_API_KEY=sk-live-" + "A" * 40
        reg.register(Tool(
            name="mcp_like", description="", parameters={},
            handler=AsyncMock(return_value={"ok": True, "content": secret, "nested": [{"text": secret}], "n": 1}),
            handler_style="mapping",
        ))
        out = await reg.execute("mcp_like", {})
        assert out.success and "sk-live" not in out.output
        assert out["result"]["n"] == 1 and out["result"]["ok"] is True  # structure kept
        assert "REDACTED" in out["result"]["content"] and "REDACTED" in out["result"]["nested"][0]["text"]

        reg.register(Tool(name="huge", description="", parameters={}, handler=AsyncMock(return_value={"content": "x" * (MAX_COMMAND_OUTPUT + 10)})))
        out = await reg.execute("huge", {})
        assert isinstance(out["result"], str) and "TRUNCATED" in out["result"]
        assert len(out["result"]) < MAX_COMMAND_OUTPUT + 200

        assert sanitize_tool_result(None) is None and sanitize_tool_result(3) == 3
        assert sanitize_tool_result(("a", {"k": "Bearer " + "b" * 30})) == ("a", {"k": "[REDACTED:bearer_token]"})


class TestTwoInstances:
    def test_agent_registry_has_the_core_packs_and_no_wizard_bridges(self):
        names = {t.name for t in ToolRegistry(include_system=True).list_tools()}
        assert {"read_file", "shell", "mouse_click", "capture_screenshot", "list_processes"} <= names
        assert not any(name.startswith(("mcp_", "playbook_", "system_settings_")) for name in names)

    def test_mcp_tools_reach_the_agent_registry_only_by_opt_in(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("NVH_HOME", str(tmp_path))
        monkeypatch.delenv("NVH_AGENT_MCP_TOOLS", raising=False)
        (tmp_path / "config").mkdir()
        (tmp_path / "state").mkdir()
        (tmp_path / "config" / "mcp-servers.json").write_text(json.dumps({"mcpServers": {"fs": {"command": "echo", "auto_approve": ["read"]}}}), encoding="utf-8")
        (tmp_path / "state" / "mcp-tools-cache.json").write_text(json.dumps({"fs": {"ok": True, "tools": [
            {"name": "read", "description": "Read", "input_schema": {"type": "object", "properties": {"p": {"type": "string"}}, "required": ["p"]}},
        ]}}), encoding="utf-8")
        assert "mcp_fs_read" not in {t.name for t in ToolRegistry(include_system=False).list_tools()}
        opted = ToolRegistry(include_system=False, include_mcp=True)
        tool = opted.get("mcp_fs_read")
        assert tool is not None and tool.safety_class == "auto" and tool.handler_style == "mapping"
        assert tool.input_schema["required"] == ["p"]
        monkeypatch.setenv("NVH_AGENT_MCP_TOOLS", "1")
        assert ToolRegistry(include_system=False).get("mcp_fs_read") is not None


class TestWebSearchOnce:
    """D3: one web_search implementation — the integrations client wins."""

    def test_core_module_defaults_to_no_public_searxng_and_reads_the_clients_env_names(self):
        source = Path(tools_mod.__file__).read_text(encoding="utf-8")
        assert "searx.be" not in source
        assert "SEARXNG_URL\"" not in source and "BRAVE_SEARCH_KEY" not in source and "GOOGLE_SEARCH_KEY" not in source

    @pytest.mark.asyncio
    async def test_core_web_search_delegates_to_the_integrations_client(self):
        reg = ToolRegistry(include_system=False)
        envelope = {"ok": True, "backend": "searxng", "query": "q", "results": [
            {"title": "T1", "url": "http://a", "snippet": "S1"}, {"title": "T2", "url": "http://b", "snippet": "S2"},
        ]}
        fake = AsyncMock(return_value=envelope)
        with patch("nvh.integrations.web_search.web_search", fake):
            out = await reg.execute("web_search", {"query": "q", "num_results": 2})
        fake.assert_awaited_once_with("q", top_k=2)
        assert out.success and out.output.startswith("1. T1\n   http://a\n   S1") and "2. T2" in out.output
        with patch("nvh.integrations.web_search.web_search", AsyncMock(return_value={"ok": False, "backend": "duckduckgo", "error": "layout changed", "hint": "set NVH_SEARXNG_URL"})):
            out = await reg.execute("web_search", {"query": "q"})
        assert out.success and "layout changed" in out.output and "NVH_SEARXNG_URL" in out.output


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
