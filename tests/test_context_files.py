"""Tests for nvh.core.context_files — HIVE.md discovery, frontmatter, prompt assembly."""

from __future__ import annotations

from nvh.core.context_files import (
    ContextFile,
    _parse_frontmatter,
    build_context_prompt,
    find_context_files,
    get_context_summary,
)


class TestParseFrontmatter:
    def test_with_frontmatter(self):
        content = "---\nname: Rules\nscope: code\npriority: 5\n---\nBody here."
        meta, body = _parse_frontmatter(content)
        assert meta["name"] == "Rules"
        assert meta["scope"] == "code"
        assert meta["priority"] == "5"
        assert body.strip() == "Body here."

    def test_without_frontmatter(self):
        content = "Just plain markdown."
        meta, body = _parse_frontmatter(content)
        assert meta == {}
        assert body == content


class TestFindContextFiles:
    def test_finds_hive_md_in_project_dir(self, tmp_path):
        hive = tmp_path / "HIVE.md"
        hive.write_text("# Project rules\nDo things right.", encoding="utf-8")
        files = find_context_files(project_dir=tmp_path, home_dir=tmp_path / "fakehome")
        assert len(files) >= 1
        assert any("Project" in f.name or "HIVE" in f.path for f in files)

    def test_finds_modular_context_files(self, tmp_path):
        ctx_dir = tmp_path / ".nvh" / "context"
        ctx_dir.mkdir(parents=True)
        (ctx_dir / "rules.md").write_text("---\nname: Rules\n---\nNo swearing.", encoding="utf-8")
        (ctx_dir / "style.md").write_text("Use black formatting.", encoding="utf-8")
        (tmp_path / ".nvh" / "rules").mkdir()
        (tmp_path / ".nvh" / "rules" / "lint.md").write_text("Run ruff.", encoding="utf-8")
        files = find_context_files(
            project_dir=tmp_path, home_dir=tmp_path / "fakehome", nvh_home=tmp_path / "nvh-home",
        )
        assert {f.name for f in files} >= {"Rules", "style", "lint"}

    def test_legacy_hive_context_dirs_are_still_read(self, tmp_path):
        """A project's pre-0.44 ``.hive/context`` keeps working as a fallback."""
        ctx_dir = tmp_path / ".hive" / "context"
        ctx_dir.mkdir(parents=True)
        (ctx_dir / "old.md").write_text("Legacy rule.", encoding="utf-8")
        (tmp_path / ".hive.md").write_text("Legacy dotfile context.", encoding="utf-8")
        files = find_context_files(
            project_dir=tmp_path, home_dir=tmp_path / "fakehome", nvh_home=tmp_path / "nvh-home",
        )
        contents = {f.content for f in files}
        assert "Legacy rule." in contents
        assert "Legacy dotfile context." in contents

    def test_finds_global_context_under_nvh_home(self, tmp_path):
        """The global context is ``$NVH_HOME/config/global_context.md``, not a dotdir in $HOME."""
        nvh_home = tmp_path / "nvh-home"
        global_ctx = nvh_home / "config" / "global_context.md"
        global_ctx.parent.mkdir(parents=True)
        global_ctx.write_text("Global rules apply everywhere.", encoding="utf-8")
        # A stray pre-0.44 ~/.hive/global_context.md is not read from there.
        stale = tmp_path / "home" / ".hive" / "global_context.md"
        stale.parent.mkdir(parents=True)
        stale.write_text("Stale global rules.", encoding="utf-8")
        files = find_context_files(
            project_dir=tmp_path / "proj", home_dir=tmp_path / "home", nvh_home=nvh_home,
        )
        globals_found = [f for f in files if f.source == "global"]
        assert [f.content for f in globals_found] == ["Global rules apply everywhere."]
        assert globals_found[0].path == str(global_ctx)

    def test_global_context_follows_nvh_home_env(self, tmp_path, monkeypatch):
        monkeypatch.setenv("NVH_HOME", str(tmp_path / "env-home"))
        for var in ("NVHIVE_HOME", "NVH_CONFIG", "HIVE_CONFIG_HOME"):
            monkeypatch.delenv(var, raising=False)
        from nvh.core.context_files import global_context_file

        assert global_context_file() == tmp_path / "env-home" / "config" / "global_context.md"


class TestBuildContextPrompt:
    def test_with_context_files_and_user_prompt(self):
        cfiles = [
            ContextFile(path="/x", name="Rules", content="Be nice.", scope="all", source="project"),
        ]
        prompt = build_context_prompt(cfiles, scope="all", user_system_prompt="You are helpful.")
        assert "Be nice." in prompt
        assert "You are helpful." in prompt

    def test_scope_filtering(self):
        cfiles = [
            ContextFile(path="/x", name="Code", content="Code rules.", scope="code", source="project"),
            ContextFile(path="/y", name="All", content="All rules.", scope="all", source="project"),
        ]
        prompt = build_context_prompt(cfiles, scope="code")
        assert "Code rules." in prompt
        assert "All rules." in prompt

    def test_empty_returns_user_prompt(self):
        prompt = build_context_prompt([], scope="all", user_system_prompt="hello")
        assert prompt == "hello"


class TestGetContextSummary:
    def test_returns_summary_list(self):
        cfiles = [
            ContextFile(path="/a.md", name="A", content="aaa", scope="all", priority=10, source="project"),
        ]
        summary = get_context_summary(cfiles)
        assert len(summary) == 1
        assert summary[0]["name"] == "A"
        assert summary[0]["size"] == 3
