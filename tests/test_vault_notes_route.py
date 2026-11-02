"""``/v1/vault/notes`` (nvh/api/routes/vault_notes.py): list, get, forget.

The router is mounted on a minimal FastAPI app here — the server's
``include_router`` is the server suite's concern — and every note is written
into the hermetic home (the autouse ``NVH_HOME`` of tests/conftest.py) through
``append_vault_memory``, exactly as the REPL, the API and the Wizard write them.
The routes read the process home only: no request can name another directory.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from nvh.api.routes.vault_notes import router
from nvh.integrations.workspace import vault
from nvh.integrations.workspace.storage import storage_layout

_HEADER_KEYS = {"id", "schema", "title", "created", "category", "tags", "source", "path"}


@pytest.fixture()
def client(monkeypatch) -> TestClient:
    monkeypatch.delenv("HIVE_API_KEY", raising=False)
    app = FastAPI()
    app.include_router(router)
    return TestClient(app, raise_server_exceptions=True)


@pytest.fixture()
def notes() -> dict[str, dict]:
    """Two records in the process-home vault (which ``init_vault`` also seeds with ``doc`` notes)."""
    first = vault.append_vault_memory(
        "Docker group fix", "Add the user to the docker group and re-login.", tags=["docker", "linux"], source="repl",
    )
    second = vault.append_vault_memory(
        "ComfyUI lives on the second NVMe", "Checkpoints under /mnt/nvme2/comfy.", tags=["comfyui"], source="wizard",
    )
    return {"first": first, "second": second}


def test_list_returns_headers_without_bodies(client: TestClient, notes) -> None:
    resp = client.get("/v1/vault/notes")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "success"
    data = body["data"]
    assert data["count"] == len(data["notes"]) >= 2
    assert data["memory_only"] is False and data["limit"] == 200
    for note in data["notes"]:
        assert set(note) == _HEADER_KEYS, "the list carries MemoryRecord minus body"
    ids = {note["id"] for note in data["notes"]}
    assert {notes["first"]["id"], notes["second"]["id"]} <= ids
    # Seeded documentation notes are in the plain list ...
    assert any(note["schema"] == "doc" for note in data["notes"])
    # ... and every path is inside the process-home vault.
    root = (storage_layout().home / "vault").resolve()
    for note in data["notes"]:
        Path(note["path"]).resolve().relative_to(root)


def test_memory_only_drops_doc_notes(client: TestClient, notes) -> None:
    data = client.get("/v1/vault/notes", params={"memory_only": "true"}).json()["data"]
    assert data["memory_only"] is True
    assert data["count"] >= 2
    assert {note["schema"] for note in data["notes"]} <= {"nvh-memory/1", "legacy"}


def test_filters_narrow_the_list(client: TestClient, notes) -> None:
    by_tag = client.get("/v1/vault/notes", params={"tag": "#comfyui"}).json()["data"]["notes"]
    assert [note["id"] for note in by_tag] == [notes["second"]["id"]]

    by_text = client.get("/v1/vault/notes", params={"q": "docker group"}).json()["data"]["notes"]
    assert [note["id"] for note in by_text] == [notes["first"]["id"]]

    by_category = client.get("/v1/vault/notes", params={"category": "Wizard Memory", "memory_only": "true"})
    assert {note["category"] for note in by_category.json()["data"]["notes"]} == {"Wizard Memory"}

    none = client.get("/v1/vault/notes", params={"category": "Decisions", "memory_only": "true"}).json()["data"]
    assert none == {"notes": [], "count": 0, "memory_only": True, "limit": 200}

    limited = client.get("/v1/vault/notes", params={"limit": 1}).json()["data"]
    assert limited["count"] == 1 and limited["limit"] == 1


@pytest.mark.parametrize("limit", ["0", "501", "-3", "many"])
def test_limit_outside_1_to_500_is_rejected(client: TestClient, limit: str) -> None:
    assert client.get("/v1/vault/notes", params={"limit": limit}).status_code == 422


def test_get_returns_the_full_record(client: TestClient, notes) -> None:
    resp = client.get(f"/v1/vault/notes/{notes['first']['id']}")
    assert resp.status_code == 200
    record = resp.json()["data"]
    assert record["id"] == notes["first"]["id"]
    assert record["schema"] == "nvh-memory/1"
    assert record["title"] == "Docker group fix"
    assert record["body"] == "Add the user to the docker group and re-login."
    assert record["tags"] == ["docker", "linux"]
    assert record["source"] == "repl"
    assert record["path"] == notes["first"]["path"]
    # Ids are matched case-insensitively, as the vault module does.
    assert client.get(f"/v1/vault/notes/{notes['first']['id'].upper()}").status_code == 200


def test_unknown_ids_are_404(client: TestClient, notes) -> None:
    assert client.get("/v1/vault/notes/" + "0" * 32).status_code == 404
    assert client.delete("/v1/vault/notes/" + "0" * 32).status_code == 404
    assert client.get("/v1/vault/notes/" + "0" * 32).json()["detail"] == "unknown note id"


def test_an_id_with_path_characters_never_leaves_the_vault(client: TestClient, notes, tmp_path: Path) -> None:
    """Ids are matched against the notes' own ``Id:`` lines, never used as paths: a
    file outside the vault is unknown under its derived id, and traversal spellings
    are plain 404s (nothing is read, nothing is deleted)."""
    outside = tmp_path / "outside.md"
    outside.write_text("# Outside\n\nId: " + "f" * 32 + "\nSchema: nvh-memory/1\n\nnot yours\n", encoding="utf-8")
    assert client.get("/v1/vault/notes/" + "f" * 32).status_code == 404
    assert client.get(f"/v1/vault/notes/{vault.derived_note_id(outside)}").status_code == 404
    for spelling in (
        "..%2F..%2Foutside.md", "..%5C..%5Coutside.md", "%2E%2E%2F%2E%2E%2Foutside.md",
        "README.md", "%2Fetc%2Fpasswd", "Wizard%20Memory",
    ):
        assert client.get(f"/v1/vault/notes/{spelling}").status_code == 404, spelling
        assert client.delete(f"/v1/vault/notes/{spelling}").status_code == 404, spelling
    assert outside.exists()
    assert Path(notes["first"]["path"]).exists() and Path(notes["second"]["path"]).exists()


def test_delete_removes_the_file_and_forgets_its_chunks(client: TestClient, notes) -> None:
    path = Path(notes["second"]["path"])
    assert path.exists()
    resp = client.delete(f"/v1/vault/notes/{notes['second']['id']}")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["deleted"] is True
    assert data["id"] == notes["second"]["id"]
    assert data["path"] == str(path)
    # The RAG drop ran against the process-home store (nothing was indexed, so 0 chunks).
    assert data["forgotten"] is True
    assert data["chunks_removed"] == 0
    assert not path.exists()
    assert client.get(f"/v1/vault/notes/{notes['second']['id']}").status_code == 404
    assert Path(notes["first"]["path"]).exists(), "only the named note is gone"


def test_delete_still_removes_the_file_when_forgetting_fails(client: TestClient, notes, monkeypatch) -> None:
    import nvh.integrations.rag as rag

    async def _boom(path, *, home_dir=None):
        raise RuntimeError("index unavailable")

    monkeypatch.setattr(rag, "forget_vault_note", _boom)
    resp = client.delete(f"/v1/vault/notes/{notes['first']['id']}")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["deleted"] is True and data["forgotten"] is False and data["chunks_removed"] == 0
    assert not Path(notes["first"]["path"]).exists()


def test_delete_passes_the_note_path_to_the_forgetter(client: TestClient, notes, monkeypatch) -> None:
    import nvh.integrations.rag as rag

    seen: list[tuple] = []

    async def _forget(path, *, home_dir=None):
        seen.append((str(path), home_dir))
        return 3

    monkeypatch.setattr(rag, "forget_vault_note", _forget)
    data = client.delete(f"/v1/vault/notes/{notes['first']['id']}").json()["data"]
    assert seen == [(notes["first"]["path"], None)], "process home only: no home_dir is ever passed"
    assert data["forgotten"] is True and data["chunks_removed"] == 3


def test_keyed_deployment_demands_the_key_on_every_route(monkeypatch, notes) -> None:
    monkeypatch.setenv("HIVE_API_KEY", "vault-notes-key")
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app, raise_server_exceptions=True)
    note_id = notes["first"]["id"]

    assert client.get("/v1/vault/notes").status_code == 401
    assert client.get(f"/v1/vault/notes/{note_id}").status_code == 401
    assert client.delete(f"/v1/vault/notes/{note_id}").status_code == 401
    assert Path(notes["first"]["path"]).exists(), "a refused delete removes nothing"

    headers = {"X-Hive-API-Key": "vault-notes-key"}
    assert client.get("/v1/vault/notes", headers=headers).status_code == 200
    assert client.get(f"/v1/vault/notes/{note_id}", headers={"Authorization": "Bearer vault-notes-key"}).status_code == 200
    assert client.get("/v1/vault/notes", headers={"X-Hive-API-Key": "wrong"}).status_code == 401


def test_routes_are_registered_once_with_the_expected_methods() -> None:
    routes = {(r.path, tuple(sorted(r.methods))) for r in router.routes}
    assert routes == {
        ("/v1/vault/notes", ("GET",)),
        ("/v1/vault/notes/{note_id}", ("GET",)),
        ("/v1/vault/notes/{note_id}", ("DELETE",)),
    }
