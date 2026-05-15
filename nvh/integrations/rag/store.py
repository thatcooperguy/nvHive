"""SQLite-backed chunk store with in-process cosine similarity search.

One DB file per rootless home: ``$NVH_HOME/rag/index.sqlite``. Chunks are
stored alongside their float32 embedding vector (packed via ``struct``,
not numpy, so we keep the dep footprint at stdlib + sqlite3).

For collections under a few hundred thousand chunks, an in-process cosine
scan over a single SQLite table is fast enough that adding sqlite-vec or
a separate vector DB would be premature. If a user's corpus outgrows
this we can swap in sqlite-vec without changing the public API.
"""

from __future__ import annotations

import logging
import math
import sqlite3
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from nvh.integrations.workspace.storage import nvh_home

logger = logging.getLogger(__name__)

_DEFAULT_COLLECTION = "default"


def _rag_db_path(home_dir: str | Path | None = None) -> Path:
    home, _ = nvh_home(home_dir)
    rag_dir = home / "rag"
    rag_dir.mkdir(parents=True, exist_ok=True)
    return rag_dir / "index.sqlite"


def _pack_vector(vec: list[float]) -> bytes:
    return struct.pack(f"{len(vec)}f", *vec)


def _unpack_vector(blob: bytes) -> list[float]:
    count = len(blob) // 4
    return list(struct.unpack(f"{count}f", blob))


def _cosine(a: list[float], b: list[float]) -> float:
    if len(a) != len(b):
        return 0.0
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b, strict=True):
        dot += x * y
        na += x * x
        nb += y * y
    denom = math.sqrt(na) * math.sqrt(nb)
    if denom == 0.0:
        return 0.0
    return dot / denom


@dataclass(frozen=True)
class Chunk:
    """One retrieved chunk with its source provenance."""

    source: str
    chunk_index: int
    text: str
    score: float


class RagStore:
    """SQLite-backed store of (collection, source, chunk_index, text, vec)."""

    def __init__(self, home_dir: str | Path | None = None) -> None:
        self.db_path = _rag_db_path(home_dir)
        self._conn = sqlite3.connect(self.db_path)
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._init_schema()

    def _init_schema(self) -> None:
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS chunks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                collection TEXT NOT NULL,
                source TEXT NOT NULL,
                chunk_index INTEGER NOT NULL,
                text TEXT NOT NULL,
                embedding BLOB NOT NULL,
                model TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE INDEX IF NOT EXISTS idx_chunks_collection ON chunks(collection);
            CREATE INDEX IF NOT EXISTS idx_chunks_source ON chunks(collection, source);
        """)
        self._conn.commit()

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass

    def __enter__(self) -> RagStore:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def delete_source(self, *, collection: str, source: str) -> int:
        """Drop all chunks for ``source`` so re-ingest is idempotent."""
        cur = self._conn.execute(
            "DELETE FROM chunks WHERE collection = ? AND source = ?",
            (collection, source),
        )
        self._conn.commit()
        return cur.rowcount

    def add_chunks(
        self,
        *,
        collection: str,
        source: str,
        chunks: list[str],
        vectors: list[list[float]],
        model: str,
    ) -> int:
        """Bulk-insert chunks. Caller is responsible for ``delete_source``
        first if they want re-ingest to replace rather than append."""
        if len(chunks) != len(vectors):
            raise ValueError(f"chunks ({len(chunks)}) and vectors ({len(vectors)}) length mismatch")
        if not chunks:
            return 0
        rows = [
            (collection, source, i, chunks[i], _pack_vector(vectors[i]), model)
            for i in range(len(chunks))
        ]
        self._conn.executemany(
            "INSERT INTO chunks (collection, source, chunk_index, text, embedding, model)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            rows,
        )
        self._conn.commit()
        return len(rows)

    def search(
        self,
        *,
        collection: str,
        query_vector: list[float],
        top_k: int = 5,
    ) -> list[Chunk]:
        """Return the top-k chunks by cosine similarity within ``collection``."""
        cur = self._conn.execute(
            "SELECT source, chunk_index, text, embedding FROM chunks WHERE collection = ?",
            (collection,),
        )
        scored: list[tuple[float, str, int, str]] = []
        for source, idx, text, blob in cur.fetchall():
            score = _cosine(query_vector, _unpack_vector(blob))
            scored.append((score, source, idx, text))
        scored.sort(key=lambda r: r[0], reverse=True)
        return [Chunk(source=s, chunk_index=i, text=t, score=score)
                for score, s, i, t in scored[:top_k]]

    def collection_stats(self, collection: str) -> dict[str, Any]:
        cur = self._conn.execute(
            "SELECT COUNT(*), COUNT(DISTINCT source) FROM chunks WHERE collection = ?",
            (collection,),
        )
        chunks, sources = cur.fetchone()
        return {"collection": collection, "chunks": int(chunks), "sources": int(sources)}


def list_collections(home_dir: str | Path | None = None) -> list[dict[str, Any]]:
    """Return ``[{name, chunks, sources}, ...]`` across all known collections."""
    db_path = _rag_db_path(home_dir)
    if not db_path.exists():
        return []
    with sqlite3.connect(db_path) as conn:
        cur = conn.execute(
            "SELECT collection, COUNT(*), COUNT(DISTINCT source) FROM chunks"
            " GROUP BY collection ORDER BY collection"
        )
        return [
            {"name": name, "chunks": int(c), "sources": int(s)}
            for name, c, s in cur.fetchall()
        ]


def default_collection() -> str:
    return _DEFAULT_COLLECTION
