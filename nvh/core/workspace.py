"""Multi-repo workspace manager for nvhive agent context."""

from __future__ import annotations

import json
import os
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from nvh.core.code_graph import build_import_graph

_SKIP_DIRS = {"node_modules", ".git", "__pycache__", ".next", "venv", ".venv", "dist"}
_EXT_LANG: dict[str, str] = {
    ".py": "python", ".js": "javascript", ".ts": "typescript",
    ".go": "go", ".rs": "rust",
}


@dataclass
class RepoInfo:
    """Metadata for a single repository in the workspace."""

    path: Path
    name: str
    language: str = "unknown"
    file_count: int = 0
    line_count: int = 0
    readonly: bool = True


@dataclass
class Workspace:
    """A collection of repositories used for cross-repo context."""

    repos: list[RepoInfo] = field(default_factory=list)
    name: str = ""
    created_at: float = field(default_factory=time.time)


@dataclass
class WorkspaceSummary:
    """Aggregated stats across all repos in a workspace."""

    total_files: int = 0
    total_lines: int = 0
    languages: dict[str, int] = field(default_factory=dict)
    cross_repo_imports: list[tuple[str, str]] = field(default_factory=list)
    shared_patterns: list[str] = field(default_factory=list)


def _walk_source(root: Path):
    """Yield (dirpath, filename) for source files under *root*."""
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for fn in filenames:
            if Path(fn).suffix in _EXT_LANG:
                yield dirpath, fn


def _count_files_and_lines(root: Path) -> tuple[int, int]:
    files = lines = 0
    for dirpath, fn in _walk_source(root):
        files += 1
        try:
            lines += Path(dirpath, fn).read_text(errors="replace").count("\n")
        except OSError:
            pass
    return files, lines


def _detect_language(root: Path) -> str:
    counts: Counter[str] = Counter()
    for _, fn in _walk_source(root):
        counts[Path(fn).suffix] += 1
    if not counts:
        return "unknown"
    return _EXT_LANG[counts.most_common(1)[0][0]]


def create_workspace(paths: list[str | Path], name: str = "") -> Workspace:
    """Build a workspace from a list of repo root paths."""
    repos: list[RepoInfo] = []
    for p in paths:
        root = Path(p).resolve()
        if not root.is_dir():
            msg = f"Not a directory: {root}"
            raise FileNotFoundError(msg)
        fc, lc = _count_files_and_lines(root)
        repos.append(RepoInfo(
            path=root, name=root.name, language=_detect_language(root),
            file_count=fc, line_count=lc,
        ))
    return Workspace(repos=repos, name=name)


def scan_workspace(workspace: Workspace) -> WorkspaceSummary:
    """Analyse a workspace and return a combined summary."""
    total_files = sum(r.file_count for r in workspace.repos)
    total_lines = sum(r.line_count for r in workspace.repos)
    lang_lines: dict[str, int] = {}
    for r in workspace.repos:
        lang_lines[r.language] = lang_lines.get(r.language, 0) + r.line_count
    # Cross-repo imports
    repo_names = {r.name for r in workspace.repos}
    cross: list[tuple[str, str]] = []
    for repo in workspace.repos:
        graph = build_import_graph(repo.path)
        for node in graph.nodes.values():
            for imp in node.imports:
                top = imp.split(".")[0]
                if top in repo_names and top != repo.name:
                    cross.append((repo.name, top))
    # Shared filename patterns
    per_repo: list[set[str]] = []
    for repo in workspace.repos:
        per_repo.append({fn for _, fn in _walk_source(repo.path)})
    shared = sorted(set.intersection(*per_repo)) if len(per_repo) > 1 else []
    return WorkspaceSummary(
        total_files=total_files, total_lines=total_lines, languages=lang_lines,
        cross_repo_imports=list(set(cross)), shared_patterns=shared,
    )


def format_workspace_context(workspace: Workspace, summary: WorkspaceSummary) -> str:
    """Format workspace info as a text block for the agent planning prompt."""
    lines = [f"## Workspace: {workspace.name or '(unnamed)'}", ""]
    for r in workspace.repos:
        mode = "read-only" if r.readonly else "read-write"
        lines.append(f"- **{r.name}** ({r.language}, {r.file_count} files, "
                      f"{r.line_count} lines) [{mode}]")
    lines.append("")
    lines.append(f"Total: {summary.total_files} files, {summary.total_lines} lines")
    if summary.languages:
        langs = ", ".join(f"{k}: {v}" for k, v in summary.languages.items())
        lines.append(f"Languages: {langs}")
    if summary.cross_repo_imports:
        lines.append("Cross-repo imports:")
        for src, dst in summary.cross_repo_imports:
            lines.append(f"  {src} -> {dst}")
    if summary.shared_patterns:
        lines.append(f"Shared files: {', '.join(summary.shared_patterns)}")
    return "\n".join(lines)


def save_workspace(workspace: Workspace, path: Path) -> None:
    """Persist workspace config to a JSON file."""
    data = {
        "name": workspace.name, "created_at": workspace.created_at,
        "repos": [
            {"path": str(r.path), "name": r.name, "language": r.language,
             "file_count": r.file_count, "line_count": r.line_count,
             "readonly": r.readonly}
            for r in workspace.repos
        ],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2))


def load_workspace(path: Path) -> Workspace | None:
    """Load a workspace config from JSON.  Returns *None* if missing."""
    if not path.exists():
        return None
    data = json.loads(path.read_text())
    repos = [
        RepoInfo(
            path=Path(r["path"]), name=r["name"],
            language=r.get("language", "unknown"),
            file_count=r.get("file_count", 0), line_count=r.get("line_count", 0),
            readonly=r.get("readonly", True),
        )
        for r in data.get("repos", [])
    ]
    return Workspace(repos=repos, name=data.get("name", ""), created_at=data.get("created_at", 0))
