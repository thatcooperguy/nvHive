"""Deep tests for nvh.core.code_graph — import extraction, symbol extraction, context formatting.

Covers edge cases in _extract_imports, _extract_symbols, _module_to_relpath,
and format_context_for_agent that are not covered by test_code_graph.py.
"""

from __future__ import annotations

from pathlib import Path

from nvh.core.code_graph import (
    ImportGraph,
    _extract_imports,
    _extract_symbols,
    _module_to_relpath,
    build_import_graph,
    find_related_files,
    find_test_files,
    format_context_for_agent,
)


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# _module_to_relpath
# ---------------------------------------------------------------------------


class TestModuleToRelpath:

    def test_simple_module(self):
        assert _module_to_relpath("os") == "os.py"

    def test_dotted_module(self):
        assert _module_to_relpath("nvh.core.engine") == "nvh/core/engine.py"

    def test_single_name(self):
        assert _module_to_relpath("json") == "json.py"


# ---------------------------------------------------------------------------
# _extract_imports
# ---------------------------------------------------------------------------


class TestExtractImports:

    def test_from_import(self):
        source = "from foo.bar import baz\n"
        result = _extract_imports(source, "main.py")
        assert "foo/bar.py" in result

    def test_plain_import(self):
        source = "import os\nimport json\n"
        result = _extract_imports(source, "main.py")
        assert "os.py" in result
        assert "json.py" in result

    def test_skips_future_import(self):
        source = "from __future__ import annotations\nimport os\n"
        result = _extract_imports(source, "main.py")
        # __future__ should not be in the result
        assert not any("__future__" in r for r in result)
        assert "os.py" in result

    def test_relative_import(self):
        source = "from .utils import helper\n"
        result = _extract_imports(source, "pkg/main.py")
        assert any("utils.py" in r for r in result)

    def test_relative_import_init(self):
        """A bare relative import (from . import x) resolves to __init__.py."""
        source = "from . import something\n"
        result = _extract_imports(source, "pkg/sub/main.py")
        assert any("__init__.py" in r for r in result)

    def test_empty_source(self):
        result = _extract_imports("", "main.py")
        assert result == []

    def test_comments_not_parsed_as_imports(self):
        source = "# from foo import bar\nx = 1\n"
        # The regex checks for leading whitespace, not comments.
        # A comment line starting with # won't match ^\s*from
        result = _extract_imports(source, "main.py")
        assert result == []

    def test_multiple_from_imports(self):
        source = "from a import x\nfrom b.c import y\n"
        result = _extract_imports(source, "main.py")
        assert "a.py" in result
        assert "b/c.py" in result


# ---------------------------------------------------------------------------
# _extract_symbols
# ---------------------------------------------------------------------------


class TestExtractSymbols:

    def test_functions(self):
        source = "def foo():\n    pass\ndef bar(x):\n    return x\n"
        symbols = _extract_symbols(source)
        assert "foo" in symbols
        assert "bar" in symbols

    def test_classes(self):
        source = "class MyClass:\n    pass\nclass Another(Base):\n    pass\n"
        symbols = _extract_symbols(source)
        assert "MyClass" in symbols
        assert "Another" in symbols

    def test_async_def(self):
        source = "async def handler():\n    pass\n"
        symbols = _extract_symbols(source)
        assert "handler" in symbols

    def test_empty_source(self):
        assert _extract_symbols("") == []

    def test_mixed(self):
        source = "class Engine:\n    pass\ndef run():\n    pass\nasync def start():\n    pass\n"
        symbols = _extract_symbols(source)
        assert set(symbols) == {"Engine", "run", "start"}


# ---------------------------------------------------------------------------
# format_context_for_agent
# ---------------------------------------------------------------------------


class TestFormatContext:

    def test_unknown_file_returns_not_found(self):
        graph = ImportGraph()
        ctx = format_context_for_agent(graph, "nonexistent.py")
        assert "not found in graph" in ctx

    def test_includes_line_count(self, tmp_path):
        _write(tmp_path / "m.py", "a = 1\nb = 2\nc = 3\n")
        graph = build_import_graph(tmp_path)
        ctx = format_context_for_agent(graph, "m.py")
        assert "Lines:" in ctx

    def test_includes_symbols(self, tmp_path):
        _write(tmp_path / "m.py", "def hello():\n    pass\n\nclass World:\n    pass\n")
        graph = build_import_graph(tmp_path)
        ctx = format_context_for_agent(graph, "m.py")
        assert "hello" in ctx
        assert "World" in ctx

    def test_includes_imports(self, tmp_path):
        _write(tmp_path / "a.py", "from b import x\n")
        _write(tmp_path / "b.py", "x = 1\n")
        graph = build_import_graph(tmp_path)
        ctx = format_context_for_agent(graph, "a.py")
        assert "Imports:" in ctx
        assert "b.py" in ctx

    def test_includes_imported_by(self, tmp_path):
        _write(tmp_path / "a.py", "from b import x\n")
        _write(tmp_path / "b.py", "x = 1\n")
        graph = build_import_graph(tmp_path)
        ctx = format_context_for_agent(graph, "b.py")
        assert "Imported by:" in ctx
        assert "a.py" in ctx

    def test_includes_related_files(self, tmp_path):
        _write(tmp_path / "a.py", "from b import x\n")
        _write(tmp_path / "b.py", "from c import y\nx = 1\n")
        _write(tmp_path / "c.py", "y = 2\n")
        graph = build_import_graph(tmp_path)
        ctx = format_context_for_agent(graph, "a.py", depth=2)
        assert "Related files" in ctx

    def test_includes_test_files(self, tmp_path):
        _write(tmp_path / "foo.py", "x = 1\n")
        _write(tmp_path / "tests" / "test_foo.py", "from foo import x\n")
        graph = build_import_graph(tmp_path)
        ctx = format_context_for_agent(graph, "foo.py")
        assert "Test files:" in ctx


# ---------------------------------------------------------------------------
# find_related_files edge cases
# ---------------------------------------------------------------------------


class TestFindRelatedEdgeCases:

    def test_target_not_in_graph(self):
        graph = ImportGraph()
        result = find_related_files(graph, "missing.py")
        assert result == ["missing.py"]

    def test_depth_zero_returns_only_target(self, tmp_path):
        _write(tmp_path / "a.py", "from b import x\n")
        _write(tmp_path / "b.py", "x = 1\n")
        graph = build_import_graph(tmp_path)
        result = find_related_files(graph, "a.py", depth=0)
        assert result == ["a.py"]

    def test_isolated_node(self, tmp_path):
        _write(tmp_path / "alone.py", "x = 1\n")
        graph = build_import_graph(tmp_path)
        result = find_related_files(graph, "alone.py", depth=3)
        assert result == ["alone.py"]


# ---------------------------------------------------------------------------
# find_test_files edge cases
# ---------------------------------------------------------------------------


class TestFindTestFilesEdgeCases:

    def test_no_test_files(self, tmp_path):
        _write(tmp_path / "foo.py", "x = 1\n")
        graph = build_import_graph(tmp_path)
        result = find_test_files(graph, "foo.py")
        assert result == []

    def test_test_file_by_name_match(self, tmp_path):
        _write(tmp_path / "bar.py", "x = 1\n")
        _write(tmp_path / "tests" / "test_bar.py", "# no imports\n")
        graph = build_import_graph(tmp_path)
        result = find_test_files(graph, "bar.py")
        assert any("test_bar.py" in r for r in result)
