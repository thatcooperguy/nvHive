"""Marketing counts must equal the code (issue #129).

"23 providers", "63 models", "25 free", "12 cabinets" were each hand-typed in
a dozen places and had drifted three different ways within five months. Any
count of nvHive's own inventory that survives in README, docs or CLI strings
must equal the value derived from the registry it describes; the safe default
is to not state a count at all.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]

# CHANGELOG.md is history and docs/proposals/ are dated audit records; both
# quote the stale numbers on purpose.
SCANNED = [
    ROOT / "README.md",
    *sorted((ROOT / "docs").glob("*.md")),
    ROOT / "nvh" / "cli" / "main.py",
    ROOT / "nvh" / "mcp_server.py",
]

COUNT = re.compile(
    r"\b(\d+)\+?\s+(providers|models|free|cabinets|tools|personas|agents?)\b",
    re.IGNORECASE,
)

# Inventory counts are all well above this; smaller numbers describe one
# request's fan-out ("a 3-model council", "2 models for --mode multi").
FANOUT_CEILING = 5

# A provider's own quota or catalogue, quoted beside its signup link — facts
# about a third party, not about nvHive.
THIRD_PARTY_FACTS = ("free credit", "free API credit", "100+ models")


def _derived_counts() -> dict[str, int]:
    from nvh.core.agents import _PERSONA_POOL, COUNCIL_PRESETS
    from nvh.core.free_tier import FREE_TIER_ADVISORS
    from nvh.core.tools import ToolRegistry
    from nvh.providers.registry import BESPOKE_ADAPTERS
    from nvh.providers.specs import PROVIDER_SPECS

    catalog = yaml.safe_load((ROOT / "nvh" / "config" / "capabilities.yaml").read_text(encoding="utf-8"))
    models = [m for m in catalog["models"].values() if m.get("provider") != "mock"]
    library = json.loads((ROOT / "nvh" / "catalog" / "agent-library.json").read_text(encoding="utf-8"))
    providers = set(PROVIDER_SPECS) | set(BESPOKE_ADAPTERS)
    return {
        "providers": len(providers - {"mock", "triton"}),
        "models": len(models),
        "free": len(FREE_TIER_ADVISORS),
        "cabinets": len(COUNCIL_PRESETS),
        "personas": len(_PERSONA_POOL),
        "tools": len(ToolRegistry(include_system=True).list_tools()),
        "agents": len(library["profiles"]),
    }


def _claims(path: Path) -> list[tuple[int, str, int, str]]:
    """(line number, noun, number, line) for every count-shaped phrase."""
    out = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if any(fact.lower() in line.lower() for fact in THIRD_PARTY_FACTS):
            continue
        for m in COUNT.finditer(line):
            number = int(m.group(1))
            if number <= FANOUT_CEILING:
                continue
            noun = m.group(2).lower()
            noun = "agents" if noun.startswith("agent") else noun
            out.append((lineno, noun, number, line.strip()))
    return out


def test_derived_counts_are_sane():
    counts = _derived_counts()
    assert all(v > FANOUT_CEILING for v in counts.values()), counts


def test_no_hand_typed_count_disagrees_with_the_code():
    counts = _derived_counts()
    offenders = []
    for path in SCANNED:
        for lineno, noun, number, line in _claims(path):
            expected = counts[noun]
            if number != expected:
                rel = path.relative_to(ROOT).as_posix()
                offenders.append(f"{rel}:{lineno}: '{number} {noun}' but the code has {expected} — {line}")
    assert not offenders, (
        "Hand-typed counts disagree with the registries they describe. "
        "Derive the number or drop it:\n  " + "\n  ".join(offenders)
    )


def test_mcp_tool_descriptions_state_no_counts():
    src = (ROOT / "nvh" / "mcp_server.py").read_text(encoding="utf-8")
    assert not COUNT.search(src), "MCP tool descriptions must not hand-type inventory counts"
