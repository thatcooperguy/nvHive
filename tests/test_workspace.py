"""Tests for nvh.core.workspace."""

from __future__ import annotations

from pathlib import Path

from nvh.core.workspace import (
    WorkspaceSummary,
    create_workspace,
    format_workspace_context,
    load_workspace,
    save_workspace,
    scan_workspace,
)


def _make_repo(tmp_path: Path, name: str, files: dict[str, str]) -> Path:
    repo = tmp_path / name
    repo.mkdir()
    for fname, content in files.items():
        (repo / fname).write_text(content)
    return repo


def test_create_workspace_single_repo(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path, "alpha", {"main.py": "x = 1\ny = 2\n", "util.py": "pass\n"})
    ws = create_workspace([repo], name="solo")
    assert ws.name == "solo"
    assert len(ws.repos) == 1
    assert ws.repos[0].name == "alpha"
    assert ws.repos[0].file_count == 2
    assert ws.repos[0].line_count >= 2
    assert ws.repos[0].readonly is True


def test_create_workspace_multi_repo(tmp_path: Path) -> None:
    r1 = _make_repo(tmp_path, "repo_a", {"app.py": "import os\n"})
    r2 = _make_repo(tmp_path, "repo_b", {"index.js": "console.log(1);\n"})
    ws = create_workspace([r1, r2])
    assert len(ws.repos) == 2
    names = {r.name for r in ws.repos}
    assert names == {"repo_a", "repo_b"}


def test_detect_language(tmp_path: Path) -> None:
    py_repo = _make_repo(tmp_path, "pyrepo", {"a.py": "\n", "b.py": "\n"})
    js_repo = _make_repo(tmp_path, "jsrepo", {"x.js": "\n", "y.js": "\n", "z.js": "\n"})
    mixed = _make_repo(tmp_path, "mixed", {"a.py": "\n", "b.js": "\n", "c.js": "\n"})

    ws_py = create_workspace([py_repo])
    assert ws_py.repos[0].language == "python"

    ws_js = create_workspace([js_repo])
    assert ws_js.repos[0].language == "javascript"

    ws_mix = create_workspace([mixed])
    assert ws_mix.repos[0].language == "javascript"


def test_workspace_summary_fields(tmp_path: Path) -> None:
    r1 = _make_repo(tmp_path, "svc", {"models.py": "class M: pass\n", "config.py": "A=1\n"})
    r2 = _make_repo(tmp_path, "lib", {"models.py": "class N: pass\n", "config.py": "B=2\n"})
    ws = create_workspace([r1, r2], name="duo")
    summary = scan_workspace(ws)

    assert isinstance(summary, WorkspaceSummary)
    assert summary.total_files == 4
    assert summary.total_lines >= 4
    assert "python" in summary.languages
    assert "models.py" in summary.shared_patterns
    assert "config.py" in summary.shared_patterns


def test_save_and_load_workspace_roundtrip(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path, "proj", {"run.py": "print(1)\n"})
    ws = create_workspace([repo], name="roundtrip")
    cfg = tmp_path / ".nvhive" / "workspace.json"
    save_workspace(ws, cfg)
    assert cfg.exists()

    loaded = load_workspace(cfg)
    assert loaded is not None
    assert loaded.name == "roundtrip"
    assert len(loaded.repos) == 1
    assert loaded.repos[0].name == "proj"
    assert loaded.repos[0].language == "python"

    # Missing file returns None
    assert load_workspace(tmp_path / "nope.json") is None


def test_format_workspace_context_non_empty(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path, "ctx", {"hello.py": "print('hi')\n"})
    ws = create_workspace([repo], name="demo")
    summary = scan_workspace(ws)
    text = format_workspace_context(ws, summary)
    assert "demo" in text
    assert "ctx" in text
    assert "python" in text
    assert "read-only" in text


def test_create_workspace_bad_path(tmp_path: Path) -> None:
    import pytest

    with pytest.raises(FileNotFoundError):
        create_workspace([tmp_path / "nonexistent"])
