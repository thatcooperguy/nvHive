"""Text chunking for RAG ingest.

Simple paragraph-aware sliding window. Naive on purpose — every "smart"
chunker we've seen either over-fits to one document style or makes the
embedding cost balloon. ~600 char chunks with 80 char overlap is a known
sweet spot for nomic-embed-text and similar local embedders.
"""

from __future__ import annotations

from collections.abc import Iterator

DEFAULT_CHUNK_CHARS = 600
DEFAULT_OVERLAP_CHARS = 80


def chunk_text(
    text: str,
    *,
    chunk_chars: int = DEFAULT_CHUNK_CHARS,
    overlap_chars: int = DEFAULT_OVERLAP_CHARS,
) -> list[str]:
    """Split text into overlapping chunks, snapping to paragraph breaks
    where the snap point is within ~20% of the target size."""
    text = text.strip()
    if not text:
        return []
    if len(text) <= chunk_chars:
        return [text]

    chunks: list[str] = []
    start = 0
    n = len(text)
    while start < n:
        end = min(start + chunk_chars, n)
        if end < n:
            # Try to snap to the last paragraph break within the chunk so
            # we don't slice mid-sentence when a clean boundary exists.
            window_start = max(start + int(chunk_chars * 0.8), start + 1)
            snap = text.rfind("\n\n", window_start, end)
            if snap == -1:
                snap = text.rfind("\n", window_start, end)
            if snap == -1:
                snap = text.rfind(". ", window_start, end)
                if snap != -1:
                    snap += 1  # include the period
            if snap != -1:
                end = snap
        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)
        if end >= n:
            break
        start = max(end - overlap_chars, start + 1)
    return chunks


def iter_chunks(text: str, **kwargs) -> Iterator[tuple[int, str]]:
    """Yield ``(index, chunk_text)`` for streaming/ingest callers."""
    yield from enumerate(chunk_text(text, **kwargs))
