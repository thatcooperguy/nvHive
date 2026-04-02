"""NVHive Memory — persistent context across sessions.

Stores facts, preferences, and decisions that persist between sessions.
Memory is stored as simple markdown files in ~/.hive/memory/.

Types of memory:
  - user:     User preferences and profile info
  - project:  Project-specific context and decisions
  - feedback: What worked/didn't work (learning from corrections)
  - fact:     Important facts to remember

Memory is automatically injected into system prompts alongside HIVE.md.

Usage:
  /remember This project uses Python 3.12 and pytest
  /remember I prefer concise answers without code comments
  /forget Remove the memory about Python version
  /memories List all stored memories
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class Memory:
    id: str                # unique identifier
    type: str              # user, project, feedback, fact
    content: str           # the memory content
    created_at: str        # ISO timestamp
    source: str = ""       # where this came from (user, auto, conversation)
    relevance: float = 1.0 # 0-1, used for sorting (higher = more relevant)
    access_count: int = 0  # how often this memory has been used
    last_accessed: str = ""


class MemoryStore:
    """File-based persistent memory store."""

    def __init__(self, memory_dir: Path | None = None):
        self.memory_dir = memory_dir or (Path.home() / ".hive" / "memory")
        self.memory_dir.mkdir(parents=True, exist_ok=True)
        self._memories: list[Memory] = []
        self._load()

    def _load(self) -> None:
        """Load all memories from disk."""
        self._memories = []
        memory_file = self.memory_dir / "memories.json"
        if memory_file.exists():
            try:
                data = json.loads(memory_file.read_text())
                for m in data:
                    self._memories.append(Memory(**m))
            except Exception as e:
                logger.warning(f"Failed to load memories: {e}")

    def _save(self) -> None:
        """Save all memories to disk."""
        memory_file = self.memory_dir / "memories.json"
        data = [
            {
                "id": m.id,
                "type": m.type,
                "content": m.content,
                "created_at": m.created_at,
                "source": m.source,
                "relevance": m.relevance,
                "access_count": m.access_count,
                "last_accessed": m.last_accessed,
            }
            for m in self._memories
        ]
        memory_file.write_text(json.dumps(data, indent=2))

    def add(self, content: str, memory_type: str = "fact", source: str = "user") -> Memory:
        """Add a new memory."""
        import uuid
        memory = Memory(
            id=str(uuid.uuid4())[:8],
            type=memory_type,
            content=content,
            created_at=datetime.now(UTC).isoformat(),
            source=source,
            last_accessed=datetime.now(UTC).isoformat(),
        )
        self._memories.append(memory)
        self._save()
        logger.info(f"Memory added: [{memory_type}] {content[:50]}...")
        return memory

    def remove(self, memory_id: str) -> bool:
        """Remove a memory by ID."""
        before = len(self._memories)
        self._memories = [m for m in self._memories if m.id != memory_id]
        if len(self._memories) < before:
            self._save()
            return True
        return False

    def forget(self, keyword: str) -> int:
        """Remove all memories containing a keyword."""
        before = len(self._memories)
        self._memories = [m for m in self._memories if keyword.lower() not in m.content.lower()]
        removed = before - len(self._memories)
        if removed > 0:
            self._save()
        return removed

    def search(self, query: str) -> list[Memory]:
        """Search memories by keyword."""
        query_lower = query.lower()
        results = [m for m in self._memories if query_lower in m.content.lower()]
        # Mark as accessed
        for m in results:
            m.access_count += 1
            m.last_accessed = datetime.now(UTC).isoformat()
        if results:
            self._save()
        return results

    def get_all(self, memory_type: str | None = None) -> list[Memory]:
        """Get all memories, optionally filtered by type."""
        if memory_type:
            return [m for m in self._memories if m.type == memory_type]
        return list(self._memories)

    def get_context_prompt(self, max_memories: int = 20) -> str:
        """Build a context string from memories for injection into system prompts.

        Sorted by relevance * access_count — most useful memories first.
        """
        if not self._memories:
            return ""

        # Sort by usefulness (relevance * access_count, with recency boost)
        scored = sorted(
            self._memories,
            key=lambda m: m.relevance * (m.access_count + 1),
            reverse=True,
        )[:max_memories]

        lines = ["<memory>", "Things I remember about you and this project:"]
        for m in scored:
            lines.append(f"  [{m.type}] {m.content}")
        lines.append("</memory>")

        return "\n".join(lines)

    def clear_all(self) -> int:
        """Clear all memories."""
        count = len(self._memories)
        self._memories = []
        self._save()
        return count


# Module-level singleton
_store: MemoryStore | None = None

def get_memory_store() -> MemoryStore:
    global _store
    if _store is None:
        _store = MemoryStore()
    return _store
