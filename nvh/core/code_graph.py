"""Lightweight code-graph for context-aware file discovery."""

from __future__ import annotations

import os
import re
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

_RE_FROM_IMPORT = re.compile(r"^\s*from\s+([\w.]+)\s+import\s", re.MULTILINE)
_RE_PLAIN_IMPORT = re.compile(r"^\s*import\s+([\w.]+)", re.MULTILINE)
_RE_DEF = re.compile(r"^\s*(?:async\s+)?def\s+(\w+)\s*\(", re.MULTILINE)
_RE_CLASS = re.compile(r"^\s*class\s+(\w+)\s*[:(]", re.MULTILINE)
_SKIP_DIRS = {"node_modules", ".git", "__pycache__", ".next", "venv", ".venv"}


@dataclass
class FileNode:
    """Metadata for a single source file."""

    path: str
    imports: list[str] = field(default_factory=list)
    imported_by: list[str] = field(default_factory=list)
    symbols: list[str] = field(default_factory=list)
    line_count: int = 0


@dataclass
class ImportGraph:
    """Directed import graph over a code-base."""

    nodes: dict[str, FileNode] = field(default_factory=dict)
    edges: list[tuple[str, str]] = field(default_factory=list)


def _iter_py_files(root: Path):
    """Yield .py files under *root*, skipping irrelevant directories."""
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for fn in filenames:
            if fn.endswith(".py"):
                yield Path(dirpath) / fn


def _module_to_relpath(module: str) -> str:
    """Convert a dotted module name to a relative file path."""
    return module.replace(".", "/") + ".py"


def _extract_imports(source: str, rel_path: str) -> list[str]:
    """Return a list of relative file paths this source imports."""
    results: list[str] = []
    for m in _RE_FROM_IMPORT.finditer(source):
        mod = m.group(1)
        if mod == "__future__":
            continue
        if mod.startswith("."):
            pkg = str(Path(rel_path).parent)
            stripped = mod.lstrip(".")
            resolved = (pkg + "/" + stripped.replace(".", "/") + ".py") if stripped else (pkg + "/__init__.py")
            results.append(resolved.replace("\\", "/"))
        else:
            results.append(_module_to_relpath(mod))
    for m in _RE_PLAIN_IMPORT.finditer(source):
        mod = m.group(1)
        results.append(_module_to_relpath(mod))
    return results


def _extract_symbols(source: str) -> list[str]:
    """Return top-level def / class names."""
    return [m.group(1) for m in _RE_DEF.finditer(source)] + [
        m.group(1) for m in _RE_CLASS.finditer(source)
    ]


def build_import_graph(
    working_dir: Path,
    language: str = "python",  # noqa: ARG001 – reserved for future use
) -> ImportGraph:
    """Walk *working_dir* and return the full import graph."""
    graph = ImportGraph()
    root = Path(working_dir).resolve()

    for filepath in _iter_py_files(root):
        rel = filepath.relative_to(root).as_posix()
        try:
            source = filepath.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        imports = _extract_imports(source, rel)
        symbols = _extract_symbols(source)
        line_count = source.count("\n") + (1 if source and not source.endswith("\n") else 0)
        graph.nodes[rel] = FileNode(
            path=rel,
            imports=imports,
            symbols=symbols,
            line_count=line_count,
        )

    # Resolve edges & back-references.
    for rel, node in graph.nodes.items():
        for imp in node.imports:
            if imp in graph.nodes:
                graph.edges.append((rel, imp))
                graph.nodes[imp].imported_by.append(rel)

    return graph


def find_related_files(
    graph: ImportGraph,
    target: str,
    depth: int = 2,
) -> list[str]:
    """BFS from *target* over imports + imported_by up to *depth* hops."""
    if target not in graph.nodes:
        return [target]

    visited: dict[str, int] = {target: 0}
    queue: deque[tuple[str, int]] = deque([(target, 0)])
    while queue:
        current, d = queue.popleft()
        if d >= depth:
            continue
        node = graph.nodes.get(current)
        if node is None:
            continue
        for neighbour in node.imports + node.imported_by:
            if neighbour not in visited and neighbour in graph.nodes:
                visited[neighbour] = d + 1
                queue.append((neighbour, d + 1))

    # Sort by hop distance then alphabetically.
    related = sorted(visited, key=lambda f: (visited[f], f))
    return related


def find_test_files(graph: ImportGraph, target: str) -> list[str]:
    """Heuristic search for test files that exercise *target*."""
    stem = Path(target).stem
    test_name = f"test_{stem}.py"
    results: set[str] = set()

    for rel, node in graph.nodes.items():
        # Name-based match.
        if Path(rel).name == test_name:
            results.add(rel)
        # Import-based match: file lives under a tests/ directory and imports
        # the target's module path.
        if "/tests/" in rel or rel.startswith("tests/"):
            if target in node.imports:
                results.add(rel)

    return sorted(results)


def format_context_for_agent(
    graph: ImportGraph,
    target: str,
    depth: int = 2,
) -> str:
    """Render a concise text summary of *target*'s neighbourhood."""
    lines: list[str] = [f"== Code-graph context for {target} =="]
    node = graph.nodes.get(target)
    if node is None:
        lines.append("(file not found in graph)")
        return "\n".join(lines)

    lines.append(f"Lines: {node.line_count}")
    if node.symbols:
        lines.append(f"Symbols: {', '.join(node.symbols)}")
    if node.imports:
        lines.append("Imports:")
        for imp in node.imports:
            tag = " (in graph)" if imp in graph.nodes else ""
            lines.append(f"  - {imp}{tag}")
    if node.imported_by:
        lines.append("Imported by:")
        for imp in node.imported_by:
            lines.append(f"  - {imp}")

    related = find_related_files(graph, target, depth=depth)
    related_others = [f for f in related if f != target]
    if related_others:
        total = sum(graph.nodes[f].line_count for f in related_others if f in graph.nodes)
        lines.append(f"Related files ({len(related_others)}, ~{total} lines):")
        for f in related_others:
            lines.append(f"  - {f}")

    tests = find_test_files(graph, target)
    if tests:
        lines.append("Test files:")
        for t in tests:
            lines.append(f"  - {t}")

    return "\n".join(lines)
