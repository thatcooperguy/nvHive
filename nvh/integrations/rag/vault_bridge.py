"""Vault → RAG bridge — make the user's own notes searchable by default.

The nvHive Vault is a Markdown directory under ``$NVH_HOME/vault/`` that
holds product memory + user notes. It's the obvious first thing a user
would want to RAG over, and it's already on disk — no extra steps.

This module wires the vault into a dedicated ``vault`` collection so the
Wizard can answer "what did I write about <X>?" without forcing the user
to run ``rag_ingest`` first.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

VAULT_COLLECTION = "vault"

# Only walk the kinds of files Vault users actually put notes in. The Vault
# might pick up images or attachments next to notes; we don't want to embed
# those.
_VAULT_EXTENSIONS = (".md", ".markdown", ".txt", ".rst")


def _vault_dir(home_dir: str | Path | None = None) -> Path:
    """Resolve the vault directory without importing the workspace module
    eagerly — the bridge is imported by Wizard tool registration, and we
    want that import cheap."""
    from nvh.integrations.workspace.storage import storage_layout

    return storage_layout(home_dir).home / "vault"


def vault_exists(home_dir: str | Path | None = None) -> bool:
    return _vault_dir(home_dir).is_dir()


async def ingest_vault(home_dir: str | Path | None = None) -> dict[str, Any]:
    """Walk the vault and replace the ``vault`` collection with fresh chunks."""
    from nvh.integrations.rag.ingest import ingest_folder

    vault = _vault_dir(home_dir)
    if not vault.is_dir():
        return {
            "ok": False,
            "error": f"Vault not initialized at {vault}. Run /v1/vault/init first.",
            "collection": VAULT_COLLECTION,
        }
    return await ingest_folder(
        vault,
        collection=VAULT_COLLECTION,
        home_dir=home_dir,
        extensions=_VAULT_EXTENSIONS,
    )


async def ensure_vault_indexed(home_dir: str | Path | None = None) -> dict[str, Any]:
    """Ingest the vault iff its collection is currently empty.

    Idempotent and cheap when the collection already has chunks — one SQLite
    count query. The Wizard's ``rag_ask_vault`` tool calls this first so the
    user never has to think about "did I index my vault?"
    """
    from nvh.integrations.rag.store import RagStore

    if not vault_exists(home_dir):
        return {
            "ok": False,
            "error": "Vault not initialized.",
            "collection": VAULT_COLLECTION,
        }

    with RagStore(home_dir=home_dir) as store:
        stats = store.collection_stats(VAULT_COLLECTION)
    if stats["chunks"] > 0:
        return {"ok": True, "already_indexed": True, **stats}

    result = await ingest_vault(home_dir=home_dir)
    result["already_indexed"] = False
    return result


async def ask_vault(
    question: str,
    *,
    top_k: int = 5,
    home_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Ensure the vault is indexed, then run a top-k cosine search against it.

    One-call API for "answer this from my notes." Returns the same shape as
    ``ask()`` with an extra ``auto_indexed`` flag so the UI/Wizard can tell
    the user when the first call took longer because we just ingested.
    """
    from nvh.integrations.rag.query import ask

    ensure_result = await ensure_vault_indexed(home_dir=home_dir)
    if not ensure_result.get("ok"):
        return {
            "ok": False,
            "error": ensure_result.get("error", "Vault not indexable"),
            "collection": VAULT_COLLECTION,
        }
    result = await ask(question, collection=VAULT_COLLECTION, top_k=top_k, home_dir=home_dir)
    result["auto_indexed"] = not ensure_result.get("already_indexed", False)
    return result
