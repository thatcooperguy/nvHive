"""docs/COMMANDS.md is generated from the Typer registry (issue #127).

`scripts/gen_commands_doc.py` renders the doc from ``get_command(app)``; the
committed file must match byte-for-byte so a command added, renamed or hidden
without regenerating the doc fails CI.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from typer.main import get_command

import nvh.cli.main as cli_main

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "gen_commands_doc.py"
DOC = ROOT / "docs" / "COMMANDS.md"


@pytest.fixture(scope="module")
def generator():
    spec = importlib.util.spec_from_file_location("gen_commands_doc", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def rendered(generator) -> str:
    return generator.render(cli_main.app)


def test_committed_doc_matches_generator(rendered: str):
    actual = DOC.read_text(encoding="utf-8").replace("\r\n", "\n")
    assert actual == rendered, (
        "docs/COMMANDS.md is stale — run: python scripts/gen_commands_doc.py"
    )


def test_check_mode_reports_current(generator, capsys):
    assert generator.main(["--check"]) == 0
    assert "is current" in capsys.readouterr().out


def test_every_visible_command_is_documented(rendered: str):
    root = get_command(cli_main.app)
    for name, cmd in root.commands.items():
        if not cmd.hidden:
            assert f"`nvh {name}" in rendered, name


def test_hidden_aliases_are_listed_as_deprecated(rendered: str):
    deprecated = rendered.split("## Deprecated spellings", 1)[1]
    assert "| `nvh code` | (alias) nvh ask --focus code |" in deprecated
    assert "| `nvh doctor` | (alias) nvh status --deep |" in deprecated
    assert "| `nvh groq` | (alias) nvh ask -p groq" in deprecated
    # Deprecated spellings never leak into the visible sections.
    visible = rendered.split("## Deprecated spellings", 1)[0]
    for name in ("code", "quick", "safe", "pipe", "clip", "health", "doctor", "selfcheck", "debug"):
        assert f"`nvh {name}`" not in visible, name


def test_flag_tables_cover_the_flagship_commands(rendered: str):
    flags = rendered.split("## Flags", 1)[1].split("## Command groups", 1)[0]
    for flag in ("--focus", "--fast", "--local", "--clipboard", "--providers", "--deep", "--smoke", "--report", "--routing"):
        assert flag in flags, flag
