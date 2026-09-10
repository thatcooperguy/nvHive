"""Exercise real public RAG boundaries with local transport doubles only."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from io import BytesIO
from unittest.mock import Mock

import pytest
from fastapi import HTTPException, Request, UploadFile

from nvh.config.settings import AtohiConfig, CouncilConfig
from nvh.core.atohi import AtohiAdmission, ResourcePaused
from nvh.core.engine import Engine
from nvh.integrations.rag import embedder, ingest, query, vault_bridge
from nvh.integrations.rag.store import RagStore
from nvh.providers.registry import ProviderRegistry


@pytest.fixture
def local_transport(monkeypatch, tmp_path):
    calls = []

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def post(self, url, **kwargs):
            calls.append((url, kwargs))
            return Mock(status_code=200, json=lambda: {"embedding": [1.0, 0.0]})

    monkeypatch.setattr(embedder.httpx, "AsyncClient", Client)
    monkeypatch.setattr(embedder, "load_config", lambda: CouncilConfig())
    monkeypatch.setenv("NVH_HOME", str(tmp_path))
    return calls


def owner(enabled=True, broker=None):
    return Engine(
        CouncilConfig(atohi=AtohiConfig(enabled=enabled)),
        ProviderRegistry(atohi_broker=broker),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "surface", ["query", "folder", "files", "documents", "vault_ingest", "vault_ask"]
)
async def test_public_rag_keeps_supplied_policy_over_disabled_disk(
    surface, tmp_path, local_transport
):
    folder = tmp_path / "vault"
    folder.mkdir()
    note = folder / "note.md"
    note.write_text("A useful private note", encoding="utf-8")
    admission = owner().registry.admission
    kwargs = {"home_dir": tmp_path, "admission": admission}
    with pytest.raises(ResourcePaused, match="broker_unavailable"):
        if surface == "query":
            await query.ask("Find this note", **kwargs)
        elif surface == "folder":
            await ingest.ingest_folder(folder, **kwargs)
        elif surface == "files":
            await ingest.ingest_files([note], **kwargs)
        elif surface == "documents":
            await ingest.ingest_documents([("note.md", "private note")], **kwargs)
        elif surface == "vault_ingest":
            await vault_bridge.ingest_vault(**kwargs)
        else:
            await vault_bridge.ask_vault("Find this note", **kwargs)
    assert local_transport == []


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["query", "ingest", "upload", "vault_ingest", "vault_ask"])
async def test_http_rag_uses_in_memory_engine_policy(
    surface, tmp_path, local_transport, monkeypatch
):
    from nvh.api import server

    engine = owner()
    monkeypatch.setattr(server, "get_engine", lambda: engine)
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "note.md").write_text("The indexed note", encoding="utf-8")
    with pytest.raises(HTTPException) as error:
        if surface == "query":
            await server.rag_ask_endpoint(
                server.RagAskRequest(question="Find the note", home_dir=str(tmp_path))
            )
        elif surface == "ingest":
            await server.rag_ingest_endpoint(
                server.RagIngestRequest(path=str(vault), home_dir=str(tmp_path))
            )
        elif surface == "upload":
            await server.rag_upload_ingest_endpoint(
                files=[UploadFile(filename="note.md", file=BytesIO(b"The uploaded note"))],
                home_dir=str(tmp_path),
                collection="test",
            )
        elif surface == "vault_ingest":
            await server.rag_vault_ingest_endpoint(
                server.RagVaultIngestRequest(home_dir=str(tmp_path))
            )
        else:
            await server.rag_vault_ask_endpoint(
                server.RagVaultAskRequest(question="Find the note", home_dir=str(tmp_path))
            )
    assert error.value.status_code == 409
    assert error.value.detail["status"] == "paused"
    assert error.value.detail["automatic_retry"] is False
    assert local_transport == []


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["rag_ingest", "rag_ask", "rag_ask_vault"])
async def test_wizard_tools_bind_authority_outside_model_arguments(tool, tmp_path, local_transport):
    from nvh.integrations.wizard.tools import default_registry

    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "note.md").write_text("The indexed note", encoding="utf-8")
    registry = default_registry(admission=owner().registry.admission)
    with pytest.raises(ResourcePaused):
        await registry.execute(
            tool,
            arguments={
                "path": str(vault),
                "question": "Find the note",
                "home_dir": str(tmp_path),
                "admission": {"enabled": False},
                "broker": {"granted": True},
            },
            confirmed=True,
        )
    assert local_transport == []


@pytest.mark.asyncio
async def test_autofold_cannot_turn_pause_into_missing_recall(tmp_path, local_transport):
    from nvh.integrations.wizard.chat import _auto_fold_vault_chunk

    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "note.md").write_text("The indexed note", encoding="utf-8")
    with pytest.raises(ResourcePaused):
        await _auto_fold_vault_chunk(
            "Please recall the indexed note",
            home_dir=tmp_path,
            admission=owner().registry.admission,
        )
    assert local_transport == []


@pytest.mark.asyncio
async def test_http_tool_cache_changes_with_engine_policy(tmp_path, local_transport, monkeypatch):
    from nvh.api import server

    monkeypatch.setattr(server, "_wizard_tool_registry", None)
    engine = owner(False)
    monkeypatch.setattr(server, "get_engine", lambda: engine)
    first = server._get_wizard_tools()
    engine = owner(True)
    second = server._get_wizard_tools()
    assert second is not first
    with pytest.raises(ResourcePaused):
        await second.execute(
            "rag_ask",
            arguments={"question": "Find the note", "home_dir": str(tmp_path)},
            confirmed=True,
        )
    assert local_transport == []


@pytest.mark.asyncio
async def test_wizard_tool_http_reports_pause(tmp_path, local_transport, monkeypatch):
    from nvh.api import server

    engine = owner()
    monkeypatch.setattr(server, "get_engine", lambda: engine)
    monkeypatch.setattr(server, "_wizard_tool_registry", None)
    with pytest.raises(HTTPException) as error:
        await server.wizard_tools_execute(
            server.WizardToolExecuteRequest(
                name="rag_ask",
                arguments={"question": "Find the note", "home_dir": str(tmp_path)},
            ),
            Request({"type": "http", "headers": [], "server": ("127.0.0.1", 8080)}),
        )
    assert error.value.detail["code"] == "resource_paused"
    assert error.value.status_code == 409
    assert local_transport == []


@pytest.mark.asyncio
async def test_wizard_sse_autofold_pause_emits_no_success(tmp_path, local_transport, monkeypatch):
    from nvh.api import server

    engine = owner()
    monkeypatch.setattr(server, "get_engine", lambda: engine)
    monkeypatch.setenv("NVH_WIZARD_AUTOFOLD_VAULT", "1")
    monkeypatch.setattr(
        "nvh.integrations.wizard.context.wizard_context",
        lambda **kwargs: {
            "gpu": {"detected": False},
            "storage": {"available": False},
            "providers": [],
            "ollama_models": [],
            "recent_jobs": [],
            "receipts": {},
            "vault": {},
        },
    )
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "note.md").write_text("The indexed note", encoding="utf-8")
    response = await server.wizard_chat_stream_endpoint(
        server.WizardChatRequest(
            question="Please recall the indexed note",
            home_dir=str(tmp_path),
        )
    )
    events = [event async for event in response.body_iterator]
    assert len(events) == 1
    assert b'"code": "resource_paused"' in events[0]
    assert b'"type": "done"' not in events[0]
    assert local_transport == []


@pytest.mark.asyncio
async def test_failed_turn_registry_is_not_rebuilt_from_disk(
    tmp_path, local_transport, monkeypatch
):
    from nvh.integrations.wizard.chat import _run_auto_tool

    builder = Mock(side_effect=AssertionError("must not rebuild a turn's registry"))
    monkeypatch.setattr("nvh.integrations.wizard.tools.default_registry", builder)
    result = await _run_auto_tool(
        "rag_ask",
        {"question": "Find the note", "home_dir": str(tmp_path)},
        registry=None,
    )
    assert result == {"ok": False, "error": "tool registry unavailable"}
    builder.assert_not_called()
    assert local_transport == []


@pytest.mark.asyncio
async def test_concurrent_engines_do_not_exchange_embedding_policy(tmp_path, local_transport):
    shared = ProviderRegistry()
    disabled = Engine(CouncilConfig(), shared)
    enabled = Engine(CouncilConfig(atohi=AtohiConfig(enabled=True)), shared)

    async def call(engine):
        try:
            return await query.ask(
                "Find the note", home_dir=tmp_path, admission=engine.registry.admission
            )
        except ResourcePaused:
            return "paused"

    ordinary, paused = await asyncio.gather(call(disabled), call(enabled))
    assert ordinary["ok"] is True
    assert paused == "paused"
    assert len(local_transport) == 1


@pytest.mark.asyncio
async def test_revoked_batch_preserves_existing_source_and_stops_next_file(
    tmp_path, local_transport, monkeypatch
):
    revoked = asyncio.Event()

    from types import SimpleNamespace

    from tests.test_atohi_admission import fixture_model_lease

    class Broker:
        @asynccontextmanager
        async def admit(self, request):
            yield fixture_model_lease(self, request)

        async def wait_revoked(self):
            await revoked.wait()

    batch_calls = []

    async def batch(texts, **kwargs):
        batch_calls.append(texts)
        revoked.set()
        await asyncio.Event().wait()

    broker = Broker()
    broker.transports = {"ollama": SimpleNamespace(embeddings=batch)}
    with RagStore(home_dir=tmp_path) as store:
        store.add_chunks(
            collection="test",
            source="first.md",
            chunks=["original"],
            vectors=[[1.0, 0.0]],
            model="test",
        )
    admission = AtohiAdmission(AtohiConfig(enabled=True), broker=broker)
    with pytest.raises(ResourcePaused, match="resource_revoked"):
        await asyncio.wait_for(
            ingest.ingest_documents(
                [("first.md", "replacement"), ("second.md", "later batch")],
                collection="test",
                home_dir=tmp_path,
                admission=admission,
            ),
            2,
        )
    assert batch_calls == [["replacement"]]
    with RagStore(home_dir=tmp_path) as store:
        chunks = store.search(collection="test", query_vector=[1.0, 0.0], top_k=5)
    assert [chunk.text for chunk in chunks] == ["original"]
    assert local_transport == []
