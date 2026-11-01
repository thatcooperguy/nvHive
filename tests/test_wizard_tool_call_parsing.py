"""Tests for the Wizard chat's TOOL_CALL marker parsing + tools block."""

from __future__ import annotations

from nvh.integrations.wizard.chat import _extract_tool_calls
from nvh.integrations.wizard.personality import (
    _PRIVILEGED_OUTCOME_LINES,
    _format_tools_block,
    build_system_prompt,
)


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


def test_tools_block_includes_decision_guide_when_tools_present() -> None:
    """The decision guide is the thing that prevents 4B-class models from
    over-calling tools they don't need. If we ship the tools block we
    must ship the guide with it."""
    tools = [{
        "name": "rag_ask",
        "description": "Local index search.",
        "safety_class": "auto",
        "parameters": {"question": {}},
        "summary_template": "",
    }]
    block = _format_tools_block(tools)
    # Guide enforces "answer from live state first; pick local-vs-web correctly"
    assert "Decision guide" in block
    assert "rag_ask" in block.lower() or "local" in block.lower()
    assert "web_search" in block.lower() or "web" in block.lower()
    # And the guide is omitted when no tools — no orphan instruction block.
    assert _format_tools_block([]) == ""


# ---------------------------------------------------------------------------
# Native variant — one set of tool instructions when the provider takes tools=
# ---------------------------------------------------------------------------


def _catalog() -> list[dict]:
    return [
        {
            "name": "refresh_models", "description": "Re-query Ollama.", "safety_class": "auto",
            "parameters": {}, "summary_template": "",
        },
        {
            "name": "save_provider_key", "description": "Persist a validated API key.",
            "safety_class": "confirm", "parameters": {"provider": {}, "api_key": {}}, "summary_template": "",
        },
        {
            "name": "system_settings_apply", "description": "Apply a host setting.",
            "safety_class": "privileged", "parameters": {"setting": {}}, "summary_template": "",
        },
        {
            "name": "diagnose", "description": "Run diagnostics.", "safety_class": "auto",
            "parameters": {}, "summary_template": "",
        },
    ]


def test_format_tools_block_native_variant_is_one_short_set_of_instructions() -> None:
    """Under native calling the schemas travel as ``tools=``: the block repeats
    neither them nor the ``TOOL_CALL:`` marker, so the model is taught exactly
    one channel. It still names every tool by safety class, because the
    function spec carries no class."""
    tools = _catalog()
    text = _format_tools_block(tools)
    native = _format_tools_block(tools, native=True)

    assert _format_tools_block([], native=True) == ""
    assert native and native != text
    assert len(native) < len(text)
    # No marker, no per-tool schema bullets.
    assert "TOOL_CALL" not in native
    assert "•" not in native and "Params:" not in native
    # Names grouped by class, in class order, every tool present once.
    assert "  auto: refresh_models, diagnose" in native
    assert "  confirm: save_provider_key" in native
    assert "  privileged: system_settings_apply" in native
    assert native.index("auto:") < native.index("confirm:") < native.index("privileged:")
    # The class rules and the decision guide ship with it.
    assert "`auto` tools" in native and "`confirm` tools" in native and "`privileged` tools" in native
    assert "Decision guide" in native
    assert native.rstrip().endswith("--- end tools ---")
    # And the text variant is unchanged in what it teaches.
    assert "TOOL_CALL: {" in text and "• refresh_models [auto]" in text


def test_native_variant_names_an_unknown_safety_class_after_the_known_ones() -> None:
    tools = _catalog() + [{
        "name": "odd_tool", "description": "x", "safety_class": "weird", "parameters": {}, "summary_template": "",
    }]
    native = _format_tools_block(tools, native=True)
    assert "  weird: odd_tool" in native
    assert native.index("privileged:") < native.index("weird:")
    # A tool without a class is auto, as the text variant renders it.
    bare = _format_tools_block([{"name": "plain", "description": "x", "parameters": {}}], native=True)
    assert "  auto: plain" in bare


def test_both_variants_carry_the_same_privileged_outcome_instructions() -> None:
    """A privileged call's outcome only ever reaches the model as a TOOL_RESULT
    line on the next turn, and a ``needs a terminal:`` summary must be repeated
    verbatim. Both channels are told that in the same words."""
    tools = _catalog()
    text = _format_tools_block(tools)
    native = _format_tools_block(tools, native=True)
    for line in _PRIVILEGED_OUTCOME_LINES:
        assert line in text, line
        assert line in native, line
    for phrase in (
        "TOOL_RESULT", "needs a terminal:", "repeat that command verbatim",
        "Never ask for a password", "red card", "will NOT see the outcome this turn",
    ):
        assert phrase in text and phrase in native, phrase
    # The native variant also says where in-turn results arrive.
    assert "Results come back as TOOL_RESULT lines" in native


def test_both_variants_render_one_rules_list() -> None:
    """The Rules list is rendered once (``_tool_rules``) and only the how-to-call
    phrase differs between the channels, so a wording fix to any rule lands in
    both: every bullet of one variant equals the other's once that phrase is
    masked, and the shared bullets appear in both rendered blocks."""
    from nvh.integrations.wizard.personality import _tool_rules

    text_rules, native_rules = _tool_rules(native=False), _tool_rules(native=True)
    assert len(text_rules) == len(native_rules) == len(set(text_rules))

    def masked(line: str) -> str:
        return (
            line.replace("END with the TOOL_CALL line", "<how>")
            .replace("call the tool", "<how>")
            .replace(" plus the TOOL_CALL line.", ".")
        )

    assert [masked(line) for line in text_rules] == [masked(line) for line in native_rules]
    assert "TOOL_CALL" in "\n".join(text_rules) and "TOOL_CALL" not in "\n".join(native_rules)
    tools = _catalog()
    text, native = _format_tools_block(tools), _format_tools_block(tools, native=True)
    for bullet in (
        "Only call a tool the user clearly asked for. Don't surprise them.",
        "Refreshing models now.", "The system runs the tool immediately.",
        "The UI shows a confirmation button", "explain what will change and why",
        "Never invent a tool. If nothing fits, answer in plain text.",
    ):
        assert bullet in text and bullet in native, bullet
    for line in text_rules:
        assert line in text, line
    for line in native_rules:
        assert line in native, line


def test_build_system_prompt_native_flag_selects_the_variant() -> None:
    tools = _catalog()
    text_prompt = build_system_prompt({}, tools=tools)
    native_prompt = build_system_prompt({}, tools=tools, native=True)
    assert "TOOL_CALL" in text_prompt
    assert "TOOL_CALL" not in native_prompt
    assert "TOOL_RESULT" in native_prompt and "privileged: system_settings_apply" in native_prompt
    # Everything outside the tools block is the same prompt.
    assert text_prompt.split("--- Available actions")[0] == native_prompt.split("--- Available actions")[0]
    # No tools: no block on either channel.
    assert build_system_prompt({}, native=True) == build_system_prompt({})
