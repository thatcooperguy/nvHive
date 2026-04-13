"""Tests for nvh.core.code_analysis."""

from __future__ import annotations

from pathlib import Path

from nvh.core.code_analysis import (
    AnalysisReport,
    CodeSmell,
    analyze_directory,
    analyze_file,
    format_analysis_report,
)


def _write(tmp_path: Path, name: str, content: str) -> Path:
    p = tmp_path / name
    p.write_text(content, encoding="utf-8")
    return p


def test_detect_long_function(tmp_path: Path) -> None:
    body = "\n".join([f"    x = {i}" for i in range(55)])
    _write(tmp_path, "long.py", f'def big():\n    """Doc."""\n{body}\n')
    smells = analyze_file(tmp_path / "long.py")
    assert any(s.smell_type == "long_function" for s in smells)


def test_detect_deep_nesting(tmp_path: Path) -> None:
    code = "def f():\n" + "    " * 1 + '"""D."""\n'
    code += "    if True:\n        if True:\n            if True:\n"
    code += "                if True:\n                    if True:\n"
    code += "                        x = 1\n"
    _write(tmp_path, "nested.py", code)
    smells = analyze_file(tmp_path / "nested.py")
    assert any(s.smell_type == "deep_nesting" for s in smells)


def test_detect_missing_docstring(tmp_path: Path) -> None:
    _write(tmp_path, "nodoc.py", "def foo():\n    pass\n")
    smells = analyze_file(tmp_path / "nodoc.py")
    assert any(s.smell_type == "missing_docstring" for s in smells)


def test_no_false_positive_docstring(tmp_path: Path) -> None:
    _write(tmp_path, "hasdoc.py", 'def foo():\n    """Good."""\n    pass\n')
    smells = analyze_file(tmp_path / "hasdoc.py")
    assert not any(s.smell_type == "missing_docstring" for s in smells)


def test_detect_magic_numbers(tmp_path: Path) -> None:
    _write(tmp_path, "magic.py", 'def f():\n    """D."""\n    return x + 42\n')
    smells = analyze_file(tmp_path / "magic.py")
    assert any(s.smell_type == "magic_number" for s in smells)


def test_no_magic_number_for_constants(tmp_path: Path) -> None:
    _write(tmp_path, "const.py", "MAX_RETRY = 10\n")
    smells = analyze_file(tmp_path / "const.py")
    assert not any(s.smell_type == "magic_number" for s in smells)


def test_detect_complex_conditional(tmp_path: Path) -> None:
    cond = "    if a and b and c and d or e:\n        pass\n"
    _write(tmp_path, "cond.py", f'def f():\n    """D."""\n{cond}')
    smells = analyze_file(tmp_path / "cond.py")
    assert any(s.smell_type == "complex_conditional" for s in smells)


def test_analyze_directory(tmp_path: Path) -> None:
    _write(tmp_path, "a.py", 'def a():\n    """D."""\n    return 99\n')
    _write(tmp_path, "b.py", 'def b():\n    """D."""\n    return 0\n')
    report = analyze_directory(tmp_path)
    assert report.files_analyzed == 2
    assert report.total_lines > 0


def test_tech_debt_score_calculation(tmp_path: Path) -> None:
    body = "\n".join([f"    x = {i}" for i in range(55)])
    _write(tmp_path, "debt.py", f'def big():\n    """D."""\n{body}\n')
    report = analyze_directory(tmp_path)
    assert 0 <= report.tech_debt_score <= 100


def test_missing_tests_detection(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    _write(src, "mod.py", 'def f():\n    """D."""\n    pass\n')
    report = analyze_directory(tmp_path)
    assert any("mod.py" in m for m in report.missing_tests)


def test_format_report_non_empty() -> None:
    report = AnalysisReport(
        files_analyzed=2,
        total_lines=100,
        smells=[
            CodeSmell("f.py", 10, "long_function", "high", "Too long.", "Split."),
        ],
        tech_debt_score=45.0,
        complexity_hotspots=[("f.py", 3)],
        missing_tests=["g.py"],
        refactoring_suggestions=["Extract helpers."],
    )
    text = format_analysis_report(report)
    assert "Code Analysis Report" in text
    assert "long_function" in text
    assert "45.0" in text
    assert "g.py" in text
    assert "Extract helpers" in text
