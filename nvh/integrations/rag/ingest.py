"""Folder → chunks → embeddings → SQLite ingest.

The ingest walker reads .md, .txt, .rst, and .py/.js/.ts source files. We
deliberately skip binary formats here — PDFs and Word docs need a parsing
layer we haven't committed to yet, and pulling pypdf+docx2txt for a v1
RAG feature adds dependency weight users don't necessarily want.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from nvh.integrations.rag.chunker import chunk_text
from nvh.integrations.rag.embedder import embed_model_name, embed_texts
from nvh.integrations.rag.store import RagStore, default_collection

logger = logging.getLogger(__name__)

# Conservative default set — text files we can read without a parser.
# Source code is included because users frequently want to RAG over a repo.
DEFAULT_EXTENSIONS = (
    ".md", ".markdown", ".txt", ".rst",
    ".py", ".js", ".jsx", ".ts", ".tsx",
    ".go", ".rs", ".java", ".rb", ".php",
    ".c", ".cc", ".cpp", ".h", ".hpp",
    ".json", ".yaml", ".yml", ".toml",
    ".html", ".xml", ".css",
)

# Skip obvious junk so users don't accidentally embed `node_modules/`.
SKIP_DIRS = frozenset({
    ".git", ".hg", ".svn",
    "node_modules", "__pycache__", ".venv", "venv", ".tox",
    "dist", "build", ".next", ".cache", "target",
})

MAX_FILE_BYTES = 1_000_000  # 1 MB cap — protects against checked-in blobs


def _iter_files(root: Path, extensions: tuple[str, ...]) -> list[Path]:
    files: list[Path] = []
    for path in root.rglob("*"):
        if path.is_dir():
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if path.suffix.lower() not in extensions:
            continue
        try:
            if path.stat().st_size > MAX_FILE_BYTES:
                continue
        except OSError:
            continue
        files.append(path)
    return files


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        try:
            return path.read_text(encoding="latin-1")
        except Exception:
            return ""
    except OSError:
        return ""


async def ingest_folder(
    path: str | Path,
    *,
    collection: str | None = None,
    home_dir: str | Path | None = None,
    extensions: tuple[str, ...] | None = None,
    max_files: int = 2000,
) -> dict[str, Any]:
    """Walk ``path``, chunk + embed each file, store under ``collection``.

    Re-ingest is idempotent at the file level — each source file's chunks
    are deleted before re-insertion, so running ingest twice on the same
    folder doesn't duplicate rows.

    Returns ``{ok, collection, files_scanned, files_ingested, chunks, skipped,
    model, error?}``.
    """
    root = Path(path).expanduser().resolve()
    collection = collection or default_collection()
    extensions = extensions or DEFAULT_EXTENSIONS

    if not root.exists():
        return {"ok": False, "error": f"Path does not exist: {root}"}
    if not root.is_dir():
        return {"ok": False, "error": f"Path is not a directory: {root}"}

    files = _iter_files(root, extensions)
    if len(files) > max_files:
        return {
            "ok": False,
            "error": (
                f"Folder has {len(files)} matching files; capped at {max_files} for safety. "
                "Narrow the path or raise --max-files."
            ),
            "files_scanned": len(files),
        }

    model = embed_model_name()
    ingested = 0
    total_chunks = 0
    skipped: list[str] = []

    with RagStore(home_dir=home_dir) as store:
        for file_path in files:
            text = _read_text(file_path)
            if not text.strip():
                skipped.append(str(file_path))
                continue
            chunks = chunk_text(text)
            if not chunks:
                skipped.append(str(file_path))
                continue
            try:
                vectors = await embed_texts(chunks)
            except Exception as exc:
                # If embedder fails mid-walk, surface the error rather than
                # silently producing a half-indexed collection.
                return {
                    "ok": False,
                    "error": f"Embedding failed at {file_path.name}: {exc}",
                    "collection": collection,
                    "files_ingested": ingested,
                    "chunks": total_chunks,
                    "model": model,
                }
            source = str(file_path)
            store.delete_source(collection=collection, source=source)
            store.add_chunks(
                collection=collection,
                source=source,
                chunks=chunks,
                vectors=vectors,
                model=model,
            )
            ingested += 1
            total_chunks += len(chunks)

    return {
        "ok": True,
        "collection": collection,
        "files_scanned": len(files),
        "files_ingested": ingested,
        "chunks": total_chunks,
        "skipped": len(skipped),
        "model": model,
    }
