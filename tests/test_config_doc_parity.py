"""docs/CONFIGURATION.md's Wizard tool roster and cabinet table are generated (derive, don't type).

scripts/gen_config_doc.py fills two marker-delimited blocks: the Wizard's
stock tool roster by safety class (``default_registry()`` with the
machine-dependent discovery passes off) and the council cabinets from
``COUNCIL_PRESETS`` / ``PRESET_DESCRIPTIONS``. The committed file must match
byte-for-byte so a tool added, reclassified or re-described, or a cabinet
changed, fails CI instead of drifting in the doc. The hand-written text the
rosters sit in is pinned to the registries it names: the kill-switch row's
list of privileged tools and the legacy-homes paragraph's doctor row.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest

import nvh.integrations.mcp_client as mcp_client
import nvh.integrations.wizard.tools as wizard_tools
from nvh.core.agents import COUNCIL_PRESETS, PRESET_DESCRIPTIONS
from nvh.core.tools import SAFETY_CLASSES
from nvh.integrations.diagnostics.checks import DEEP, checks_for
from nvh.integrations.workspace.migrate_legacy import LEGACY_MIGRATION_ENV

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "gen_config_doc.py"
DOC = ROOT / "docs" / "CONFIGURATION.md"


@pytest.fixture(scope="module")
def generator():
    spec = importlib.util.spec_from_file_location("gen_config_doc", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def doc() -> str:
    return DOC.read_text(encoding="utf-8").replace("\r\n", "\n")


@pytest.fixture(scope="module")
def stock(generator) -> list:
    return generator.stock_registry().list_tools()


def _rows(table: str) -> list[str]:
    """The body rows of a markdown table block (header and rule dropped)."""
    lines = [line for line in table.strip().splitlines() if line.startswith("|")]
    return lines[2:]


def _cells(row: str) -> list[str]:
    """A row's cells, unescaped; split on the pipes that are not ``\\|``."""
    return [cell.strip().replace("\\|", "|") for cell in re.split(r"(?<!\\)\|", row.strip())[1:-1]]


def _section(doc: str, start: str, end: str) -> str:
    return doc.split(start, 1)[1].split(end, 1)[0]


# --- the generated doc ----------------------------------------------------------


def test_check_mode_reports_current(generator, capsys):
    assert generator.main(["--check"]) == 0
    assert "is current" in capsys.readouterr().out


def test_committed_doc_matches_generator(generator, doc: str):
    assert generator.render(doc) == doc, "docs/CONFIGURATION.md is stale — run: python scripts/gen_config_doc.py"


def test_every_block_has_one_marker_pair(generator, doc: str):
    assert set(generator.BLOCKS) == {"wizard-tools", "council-cabinets"}
    for name in generator.BLOCKS:
        assert doc.count(generator.BEGIN.format(name=name)) == 1, name
        assert doc.count(generator.END.format(name=name)) == 1, name
        assert generator.block_text(doc, name).strip(), name


def test_stale_doc_fails_check_and_is_repaired(generator, tmp_path, monkeypatch, doc: str):
    end = generator.END.format(name="wizard-tools")
    stale = tmp_path / "CONFIGURATION.md"
    stale.write_text(doc.replace(end, "| `stale_tool` | auto | Never shipped. |\n" + end), encoding="utf-8")
    monkeypatch.setattr(generator, "DOC_PATH", stale)
    assert generator.main(["--check"]) == 1
    # And the non-check form repairs it.
    assert generator.main([]) == 0
    assert stale.read_text(encoding="utf-8") == doc


def test_doubled_marker_is_refused(generator, doc: str):
    begin = generator.BEGIN.format(name="council-cabinets")
    with pytest.raises(SystemExit, match="exactly one"):
        generator.render(doc.replace(begin, begin + "\n" + begin))


# --- the Wizard tool roster ---------------------------------------------------


def test_roster_lists_every_stock_tool_once_in_registry_order(generator, doc: str, stock: list):
    rows = [_cells(row) for row in _rows(generator.block_text(doc, "wizard-tools"))]
    assert [cells[0] for cells in rows] == [f"`{tool.name}`" for tool in stock]
    for cells, tool in zip(rows, stock, strict=True):
        assert cells[1] == tool.safety_class, tool.name
    # list_tools() order: the safety classes as SAFETY_CLASSES ranks them, names sorted within.
    classes = [cells[1] for cells in rows]
    assert classes == sorted(classes, key=SAFETY_CLASSES.index)
    for klass in SAFETY_CLASSES:
        names = [cells[0].strip("`") for cells in rows if cells[1] == klass]
        assert names == sorted(names), klass
        assert names, f"no {klass} tool in the stock roster"


def test_roster_purpose_is_the_descriptions_first_sentence(generator, doc: str, stock: list):
    rows = [_cells(row) for row in _rows(generator.block_text(doc, "wizard-tools"))]
    for cells, tool in zip(rows, stock, strict=True):
        flat = " ".join(tool.description.split())
        assert cells[2] == generator.first_sentence(tool.description), tool.name
        assert flat.startswith(cells[2]) and cells[2], tool.name


def test_first_sentence_keeps_abbreviations_and_code_spans(generator):
    assert generator.first_sentence("Call a service, e.g. light.turn_on with {\"x\": 1}. Changes the home.") == (
        "Call a service, e.g. light.turn_on with {\"x\": 1}."
    )
    assert generator.first_sentence("Install with `apt-get install -y` (sudo). Never upgrades.") == (
        "Install with `apt-get install -y` (sudo)."
    )
    assert generator.first_sentence("One  line\nwith   breaks") == "One line with breaks"


def test_stock_registry_leaves_out_plugins_and_mcp_tools(generator, tmp_path, monkeypatch, stock: list):
    # A .py plugin in the plugins directory reaches default_registry() ...
    (tmp_path / "extra.py").write_text(
        "from nvh.integrations.wizard.tools import WizardTool\n"
        "def register(reg):\n"
        "    reg.register(WizardTool(name='doc_parity_extra', description='From a plugin.',\n"
        "                            safety_class='auto', parameters={}, handler=None))\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(wizard_tools, "_wizard_plugin_dirs", lambda: [tmp_path])
    # ... while the other two passes stay off so nothing reads this machine's wheels or MCP cache.
    monkeypatch.setattr(wizard_tools, "_load_entry_point_tools", lambda reg: None)
    monkeypatch.setattr(mcp_client, "register_mcp_tools", lambda reg, home_dir=None: None)
    assert "doc_parity_extra" in {tool.name for tool in wizard_tools.default_registry().list_tools()}
    # ... but never the generated roster.
    names = {tool.name for tool in generator.stock_registry().list_tools()}
    assert "doc_parity_extra" not in names
    assert not any(name.startswith("mcp_") for name in names)
    assert names == {tool.name for tool in stock}


def test_kill_switch_row_names_every_privileged_tool(doc: str, stock: list):
    row = next(line for line in doc.splitlines() if line.startswith("| `NVH_ALLOW_PRIVILEGED` |"))
    listed = re.search(r"`privileged` tools \(([^)]*)\)", row).group(1)
    assert set(re.findall(r"`([a-z_]+)`", listed)) == {t.name for t in stock if t.safety_class == "privileged"}
    assert "| unset (on) |" in row


# --- the cabinet table --------------------------------------------------------


def test_cabinet_table_rows_are_the_presets(generator, doc: str):
    rows = [_cells(row) for row in _rows(generator.block_text(doc, "council-cabinets"))]
    assert [cells[0] for cells in rows] == [f"`{name}`" for name in COUNCIL_PRESETS]
    for cells, (name, templates) in zip(rows, COUNCIL_PRESETS.items(), strict=True):
        assert cells[1] == ", ".join(template.role for template in templates), name
        assert cells[2] == PRESET_DESCRIPTIONS[name], name


def test_every_preset_has_a_description():
    assert set(PRESET_DESCRIPTIONS) == set(COUNCIL_PRESETS)
    assert all(PRESET_DESCRIPTIONS.values())


# --- the hand-written text around the rosters -----------------------------------


def test_legacy_homes_paragraph_names_the_doctor_row(doc: str):
    check = next(c for c in checks_for(DEEP) if c.id == "legacy_homes")
    section = _section(doc, "### Legacy homes (pre-0.44)", "## Environment variables")
    assert f"**{check.title}**" in section
    assert f"`{check.id}`" in section
    assert f"`{LEGACY_MIGRATION_ENV}=0`" in section
    row = next(line for line in doc.splitlines() if line.startswith(f"| `{LEGACY_MIGRATION_ENV}` |"))
    assert f"`{check.id}`" in row
