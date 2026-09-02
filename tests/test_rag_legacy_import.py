"""One-shot import of the pre-0.42 ``~/.hive/knowledge`` store into the RAG index.

The embedder is mocked (no Ollama in CI); what matters is that every legacy
document ends up in the ``legacy-knowledge`` collection — re-read from disk
when the original file survives, rebuilt from the stored chunks otherwise —
and that the marker makes ``nvh doctor`` stop nagging.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from nvh.integrations.rag.legacy import (
    _rebuild_text,
    import_legacy_knowledge,
    legacy_knowledge_status,
)
from nvh.integrations.rag.store import RagStore, list_collections

_WORDS = [f"w{i}" for i in range(1200)]


def _write_legacy_store(root: Path, existing_file: Path) -> None:
    chunks = root / "chunks"
    chunks.mkdir(parents=True)
    # Two overlapping 1000/200-word chunks for a document whose file is gone.
    for index, content in enumerate((" ".join(_WORDS[:1000]), " ".join(_WORDS[800:]))):
        (chunks / f"bbbb_{index:04d}.json").write_text(
            json.dumps({"doc_id": "bbbb", "chunk_index": index, "content": content, "metadata": {}}),
            encoding="utf-8",
        )
    (root / "documents.json").write_text(json.dumps([
        {"id": "aaaa", "filename": existing_file.name, "path": str(existing_file),
         "doc_type": "md", "num_chunks": 1, "ingested_at": "", "size_bytes": 10},
        {"id": "bbbb", "filename": "gone.md", "path": str(root / "nope" / "gone.md"),
         "doc_type": "md", "num_chunks": 2, "ingested_at": "", "size_bytes": 10},
    ]), encoding="utf-8")


async def _fake_embed(texts, **_kwargs):
    return [[1.0, 0.0] for _ in texts]


def test_rebuild_text_undoes_the_word_overlap(tmp_path: Path) -> None:
    keep = tmp_path / "keep.md"
    keep.write_text("kept")
    _write_legacy_store(tmp_path / "legacy", keep)
    assert _rebuild_text(tmp_path / "legacy" / "chunks", "bbbb").split() == _WORDS


def test_status_reports_missing_store(tmp_path: Path) -> None:
    status = legacy_knowledge_status(home_dir=tmp_path / "home", legacy_dir=tmp_path / "none")
    assert status["found"] is False
    assert status["documents"] == 0
    assert status["imported"] is False


async def test_import_reingests_existing_and_rebuilds_missing(tmp_path: Path) -> None:
    home = tmp_path / "home"
    legacy = tmp_path / "legacy"
    keep = tmp_path / "keep.md"
    keep.write_text("kept text about pytest fixtures", encoding="utf-8")
    _write_legacy_store(legacy, keep)

    before = legacy_knowledge_status(home_dir=home, legacy_dir=legacy)
    assert before["found"] is True and before["documents"] == 2 and before["imported"] is False

    with patch("nvh.integrations.rag.ingest.embed_texts", new=_fake_embed):
        result = await import_legacy_knowledge(home_dir=home, legacy_dir=legacy)

    assert result["ok"] is True
    assert result["reingested"] == 1
    assert result["rebuilt"] == 1
    assert result["files_ingested"] == 2

    collections = {c["name"]: c for c in list_collections(home_dir=home)}
    assert collections["legacy-knowledge"]["sources"] == 2
    with RagStore(home_dir=home) as store:
        sources = {
            row[0] for row in store._conn.execute(
                "SELECT DISTINCT source FROM chunks WHERE collection = 'legacy-knowledge'"
            )
        }
    assert str(keep.resolve()) in sources
    assert "legacy:gone.md" in sources

    after = legacy_knowledge_status(home_dir=home, legacy_dir=legacy)
    assert after["imported"] is True
    assert Path(result["marker"]).is_file()


async def test_import_without_store_is_a_clean_error(tmp_path: Path) -> None:
    result = await import_legacy_knowledge(home_dir=tmp_path / "home", legacy_dir=tmp_path / "none")
    assert result["ok"] is False
    assert "No legacy knowledge base" in result["error"]
