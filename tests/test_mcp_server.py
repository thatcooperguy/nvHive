"""Tests for MCP server module — import and helper functions."""

import pytest


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
