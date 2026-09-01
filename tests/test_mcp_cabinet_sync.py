"""Drift guard: the MCP-visible cabinet set must stay in lockstep with
nvh.core.agents.COUNCIL_PRESETS.

mcp_server.py used to hardcode its own cabinet set, which silently dropped
presets added to agents.py (product_resilience). These tests fail if either
side changes without the other.
"""

import pytest

from nvh.core.agents import COUNCIL_PRESETS
from nvh.mcp_server import _valid_cabinets


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
    text = str(result)

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
    text = str(result)

    assert "Invalid cabinet" in text
    for preset in COUNCIL_PRESETS:
        assert preset in text
