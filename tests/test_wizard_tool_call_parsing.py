"""Tests for the Wizard chat's TOOL_CALL marker parsing + tools block."""

from __future__ import annotations

from nvh.integrations.wizard.chat import _extract_tool_calls
from nvh.integrations.wizard.personality import _format_tools_block, build_system_prompt


def test_extract_no_tool_call_returns_text_untouched() -> None:
    text = "Welcome back. Everything looks healthy."
    cleaned, calls = _extract_tool_calls(text)
    assert cleaned == text
    assert calls == []


def test_extract_simple_tool_call_with_empty_args() -> None:
    text = 'Refreshing models now.\nTOOL_CALL: {"name": "refresh_models", "arguments": {}}'
    cleaned, calls = _extract_tool_calls(text)
    assert "TOOL_CALL" not in cleaned
    assert cleaned == "Refreshing models now."
    assert len(calls) == 1
    assert calls[0]["name"] == "refresh_models"
    assert calls[0]["arguments"] == {}


def test_extract_tool_call_with_arguments() -> None:
    text = (
        "Saving your Groq key once you confirm.\n"
        'TOOL_CALL: {"name": "save_provider_key", "arguments": {"provider": "groq", "api_key": "gsk_..."}}'
    )
    cleaned, calls = _extract_tool_calls(text)
    assert "TOOL_CALL" not in cleaned
    assert "Saving your Groq key" in cleaned
    assert calls[0]["name"] == "save_provider_key"
    assert calls[0]["arguments"]["provider"] == "groq"


def test_extract_multiple_tool_calls() -> None:
    text = (
        "Two things at once.\n"
        'TOOL_CALL: {"name": "refresh_models", "arguments": {}}\n'
        'TOOL_CALL: {"name": "repair_workspace", "arguments": {"home_dir": "/persist"}}'
    )
    cleaned, calls = _extract_tool_calls(text)
    assert len(calls) == 2
    assert {c["name"] for c in calls} == {"refresh_models", "repair_workspace"}
    assert calls[1]["arguments"]["home_dir"] == "/persist"


def test_extract_malformed_json_is_dropped() -> None:
    """A malformed TOOL_CALL line should not crash the chat — just drop it."""
    text = (
        "All good.\n"
        'TOOL_CALL: {"name": "refresh_models", "arguments": {oops_not_json}}'
    )
    cleaned, calls = _extract_tool_calls(text)
    # The marker is stripped even when the JSON is bad so the user doesn't
    # see a raw TOOL_CALL line in their answer.
    assert "TOOL_CALL" not in cleaned
    assert calls == []


def test_extract_ignores_missing_name() -> None:
    """JSON without a name field is not a valid tool call."""
    text = 'Sure.\nTOOL_CALL: {"arguments": {"foo": "bar"}}'
    _cleaned, calls = _extract_tool_calls(text)
    assert calls == []


def test_format_tools_block_empty_returns_empty_string() -> None:
    """No tools available → no block, so the rest of the prompt isn't affected."""
    assert _format_tools_block([]) == ""


def test_format_tools_block_includes_safety_class_and_params() -> None:
    tools = [
        {
            "name": "refresh_models",
            "description": "Re-query Ollama.",
            "safety_class": "auto",
            "parameters": {},
            "summary_template": "",
        },
        {
            "name": "save_provider_key",
            "description": "Persist a validated API key.",
            "safety_class": "confirm",
            "parameters": {"provider": {}, "api_key": {}},
            "summary_template": "",
        },
    ]
    block = _format_tools_block(tools)
    assert "TOOL_CALL" in block
    assert "[auto]" in block
    assert "[confirm]" in block
    assert "refresh_models" in block
    assert "save_provider_key" in block
    # Both params listed for save_provider_key
    assert "api_key" in block
    assert "provider" in block


def test_build_system_prompt_appends_tools_block_when_provided() -> None:
    tools = [{
        "name": "refresh_models",
        "description": "Re-query Ollama.",
        "safety_class": "auto",
        "parameters": {},
        "summary_template": "",
    }]
    with_tools = build_system_prompt({}, tools=tools)
    without_tools = build_system_prompt({})
    assert "refresh_models" in with_tools
    assert "refresh_models" not in without_tools
    assert "TOOL_CALL" in with_tools
    assert "TOOL_CALL" not in without_tools
