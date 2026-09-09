"""No file under nvh/ hard-codes a pre-0.44 home path (design D7, invariant I8).

``storage_layout()`` in ``nvh/integrations/workspace/storage.py`` is the single
path oracle since 0.44. The old roots — ``~/.hive``, ``~/.council``, ``~/nvh``
— and the retired ``HIVE_DATA_DIR`` knob may be spelled in exactly one module,
``nvh/integrations/workspace/migrate_legacy.py`` (the one-shot import that
copies them into the layout and never writes back), plus the two small tables
below. Like tests/test_no_retired_model_tags.py this scans *values*: Python
string constants (docstrings and comments are prose, not paths the code
uses), JSON values, YAML keys and values, and plain text files. Every
exclusion names its reason and a second test asserts it still hits, so the
tables cannot rot once a file is fixed.
"""

from __future__ import annotations

import ast
import json
import re
from collections.abc import Iterator
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "nvh"
MIGRATION_MODULE = "nvh/integrations/workspace/migrate_legacy.py"
SCANNED_SUFFIXES = {".py", ".json", ".yaml", ".yml", ".txt", ".toml", ".sh"}

# ``.hive`` / ``.council`` as a path component (``~/.hive/x``, ``.hive/config.yaml``,
# ``.hive.yaml``) but not ``nvhive`` or ``self.config.council``; ``~/nvh`` but not
# ``~/nvhive``.
PATTERNS: dict[str, re.Pattern[str]] = {
    ".hive": re.compile(r"(?<![\w-])\.hive(?![\w-])"),
    ".council": re.compile(r"(?<![\w-])\.council(?![\w-])"),
    "HIVE_DATA_DIR": re.compile(r"\bHIVE_DATA_DIR\b"),
    "~/nvh": re.compile(r"~/nvh(?![\w-])"),
}

# Values allowed to carry a legacy name, per file, with the reason.
ALLOWED: dict[str, set[str]] = {
    # The restore error explains what a pre-0.41.1 `nvh snapshot` tarball
    # contained (``~/.hive`` + ``~/.council``); it describes an archive, not a
    # path the code reads or writes.
    "nvh/integrations/workspace/snapshot.py": {".hive", ".council"},
}

# Files another change owns that still carry a legacy path as a value. Each
# entry must still hit (``test_exclusions_are_still_needed``), so it has to be
# removed the moment the file is fixed; nothing joins this table without a
# reason. Fixing them is the follow-up, not widening this list.
KNOWN_STALE: dict[str, set[str]] = {
    # Two "check logs at ~/.hive/nvhive.log" hints -> ``$NVH_LOGS/nvhive.log``.
}


def legacy_names_in(value: str) -> set[str]:
    """Every legacy name ``value`` carries."""
    return {name for name, pattern in PATTERNS.items() if pattern.search(value)}


def _python_values(path: Path) -> Iterator[tuple[int, str]]:
    """Every string constant in a module except docstrings (comments never parse)."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    docstrings: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            first = node.body[0] if node.body else None
            if (
                isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)
            ):
                docstrings.add(id(first.value))
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
            yield node.lineno, node.value


def _structured_values(data: object, *, keys: bool) -> Iterator[str]:
    if isinstance(data, dict):
        for key, value in data.items():
            if keys and isinstance(key, str):
                yield key
            yield from _structured_values(value, keys=keys)
    elif isinstance(data, list):
        for item in data:
            yield from _structured_values(item, keys=keys)
    elif isinstance(data, str):
        yield data


def _line_of(text: str, value: str) -> int:
    needle = value.splitlines()[0] if value.strip() else value
    for lineno, line in enumerate(text.splitlines(), 1):
        if needle and needle in line:
            return lineno
    return 0


def _values(path: Path) -> Iterator[tuple[int, str]]:
    if path.suffix == ".py":
        yield from _python_values(path)
        return
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".json":
        for value in _structured_values(json.loads(text), keys=False):
            yield _line_of(text, value), value
    elif path.suffix in {".yaml", ".yml"}:
        for value in _structured_values(yaml.safe_load(text), keys=True):
            yield _line_of(text, value), value
    else:
        yield from enumerate(text.splitlines(), 1)


def scan() -> dict[str, dict[str, list[tuple[int, str]]]]:
    """``{relative path: {legacy name: [(line, value), ...]}}`` for every hit under nvh/."""
    hits: dict[str, dict[str, list[tuple[int, str]]]] = {}
    for path in sorted(PKG.rglob("*")):
        if not path.is_file() or path.suffix not in SCANNED_SUFFIXES or "__pycache__" in path.parts:
            continue
        rel = path.relative_to(ROOT).as_posix()
        if rel == MIGRATION_MODULE:
            continue
        for lineno, value in _values(path):
            for name in legacy_names_in(value):
                hits.setdefault(rel, {}).setdefault(name, []).append((lineno, value))
    return hits


def test_no_legacy_home_path_is_a_value_under_nvh() -> None:
    offenders = []
    for rel, by_name in sorted(scan().items()):
        allowed = ALLOWED.get(rel, set()) | KNOWN_STALE.get(rel, set())
        for name, sites in sorted(by_name.items()):
            if name in allowed:
                continue
            offenders += [
                f"{rel}:{lineno}: {value[:80]!r} names legacy root {name!r}" for lineno, value in sites
            ]
    assert not offenders, (
        "Pre-0.44 home paths as values under nvh/ -- derive the path from "
        "nvh.integrations.workspace.storage.storage_layout() (legacy names belong "
        "only in nvh/integrations/workspace/migrate_legacy.py):\n  " + "\n  ".join(offenders)
    )


def test_exclusions_are_still_needed() -> None:
    """Every allowance and every known-stale entry must still hit, or it is deleted."""
    hits = scan()
    stale = [
        f"{rel} no longer carries {name!r}: remove it from {table_name}"
        for table_name, table in (("ALLOWED", ALLOWED), ("KNOWN_STALE", KNOWN_STALE))
        for rel, names in table.items()
        for name in sorted(names)
        if name not in hits.get(rel, {})
    ]
    assert not stale, "\n".join(stale)


def test_migration_module_is_where_the_legacy_names_live() -> None:
    """The one module allowed to spell the old roots spells all of them."""
    from nvh.integrations.workspace import migrate_legacy as m

    assert m.LEGACY_HOME_DIRNAME == ".hive"
    assert m.LEGACY_DB_DIRNAME == ".council"
    assert m.LEGACY_INSTALL_DIRNAME == "nvh"
    assert m.LEGACY_DATA_DIR_ENV == "HIVE_DATA_DIR"
    values = {value for _, value in _python_values(ROOT / MIGRATION_MODULE)}
    assert {".hive", ".council", "nvh", "HIVE_DATA_DIR"} <= values


def test_patterns_match_the_legacy_shapes_and_nothing_else() -> None:
    assert legacy_names_in("~/.hive/config.yaml") == {".hive"}
    assert legacy_names_in(".hive/context") == {".hive"}
    assert legacy_names_in(".hive.yaml") == {".hive"}
    assert legacy_names_in("~/.council/council.db") == {".council"}
    assert legacy_names_in("~/nvh/repo/web") == {"~/nvh"}
    assert legacy_names_in("HIVE_DATA_DIR") == {"HIVE_DATA_DIR"}
    for clean in ("nvhive", "HIVE.md", "hive.md", ".nvh/context", "~/nvhive/bin", "~/.nvh",
                  "config.council.strategy", "$NVH_HOME/config/config.yaml", "HIVE_CONFIG_HOME"):
        assert not legacy_names_in(clean), clean


def test_settings_default_paths_are_the_layout(tmp_path: Path, monkeypatch) -> None:
    """The oracle in practice: settings, setup and the repository agree with storage_layout()."""
    import nvh.config.settings as settings
    from nvh.cli.setup import _layout_config_dir
    from nvh.integrations.workspace.storage import storage_layout
    from nvh.storage import repository

    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    for var in ("NVHIVE_HOME", "NVH_CONFIG", "HIVE_CONFIG_HOME", "NVH_STATE", "HIVE_DATA_DIR"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("NVH_HOME", str(tmp_path / "nvh"))
    settings.reset_default_paths()
    try:
        layout = storage_layout()
        assert layout.home == tmp_path / "nvh"
        assert settings.DEFAULT_CONFIG_DIR == layout.config_dir == _layout_config_dir()
        assert settings.DEFAULT_CONFIG_PATH == layout.config_dir / "config.yaml"
        assert repository._default_db_path() == layout.state_dir / "nvhive.db"
    finally:
        settings.reset_default_paths()
