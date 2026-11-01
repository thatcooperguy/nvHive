"""Tests for MCP server module — import, helper functions and the dual-SDK shim.

``create_server()`` binds mcp 2.x's ``mcp.server.mcpserver.MCPServer`` first
and 1.x's ``mcp.server.fastmcp.FastMCP`` second, then probes the constructor
keywords (``version=`` is 2.x only) with try/except TypeError. The fakes below
pin that order without any SDK installed; the installed-SDK tests cover
whichever major the venv resolves.
"""

import sys
import types

import pytest

import nvh
from nvh.mcp_server import create_server


class _Recorder:
    """Shared shape of the fakes: records constructor kwargs, no-op ``@tool()``."""

    def __init__(self, name, **kwargs):
        self.name = name
        self.kwargs = kwargs

    def tool(self, *args, **kwargs):
        return lambda fn: fn


class _V2Server(_Recorder):
    """mcp 2.x MCPServer: ``instructions=`` and ``version=``."""

    def __init__(self, name, *, instructions=None, version=""):
        super().__init__(name, instructions=instructions, version=version)


class _V1Server(_Recorder):
    """mcp 1.27+ FastMCP: ``instructions=`` only (TypeError on ``version=``)."""

    def __init__(self, name, *, instructions=None):
        super().__init__(name, instructions=instructions)


class _OldV1Server(_Recorder):
    """pre-1.27 FastMCP: ``description=`` only."""

    def __init__(self, name, *, description=None):
        super().__init__(name, description=description)


def _install_fake_sdk(monkeypatch, *, mcpserver=None, fastmcp=None) -> None:
    """Bind the two server-class import paths to fakes; ``None`` makes that
    import raise ImportError (``None`` in sys.modules), i.e. "module absent"."""
    for mod_name, attr, cls in (
        ("mcp.server.mcpserver", "MCPServer", mcpserver),
        ("mcp.server.fastmcp", "FastMCP", fastmcp),
    ):
        if cls is None:
            monkeypatch.setitem(sys.modules, mod_name, None)
        else:
            mod = types.ModuleType(mod_name)
            setattr(mod, attr, cls)
            monkeypatch.setitem(sys.modules, mod_name, mod)


def test_create_server_prefers_mcp_2_and_advertises_the_package_version(monkeypatch):
    _install_fake_sdk(monkeypatch, mcpserver=_V2Server, fastmcp=None)
    server = create_server()
    assert isinstance(server, _V2Server)
    assert server.name == "nvhive"
    assert server.kwargs["version"] == nvh.__version__
    assert "NVHive" in server.kwargs["instructions"]


def test_create_server_falls_back_to_fastmcp_without_version(monkeypatch):
    _install_fake_sdk(monkeypatch, mcpserver=None, fastmcp=_V1Server)
    server = create_server()
    assert isinstance(server, _V1Server)
    assert "version" not in server.kwargs
    assert "NVHive" in server.kwargs["instructions"]


def test_create_server_falls_back_to_description_on_old_fastmcp(monkeypatch):
    _install_fake_sdk(monkeypatch, mcpserver=None, fastmcp=_OldV1Server)
    server = create_server()
    assert isinstance(server, _OldV1Server)
    assert set(server.kwargs) == {"description"}
    assert "NVHive" in server.kwargs["description"]


def test_create_server_without_any_sdk_raises_the_install_hint(monkeypatch):
    _install_fake_sdk(monkeypatch, mcpserver=None, fastmcp=None)
    with pytest.raises(ImportError, match="MCP SDK not installed"):
        create_server()


def test_create_server_on_the_installed_sdk_sets_instructions_and_version():
    pytest.importorskip("mcp", reason="MCP SDK not installed")
    server = create_server()
    assert "NVHive" in (server.instructions or "")
    # mcp 2.x exposes the advertised version; 1.x has no such attribute and
    # must not have been handed the keyword (the TypeError fallback did its job).
    if hasattr(server, "version"):
        assert server.version == nvh.__version__


def test_mcp_server_format_response():
    """Test the response formatter handles CompletionResponse-like objects."""
    from nvh.mcp_server import _format_response

    class MockUsage:
        total_tokens = 100

    class MockResp:
        content = "Hello, world!"
        provider = "openai"
        model = "gpt-4o"
        usage = MockUsage()
        cost_usd = 0.001
        latency_ms = 250

    result = _format_response(MockResp())
    assert "Hello, world!" in result
    assert "openai" in result
    assert "gpt-4o" in result
    assert "100" in result


def test_mcp_server_format_council_response():
    """Test the council response formatter."""
    from nvh.mcp_server import _format_council_response

    class MockSynthesis:
        content = "Synthesized answer"

    class MockResp:
        content = "Individual response"

    class MockResult:
        synthesis = MockSynthesis()
        member_responses = {"openai": MockResp(), "groq": MockResp()}
        strategy = "weighted_consensus"
        total_cost_usd = 0.005
        total_latency_ms = 1200
        agents_used = ["analyst", "engineer"]

    result = _format_council_response(MockResult())
    assert "Synthesized answer" in result
    assert "Individual Responses" in result
    assert "openai" in result
    assert "weighted_consensus" in result


def test_mcp_server_create_server_without_sdk():
    """Creating the server without mcp installed should raise ImportError."""

    # If mcp is actually installed, skip
    try:
        import mcp  # noqa: F401
        pytest.skip("mcp SDK is installed — cannot test missing-SDK path")
    except ImportError:
        pass

    from nvh.mcp_server import create_server
    with pytest.raises(ImportError, match="MCP SDK not installed"):
        create_server()


def test_mcp_server_create():
    pytest.importorskip("mcp", reason="MCP SDK not installed")
    from nvh.mcp_server import create_server
    server = create_server()
    assert server is not None


def test_mcp_server_import():
    try:
        from nvh import mcp_server
        assert hasattr(mcp_server, "main") or hasattr(mcp_server, "serve")
    except ImportError:
        pytest.skip("MCP dependencies not installed")


def test_mcp_server_has_main():
    try:
        from nvh import mcp_server
        assert hasattr(mcp_server, "main") or hasattr(mcp_server, "serve")
    except ImportError:
        pytest.skip("MCP dependencies not installed")


def test_mcp_server_import_optional_sdk():
    try:
        from nvh import mcp_server
        assert hasattr(mcp_server, "main") or hasattr(mcp_server, "serve")
    except ImportError:
        pytest.skip("MCP dependencies not installed")
