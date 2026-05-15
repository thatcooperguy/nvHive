"""Folder → chunks → embeddings → SQLite ingest.

The walker reads .md/.txt/.rst and common source-code extensions natively.
PDFs are supported via pypdf when the ``nvhive[rag]`` extra is installed;
without it, PDFs in the walk are reported back to the caller with a hint
so the user can install the optional dep rather than getting a silent skip.
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
# PDFs are included only when pypdf is installed; ``_can_read_pdf`` gates them.
DEFAULT_EXTENSIONS = (
    ".md", ".markdown", ".txt", ".rst",
    ".py", ".js", ".jsx", ".ts", ".tsx",
    ".go", ".rs", ".java", ".rb", ".php",
    ".c", ".cc", ".cpp", ".h", ".hpp",
    ".json", ".yaml", ".yml", ".toml",
    ".html", ".xml", ".css",
    ".pdf",
)

# Skip obvious junk so users don't accidentally embed `node_modules/`.
SKIP_DIRS = frozenset({
    ".git", ".hg", ".svn",
    "node_modules", "__pycache__", ".venv", "venv", ".tox",
    "dist", "build", ".next", ".cache", "target",
})

MAX_FILE_BYTES = 1_000_000  # 1 MB cap — protects against checked-in blobs
# PDFs are usually larger than text. Bumping the cap just for PDFs keeps the
# text-file limit tight without forcing users to skip every research paper.
MAX_PDF_BYTES = 20_000_000  # 20 MB


def _can_read_pdf() -> bool:
    try:
        import pypdf  # noqa: F401

        return True
    except ImportError:
        return False


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
            size = path.stat().st_size
        except OSError:
            continue
        cap = MAX_PDF_BYTES if path.suffix.lower() == ".pdf" else MAX_FILE_BYTES
        if size > cap:
            continue
        files.append(path)
    return files


def _read_pdf(path: Path) -> str:
    """Extract text from a PDF via pypdf. Returns "" on parse failure so the
    walker treats malformed PDFs the same as empty files (skip, don't fail)."""
    try:
        import pypdf
    except ImportError:
        return ""
    try:
        reader = pypdf.PdfReader(str(path))
        pages: list[str] = []
        for page in reader.pages:
            try:
                pages.append(page.extract_text() or "")
            except Exception as exc:
                logger.debug("pypdf page extract failed in %s: %s", path.name, exc)
        return "\n\n".join(p for p in pages if p.strip())
    except Exception as exc:
        logger.warning("Failed to read PDF %s: %s", path.name, exc)
        return ""


def _read_text(path: Path) -> str:
    if path.suffix.lower() == ".pdf":
        return _read_pdf(path)
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
    model, pdfs_skipped_missing_pypdf?, error?}``. The ``pdfs_skipped_...``
    key is only present when PDFs were found in the walk but pypdf isn't
    installed — the UI uses it to suggest ``pip install nvhive[rag]``.
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

    pdf_capable = _can_read_pdf()
    pdfs_skipped_missing = 0

    model = embed_model_name()
    ingested = 0
    total_chunks = 0
    skipped: list[str] = []

    with RagStore(home_dir=home_dir) as store:
        for file_path in files:
            if file_path.suffix.lower() == ".pdf" and not pdf_capable:
                pdfs_skipped_missing += 1
                continue
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

    result: dict[str, Any] = {
        "ok": True,
        "collection": collection,
        "files_scanned": len(files),
        "files_ingested": ingested,
        "chunks": total_chunks,
        "skipped": len(skipped),
        "model": model,
    }
    if pdfs_skipped_missing:
        result["pdfs_skipped_missing_pypdf"] = pdfs_skipped_missing
        result["hint"] = (
            f"Found {pdfs_skipped_missing} PDF(s) but pypdf isn't installed. "
            "Install with `pip install nvhive[rag]` to index PDFs too."
        )
    return result
