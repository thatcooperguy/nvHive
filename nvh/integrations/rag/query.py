"""Retrieve top-k chunks for a question; let the caller format the prompt.

Keeping this thin matters: the Wizard chat already owns prompt assembly
and tool integration. The retrieval layer's only job is "given a question
and a collection, return the most relevant chunks with their sources."
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from nvh.integrations.rag.embedder import embed_one
from nvh.integrations.rag.store import RagStore, default_collection

logger = logging.getLogger(__name__)


async def ask(
    question: str,
    *,
    collection: str | None = None,
    top_k: int = 5,
    home_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Embed the question, run a top-k cosine search, return chunks.

    Returns ``{ok, collection, question, chunks: [{source, chunk_index, text, score}],
    error?}``. The Wizard wraps this with prompt formatting; bare callers
    can also stitch the chunks into their own context.
    """
    collection = collection or default_collection()
    question = (question or "").strip()
    if not question:
        return {"ok": False, "error": "Empty question."}

    try:
        qvec = await embed_one(question)
    except Exception as exc:
        return {"ok": False, "error": f"Failed to embed question: {exc}", "collection": collection}

    with RagStore(home_dir=home_dir) as store:
        chunks = store.search(collection=collection, query_vector=qvec, top_k=top_k)

    return {
        "ok": True,
        "collection": collection,
        "question": question,
        "chunks": [
            {
                "source": c.source,
                "chunk_index": c.chunk_index,
                "text": c.text,
                "score": round(c.score, 4),
            }
            for c in chunks
        ],
    }


def format_context_block(chunks: list[dict[str, Any]], *, max_chars: int = 4000) -> str:
    """Render retrieved chunks as a single prompt-ready context block.

    Output is sized to fit comfortably inside small-model context windows;
    if you need more, raise ``max_chars`` at the call site.
    """
    if not chunks:
        return ""
    lines: list[str] = ["Retrieved context from your indexed folder:"]
    remaining = max_chars
    for i, chunk in enumerate(chunks, start=1):
        source = Path(chunk.get("source", "?")).name
        snippet = chunk.get("text", "").strip()
        entry = f"\n[{i}] {source} (chunk {chunk.get('chunk_index', 0)}, score {chunk.get('score', 0)}):\n{snippet}"
        if len(entry) > remaining:
            entry = entry[: max(0, remaining)]
            lines.append(entry)
            break
        remaining -= len(entry)
        lines.append(entry)
    return "".join(lines)
