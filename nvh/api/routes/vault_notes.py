"""``/v1/vault/notes`` — list, read and forget the vault's memory records.

The three routes behind the ``/vault`` page's note list and the Wizard's
``remember`` card link (design 0.46 §2). They read the **process home** only:
unlike ``POST /v1/vault/memory`` there is no ``home_dir`` field, so a request
body can never point the server at another directory. Ids are matched against
the ``Id:`` line (or the derived id) of the notes ``list_vault_notes`` can see,
never used as paths, and the vault walk itself drops any entry whose resolved
path leaves the vault — a note id with path characters in it is simply unknown.

Mounted by ``nvh.api.server`` with ``app.include_router(router)``; every route
takes ``require_auth`` from :mod:`nvh.api.deps`, so a keyed deployment demands
the key here exactly as on the rest of the API.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from nvh.api.deps import _response_envelope, require_auth
from nvh.integrations.workspace.vault import (
    MemoryRecord,
    delete_vault_note,
    get_vault_note,
    list_vault_notes,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["vault"])

NOTES_LIMIT_DEFAULT = 200
NOTES_LIMIT_MAX = 500


def _header_dict(record: MemoryRecord) -> dict[str, Any]:
    """A record's fields minus ``body`` — what the list carries (headers only)."""
    data = record.to_dict()
    data.pop("body", None)
    return data


@router.get("/v1/vault/notes", dependencies=[Depends(require_auth)])
async def vault_notes_list(
    category: str | None = Query(None, max_length=64, description="Vault folder name, e.g. `Wizard Memory`."),
    tag: str | None = Query(None, max_length=64, description="One tag slug (a leading `#` is tolerated)."),
    q: str | None = Query(None, max_length=200, description="Case-insensitive match over title, tags and opening lines."),
    memory_only: bool = Query(False, description="Records only (`nvh-memory/1` and `legacy`); drop seeded `doc` notes."),
    limit: int = Query(NOTES_LIMIT_DEFAULT, ge=1, le=NOTES_LIMIT_MAX),
) -> dict[str, Any]:
    """Headers of the vault's notes, newest first (``body`` is never in the list)."""
    records = await asyncio.to_thread(
        list_vault_notes,
        category=category or None,
        tag=tag or None,
        query=q or None,
        memory_only=memory_only,
        limit=limit,
    )
    return _response_envelope({
        "notes": [_header_dict(record) for record in records],
        "count": len(records),
        "memory_only": memory_only,
        "limit": limit,
    })


@router.get("/v1/vault/notes/{note_id}", dependencies=[Depends(require_auth)])
async def vault_notes_get(note_id: str) -> dict[str, Any]:
    """The full record (body included) for one note id; 404 when no note carries it."""
    record = await asyncio.to_thread(get_vault_note, note_id)
    if record is None:
        raise HTTPException(status_code=404, detail="unknown note id")
    return _response_envelope(record.to_dict())


@router.delete("/v1/vault/notes/{note_id}", dependencies=[Depends(require_auth)])
async def vault_notes_delete(note_id: str) -> dict[str, Any]:
    """Remove the note file and, best-effort, its RAG chunks.

    ``forgotten`` says whether the chunk drop succeeded (``chunks_removed``
    may be 0 for a note that was never indexed); the file is gone either way.
    """
    result = await asyncio.to_thread(delete_vault_note, note_id)
    if not result.get("deleted"):
        error = str(result.get("error") or "unknown note id")
        status_code = 403 if "outside the vault" in error else 404
        raise HTTPException(status_code=status_code, detail=error)
    forgotten = False
    chunks_removed = 0
    try:
        from nvh.integrations.rag import forget_vault_note

        chunks_removed = int(await forget_vault_note(result["path"]))
        forgotten = True
    except Exception:  # the index is derived; the delete already happened
        logger.debug("forget_vault_note(%s) failed", result.get("path"), exc_info=True)
    return _response_envelope({
        "deleted": True,
        "id": result.get("id", note_id),
        "path": result.get("path", ""),
        "forgotten": forgotten,
        "chunks_removed": chunks_removed,
    })
