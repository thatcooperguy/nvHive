"""CLI surface after the 0.42 core-module deletions (issue #125).

``nvh knowledge`` -> ``nvh rag`` (hidden forwarding alias), ``nvh template`` ->
hidden migration hint, ``nvh learn`` -> hidden alias of ``nvh rag add``,
``nvh test`` runs the diagnostics smoke report (``--imports`` adds the module
probe, ``smoke`` is a hidden alias), and ``nvh models pull --recommended``
replaces the deleted ``scripts/ollama-setup.sh``.
"""

from __future__ import annotations

import json
import re
import subprocess
import types

import pytest
from typer.main import get_command
from typer.testing import CliRunner

import nvh.cli.main as cli_main

_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _plain(text: str) -> str:
    # Rich colours help output on CI and styles `--flag` as `-` + `-flag`
    # with escape codes between; substring checks need the de-styled text.
    return _ANSI.sub("", text)


def _json_payload(text: str):
    # Under click < 8.2 CliRunner mixes the stderr header into stdout.
    return json.loads(text[text.index("{"):])


@pytest.fixture()
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture()
def nvh_home(tmp_path, monkeypatch):
    monkeypatch.setenv("NVH_HOME", str(tmp_path))
    for var in ("NVHIVE_HOME", "HIVE_CONFIG_HOME", "NVH_STATE"):
        monkeypatch.delenv(var, raising=False)
    return tmp_path


class TestRegistryShape:
    def test_rag_group_with_hidden_knowledge_alias(self):
        root = get_command(cli_main.app)
        rag = root.commands["rag"]
        assert rag.hidden is False
        assert {"add", "ingest", "ask", "list", "remove", "import-legacy"} <= set(rag.commands)
        assert rag.commands["search"].hidden is True  # pre-0.42 `knowledge search`
        knowledge = root.commands["knowledge"]
        assert knowledge.hidden is True
        # A forwarder, not a second copy of the group: `nvh knowledge X` re-enters `nvh rag X`.
        assert knowledge.help == "(alias) nvh rag" and cli_main.DEPRECATED_ALIASES["knowledge"] == "rag"

    def test_removed_spellings_are_hidden(self):
        root = get_command(cli_main.app)
        for name in ("template", "learn", "smoke"):
            assert root.commands[name].hidden is True, name
        assert cli_main.DEPRECATED_ALIASES["smoke"] == cli_main.DEPRECATED_ALIASES["test"] == "status --smoke"
        assert cli_main.DEPRECATED_ALIASES["learn"] == "rag add"
        # Removed outright (migration hint), so not a deprecated spelling of anything.
        assert "template" not in cli_main.DEPRECATED_ALIASES

    def test_deleted_modules_are_gone(self):
        for module in (
            "nvh.core.knowledge", "nvh.core.memory", "nvh.core.smoke_test",
            "nvh.core.templates", "nvh.core.docker_sandbox",
        ):
            with pytest.raises(ImportError):
                __import__(module)


class TestTemplateHint:
    @pytest.mark.parametrize("argv", [["template", "list"], ["template", "show", "x"]])
    def test_prints_migration_hint_and_exits_1(self, runner: CliRunner, argv):
        result = runner.invoke(cli_main.app, argv)
        assert result.exit_code == 1
        assert "prompt_template" in result.output
        assert "agent-profiles" in result.output

    def test_ask_template_renders_profile_prompt_template(self, nvh_home):
        from nvh.integrations.wizard.profiles import AgentProfile, save_user_profile

        save_user_profile(
            AgentProfile(
                name="reviewer", title="Reviewer", description="",
                system_prompt="Be terse.",
                prompt_template="Review this {{lang}} code:\n{{input}}",
            ),
            home_dir=nvh_home,
        )
        prompt, system = cli_main._render_profile_template(
            "reviewer", "print(1)", {"lang": "python"},
        )
        assert prompt == "Review this python code:\nprint(1)"
        assert system == "Be terse."

    def test_ask_template_unknown_profile_points_at_migration(self, nvh_home):
        with pytest.raises(ValueError, match="prompt_template"):
            cli_main._render_profile_template("nope", "x", {})
        # a profile without a template is an error too, not a silent pass-through
        with pytest.raises(ValueError, match="no prompt_template"):
            cli_main._render_profile_template("coder", "x", {})


class TestRagCommands:
    @pytest.mark.parametrize("group", ["rag", "knowledge"])
    def test_list_empty_store(self, runner: CliRunner, nvh_home, group):
        result = runner.invoke(cli_main.app, [group, "list"])
        assert result.exit_code == 0, result.output
        assert "RAG store is empty" in result.output

    def test_add_indexes_file_into_default_collection(self, runner: CliRunner, nvh_home, tmp_path):
        note = tmp_path / "note.md"
        note.write_text("nvHive routes to the cheapest healthy advisor.", encoding="utf-8")

        async def fake_embed(texts, **_kwargs):
            return [[1.0, 0.0] for _ in texts]

        with pytest.MonkeyPatch.context() as mp:
            import nvh.integrations.rag.ingest as ingest_mod

            mp.setattr(ingest_mod, "embed_texts", fake_embed)
            result = runner.invoke(cli_main.app, ["rag", "add", str(note)])
        assert result.exit_code == 0, result.output
        assert "Indexed: 1 file(s)" in _plain(result.output)

        listing = runner.invoke(cli_main.app, ["rag", "list"])
        assert "default" in _plain(listing.output)

    def test_learn_alias_forwards_to_rag_add(self, runner: CliRunner, nvh_home, tmp_path, monkeypatch):
        import nvh.integrations.rag as rag_pkg

        seen: dict = {}

        async def fake_ingest_files(files, collection=None, **_kwargs):
            seen["files"], seen["collection"] = list(files), collection
            return {"ok": True, "files_ingested": len(files), "chunks": 3, "collection": "default"}

        monkeypatch.setattr(rag_pkg, "ingest_files", fake_ingest_files)
        note = tmp_path / "note.md"
        note.write_text("x", encoding="utf-8")
        result = runner.invoke(cli_main.app, ["learn", str(note)])
        assert result.exit_code == 0, result.output
        assert seen == {"files": [note], "collection": None}
        assert "Indexed: 1 file(s)" in _plain(result.output)

    def test_import_legacy_without_store_is_a_noop(self, runner: CliRunner, nvh_home, monkeypatch):
        import nvh.integrations.rag.legacy as legacy_mod

        monkeypatch.setattr(legacy_mod, "legacy_knowledge_dir", lambda: nvh_home / "no-legacy")
        result = runner.invoke(cli_main.app, ["rag", "import-legacy"])
        assert result.exit_code == 0, result.output
        assert "nothing to import" in result.output

    def test_doctor_offers_the_legacy_import(self, runner: CliRunner, nvh_home, tmp_path, monkeypatch):
        import nvh.integrations.rag.legacy as legacy_mod

        legacy = tmp_path / "old-knowledge"
        (legacy / "chunks").mkdir(parents=True)
        (legacy / "documents.json").write_text(json.dumps([
            {"id": "abcd", "filename": "spec.md", "path": str(legacy / "spec.md")},
        ]), encoding="utf-8")
        monkeypatch.setattr(legacy_mod, "legacy_knowledge_dir", lambda: legacy)

        result = runner.invoke(cli_main.app, ["doctor", "--json"])
        report = _json_payload(result.stdout)
        rows = [r for r in json.dumps(report).split('{"check": "') if r.startswith("Legacy knowledge base")]
        assert rows, report
        assert '"status": "warn"' in rows[0]
        assert "nvh rag import-legacy" in rows[0]


class TestSmokeCommand:
    def test_json_report_with_import_probe(self, runner: CliRunner, nvh_home):
        result = runner.invoke(cli_main.app, ["test", "--json", "--imports"])
        report = _json_payload(result.stdout)
        by_id = {t["id"]: t for t in report["tests"]}
        assert by_id["core-imports"]["status"] == "pass", by_id["core-imports"]
        assert report["failed"] == 0
        assert result.exit_code == 0

    def test_quick_flag_is_accepted_and_hidden(self, runner: CliRunner, nvh_home):
        result = runner.invoke(cli_main.app, ["test", "--quick", "--json"])
        assert result.exit_code in (0, 1)
        assert "tests" in _json_payload(result.stdout)
        assert "--quick" in _plain(result.output) and "no longer apply" in _plain(result.output)
        # --help is the target's help: `nvh status`, where the flag never existed.
        help_text = _plain(runner.invoke(cli_main.app, ["test", "--help"]).output)
        assert "--imports" in help_text and "--smoke" in help_text
        assert "quick" not in help_text


class TestModelsPullRecommended:
    def _patch_studio(self, monkeypatch, *, vram: int, installed: set[str]):
        import nvh.integrations.installs.studio_packs as sp

        monkeypatch.setattr(sp, "_detect_vram_gb", lambda: vram)
        monkeypatch.setattr(sp, "_ollama_models", lambda home_dir=None: installed)
        monkeypatch.setattr(sp, "_ollama_binary", lambda home_dir=None: "ollama")
        calls: list[list[str]] = []

        def fake_run(argv, check=False, **_kwargs):
            calls.append(list(argv))
            return types.SimpleNamespace(returncode=0)

        monkeypatch.setattr(subprocess, "run", fake_run)
        return calls

    def test_pulls_vram_tier_models_that_are_missing(self, runner: CliRunner, monkeypatch):
        calls = self._patch_studio(monkeypatch, vram=8, installed={"gemma3:4b"})
        result = runner.invoke(cli_main.app, ["models", "pull", "--recommended"])
        assert result.exit_code == 0, result.output
        pulled = [argv[2] for argv in calls]
        assert "gemma3:4b" not in pulled          # already installed -> skipped
        assert "nomic-embed-text" in pulled       # RAG embedder is in every tier
        assert "qwen3:8b" in pulled               # 8 GB tier
        assert "nemotron" not in pulled           # 40 GB+ tier
        assert "Detected 8 GB VRAM" in _plain(result.output)

    def test_nothing_to_pull_when_all_installed(self, runner: CliRunner, monkeypatch):
        import nvh.integrations.installs.studio_packs as sp

        everything = {m.install_target for m in sp.STUDIO_MODELS}
        calls = self._patch_studio(monkeypatch, vram=0, installed=everything)
        result = runner.invoke(cli_main.app, ["models", "pull", "--recommended"])
        assert result.exit_code == 0, result.output
        assert calls == []
        assert "already installed" in _plain(result.output)

    def test_name_and_flag_are_exclusive(self, runner: CliRunner):
        assert runner.invoke(cli_main.app, ["models", "pull"]).exit_code == 1
        assert runner.invoke(
            cli_main.app, ["models", "pull", "gemma3:4b", "--recommended"],
        ).exit_code == 1


def test_do_sandbox_flag_requires_docker(runner: CliRunner, monkeypatch):
    monkeypatch.delenv("NVH_SANDBOX_REQUIRE_DOCKER", raising=False)
    # --dry-run exits before any engine call; the flag must already be applied.
    monkeypatch.setattr(cli_main, "_run", lambda coro: coro.close())
    result = runner.invoke(cli_main.app, ["do", "list files", "--dry-run", "--sandbox"])
    assert result.exit_code == 0, result.output
    import os

    assert os.environ.get("NVH_SANDBOX_REQUIRE_DOCKER") == "1"
