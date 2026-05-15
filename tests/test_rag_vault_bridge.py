"""Tests for the Vault → RAG bridge.

Embedder is mocked. We verify:
  - `vault_exists` reflects whether the vault directory has been created
  - `ingest_vault` errors cleanly when the vault doesn't exist yet
  - `ingest_vault` walks the vault dir and only embeds Markdown/text files
  - `ensure_vault_indexed` is a true no-op when the collection has chunks
  - `ask_vault` triggers the auto-index then returns chunks in similarity order
  - The Wizard tool `rag_ask_vault` is registered and auto-class
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from nvh.integrations.rag.store import RagStore
from nvh.integrations.rag.vault_bridge import (
    VAULT_COLLECTION,
    ask_vault,
    ensure_vault_indexed,
    ingest_vault,
    vault_exists,
)


def _make_vault(home: Path) -> Path:
    vault = home / "vault"
    vault.mkdir(parents=True)
    (vault / "notes.md").write_text("# nvHive setup notes\nUsing rootless mount.")
    (vault / "ideas.md").write_text("# Ideas\nAuto-index vault into RAG.")
    # Non-text/MD file — must not be embedded.
    (vault / "image.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    return vault


def test_vault_exists_false_when_uninitialized(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    assert vault_exists(home_dir=home) is False


def test_vault_exists_true_after_directory_created(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _make_vault(home)
    assert vault_exists(home_dir=home) is True


@pytest.mark.asyncio
async def test_ingest_vault_errors_when_vault_missing(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    result = await ingest_vault(home_dir=home)
    assert result["ok"] is False
    assert "vault" in result["error"].lower()
    assert result["collection"] == VAULT_COLLECTION


@pytest.mark.asyncio
async def test_ingest_vault_embeds_only_text_files(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _make_vault(home)

    fake_embed = AsyncMock(side_effect=lambda texts, **kw: [[1.0, 0.0]] * len(texts))
    with patch("nvh.integrations.rag.ingest.embed_texts", new=fake_embed):
        result = await ingest_vault(home_dir=home)

    assert result["ok"] is True
    assert result["collection"] == VAULT_COLLECTION
    # PNG should be skipped — only 2 markdown files.
    assert result["files_ingested"] == 2


@pytest.mark.asyncio
async def test_ensure_vault_indexed_is_noop_after_first_run(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _make_vault(home)

    fake_embed = AsyncMock(side_effect=lambda texts, **kw: [[1.0, 0.0]] * len(texts))
    with patch("nvh.integrations.rag.ingest.embed_texts", new=fake_embed):
        first = await ensure_vault_indexed(home_dir=home)
        second = await ensure_vault_indexed(home_dir=home)

    assert first["ok"] is True
    assert first["already_indexed"] is False
    # The second call must short-circuit — no new ingest run.
    assert second["ok"] is True
    assert second["already_indexed"] is True
    assert fake_embed.await_count == 2  # only the first call drove embeddings


@pytest.mark.asyncio
async def test_ask_vault_auto_indexes_then_returns_chunks(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _make_vault(home)

    embed = AsyncMock(side_effect=lambda texts, **kw: [[1.0, 0.0]] * len(texts))
    with (
        patch("nvh.integrations.rag.ingest.embed_texts", new=embed),
        patch("nvh.integrations.rag.query.embed_one", new=AsyncMock(return_value=[1.0, 0.0])),
    ):
        result = await ask_vault("rootless?", home_dir=home, top_k=3)

    assert result["ok"] is True
    assert result["collection"] == VAULT_COLLECTION
    assert result["auto_indexed"] is True
    assert len(result["chunks"]) > 0
    # Sources should be vault notes
    assert any("notes.md" in c["source"] or "ideas.md" in c["source"] for c in result["chunks"])


@pytest.mark.asyncio
async def test_ask_vault_reports_already_indexed_on_second_call(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _make_vault(home)

    # Pre-seed the vault collection so ensure_vault_indexed short-circuits.
    with RagStore(home_dir=home) as store:
        store.add_chunks(
            collection=VAULT_COLLECTION,
            source=str(home / "vault" / "notes.md"),
            chunks=["seeded"],
            vectors=[[1.0, 0.0]],
            model="test",
        )

    with patch("nvh.integrations.rag.query.embed_one", new=AsyncMock(return_value=[1.0, 0.0])):
        result = await ask_vault("rootless?", home_dir=home)

    assert result["ok"] is True
    assert result["auto_indexed"] is False  # collection was already populated


def test_wizard_registry_includes_rag_ask_vault() -> None:
    from nvh.integrations.wizard.tools import default_registry

    registry = default_registry()
    tool = registry.get("rag_ask_vault")
    assert tool is not None
    assert tool.safety_class == "auto"
    assert "question" in tool.parameters
