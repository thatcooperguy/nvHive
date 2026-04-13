"""Advanced code analysis — structural issues, code smells, tech debt."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class CodeSmell:
    """A detected code smell in a source file."""

    file: str
    line: int | None
    smell_type: str
    severity: str  # "high", "medium", "low"
    description: str
    suggestion: str


@dataclass
class AnalysisReport:
    """Aggregated analysis results for a codebase."""

    files_analyzed: int = 0
    total_lines: int = 0
    smells: list[CodeSmell] = field(default_factory=list)
    tech_debt_score: float = 0.0
    complexity_hotspots: list[tuple[str, int]] = field(default_factory=list)
    missing_tests: list[str] = field(default_factory=list)
    refactoring_suggestions: list[str] = field(default_factory=list)


_SEVERITY_WEIGHT = {"high": 3, "medium": 2, "low": 1}
_MAGIC_NUM_RE = re.compile(r"(?<!\w)(\d+\.?\d*)(?!\w)")
_UPPER_ASSIGN_RE = re.compile(r"^[A-Z_][A-Z0-9_]*\s*=")


def analyze_file(file_path: Path) -> list[CodeSmell]:
    """Detect code smells in a single Python file."""
    smells: list[CodeSmell] = []
    name = str(file_path)
    try:
        lines = file_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return smells

    # --- long file ---
    if len(lines) > 500:
        smells.append(CodeSmell(
            name, None, "long_file", "medium",
            f"File has {len(lines)} lines (>500).",
            "Split into smaller modules.",
        ))

    func_start: int | None = None
    func_name = ""
    in_class = False

    for idx, raw in enumerate(lines, 1):
        stripped = raw.lstrip()
        indent = len(raw) - len(stripped)

        # --- deep nesting (>4 levels, assuming 4-space indent) ---
        step = 4
        level = indent // step if step else 0
        if level > 4 and stripped and not stripped.startswith("#"):
            smells.append(CodeSmell(
                name, idx, "deep_nesting", "medium",
                f"Nesting depth {level} at line {idx}.",
                "Extract nested logic into helper functions.",
            ))

        # --- complex conditional ---
        if stripped.startswith(("if ", "elif ")) or " if " in stripped:
            bool_ops = stripped.count(" and ") + stripped.count(" or ")
            if bool_ops > 3:
                smells.append(CodeSmell(
                    name, idx, "complex_conditional", "medium",
                    f"Conditional with {bool_ops} boolean operators.",
                    "Extract conditions into well-named variables or a helper.",
                ))

        # --- magic numbers ---
        if not _UPPER_ASSIGN_RE.match(stripped) and not stripped.startswith("#"):
            for m in _MAGIC_NUM_RE.finditer(stripped):
                val = float(m.group(1))
                if val > 1 and "def " not in stripped and "import" not in stripped:
                    smells.append(CodeSmell(
                        name, idx, "magic_number", "low",
                        f"Magic number {m.group(1)} at line {idx}.",
                        "Extract into a named constant.",
                    ))
                    break  # one per line is enough

        # --- function / class tracking ---
        if stripped.startswith("class "):
            in_class = True
            # check docstring on next non-blank line
            _check_docstring(lines, idx, name, "class", smells)

        if stripped.startswith("def "):
            # close previous function span
            if func_start is not None:
                length = idx - func_start
                if length > 50:
                    smells.append(CodeSmell(
                        name, func_start, "long_function", "high",
                        f"Function '{func_name}' is {length} lines (>50).",
                        "Break into smaller functions.",
                    ))
            func_start = idx
            func_name = stripped.split("(")[0].replace("def ", "")
            _check_docstring(lines, idx, name, "function", smells)

    # close last function
    if func_start is not None:
        length = len(lines) - func_start + 1
        if length > 50:
            smells.append(CodeSmell(
                name, func_start, "long_function", "high",
                f"Function '{func_name}' is {length} lines (>50).",
                "Break into smaller functions.",
            ))

    _ = in_class  # consumed for future expansion
    return smells


def _check_docstring(
    lines: list[str], def_line: int, name: str, kind: str, smells: list[CodeSmell],
) -> None:
    """Check whether the construct at *def_line* has a docstring."""
    for subsequent in lines[def_line:]:  # lines after the def/class line
        s = subsequent.strip()
        if not s or s.startswith("#"):
            continue
        if s.startswith(('"""', "'''", '"', "'")):
            return  # has a docstring (or at least a string)
        break
    smells.append(CodeSmell(
        name, def_line, "missing_docstring", "low",
        f"Missing docstring for {kind} at line {def_line}.",
        f"Add a docstring to the {kind}.",
    ))


def analyze_directory(
    directory: Path, pattern: str = "**/*.py",
) -> AnalysisReport:
    """Analyze all matching files under *directory*."""
    report = AnalysisReport()
    smell_counts: dict[str, int] = {}

    test_files = {p.name for p in directory.rglob("test_*.py")}

    for path in sorted(directory.glob(pattern)):
        if path.name.startswith("__"):
            continue
        try:
            line_count = len(path.read_text(encoding="utf-8", errors="replace").splitlines())
        except OSError:
            continue
        report.files_analyzed += 1
        report.total_lines += line_count
        file_smells = analyze_file(path)
        report.smells.extend(file_smells)
        if file_smells:
            smell_counts[str(path)] = len(file_smells)

        # missing tests
        if not path.name.startswith("test_"):
            expected = f"test_{path.name}"
            if expected not in test_files:
                report.missing_tests.append(str(path))

    # complexity hotspots — top 5 files by smell count
    report.complexity_hotspots = sorted(
        smell_counts.items(), key=lambda x: x[1], reverse=True,
    )[:5]

    # tech debt score (0-100)
    weighted = sum(_SEVERITY_WEIGHT.get(s.severity, 1) for s in report.smells)
    max_possible = max(report.files_analyzed * 10, 1)
    report.tech_debt_score = min(round(weighted / max_possible * 100, 1), 100.0)

    # refactoring suggestions
    types = {s.smell_type for s in report.smells}
    if "long_function" in types:
        report.refactoring_suggestions.append("Extract long functions into smaller helpers.")
    if "deep_nesting" in types:
        report.refactoring_suggestions.append("Flatten deeply nested code with early returns.")
    if "magic_number" in types:
        report.refactoring_suggestions.append("Replace magic numbers with named constants.")
    if "missing_docstring" in types:
        report.refactoring_suggestions.append("Add docstrings to public functions and classes.")

    return report


def format_analysis_report(report: AnalysisReport) -> str:
    """Return a human-readable summary of the analysis report."""
    parts: list[str] = []
    parts.append("=== Code Analysis Report ===")
    parts.append(f"Files analyzed: {report.files_analyzed}")
    parts.append(f"Total lines:    {report.total_lines}")
    parts.append(f"Tech-debt score: {report.tech_debt_score}/100")
    parts.append("")

    if report.smells:
        parts.append(f"--- Code Smells ({len(report.smells)}) ---")
        for s in report.smells:
            loc = f":{s.line}" if s.line else ""
            parts.append(f"  [{s.severity.upper()}] {s.smell_type} @ {s.file}{loc}")
            parts.append(f"    {s.description}")
        parts.append("")

    if report.complexity_hotspots:
        parts.append("--- Complexity Hotspots ---")
        for path, count in report.complexity_hotspots:
            parts.append(f"  {path}: {count} smells")
        parts.append("")

    if report.missing_tests:
        parts.append(f"--- Missing Tests ({len(report.missing_tests)}) ---")
        for f in report.missing_tests:
            parts.append(f"  {f}")
        parts.append("")

    if report.refactoring_suggestions:
        parts.append("--- Suggestions ---")
        for s in report.refactoring_suggestions:
            parts.append(f"  - {s}")

    return "\n".join(parts)
