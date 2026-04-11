"""Cross-session memory for the nvhive agentic coding feature.

Stores project knowledge in ``.nvhive/agent-memory.json`` so subsequent runs start faster.
"""

from __future__ import annotations

import json
import logging
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)
_MEMORY_DIR, _MEMORY_FILE, _MAX_SESSIONS = ".nvhive", "agent-memory.json", 20


@dataclass
class SessionRecord:
    task: str = ""
    files_modified: list[str] = field(default_factory=list)
    outcome: str = ""
    timestamp: str = ""


@dataclass
class CodingConventions:
    indentation: str = ""
    import_style: str = ""


@dataclass
class AgentMemory:
    """Persistent project-level knowledge for the agentic coder."""
    project_root: str = ""
    detected_language: str = ""
    test_framework: str = ""
    linter: str = ""
    file_count: int = 0
    key_files: list[str] = field(default_factory=list)
    coding_conventions: CodingConventions = field(default_factory=CodingConventions)
    past_sessions: list[SessionRecord] = field(default_factory=list)


def load_memory(working_dir: Path) -> AgentMemory:
    """Load agent memory from disk; returns empty memory if not found."""
    mem_path = working_dir / _MEMORY_DIR / _MEMORY_FILE
    if not mem_path.exists():
        return AgentMemory(project_root=str(working_dir))
    try:
        raw = json.loads(mem_path.read_text(encoding="utf-8"))
        conventions = raw.pop("coding_conventions", {})
        sessions = raw.pop("past_sessions", [])
        return AgentMemory(
            **{k: v for k, v in raw.items() if k in AgentMemory.__dataclass_fields__},
            coding_conventions=CodingConventions(**conventions),
            past_sessions=[SessionRecord(**s) for s in sessions],
        )
    except Exception:
        logger.warning("Corrupted agent memory at %s — starting fresh", mem_path)
        return AgentMemory(project_root=str(working_dir))


def save_memory(memory: AgentMemory, working_dir: Path) -> None:
    """Atomic write of agent memory to disk."""
    mem_dir = working_dir / _MEMORY_DIR
    mem_dir.mkdir(parents=True, exist_ok=True)
    dest = mem_dir / _MEMORY_FILE
    data = json.dumps(asdict(memory), indent=2, ensure_ascii=False)
    tmp_fd = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=mem_dir, suffix=".tmp", delete=False,
    )
    try:
        tmp_path = Path(tmp_fd.name)
        tmp_fd.write(data)
        tmp_fd.close()
        tmp_path.replace(dest)
    except BaseException:
        tmp_fd.close()
        Path(tmp_fd.name).unlink(missing_ok=True)
        raise


def update_memory_from_result(memory: AgentMemory, result: object) -> AgentMemory:
    """Append session data from *result* (duck-typed: task, files_modified, outcome)."""
    memory.past_sessions.append(SessionRecord(
        task=getattr(result, "task", ""),
        files_modified=list(getattr(result, "files_modified", [])),
        outcome=getattr(result, "outcome", ""),
        timestamp=datetime.now(UTC).isoformat(),
    ))
    if len(memory.past_sessions) > _MAX_SESSIONS:
        memory.past_sessions = memory.past_sessions[-_MAX_SESSIONS:]
    return memory


def format_memory_context(memory: AgentMemory) -> str:
    """Format memory into a text block for injection into the planner prompt."""
    lines: list[str] = ["# Project Memory"]
    if memory.project_root:
        lines.append(f"Root: {memory.project_root}")
    if memory.detected_language:
        lines.append(f"Language: {memory.detected_language}")
    if memory.test_framework:
        lines.append(f"Tests: {memory.test_framework}")
    if memory.linter:
        lines.append(f"Linter: {memory.linter}")
    if memory.file_count:
        lines.append(f"Files: {memory.file_count}")
    if memory.key_files:
        lines.append("Key files: " + ", ".join(memory.key_files))
    conv = memory.coding_conventions
    if conv.indentation or conv.import_style:
        lines.append(f"Style: indent={conv.indentation}, imports={conv.import_style}")
    if memory.past_sessions:
        lines.append(f"\n## Recent sessions ({len(memory.past_sessions)})")
        for s in memory.past_sessions[-5:]:
            lines.append(f"- [{s.timestamp}] {s.task} -> {s.outcome}")
    return "\n".join(lines)
