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
        ctx_dir = tmp_path / ".hive" / "context"
        ctx_dir.mkdir(parents=True)
        (ctx_dir / "rules.md").write_text("---\nname: Rules\n---\nNo swearing.", encoding="utf-8")
        (ctx_dir / "style.md").write_text("Use black formatting.", encoding="utf-8")
        files = find_context_files(project_dir=tmp_path, home_dir=tmp_path / "fakehome")
        assert len(files) >= 2

    def test_finds_global_context(self, tmp_path):
        home = tmp_path / "home"
        global_ctx = home / ".hive" / "global_context.md"
        global_ctx.parent.mkdir(parents=True)
        global_ctx.write_text("Global rules apply everywhere.", encoding="utf-8")
        files = find_context_files(project_dir=tmp_path / "proj", home_dir=home)
        assert any(f.source == "global" for f in files)


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
