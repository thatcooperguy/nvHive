"""Drift guard: the MCP-visible cabinet set must stay in lockstep with
nvh.core.agents.COUNCIL_PRESETS.

mcp_server.py used to hardcode its own cabinet set, which silently dropped
presets added to agents.py (product_resilience). These tests fail if either
side changes without the other.
"""

from types import SimpleNamespace

import pytest

from nvh.core.agents import COUNCIL_PRESETS
from nvh.mcp_server import _valid_cabinets


def _result_text(result) -> str:
    """Text of a server-side ``call_tool`` result on either mcp SDK major.

    mcp 2.x returns a ``CallToolResult`` (``.content`` blocks); 1.x returns the
    content sequence itself, or a ``(content, structured)`` tuple when the tool
    has an output schema. ``str(result)`` happened to contain the text on 1.x
    and stops doing so whenever the repr changes, so the text blocks are read
    explicitly.
    """
    content = getattr(result, "content", None)
    if content is None:
        content = result[0] if isinstance(result, tuple) else result
    return "\n".join(
        block.text for block in content if getattr(block, "type", None) == "text"
    )


def test_result_text_reads_both_sdk_shapes():
    block = SimpleNamespace(type="text", text="| a |")
    other = SimpleNamespace(type="image", data="...")
    assert _result_text(SimpleNamespace(content=[block, other])) == "| a |"  # 2.x
    assert _result_text(([block], {"result": "| a |"})) == "| a |"  # 1.10+ tuple
    assert _result_text([block]) == "| a |"  # pre-1.10 sequence


def test_valid_cabinets_matches_council_presets_exactly():
    assert _valid_cabinets() == set(COUNCIL_PRESETS)


def test_product_resilience_is_mcp_visible():
    assert "product_resilience" in _valid_cabinets()


def test_preset_descriptions_cover_every_preset():
    # Descriptions live beside the registry so listings can't half-drift:
    # every preset must have a curated blurb, and no blurb may outlive its
    # preset (a renamed preset keeping a stale description via exact-key
    # match was the failure mode of the old hardcoded dict).
    from nvh.core.agents import PRESET_DESCRIPTIONS

    assert set(PRESET_DESCRIPTIONS) == set(COUNCIL_PRESETS)
    assert all(PRESET_DESCRIPTIONS[name].strip() for name in COUNCIL_PRESETS)


async def test_list_cabinets_tool_covers_every_preset():
    pytest.importorskip("mcp")
    from nvh.mcp_server import create_server

    server = create_server()
    result = await server.call_tool("list_cabinets", {})
    text = _result_text(result)

    for preset in COUNCIL_PRESETS:
        assert f"| {preset} |" in text
    # Fallback description joins persona roles, so even a preset missing
    # from cabinet_descriptions must not render an empty cell.
    assert "|  |" not in text


async def test_council_rejects_unknown_cabinet_and_lists_all_presets():
    pytest.importorskip("mcp")
    from nvh.mcp_server import create_server

    server = create_server()
    result = await server.call_tool(
        "council", {"prompt": "hi", "cabinet": "not_a_cabinet"}
    )
    text = _result_text(result)

    assert "Invalid cabinet" in text
    for preset in COUNCIL_PRESETS:
        assert preset in text
