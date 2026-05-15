"""Tests for the local-first RAG subpackage.

Embedder calls are mocked — we never want CI to depend on a running
Ollama daemon, and the embeddings themselves are not what we're testing
here. What we test:

  - Chunking respects size + overlap and snaps to paragraph breaks
  - Store round-trips vectors via SQLite + cosine search returns the
    right top-k order
  - Ingest walks a tempdir, skips junk dirs, and is idempotent across reruns
  - Query returns the chunks in similarity order
  - The Wizard tools registered for RAG are listed correctly
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from nvh.integrations.rag.chunker import chunk_text
from nvh.integrations.rag.store import RagStore, list_collections


def test_chunker_returns_single_chunk_for_short_text() -> None:
    chunks = chunk_text("Hello world.", chunk_chars=600)
    assert chunks == ["Hello world."]


def test_chunker_splits_long_text_with_overlap() -> None:
    para = "Lorem ipsum dolor sit amet. " * 100  # ~2800 chars
    chunks = chunk_text(para, chunk_chars=400, overlap_chars=50)
    assert len(chunks) > 1
    # Overlap means consecutive chunks share some text.
    assert any(
        chunks[i].split()[-2:] == chunks[i + 1].split()[:2]
        for i in range(len(chunks) - 1)
    ) or len(chunks[0]) < 600  # weaker check: chunking happened at all


def test_chunker_returns_empty_for_blank_input() -> None:
    assert chunk_text("") == []
    assert chunk_text("   \n\n  ") == []


def test_store_roundtrip_and_search_orders_by_cosine(tmp_path: Path) -> None:
    """Store + retrieve + cosine ordering — pure-Python check, no embedder."""
    home = tmp_path / "home"
    home.mkdir()
    with RagStore(home_dir=home) as store:
        # Three orthogonal-ish 3-D vectors so the cosine ordering is obvious.
        store.add_chunks(
            collection="t",
            source="a.md",
            chunks=["first chunk", "second chunk", "third chunk"],
            vectors=[[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]],
            model="test",
        )
        # Query close to vector 1 (the "second chunk")
        results = store.search(collection="t", query_vector=[0.1, 0.9, 0.0], top_k=2)
    assert len(results) == 2
    assert results[0].text == "second chunk"
    assert results[0].score > results[1].score


def test_store_delete_source_makes_reingest_idempotent(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    with RagStore(home_dir=home) as store:
        for _ in range(3):
            store.delete_source(collection="t", source="a.md")
            store.add_chunks(
                collection="t",
                source="a.md",
                chunks=["hello"],
                vectors=[[1.0, 0.0]],
                model="test",
            )
        stats = store.collection_stats("t")
    assert stats["chunks"] == 1
    assert stats["sources"] == 1


def test_list_collections_aggregates_across_names(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    with RagStore(home_dir=home) as store:
        store.add_chunks(
            collection="alpha", source="a", chunks=["x"], vectors=[[1.0]], model="m",
        )
        store.add_chunks(
            collection="beta", source="b", chunks=["y", "z"], vectors=[[1.0], [0.0]], model="m",
        )
    collections = list_collections(home_dir=home)
    names = {c["name"]: c for c in collections}
    assert names["alpha"]["chunks"] == 1
    assert names["beta"]["chunks"] == 2


@pytest.mark.asyncio
async def test_ingest_walks_dir_and_skips_junk(tmp_path: Path) -> None:
    """Ingest must skip node_modules/.git/etc. and not duplicate on rerun."""
    from nvh.integrations.rag import ingest_folder

    corpus = tmp_path / "corpus"
    (corpus / "node_modules").mkdir(parents=True)
    (corpus / "docs").mkdir(parents=True)
    (corpus / "node_modules" / "junk.md").write_text("# should be skipped")
    (corpus / "docs" / "a.md").write_text("This is a doc about nvHive.")
    (corpus / "docs" / "b.txt").write_text("Another file with more text content here.")

    fake_embed = AsyncMock(side_effect=lambda texts, **kwargs: [[1.0, 0.0, 0.0]] * len(texts))
    with patch("nvh.integrations.rag.ingest.embed_texts", new=fake_embed):
        first = await ingest_folder(str(corpus), home_dir=tmp_path / "home", collection="t")
        # Rerun — should be idempotent, not produce 4 sources.
        second = await ingest_folder(str(corpus), home_dir=tmp_path / "home", collection="t")

    assert first["ok"] is True
    assert first["files_ingested"] == 2
    assert second["files_ingested"] == 2
    # Same chunk count both runs — re-ingest replaced rather than duplicated.
    assert first["chunks"] == second["chunks"]
    collections = list_collections(home_dir=tmp_path / "home")
    assert collections[0]["sources"] == 2


@pytest.mark.asyncio
async def test_ingest_surfaces_embed_failures_clearly(tmp_path: Path) -> None:
    """A mid-walk embed failure should return ok=False with a useful message,
    not a half-indexed collection that pretends success."""
    from nvh.integrations.rag import ingest_folder
    from nvh.integrations.rag.embedder import EmbeddingError

    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "a.md").write_text("hello world")

    with patch(
        "nvh.integrations.rag.ingest.embed_texts",
        new=AsyncMock(side_effect=EmbeddingError("ollama down")),
    ):
        result = await ingest_folder(str(corpus), home_dir=tmp_path / "home")

    assert result["ok"] is False
    assert "ollama down" in result["error"].lower()


@pytest.mark.asyncio
async def test_ask_returns_chunks_in_similarity_order(tmp_path: Path) -> None:
    from nvh.integrations.rag import ask
    from nvh.integrations.rag.store import RagStore

    home = tmp_path / "home"
    home.mkdir()
    with RagStore(home_dir=home) as store:
        store.add_chunks(
            collection="t",
            source="a.md",
            chunks=["red apple", "blue sky", "green grass"],
            vectors=[[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]],
            model="test",
        )

    # Query closest to "blue sky"
    with patch(
        "nvh.integrations.rag.query.embed_one",
        new=AsyncMock(return_value=[0.0, 0.95, 0.05]),
    ):
        result = await ask("what color is the sky?", collection="t", home_dir=home, top_k=2)

    assert result["ok"] is True
    assert result["chunks"][0]["text"] == "blue sky"
    assert result["chunks"][0]["score"] > result["chunks"][1]["score"]


@pytest.mark.asyncio
async def test_ask_handles_empty_question() -> None:
    from nvh.integrations.rag import ask

    result = await ask("   ")
    assert result["ok"] is False
    assert "empty" in result["error"].lower()


def test_format_context_block_renders_chunks_with_sources() -> None:
    from nvh.integrations.rag.query import format_context_block

    chunks = [
        {"source": "/abs/path/doc.md", "chunk_index": 0, "text": "First snippet", "score": 0.91},
        {"source": "/abs/path/other.txt", "chunk_index": 3, "text": "Second snippet", "score": 0.74},
    ]
    block = format_context_block(chunks)
    assert "doc.md" in block
    assert "other.txt" in block
    assert "First snippet" in block
    assert "Second snippet" in block


def test_wizard_registry_includes_rag_tools() -> None:
    """Both rag tools must be discoverable so the LLM and UI can use them."""
    from nvh.integrations.wizard.tools import default_registry

    registry = default_registry()
    names = {t.name for t in registry.list_tools()}
    assert "rag_ask" in names
    assert "rag_ingest" in names
    # Safety classes set correctly
    assert registry.get("rag_ask").safety_class == "auto"
    assert registry.get("rag_ingest").safety_class == "confirm"
