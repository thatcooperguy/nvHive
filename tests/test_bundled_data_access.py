"""Bundled files are read through nvh.data, and nvh.data alone touches importlib.resources.

nvHive ships a few non-Python files inside the ``nvh`` package (the
``package-data`` globs in pyproject.toml). ``nvh/data/__init__.py`` is the one
module that resolves them through :mod:`importlib.resources`; every other
reader asks ``nvh.data`` (``read_json`` / ``read_text`` / ``bundled``), so how
the install is laid out is a question answered in exactly one place.

Until 0.46 three readers still called ``resources.files("nvh.catalog")`` /
``resources.files("nvh.prompts")`` on their own: the setup catalog
(``setup_catalog.bundled_catalog``), the Agent Library
(``profiles._load_library_profiles``) and the Wizard system prompt
(``setup_agent.nvwizard_system_prompt``). This file pins three things:

1. each of them returns exactly what the direct ``importlib.resources`` read
   returns (the baseline is computed here, the old way);
2. each keeps its fallback -- generated catalog, empty library with one
   warning, built-in prompt with one warning -- when the bundled read fails;
3. no module under nvh/ other than nvh/data/__init__.py reaches for
   ``importlib.resources`` in any spelling (``import importlib.resources``,
   ``from importlib import resources``, ``from importlib.resources import``,
   the ``importlib.resources`` attribute chain, ``resources.files(...)``, the
   ``importlib_resources`` backport, ``pkgutil.get_data``).

The guard reads the AST, not the text: nvh/providers/registry.py and
nvh/core/workflows.py say "importlib.resources" in a comment, which is prose,
not a call. Like tests/test_single_root.py the allowance table names its
reason and a second test asserts the allowed module still hits, so the row
cannot rot.
"""

from __future__ import annotations

import ast
import json
import logging
from importlib import resources
from pathlib import Path

import pytest

import nvh.data
from nvh.integrations import setup_catalog
from nvh.integrations.wizard import profiles, setup_agent

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "nvh"

# Modules under nvh/ allowed to touch importlib.resources, with the reason.
# ``test_the_resources_allowance_is_still_needed`` evicts a row the moment the
# module stops using it. Nothing joins this table without a reason.
RESOURCES_ALLOWED: dict[str, str] = {
    "nvh/data/__init__.py": (
        "the single resolver: bundled()/bundled_path()/read_* are the only "
        "way the rest of the package reaches a shipped file"
    ),
}


# ---------------------------------------------------------------------------
# 1. Same payload as the direct read.
# ---------------------------------------------------------------------------


def _direct_text(package: str, name: str) -> str:
    """The pre-0.46 spelling, kept here as the baseline the loaders must match."""
    return resources.files(package).joinpath(name).read_text(encoding="utf-8")


def test_bundled_catalog_matches_the_direct_read() -> None:
    expected = json.loads(_direct_text("nvh.catalog", "nvhive-catalog.json"))
    got = setup_catalog.bundled_catalog()
    assert got == expected
    # The shipped file, not the generated fallback (whose channel differs).
    assert got["channel"] == "bundled"
    assert got["channel"] != setup_catalog._generated_fallback_catalog()["channel"]


def test_library_profiles_match_the_direct_read() -> None:
    entries = json.loads(_direct_text("nvh.catalog", "agent-library.json"))["profiles"]
    expected = [profiles._library_profile(entry) for entry in entries]
    assert expected
    assert profiles._load_library_profiles() == expected


def test_wizard_system_prompt_matches_the_direct_read() -> None:
    expected = _direct_text("nvh.prompts", "nvwizard_system.md").strip()
    got = setup_agent.nvwizard_system_prompt()
    assert got == expected
    assert got != setup_agent.PRODUCT_CONTEXT_FALLBACK.strip()


def test_nvh_data_resolves_the_same_files_as_the_sub_package_anchors() -> None:
    """``bundled("catalog", x)`` is the file ``resources.files("nvh.catalog") / x`` was."""
    pairs = [
        (("catalog", "nvhive-catalog.json"), ("nvh.catalog", "nvhive-catalog.json")),
        (("catalog", "agent-library.json"), ("nvh.catalog", "agent-library.json")),
        (("prompts", "nvwizard_system.md"), ("nvh.prompts", "nvwizard_system.md")),
    ]
    for parts, (package, name) in pairs:
        direct = Path(str(resources.files(package).joinpath(name))).resolve()
        assert nvh.data.bundled_path(*parts).resolve() == direct, parts
        assert nvh.data.read_text(*parts) == _direct_text(package, name), parts


# ---------------------------------------------------------------------------
# 2. The fallbacks still fire when the bundled read fails.
# ---------------------------------------------------------------------------


def _withheld(*_parts: str, **_kw: object) -> None:
    raise FileNotFoundError("bundled file withheld by the test")


def test_catalog_falls_back_to_the_generated_catalog_when_unreadable(monkeypatch) -> None:
    monkeypatch.setattr(setup_catalog, "read_json", _withheld)
    catalog = setup_catalog.bundled_catalog()
    assert catalog["channel"] == "generated-fallback"
    assert {profile["id"] for profile in catalog["profiles"]} >= {"student", "creator"}


def test_catalog_falls_back_when_the_bundled_file_fails_validation(monkeypatch) -> None:
    monkeypatch.setattr(setup_catalog, "read_json", lambda *_parts: {"schema_version": 99})
    assert setup_catalog.bundled_catalog()["channel"] == "generated-fallback"


def test_library_falls_back_to_empty_with_one_warning(monkeypatch, caplog) -> None:
    # profiles imports read_json inside the function, so patch the source.
    monkeypatch.setattr(nvh.data, "read_json", _withheld)
    with caplog.at_level(logging.WARNING, logger="nvh.integrations.wizard.profiles"):
        assert profiles._load_library_profiles() == []
    warnings = [r for r in caplog.records if "agent library catalog unavailable" in r.getMessage()]
    assert len(warnings) == 1


def test_prompt_falls_back_to_the_built_in_with_one_warning(monkeypatch, caplog) -> None:
    monkeypatch.setattr(setup_agent, "read_text", _withheld)
    with caplog.at_level(logging.WARNING, logger="nvh.integrations.wizard.setup_agent"):
        assert setup_agent.nvwizard_system_prompt() == setup_agent.PRODUCT_CONTEXT_FALLBACK.strip()
    warnings = [r for r in caplog.records if "nvwizard_system.md unreadable" in r.getMessage()]
    assert len(warnings) == 1


# ---------------------------------------------------------------------------
# 3. Only nvh/data/__init__.py touches importlib.resources.
# ---------------------------------------------------------------------------

_RESOURCES_MODULES = ("importlib.resources", "importlib_resources")


def _names_module(dotted: str) -> bool:
    return any(dotted == m or dotted.startswith(m + ".") for m in _RESOURCES_MODULES)


def _spellings(tree: ast.AST) -> list[str]:
    """Every way a module reaches importlib.resources (or an equivalent), as ``line: form``."""
    hits: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if _names_module(alias.name):
                    hits.append(f"{node.lineno}: import {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module == "importlib" and any(a.name == "resources" for a in node.names):
                hits.append(f"{node.lineno}: from importlib import resources")
            elif _names_module(module):
                hits.append(f"{node.lineno}: from {module} import ...")
            elif module == "pkgutil" and any(a.name == "get_data" for a in node.names):
                hits.append(f"{node.lineno}: from pkgutil import get_data")
        elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            if node.value.id == "importlib" and node.attr == "resources":
                hits.append(f"{node.lineno}: importlib.resources")
            elif node.value.id == "importlib_resources":
                hits.append(f"{node.lineno}: importlib_resources.{node.attr}")
            elif node.value.id == "pkgutil" and node.attr == "get_data":
                hits.append(f"{node.lineno}: pkgutil.get_data")
            elif node.value.id == "resources" and node.attr == "files":
                hits.append(f"{node.lineno}: resources.files(...)")
    return hits


def modules_touching_importlib_resources() -> dict[str, list[str]]:
    """``{path relative to the repo: [hits]}`` for every module under nvh/ that spells it."""
    found: dict[str, list[str]] = {}
    for path in sorted(PKG.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        hits = _spellings(tree)
        if hits:
            found[path.relative_to(ROOT).as_posix()] = hits
    return found


def test_only_nvh_data_touches_importlib_resources() -> None:
    found = modules_touching_importlib_resources()
    offenders = {rel: hits for rel, hits in found.items() if rel not in RESOURCES_ALLOWED}
    assert not offenders, (
        "bundled files are read through nvh.data (read_json / read_text / bundled), "
        "never through importlib.resources directly:\n  "
        + "\n  ".join(f"{rel}: {'; '.join(hits)}" for rel, hits in sorted(offenders.items()))
    )


def test_the_resources_allowance_is_still_needed() -> None:
    found = modules_touching_importlib_resources()
    stale = [
        f"{rel} no longer touches importlib.resources: remove it from RESOURCES_ALLOWED"
        for rel in sorted(RESOURCES_ALLOWED)
        if rel not in found
    ]
    assert not stale, "\n".join(stale)


@pytest.mark.parametrize(
    "snippet",
    [
        "import importlib.resources",
        "import importlib.resources as r",
        "from importlib import resources",
        "from importlib import abc, resources",
        "from importlib.resources import files",
        "from importlib.resources.abc import Traversable",
        "import importlib\nimportlib.resources.files('nvh')",
        "resources.files('nvh.catalog').joinpath('x').read_text()",
        "import importlib_resources",
        "from importlib_resources import files",
        "importlib_resources.files('nvh')",
        "import pkgutil\npkgutil.get_data('nvh', 'catalog/x.json')",
        "from pkgutil import get_data",
    ],
)
def test_the_guard_recognises_every_spelling(snippet: str) -> None:
    assert _spellings(ast.parse(snippet)), snippet


def test_the_guard_ignores_prose_and_strings() -> None:
    prose = (
        '"""Resolved through importlib.resources; see resources.files("nvh")."""\n'
        "# the bundled config directory, resolved through importlib.resources\n"
        "LABEL = 'resources.files(\"nvh\")'\n"
        "import importlib\n"
        "import pkgutil\n"
        "mod = importlib.import_module('nvh')\n"
        "for _ in pkgutil.walk_packages([]):\n"
        "    pass\n"
    )
    assert _spellings(ast.parse(prose)) == []
