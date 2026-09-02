"""Contract tests for chat history (roadmap: chat-history polish).

The WebUI sidebar, the /wizard resume flow, and the shared LayoutShell all
depend on a precise wire contract from the conversations API: an object
envelope, epoch-millisecond timestamps, and pinned/mode on every summary.
These tests pin that contract, the previously-missing create/rename/search
endpoints, the SQLite column auto-migration, and the wizard persist→resume
round trip.
"""

from __future__ import annotations

import warnings
from pathlib import Path

import pytest

warnings.filterwarnings("ignore")

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """A TestClient backed by a throwaway $NVH_HOME."""
    monkeypatch.setenv("NVH_HOME", str(tmp_path))
    monkeypatch.delenv("HIVE_DATA_DIR", raising=False)
    monkeypatch.delenv("NVH_STATE", raising=False)
    from fastapi.testclient import TestClient

    from nvh.api.server import app

    with TestClient(app) as c:
        yield c


class TestConversationEndpoints:
    def test_create_returns_contract_summary(self, client):
        r = client.post("/v1/conversations", json={"title": "Hello", "mode": "wizard"})
        assert r.status_code == 200
        conv = r.json()["data"]
        assert conv["title"] == "Hello"
        assert conv["mode"] == "wizard"
        assert conv["pinned"] is False
        # Epoch-millisecond ints — the sidebar does date arithmetic on these.
        assert isinstance(conv["created_at"], int)
        assert conv["created_at"] > 1_500_000_000_000
        assert isinstance(conv["updated_at"], int)

    def test_list_envelope_shape(self, client):
        client.post("/v1/conversations", json={"title": "One"})
        r = client.get("/v1/conversations")
        data = r.json()["data"]
        assert isinstance(data["conversations"], list)
        assert data["count"] == len(data["conversations"]) >= 1
        row = data["conversations"][0]
        for field in ("id", "title", "mode", "pinned", "created_at", "updated_at"):
            assert field in row, f"summary missing {field}"

    def test_rename(self, client):
        cid = client.post("/v1/conversations", json={"title": "Old"}).json()["data"]["id"]
        r = client.patch(f"/v1/conversations/{cid}", json={"title": "New title"})
        assert r.status_code == 200
        assert client.get(f"/v1/conversations/{cid}").json()["data"]["title"] == "New title"

    def test_rename_missing_404(self, client):
        assert client.patch(
            "/v1/conversations/nope", json={"title": "x"}
        ).status_code == 404

    def test_rename_whitespace_only_rejected(self, client):
        cid = client.post("/v1/conversations", json={"title": "Keep"}).json()["data"]["id"]
        r = client.patch(f"/v1/conversations/{cid}", json={"title": "   "})
        assert r.status_code == 422
        assert client.get(f"/v1/conversations/{cid}").json()["data"]["title"] == "Keep"

    def test_rename_preserves_updated_at(self, client):
        """Renaming is metadata editing, not activity — it must not bump
        updated_at, or a weeks-old chat teleports to 'Today' in the sidebar."""
        cid = client.post("/v1/conversations", json={"title": "Old"}).json()["data"]["id"]
        before = client.get(f"/v1/conversations/{cid}").json()["data"]["updated_at"]
        import time

        time.sleep(1.1)  # updated_at has second-level resolution in SQLite
        client.patch(f"/v1/conversations/{cid}", json={"title": "Renamed"})
        after = client.get(f"/v1/conversations/{cid}").json()["data"]["updated_at"]
        assert after == before

    def test_search_limit_bounds(self, client):
        assert client.get(
            "/v1/conversations/search", params={"q": "e", "limit": 0}
        ).status_code == 422
        assert client.get(
            "/v1/conversations/search", params={"q": "e", "limit": -1}
        ).status_code == 422
        assert client.get(
            "/v1/conversations/search", params={"q": "e", "limit": 101}
        ).status_code == 422

    def test_pinned_survives_recency_window(self, client):
        """A pinned conversation stays in the list response even when the
        recency window is full of newer conversations."""
        pinned_id = client.post(
            "/v1/conversations", json={"title": "Keep me"}
        ).json()["data"]["id"]
        client.post(f"/v1/conversations/{pinned_id}/pin", json={"pinned": True})
        for i in range(6):
            client.post("/v1/conversations", json={"title": f"Newer {i}"})
        rows = client.get(
            "/v1/conversations", params={"limit": 5}
        ).json()["data"]["conversations"]
        assert any(c["id"] == pinned_id and c["pinned"] for c in rows)

    def test_pin_roundtrip_in_list(self, client):
        cid = client.post("/v1/conversations", json={"title": "Pin me"}).json()["data"]["id"]
        assert client.post(
            f"/v1/conversations/{cid}/pin", json={"pinned": True}
        ).status_code == 200
        rows = client.get("/v1/conversations").json()["data"]["conversations"]
        assert [c for c in rows if c["id"] == cid][0]["pinned"] is True

    def test_search_literal_route_beats_id_route(self, client):
        # /v1/conversations/search must not be captured by /{conversation_id}.
        r = client.get("/v1/conversations/search", params={"q": "zzz-no-match"})
        assert r.status_code == 200
        assert r.json()["data"]["results"] == []

    def test_legacy_mode_serializes_as_single(self, client):
        # Rows created without a mode (legacy) group as main-chat entries.
        cid = client.post("/v1/conversations", json={"title": "Legacy"}).json()["data"]["id"]
        row = client.get(f"/v1/conversations/{cid}").json()["data"]
        assert row["mode"] == "single"


class TestWizardPersistResume:
    def test_wizard_turn_persists_and_resumes(self, client):
        """The full WebUI flow: create (mode=wizard) → wizard chat turn →
        the conversation holds the user/assistant pair with an auto title.

        Works offline: with no provider the Wizard answers via its
        deterministic fallback, which persists through the same path.
        """
        conv = client.post(
            "/v1/conversations", json={"title": "", "mode": "wizard"}
        ).json()["data"]
        cid = conv["id"]

        r = client.post(
            "/v1/wizard/chat",
            json={"question": "What is my GPU status?", "conversation_id": cid},
        )
        assert r.status_code == 200

        detail = client.get(f"/v1/conversations/{cid}").json()["data"]
        roles = [m["role"] for m in detail["messages"]]
        assert roles[:2] == ["user", "assistant"], roles
        # Auto-title from the first user message (create sent an empty title).
        assert detail["title"].startswith("What is my GPU status")
        assert detail["mode"] == "wizard"
        # Message contract the resume mapper reads.
        msg = detail["messages"][0]
        assert isinstance(msg["timestamp"], int) and msg["timestamp"] > 0
        assert "tokens" in msg

    def test_search_escapes_like_metacharacters(self, client):
        """'_' and '%' in a query match literally, not as LIKE wildcards."""
        cid = client.post("/v1/conversations", json={"title": "esc"}).json()["data"]["id"]
        client.post(
            "/v1/wizard/chat",
            json={"question": "note the totalXcost figure here", "conversation_id": cid},
        )
        # '_' must not act as a single-char wildcard matching 'totalXcost'.
        results = client.get(
            "/v1/conversations/search", params={"q": "total_cost"}
        ).json()["data"]["results"]
        assert not any(r["id"] == cid for r in results)
        # The literal text still matches.
        results = client.get(
            "/v1/conversations/search", params={"q": "totalXcost"}
        ).json()["data"]["results"]
        assert any(r["id"] == cid for r in results)

    def test_search_finds_persisted_turn(self, client):
        cid = client.post(
            "/v1/conversations", json={"mode": "wizard"}
        ).json()["data"]["id"]
        client.post(
            "/v1/wizard/chat",
            json={"question": "xylophone-marker question", "conversation_id": cid},
        )
        results = client.get(
            "/v1/conversations/search", params={"q": "xylophone-marker"}
        ).json()["data"]["results"]
        assert any(r["id"] == cid for r in results)
        assert all("snippet" in r for r in results)


class TestColumnMigration:
    def test_init_db_adds_missing_columns(self, tmp_path):
        """A database created before pinned/mode existed gains both columns
        on init_db — create_all never ALTERs, so the explicit migration is
        what keeps old workspaces working."""
        import asyncio
        import sqlite3

        db = tmp_path / "state" / "nvhive.db"
        db.parent.mkdir(parents=True)
        legacy = sqlite3.connect(db)
        legacy.execute(
            """
            CREATE TABLE conversations (
                id VARCHAR(36) PRIMARY KEY,
                title VARCHAR(255),
                provider VARCHAR(64),
                model VARCHAR(128),
                created_at DATETIME,
                updated_at DATETIME,
                message_count INTEGER,
                total_tokens INTEGER,
                total_cost_usd NUMERIC(12, 6)
            )
            """
        )
        legacy.execute(
            "INSERT INTO conversations (id, title) VALUES ('legacy-1', 'Old chat')"
        )
        legacy.commit()
        legacy.close()

        from nvh.storage import repository as repo

        async def run() -> list:
            await repo.init_db(db)
            try:
                return await repo.list_conversations()
            finally:
                await repo.close_db()

        convs = asyncio.run(run())
        legacy_row = [c for c in convs if c.id == "legacy-1"][0]
        assert legacy_row.pinned is False
        assert legacy_row.mode == ""

        cols = {
            row[1]
            for row in sqlite3.connect(db).execute("PRAGMA table_info(conversations)")
        }
        assert {"pinned", "mode"}.issubset(cols)


class TestFrontendWiring:
    def test_wizard_resume_and_layoutshell_sidebar(self):
        wizard = (ROOT / "web" / "components" / "WizardChat.tsx").read_text(encoding="utf-8")
        # Resume path + meta-tail stripping + mode-tagged creation.
        assert "searchParams?.get('conversation')" in wizard
        assert "parseWizardMeta" in wizard
        assert "createConversation('', 'wizard')" in wizard

        shell = (ROOT / "web" / "components" / "LayoutShell.tsx").read_text(encoding="utf-8")
        # The shared sidebar is fed real history on non-root pages and routes
        # a conversation back to the surface that produced it.
        assert "getConversations" in shell
        assert "/wizard?conversation=" in shell
        assert "onSelectConversation" in shell

        page = (ROOT / "web" / "app" / "page.tsx").read_text(encoding="utf-8")
        # 0.42: the server is the only chat store — the main chat creates the
        # conversation lazily and persists every turn, no localStorage merge.
        assert "void renameConversation(" in page
        assert "void deleteConversation(" in page
        assert "void pinConversation(" in page
        assert "createConversation(" in page
        assert "appendConversationMessage(" in page
        assert "council_chats_v2" not in page
