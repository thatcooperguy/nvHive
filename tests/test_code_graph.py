"""Tests for nvh.core.code_graph — import-graph builder and related-file finder."""

from __future__ import annotations

from pathlib import Path

import pytest

from nvh.core.code_graph import (
    FileNode,
    ImportGraph,
    build_import_graph,
    find_related_files,
    find_test_files,
    format_context_for_agent,
)


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


# ── build_import_graph ──────────────────────────────────────────────


def test_build_graph_finds_files(tmp_path: Path) -> None:
    _write(tmp_path / "a.py", "x = 1\n")
    _write(tmp_path / "b.py", "y = 2\n")
    _write(tmp_path / "c.py", "z = 3\n")
    graph = build_import_graph(tmp_path)
    assert len(graph.nodes) == 3
    assert all(isinstance(n, FileNode) for n in graph.nodes.values())


def test_build_graph_extracts_imports(tmp_path: Path) -> None:
    _write(tmp_path / "file_a.py", "from file_b import something\n")
    _write(tmp_path / "file_b.py", "something = 1\n")
    graph = build_import_graph(tmp_path)
    assert ("file_a.py", "file_b.py") in graph.edges


def test_build_graph_extracts_symbols(tmp_path: Path) -> None:
    _write(tmp_path / "mod.py", "def foo():\n    pass\n\nclass Bar:\n    pass\n")
    graph = build_import_graph(tmp_path)
    symbols = graph.nodes["mod.py"].symbols
    assert "foo" in symbols
    assert "Bar" in symbols


# ── find_related_files ──────────────────────────────────────────────


@pytest.fixture()
def chain_graph(tmp_path: Path) -> ImportGraph:
    """a -> b -> c import chain."""
    _write(tmp_path / "a.py", "from b import x\n")
    _write(tmp_path / "b.py", "from c import y\nx = 1\n")
    _write(tmp_path / "c.py", "y = 2\n")
    return build_import_graph(tmp_path)


def test_find_related_files_depth_1(chain_graph: ImportGraph) -> None:
    related = find_related_files(chain_graph, "a.py", depth=1)
    assert "b.py" in related
    assert "c.py" not in related


def test_find_related_files_depth_2(chain_graph: ImportGraph) -> None:
    related = find_related_files(chain_graph, "a.py", depth=2)
    assert "b.py" in related
    assert "c.py" in related


def test_find_related_includes_target(chain_graph: ImportGraph) -> None:
    related = find_related_files(chain_graph, "a.py", depth=1)
    assert "a.py" in related


# ── find_test_files ─────────────────────────────────────────────────


def test_find_test_files(tmp_path: Path) -> None:
    _write(tmp_path / "foo.py", "x = 1\n")
    _write(tmp_path / "tests" / "test_foo.py", "from foo import x\n")
    graph = build_import_graph(tmp_path)
    results = find_test_files(graph, "foo.py")
    assert any("test_foo.py" in r for r in results)


# ── format_context_for_agent ────────────────────────────────────────


def test_format_context_returns_string(tmp_path: Path) -> None:
    _write(tmp_path / "m.py", "def hello():\n    pass\n")
    graph = build_import_graph(tmp_path)
    ctx = format_context_for_agent(graph, "m.py")
    assert isinstance(ctx, str)
    assert len(ctx) > 0


# ── skip / edge-case behaviour ──────────────────────────────────────


def test_skips_pycache_and_venv(tmp_path: Path) -> None:
    _write(tmp_path / "real.py", "x = 1\n")
    _write(tmp_path / "__pycache__" / "cached.py", "x = 2\n")
    _write(tmp_path / "venv" / "lib.py", "x = 3\n")
    graph = build_import_graph(tmp_path)
    assert len(graph.nodes) == 1
    assert "real.py" in graph.nodes


def test_empty_directory(tmp_path: Path) -> None:
    graph = build_import_graph(tmp_path)
    assert graph.nodes == {}
    assert graph.edges == []
