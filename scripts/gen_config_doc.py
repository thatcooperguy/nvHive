#!/usr/bin/env python
"""Regenerate the derived rosters in docs/CONFIGURATION.md from the code that owns them.

    python scripts/gen_config_doc.py          # rewrite the generated blocks
    python scripts/gen_config_doc.py --check  # exit 1 when a block is stale

Five blocks sit between ``<!-- BEGIN GENERATED: <name> -->`` and
``<!-- END GENERATED: <name> -->`` markers; the prose around them is
hand-written and left alone:

* ``wizard-tools`` -- the AI Wizard's stock tool roster by safety class:
  ``nvh.integrations.wizard.tools.default_registry()`` built with its three
  discovery passes switched off (``nvh.wizard_tools`` entry points, the
  ``.py`` plugin directory, cached MCP servers), so the table is the same on
  every machine. One row per tool in ``list_tools()`` order (``auto``, then
  ``confirm``, then ``privileged``; names sorted within a class) with the
  first sentence of the tool's own description.
* ``council-cabinets`` -- ``nvh.core.agents.COUNCIL_PRESETS`` in table order
  with each cabinet's members (the persona roles) and the summary
  ``PRESET_DESCRIPTIONS`` gives the MCP ``list_cabinets`` tool.
* ``memory-record`` -- the ``nvh-memory/1`` vault note schema: one row per
  field of ``nvh.integrations.workspace.vault.MemoryRecord`` in ``fields()``
  order, with the field's type and its ``metadata["doc"]`` sentence.
* ``schedule-record`` -- the ``nvh-schedules/2`` record schema: one row per
  field of ``nvh.core.scheduler.ScheduledTask`` with its type, the JSON
  spelling of its default (``—`` for the required pre-0.46 fields) and its
  ``metadata["doc"]`` sentence.
* ``wizard-meta`` -- the keys of the ``<!-- wizard-meta: {...} -->`` tail the
  Wizard chat appends to the assistant rows it persists: one row per entry
  of ``nvh.integrations.wizard.chat.WIZARD_META_KEYS`` (key, one-line doc).

The renderers import their sources lazily, so this module itself imports
without the Wizard or API stack. tests/test_config_doc_parity.py runs the
--check form, so a Wizard tool added, reclassified or re-described, a
cabinet changed, a record field added or re-described, or a meta key
changed fails CI instead of drifting in the doc.
"""

from __future__ import annotations

import dataclasses
import json
import re
import sys
from collections.abc import Callable
from contextlib import ExitStack
from pathlib import Path
from typing import Any
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
DOC_PATH = ROOT / "docs" / "CONFIGURATION.md"
SCRIPT = "scripts/gen_config_doc.py"

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _gendoc  # noqa: E402 — the marker plumbing shared with gen_models_doc.py
from _gendoc import BEGIN, END  # noqa: E402,F401
from _gendoc import code as _code  # noqa: E402
from _gendoc import table as _table  # noqa: E402

# A sentence ends at . ! or ? followed by whitespace and the start of the
# next sentence (a capital, a code span, a quote or a bracket) -- so
# "e.g. light.turn_on" and "{"brightness_pct": 40}. Changes" split correctly.
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=[A-Z`\"'(\[])")


def _cell(text: str) -> str:
    """One table cell: whitespace collapsed, pipes escaped."""
    return " ".join(text.split()).replace("|", "\\|")


def first_sentence(text: str) -> str:
    """The first sentence of a tool description, whitespace collapsed."""
    flat = " ".join(text.split())
    return _SENTENCE_END.split(flat, 1)[0]


# --- the stock Wizard registry ----------------------------------------------


def _skip(reg, *args, **kwargs) -> None:
    return None


def stock_registry():
    """``default_registry()`` with every discovery pass off: nvHive's stock tools only.

    Packaged entry points, ``$NVH_WIZARD_PLUGIN_DIR`` / ``$NVH_HOME/plugins``
    files and the ``mcp_<server>_<tool>`` cache depend on the machine the
    generator runs on; the doc lists what ships. The patches are undone on
    return, so a caller's registry build is unaffected.
    """
    sys.path.insert(0, str(ROOT))
    import nvh.integrations.mcp_client as mcp_client
    import nvh.integrations.wizard.tools as wizard_tools

    with ExitStack() as stack:
        stack.enter_context(mock.patch.object(wizard_tools, "_load_entry_point_tools", _skip))
        stack.enter_context(mock.patch.object(wizard_tools, "_load_workspace_plugin_tools", _skip))
        stack.enter_context(mock.patch.object(mcp_client, "register_mcp_tools", _skip))
        return wizard_tools.default_registry()


# --- renderers ---------------------------------------------------------------


def render_wizard_tools() -> str:
    rows = [
        [_code(tool.name), tool.safety_class, _cell(first_sentence(tool.description))]
        for tool in stock_registry().list_tools()
    ]
    return _table(["Tool", "Class", "What it does"], rows)


def render_council_cabinets() -> str:
    sys.path.insert(0, str(ROOT))
    from nvh.core.agents import COUNCIL_PRESETS, PRESET_DESCRIPTIONS

    rows = [
        [
            _code(name),
            _cell(", ".join(template.role for template in templates)),
            _cell(PRESET_DESCRIPTIONS.get(name, "")),
        ]
        for name, templates in COUNCIL_PRESETS.items()
    ]
    return _table(["Cabinet", "Members", "Summary"], rows)


# --- record schemas (derive, don't type: the dataclass is the schema) --------


def _type_name(annotation: Any) -> str:
    """A field's annotation as the source spells it: ``str``, ``float | None``, ``dict[str, Any]``.

    Under ``from __future__ import annotations`` ``fields()`` hands back the
    string itself; otherwise a plain class prints its name and a union or a
    generic alias its ``str()`` with the ``typing.`` prefix dropped.
    """
    if isinstance(annotation, str):
        return annotation
    if isinstance(annotation, type):
        return annotation.__name__
    return str(annotation).replace("typing.", "")


def _default_cell(f: dataclasses.Field) -> str:
    """The JSON spelling of a field's default (``""``, ``null``, ``true``, ``0``, ``{}``); ``—`` when required."""
    if f.default is not dataclasses.MISSING:
        return _cell(_code(json.dumps(f.default)))
    if f.default_factory is not dataclasses.MISSING:
        return _cell(_code(json.dumps(f.default_factory())))
    return "—"


def _doc_cell(f: dataclasses.Field) -> str:
    return _cell(f.metadata["doc"])


def render_memory_record() -> str:
    sys.path.insert(0, str(ROOT))
    from nvh.integrations.workspace.vault import MemoryRecord

    rows = [
        [_code(f.name), _cell(_code(_type_name(f.type))), _doc_cell(f)]
        for f in dataclasses.fields(MemoryRecord)
    ]
    return _table(["Field", "Type", "Meaning"], rows)


def render_schedule_record() -> str:
    sys.path.insert(0, str(ROOT))
    from nvh.core.scheduler import ScheduledTask

    rows = [
        [_code(f.name), _cell(_code(_type_name(f.type))), _default_cell(f), _doc_cell(f)]
        for f in dataclasses.fields(ScheduledTask)
    ]
    return _table(["Field", "Type", "Default", "Meaning"], rows)


def render_wizard_meta() -> str:
    sys.path.insert(0, str(ROOT))
    from nvh.integrations.wizard.chat import WIZARD_META_KEYS

    rows = [[_code(key), _cell(doc)] for key, doc in WIZARD_META_KEYS]
    return _table(["Key", "Meaning"], rows)


BLOCKS: dict[str, Callable[[], str]] = {
    "wizard-tools": render_wizard_tools,
    "council-cabinets": render_council_cabinets,
    "memory-record": render_memory_record,
    "schedule-record": render_schedule_record,
    "wizard-meta": render_wizard_meta,
}


# --- marker plumbing (scripts/_gendoc.py, shared with the other generators) --


def block_text(text: str, name: str) -> str:
    """The generated body currently between a block's markers."""
    return _gendoc.block_text(text, name, "docs/CONFIGURATION.md")


def render(text: str) -> str:
    """``text`` with every generated block replaced by its renderer's output."""
    return _gendoc.render(text, BLOCKS, "docs/CONFIGURATION.md")


def main(argv: list[str] | None = None) -> int:
    return _gendoc.main(argv, doc_path=DOC_PATH, root=ROOT, blocks=BLOCKS, script=SCRIPT)


if __name__ == "__main__":
    raise SystemExit(main())
