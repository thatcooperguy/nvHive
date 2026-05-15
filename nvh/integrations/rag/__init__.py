"""Local-first RAG over a folder of documents.

The Wizard can ingest a folder of text/markdown files, embed the chunks via
the local Ollama daemon (``nomic-embed-text``), and answer questions
grounded in the retrieved chunks. Storage is a single SQLite file inside
the rootless ``NVH_HOME/rag/`` directory — no external vector DB.

Public surface:
    - ``ingest_folder(path, collection=...)`` — walk + chunk + embed + store
    - ``ask(question, collection=..., top_k=...)`` — retrieve top-k chunks
    - ``list_collections()`` — names + counts
    - ``RagStore`` — low-level handle for tests/inspection
"""

from __future__ import annotations

from nvh.integrations.rag.ingest import ingest_folder
from nvh.integrations.rag.query import ask
from nvh.integrations.rag.store import RagStore, list_collections
from nvh.integrations.rag.vault_bridge import (
    VAULT_COLLECTION,
    ask_vault,
    ensure_vault_indexed,
    ingest_vault,
)

__all__ = [
    "RagStore",
    "VAULT_COLLECTION",
    "ask",
    "ask_vault",
    "ensure_vault_indexed",
    "ingest_folder",
    "ingest_vault",
    "list_collections",
]
