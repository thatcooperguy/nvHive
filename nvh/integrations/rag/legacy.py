"""One-shot import of the pre-0.42 ``~/.hive/knowledge`` store into the RAG index.

``nvh/core/knowledge.py`` (deleted in 0.42) kept ``documents.json`` plus
word-chunked JSON files under ``chunks/``. The original file is re-ingested
when it still exists; otherwise the text is rebuilt from the chunks so
nothing the user indexed is lost. A marker under ``$NVH_HOME/rag/`` makes
the import idempotent — ``nvh status --deep`` stops nagging once it has run.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from nvh.integrations.rag.ingest import _read_text, ingest_documents
from nvh.integrations.rag.store import _rag_db_path

LEGACY_COLLECTION = "legacy-knowledge"
# The legacy chunker split on words: 1000-word windows, 200-word overlap.
_CHUNK_WORDS = 1000
_OVERLAP_WORDS = 200


def legacy_knowledge_dir() -> Path:
    return Path.home() / ".hive" / "knowledge"


def _marker_path(home_dir: str | Path | None) -> Path:
    return _rag_db_path(home_dir).with_name("legacy-import.json")


def _load_documents(legacy_dir: Path) -> list[dict[str, Any]]:
    docs_file = legacy_dir / "documents.json"
    if not docs_file.is_file():
        return []
    try:
        data = json.loads(docs_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [d for d in data if isinstance(d, dict) and d.get("id")]


def legacy_knowledge_status(
    *, home_dir: str | Path | None = None, legacy_dir: str | Path | None = None,
) -> dict[str, Any]:
    """``{found, path, documents, imported, imported_at}`` for doctor / CLI."""
    root = Path(legacy_dir) if legacy_dir else legacy_knowledge_dir()
    docs = _load_documents(root)
    marker = _marker_path(home_dir)
    imported_at: str | None = None
    if marker.is_file():
        try:
            imported_at = json.loads(marker.read_text(encoding="utf-8")).get("imported_at")
        except (OSError, ValueError):
            imported_at = None
    return {
        "found": bool(docs),
        "path": str(root),
        "documents": len(docs),
        "imported": imported_at is not None,
        "imported_at": imported_at,
    }


def _rebuild_text(chunks_dir: Path, doc_id: str) -> str:
    """Undo the legacy overlap: keep chunk 0 whole, then drop each later
    chunk's first ``_OVERLAP_WORDS`` words."""
    words: list[str] = []
    for index, chunk_file in enumerate(sorted(chunks_dir.glob(f"{doc_id}_*.json"))):
        try:
            content = json.loads(chunk_file.read_text(encoding="utf-8")).get("content", "")
        except (OSError, ValueError):
            continue
        chunk_words = str(content).split()
        words.extend(chunk_words if index == 0 else chunk_words[_OVERLAP_WORDS:])
    return " ".join(words)


async def import_legacy_knowledge(
    *,
    home_dir: str | Path | None = None,
    legacy_dir: str | Path | None = None,
    collection: str = LEGACY_COLLECTION,
) -> dict[str, Any]:
    """Re-ingest every legacy document into ``collection`` and write the marker."""
    root = Path(legacy_dir) if legacy_dir else legacy_knowledge_dir()
    docs = _load_documents(root)
    if not docs:
        return {"ok": False, "error": f"No legacy knowledge base at {root}"}

    documents: list[tuple[str, str]] = []
    reingested = 0
    rebuilt = 0
    for doc in docs:
        original = Path(str(doc.get("path", "")))
        if original.is_file():
            documents.append((str(original.resolve()), _read_text(original)))
            reingested += 1
        else:
            text = _rebuild_text(root / "chunks", str(doc["id"]))
            if text:
                documents.append((f"legacy:{doc.get('filename', doc['id'])}", text))
                rebuilt += 1

    result = await ingest_documents(documents, collection=collection, home_dir=home_dir)
    if not result.get("ok"):
        return result

    marker = _marker_path(home_dir)
    marker.write_text(
        json.dumps({
            "imported_at": datetime.now(UTC).isoformat(),
            "legacy_dir": str(root),
            "documents": len(docs),
            "collection": collection,
        }, indent=2),
        encoding="utf-8",
    )
    result.update({
        "documents": len(docs),
        "reingested": reingested,
        "rebuilt": rebuilt,
        "marker": str(marker),
    })
    return result
