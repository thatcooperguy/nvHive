"""Agent memory lives under ``$NVH_HOME/state/agent-memory/`` (0.42).

The pre-0.42 project-local ``.nvhive/agent-memory.json`` is still read once
so existing projects keep their history; the next save lands in NVH_HOME.
Also covers the prompt-context formatter and the session-history cap.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from nvh.core.agent_memory import (
    AgentMemory,
    CodingConventions,
    SessionRecord,
    format_memory_context,
    load_memory,
    memory_path,
    save_memory,
    update_memory_from_result,
)


@pytest.fixture()
def nvh_home(tmp_path, monkeypatch) -> Path:
    home = tmp_path / "home"
    monkeypatch.setenv("NVH_HOME", str(home))
    for var in ("NVHIVE_HOME", "NVH_STATE"):
        monkeypatch.delenv(var, raising=False)
    return home


def test_round_trip_lands_in_nvh_home_state(nvh_home, tmp_path: Path) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    save_memory(AgentMemory(project_root=str(project), detected_language="Python"), project)

    path = memory_path(project)
    assert path.parent == nvh_home / "state" / "agent-memory"
    assert path.name.startswith("proj-")
    assert path.is_file()
    assert not (project / ".nvhive").exists()
    assert load_memory(project).detected_language == "Python"


def test_two_projects_do_not_share_a_file(nvh_home, tmp_path: Path) -> None:
    a = tmp_path / "same-name" / "proj"
    b = tmp_path / "other" / "proj"
    a.mkdir(parents=True)
    b.mkdir(parents=True)
    assert memory_path(a) != memory_path(b)


def test_legacy_project_local_file_is_read_then_migrated(nvh_home, tmp_path: Path) -> None:
    project = tmp_path / "old"
    (project / ".nvhive").mkdir(parents=True)
    (project / ".nvhive" / "agent-memory.json").write_text(json.dumps({
        "project_root": str(project), "detected_language": "Go",
        "coding_conventions": {}, "past_sessions": [],
    }), encoding="utf-8")

    loaded = load_memory(project)
    assert loaded.detected_language == "Go"
    save_memory(loaded, project)
    assert memory_path(project).is_file()
    assert load_memory(project).detected_language == "Go"


def test_missing_memory_is_empty(nvh_home, tmp_path: Path) -> None:
    project = tmp_path / "fresh"
    project.mkdir()
    memory = load_memory(project)
    assert memory.project_root == str(project)
    assert memory.past_sessions == []


class TestFormatMemoryContext:
    def test_rich_data(self):
        mem = AgentMemory(
            project_root="/proj",
            detected_language="Python",
            test_framework="pytest",
            linter="ruff",
            file_count=42,
            key_files=["main.py", "config.py"],
            coding_conventions=CodingConventions(
                indentation="4 spaces", import_style="isort"
            ),
            past_sessions=[
                SessionRecord(task="fix bug", outcome="ok", timestamp="2025-01-01"),
            ],
        )
        text = format_memory_context(mem)
        assert "Python" in text
        assert "pytest" in text
        assert "ruff" in text
        assert "42" in text
        assert "main.py" in text
        assert "4 spaces" in text
        assert "isort" in text
        assert "fix bug" in text

    def test_empty_memory_minimal(self):
        mem = AgentMemory()
        text = format_memory_context(mem)
        assert "Project Memory" in text


class TestUpdateMemoryCap:
    def test_cap_at_20_sessions(self):
        mem = AgentMemory()
        for i in range(25):
            obj = MagicMock()
            obj.task = f"task {i}"
            obj.files_modified = []
            obj.outcome = "done"
            mem = update_memory_from_result(mem, obj)
        assert len(mem.past_sessions) == 20
        # Oldest sessions trimmed — last task should be "task 24"
        assert mem.past_sessions[-1].task == "task 24"
