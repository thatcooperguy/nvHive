"""Tests for rootless nvHive Vault memory helpers."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from nvh.api.server import app
from nvh.integrations import vault


def test_init_vault_creates_markdown_memory_tree(tmp_path) -> None:
    home = tmp_path / "nvhive"

    result = vault.init_vault(home)

    vault_dir = Path(result["vault_dir"])
    assert result["initialized"] is True
    assert vault_dir == home / "vault"
    assert (vault_dir / "README.md").exists()
    assert (vault_dir / "MAP.md").exists()
    assert (vault_dir / "Wizard Memory" / "AI Wizard.md").exists()
    assert (vault_dir / "Conflicts" / "Product Conflicts.md").exists()
    assert (vault_dir / "Process Playbooks" / "Pilot Test Checklist.md").exists()
    assert (vault_dir / ".obsidian" / "app.json").exists()
    # Graph view defaults are seeded so the graph isn't grey-dots on first open.
    assert (vault_dir / ".obsidian" / "graph.json").exists()
    assert result["markdown_files"] >= 6
    assert any(item["title"] == "OS disk vs persistent mount" for item in result["conflicts"])

    # Wikilinks: every category note should reference the hub and at least
    # one sibling, so Obsidian's Graph view shows real mesh structure.
    readme = (vault_dir / "README.md").read_text()
    assert "[[MAP]]" in readme
    assert "[[AI Wizard]]" in readme
    wizard_note = (vault_dir / "Wizard Memory" / "AI Wizard.md").read_text()
    assert "[[Product Direction]]" in wizard_note
    assert "[[Support Report Flow]]" in wizard_note
    map_note = (vault_dir / "MAP.md").read_text()
    assert "[[Product Direction]]" in map_note
    assert "[[Pilot Test Checklist]]" in map_note

    # Tag clusters: each seeded note carries a YAML tags block so the
    # Graph view color-by-tag shows the structure we wired in.
    for relative_path in (
        "README.md",
        "MAP.md",
        "Wizard Memory/AI Wizard.md",
        "Decisions/Product Direction.md",
        "Conflicts/Product Conflicts.md",
        "Process Playbooks/Pilot Test Checklist.md",
        "Process Playbooks/Support Report Flow.md",
    ):
        content = (vault_dir / relative_path).read_text()
        assert content.startswith("---\n"), f"{relative_path} missing frontmatter"
        assert "tags:" in content.splitlines()[1], f"{relative_path} missing tags"


def test_append_vault_memory_writes_safe_markdown(tmp_path) -> None:
    home = tmp_path / "nvhive"

    result = vault.append_vault_memory(
        "GPU VM test: ready?",
        "The 48 GB RTX profile installed the local model and passed the quick test.",
        category="Troubleshooting",
        tags=["pilot", "gpu"],
        home_dir=home,
    )

    path = Path(result["path"])
    assert result["saved"] is True
    assert result["category"] == "Troubleshooting"
    assert path.exists()
    assert path.parent == home / "vault" / "Troubleshooting"
    text = path.read_text(encoding="utf-8")
    assert "# GPU VM test: ready?" in text
    assert "#pilot #gpu" in text


def test_unknown_memory_category_falls_back_to_wizard_memory(tmp_path) -> None:
    home = tmp_path / "nvhive"

    result = vault.append_vault_memory(
        "Do not create arbitrary folders",
        "Unknown categories should land in the safe default.",
        category="../Secrets",
        home_dir=home,
    )

    assert result["category"] == "Wizard Memory"
    assert Path(result["path"]).parent == home / "vault" / "Wizard Memory"


def test_obsidian_status_reports_optional_rootless_app(tmp_path, monkeypatch) -> None:
    home = tmp_path / "nvhive"
    monkeypatch.setattr(vault.shutil, "which", lambda _name: None)

    status = vault.obsidian_status(home)

    assert status["installed"] is False
    assert status["rootless_app"] is False
    assert status["vault_dir"] == str(home / "vault")
    assert status["launcher_path"] == str(home / "bin" / "nvhive-vault")
    assert status["download_url"] == vault.OBSIDIAN_DOWNLOAD_PAGE


def test_vault_api_routes_create_rootless_vault(tmp_path) -> None:
    home = tmp_path / "nvhive"
    client = TestClient(app)

    init_response = client.post("/v1/vault/init", json={"home_dir": str(home)})
    assert init_response.status_code == 200
    init_data = init_response.json()["data"]
    assert init_data["initialized"] is True
    assert init_data["vault_dir"] == str(home / "vault")

    memory_response = client.post(
        "/v1/vault/memory",
        json={
            "home_dir": str(home),
            "title": "API memory",
            "body": "The API writes to the vault without root.",
            "category": "Decisions",
        },
    )
    assert memory_response.status_code == 200
    memory_data = memory_response.json()["data"]
    assert memory_data["saved"] is True
    assert Path(memory_data["path"]).exists()
    # 0.46: the route passes the writer's record fields through unchanged.
    assert memory_data["schema"] == "nvh-memory/1" and len(memory_data["id"]) == 32
    assert memory_data["redacted"] == 0
    assert "Source: api" in Path(memory_data["path"]).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# nvh-memory/1 records (0.46)
# ---------------------------------------------------------------------------


def test_append_vault_memory_writes_an_nvh_memory_record(tmp_path) -> None:
    home = tmp_path / "nvhive"

    result = vault.append_vault_memory(
        "Record shape", "The vault note carries a stable id.",
        tags=["pilot"], home_dir=home, source="repl",
    )

    assert result["schema"] == vault.MEMORY_SCHEMA == "nvh-memory/1"
    assert re.fullmatch(r"[0-9a-f]{32}", result["id"])
    assert result["redacted"] == 0 and result["source"] == "repl" and result["tags"] == ["pilot"]
    lines = Path(result["path"]).read_text(encoding="utf-8").split("\n")
    assert lines[0] == "# Record shape" and lines[1] == ""
    assert lines[2] == f"Id: {result['id']}"
    assert lines[3] == "Schema: nvh-memory/1"
    assert lines[4] == f"Created: {result['created']}" and lines[4].endswith("+00:00")
    assert lines[5] == "Category: Wizard Memory"
    assert lines[6] == "Tags: #pilot"
    assert lines[7] == "Source: repl"
    assert lines[8] == "" and lines[9] == "The vault note carries a stable id."

    # ``source`` defaults to the API writer; an odd value is reduced to a slug.
    api_note = vault.append_vault_memory("Default source", "Written through the default path.", home_dir=home)
    assert "Source: api" in Path(api_note["path"]).read_text(encoding="utf-8")
    odd = vault.append_vault_memory("Odd source", "Written with a strange source label.", home_dir=home, source="Web UI!")
    assert odd["source"] == "webui"


def test_same_second_same_title_saves_do_not_overwrite(tmp_path, monkeypatch) -> None:
    home = tmp_path / "nvhive"
    monkeypatch.setattr(vault, "_now", lambda: "2026-11-01T12:00:00+00:00")

    first = vault.append_vault_memory("Same title", "First body of the pair.", home_dir=home)
    second = vault.append_vault_memory("Same title", "Second body of the pair.", home_dir=home)

    assert first["path"] != second["path"] and first["id"] != second["id"]
    assert Path(first["path"]).name == "2026-11-01T120000Z0000-Same-title.md"
    assert Path(second["path"]).name == f"2026-11-01T120000Z0000-Same-title-{second['id'][:8]}.md"
    assert "First body" in Path(first["path"]).read_text(encoding="utf-8")
    assert "Second body" in Path(second["path"]).read_text(encoding="utf-8")
    # tmp + os.replace leaves no temp file behind.
    assert not list((home / "vault" / "Wizard Memory").glob(".*.tmp"))


def test_multi_line_title_round_trips_through_parse_vault_note(tmp_path) -> None:
    home = tmp_path / "nvhive"

    result = vault.append_vault_memory(
        "ComfyUI lives\n\non the second NVMe", "Mount it before launching ComfyUI.",
        tags=["Wizard", "disk"], home_dir=home, source="wizard",
    )

    assert result["title"] == "ComfyUI lives on the second NVMe"
    path = Path(result["path"])
    record = vault.parse_vault_note(path.read_text(encoding="utf-8"), path=path)
    assert record is not None
    assert record.id == result["id"] and record.schema == "nvh-memory/1"
    assert record.title == "ComfyUI lives on the second NVMe"
    assert record.body == "Mount it before launching ComfyUI."
    assert record.tags == ["wizard", "disk"] and record.source == "wizard"
    assert record.category == "Wizard Memory" and record.created == result["created"]
    assert record.path == str(path)
    assert record.to_dict()["id"] == result["id"]


def test_append_vault_memory_redacts_secrets_and_counts_them(tmp_path) -> None:
    home = tmp_path / "nvhive"
    key = "sk-" + "a1b2c3d4" * 4

    result = vault.append_vault_memory(
        "Provider key note", f"The key {key} is for the staging account only.", home_dir=home,
    )

    assert result["redacted"] == 1
    text = Path(result["path"]).read_text(encoding="utf-8")
    assert key not in text and "[REDACTED:api_key]" in text
    assert "is for the staging account only." in text


def test_secret_only_body_is_refused_before_anything_is_written(tmp_path) -> None:
    home = tmp_path / "nvhive"
    vault.init_vault(home)
    folder = home / "vault" / "Wizard Memory"
    before = sorted(p.name for p in folder.iterdir())

    with pytest.raises(ValueError, match="nothing to remember after redacting secrets"):
        vault.append_vault_memory("my key", "sk-" + "z9" * 16, home_dir=home)
    assert sorted(p.name for p in folder.iterdir()) == before

    # Real content around a secret is kept, redacted.
    kept = vault.append_vault_memory(
        "my key", "Use sk-" + "z9" * 16 + " only in staging; rotate monthly.", home_dir=home,
    )
    assert kept["saved"] is True and kept["redacted"] == 1


def test_unknown_category_still_lands_in_wizard_memory_as_a_record(tmp_path) -> None:
    home = tmp_path / "nvhive"

    result = vault.append_vault_memory("Elsewhere", "An unknown category is not a new folder.", category="Secrets", home_dir=home)

    assert result["category"] == "Wizard Memory"
    assert "Category: Wizard Memory" in Path(result["path"]).read_text(encoding="utf-8")
    assert not (home / "vault" / "Secrets").exists()


def test_parse_vault_note_reads_legacy_and_doc_notes_with_derived_ids(tmp_path) -> None:
    home = tmp_path / "nvhive"
    vault.init_vault(home)

    # A pre-0.46 append_vault_memory note: meta paragraph, no Id/Schema/Source.
    legacy = home / "vault" / "Wizard Memory" / "2026-01-01T000000Z0000-Old-note.md"
    legacy.write_text(
        "# Old note\n\nCreated: 2026-01-01T00:00:00+00:00\nCategory: Wizard Memory\n"
        "Tags: #repl #old\n\nbody line 1\n\nbody line 2\n",
        encoding="utf-8",
    )
    record = vault.parse_vault_note(legacy.read_text(encoding="utf-8"), path=legacy)
    expected = hashlib.sha1(b"Wizard Memory/2026-01-01T000000Z0000-Old-note.md").hexdigest()[:32]
    assert record.schema == "legacy"
    assert record.id == expected == vault.derived_note_id(legacy)
    assert (record.title, record.tags, record.body) == ("Old note", ["repl", "old"], "body line 1\n\nbody line 2")
    assert record.source == "api" and record.created == "2026-01-01T00:00:00+00:00"
    assert record.category == "Wizard Memory"

    # A seeded YAML-frontmatter note is documentation, with the same derived-id rule.
    seeded = home / "vault" / "Wizard Memory" / "AI Wizard.md"
    doc = vault.parse_vault_note(seeded.read_text(encoding="utf-8"), path=seeded)
    assert doc.schema == "doc" and doc.id == vault.derived_note_id(seeded)
    assert doc.title == "AI Wizard" and doc.tags == ["wizard", "decision"]
    assert doc.category == "Wizard Memory" and doc.source == "" and doc.created == ""
    assert "[[Product Direction]]" in doc.body

    # A hand-written page with no meta paragraph keeps its first paragraph in the body.
    plain = home / "vault" / "Projects" / "Plain.md"
    plain.write_text("# Plain\n\nNote: the first paragraph is body text.\n\nSecond paragraph.\n", encoding="utf-8")
    hand = vault.parse_vault_note(plain.read_text(encoding="utf-8"), path=plain)
    assert hand.schema == "doc" and hand.category == "Projects" and hand.title == "Plain"
    assert hand.body == "Note: the first paragraph is body text.\n\nSecond paragraph."

    assert vault.parse_vault_note("   \n") is None
    # Without a path there is nothing to derive an id from; the header is still read.
    pathless = vault.parse_vault_note("# Loose\n\nCreated: x\nCategory: Models\nTags: none\n\nbody\n")
    assert pathless.id == "" and pathless.schema == "legacy" and pathless.category == "Models"


def test_list_get_and_delete_vault_notes(tmp_path) -> None:
    home = tmp_path / "nvhive"
    a = vault.append_vault_memory(
        "Alpha memory", "The alpha note mentions quokkas.", tags=["repl"], home_dir=home, source="repl",
    )
    b = vault.append_vault_memory(
        "Beta decision", "The beta note is a decision.", category="Decisions", tags=["wizard"],
        home_dir=home, source="wizard",
    )

    everything = vault.list_vault_notes(home_dir=home, limit=None)
    assert {a["id"], b["id"]} <= {r.id for r in everything}
    assert any(r.schema == "doc" for r in everything)
    assert all(r.body == "" for r in everything)  # headers only

    memory = vault.list_vault_notes(memory_only=True, home_dir=home)
    assert {r.id for r in memory} == {a["id"], b["id"]}
    assert all(r.schema == "nvh-memory/1" for r in memory)
    assert [r.id for r in vault.list_vault_notes(category="Decisions", memory_only=True, home_dir=home)] == [b["id"]]
    assert [r.id for r in vault.list_vault_notes(tag="#repl", home_dir=home)] == [a["id"]]
    # Case-insensitive over title, tags and opening lines (the seeded notes embed the
    # home path, so the needle must be a word no tmp path contains).
    assert [r.id for r in vault.list_vault_notes(query="QUOKKA", home_dir=home)] == [a["id"]]
    assert len(vault.list_vault_notes(home_dir=home, limit=1)) == 1
    assert vault.list_vault_notes(home_dir=tmp_path / "missing") == []

    full = vault.get_vault_note(a["id"], home_dir=home)
    assert full is not None and full.body == "The alpha note mentions quokkas." and full.path == a["path"]
    assert vault.get_vault_note("0" * 32, home_dir=home) is None

    gone = vault.delete_vault_note(a["id"], home_dir=home)
    assert gone == {"deleted": True, "path": a["path"], "id": a["id"]}
    assert not Path(a["path"]).exists()
    refused = vault.delete_vault_note(a["id"], home_dir=home)
    assert refused["deleted"] is False and refused["path"] == "" and "unknown" in refused["error"]
    assert Path(b["path"]).exists()


def test_vault_status_counts_memory_files_without_parsing(tmp_path) -> None:
    home = tmp_path / "nvhive"
    vault.init_vault(home)
    folder = home / "vault" / "Wizard Memory"
    seeded = vault.vault_status(home)["memory_files"]
    assert seeded == len(list(folder.glob("*.md"))) >= 1

    vault.append_vault_memory("Counted", "This note is counted by the directory scan.", home_dir=home)
    (folder / "nested").mkdir()
    (folder / "nested" / "deep.md").write_text("# deep\n", encoding="utf-8")

    status = vault.vault_status(home)
    assert status["memory_files"] == seeded + 1  # direct children only
    assert status["markdown_files"] >= status["memory_files"]
    assert vault.vault_status(tmp_path / "missing")["memory_files"] == 0


def test_vault_memory_route_refuses_a_secret_only_body_with_a_400(tmp_path) -> None:
    """The writer's refusal is a client-input condition: a 400 carrying its words
    (the text the REPL prints), not a 500 with a server traceback."""
    home = tmp_path / "nvhive"
    client = TestClient(app, raise_server_exceptions=False)

    resp = client.post(
        "/v1/vault/memory",
        json={"home_dir": str(home), "title": "groq", "body": "gsk_" + "Ab12" * 8},
    )
    assert resp.status_code == 400
    assert resp.json()["detail"] == "nothing to remember after redacting secrets"
    assert not (home / "vault").exists(), "refused before init_vault: no side effects"

    # The route forwards ``source`` (default ``api``) to the writer.
    saved = client.post(
        "/v1/vault/memory",
        json={"home_dir": str(home), "title": "web note", "body": "Saved from the web vault page.", "source": "web"},
    )
    assert saved.status_code == 200
    assert saved.json()["data"]["source"] == "web"
    assert "Source: web" in Path(saved.json()["data"]["path"]).read_text(encoding="utf-8")


def test_delete_vault_note_refuses_a_path_that_resolves_outside_the_vault(tmp_path, monkeypatch) -> None:
    home = tmp_path / "nvhive"
    vault.init_vault(home)
    outside = tmp_path / "outside.md"
    outside.write_text("# Outside\n\nId: " + "f" * 32 + "\nSchema: nvh-memory/1\n\nnot yours\n", encoding="utf-8")
    monkeypatch.setattr(vault, "_find_note_path", lambda note_id, root: outside)

    refused = vault.delete_vault_note("f" * 32, home_dir=home)

    assert refused == {"deleted": False, "path": str(outside), "id": "f" * 32, "error": "note is outside the vault"}
    assert outside.exists()


def test_vault_listing_skips_a_link_that_leaves_the_vault(tmp_path) -> None:
    """list, get and delete share one boundary: a symlink planted in a synced vault
    that points at a file outside it is not a note (and never read as one)."""
    home = tmp_path / "nvhive"
    vault.init_vault(home)
    secret = tmp_path / "id_ed25519.md"
    secret.write_text("# Key\n\n-----BEGIN OPENSSH PRIVATE KEY-----\nSECRETMATERIAL\n", encoding="utf-8")
    link = home / "vault" / "Wizard Memory" / "x.md"
    try:
        link.symlink_to(secret)
    except (OSError, NotImplementedError) as exc:  # Windows without the symlink privilege
        pytest.skip(f"symlinks unavailable here: {exc}")

    assert link.is_file()
    assert link not in list(vault._iter_vault_notes(home / "vault"))
    assert all(Path(r.path) != link for r in vault.list_vault_notes(home_dir=home, limit=None))
    assert vault.get_vault_note(vault.derived_note_id(link), home_dir=home) is None
    assert vault.delete_vault_note(vault.derived_note_id(link), home_dir=home)["error"] == "unknown note id"
    assert secret.exists()


def test_a_lone_known_header_line_is_body_text_not_a_record(tmp_path) -> None:
    """A record's header carries at least two known keys (the pre-0.46 writer always
    wrote three); a hand-written page whose first paragraph is a single
    ``Created:`` or ``Source:`` line stays a ``doc`` with that line in its body."""
    home = tmp_path / "nvhive"
    vault.init_vault(home)
    page = home / "vault" / "Projects" / "Project X.md"
    page.write_text("# Project X\n\nCreated: 2026-01-01\n\nBody of a hand-written page.\n", encoding="utf-8")

    record = vault.parse_vault_note(page.read_text(encoding="utf-8"), path=page)
    assert (record.schema, record.created, record.source) == ("doc", "", "")
    assert record.body == "Created: 2026-01-01\n\nBody of a hand-written page."
    reading = vault.parse_vault_note("# Reading\n\nSource: Wikipedia\n\nNotes here.\n")
    assert reading.schema == "doc" and reading.source == "" and reading.body.startswith("Source: Wikipedia")
    # Two known keys are a header.
    two = vault.parse_vault_note("# Two\n\nCreated: 2026-01-01T00:00:00+00:00\nTags: #x\n\nbody\n")
    assert (two.schema, two.tags, two.body, two.source) == ("legacy", ["x"], "body", "api")
    assert str(page) not in {r.path for r in vault.list_vault_notes(memory_only=True, home_dir=home)}

    # The seeded root notes carry no category: ``category`` is one of VAULT_DIRS or empty.
    roots = {
        Path(r.path).name: r
        for r in vault.list_vault_notes(home_dir=home, limit=None)
        if Path(r.path).parent == home / "vault"
    }
    assert {"README.md", "MAP.md"} <= set(roots)
    assert all(r.category == "" and r.schema == "doc" for r in roots.values())
