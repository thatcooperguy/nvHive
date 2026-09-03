"""CLI surface after issue #127: `ask` flags, `status` tiers, hidden aliases,
registry-derived dispatch, did-you-mean, and the explicit-`nvh do` gate."""

from __future__ import annotations

import io
import json
import re
import sys
import types
from decimal import Decimal
from pathlib import Path

import pytest
import typer
from typer.main import get_command
from typer.testing import CliRunner

import nvh.cli.main as cli_main
from nvh.integrations.diagnostics import checks as diag

_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _plain(text: str) -> str:
    # Rich colours output on CI (FORCE_COLOR) and styles `--flag` as `-` + `-flag`
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


def _fake_checks(monkeypatch, results_by_tier):
    """Replace the registry run with canned rows so no probe touches the box."""
    calls: list[str] = []

    async def fake_run_checks(tier, ctx=None):
        calls.append(tier)
        return list(results_by_tier.get(tier, []))

    monkeypatch.setattr(diag, "run_checks", fake_run_checks)
    return calls


# ---------------------------------------------------------------------------
# Registry shape
# ---------------------------------------------------------------------------

class TestRegistryShape:
    def test_every_deprecated_alias_is_a_hidden_forwarder(self):
        root = get_command(cli_main.app)
        for name, replacement in cli_main.DEPRECATED_ALIASES.items():
            cmd = root.commands[name]
            assert cmd.hidden is True, name
            assert cmd.help.startswith(f"(alias) nvh {replacement}"), name
            # No copied option lists: everything after the name is re-parsed by the target.
            assert cmd.params == [], name
            assert cmd.context_settings["allow_extra_args"] and cmd.context_settings["ignore_unknown_options"], name

    def test_alias_table_covers_the_0_42_spellings(self):
        table = cli_main.DEPRECATED_ALIASES
        assert {n for n, t in table.items() if t.startswith("ask --")} == {
            "code", "write", "research", "math", "quick", "safe", "pipe", "clip",
        }
        assert {n: t.split()[1] for n, t in table.items() if t.startswith("status ")} == {
            "health": "--providers", "why": "--routing", "doctor": "--deep", "test": "--smoke",
            "smoke": "--smoke", "debug": "--report", "selfcheck": "--report",
        }
        assert table["selfcheck"] == "status --report --live --imports"
        assert {n for n, t in table.items() if t.startswith("ask -p ")} == set(cli_main.KNOWN_ADVISORS) - {"mock", "nvidia"}
        assert {"knowledge": "rag", "learn": "rag add"}.items() <= table.items()

    def test_hidden_for_other_reasons_is_not_deprecated(self):
        root = get_command(cli_main.app)
        for name in ("benchmark", "template"):
            assert root.commands[name].hidden is True and name not in cli_main.DEPRECATED_ALIASES, name
        assert root.commands["services"].commands["status"].hidden is True

    def test_real_commands_are_visible_with_their_flags(self):
        root = get_command(cli_main.app)
        assert root.commands["ask"].hidden is False
        assert root.commands["nvidia"].hidden is False  # the dashboard, not the advisor
        flags = {opt for p in root.commands["ask"].params for opt in p.opts}
        assert {"--focus", "--fast", "--local", "--clipboard", "--copy"} <= flags
        tiers = {opt for p in root.commands["status"].params for opt in p.opts}
        assert {"--providers", "--deep", "--smoke", "--report", "--routing"} <= tiers

    def test_known_commands_come_from_the_click_tree(self):
        known = cli_main._known_commands()
        assert known == set(get_command(cli_main.app).commands)
        assert {"ask", "status", "config", "test-gen", "groq", "code", "doctor"} <= known


# ---------------------------------------------------------------------------
# ask flags
# ---------------------------------------------------------------------------

class TestAskFlags:
    def test_unknown_focus_exits_1(self, runner: CliRunner):
        result = runner.invoke(cli_main.app, ["ask", "--focus", "poetry", "hi"])
        assert result.exit_code == 1
        assert "Unknown focus" in result.output

    def test_focus_and_fast_shape_the_query(self, monkeypatch):
        seen: dict = {}

        class FakeRegistry:
            def has(self, name):
                return True

        class FakeEngine:
            registry = FakeRegistry()

            def __init__(self, config=None):
                pass

            async def initialize(self):
                return ["groq", "openai"]

            async def query(self, **kwargs):
                seen.update(kwargs)
                return types.SimpleNamespace(
                    content="42", metadata={}, fallback_from=None, provider="groq",
                    model="m", cost_usd=0, latency_ms=1, cache_hit=False,
                    usage=types.SimpleNamespace(total_tokens=0, input_tokens=0, output_tokens=0),
                )

        import nvh.core.engine as engine_mod

        monkeypatch.setattr(engine_mod, "Engine", FakeEngine)
        monkeypatch.setattr(cli_main, "_read_stdin", lambda: "")
        cli_main._ask("prove it", focus="math", fast=True, output="raw", quiet=True)
        assert seen["provider"] == "openai"  # math preference beats the fast list
        assert seen["strategy"] == "cheapest"
        assert seen["system_prompt"].startswith("You are an expert mathematician")

        seen.clear()
        cli_main._ask("hi", fast=True, output="raw", quiet=True)
        assert seen["provider"] == "groq"

    def test_local_requires_ollama(self, monkeypatch, capsys):
        class FakeRegistry:
            def has(self, name):
                return False

        class FakeEngine:
            registry = FakeRegistry()

            def __init__(self, config=None):
                pass

            async def initialize(self):
                return ["groq"]

        import nvh.core.engine as engine_mod

        monkeypatch.setattr(engine_mod, "Engine", FakeEngine)
        monkeypatch.setattr(cli_main, "_read_stdin", lambda: "")
        with pytest.raises(typer.Exit) as exc:
            cli_main._ask("secret", local=True, output="raw", quiet=True)
        assert exc.value.exit_code == 1
        assert "--local needs Ollama" in capsys.readouterr().out

    def test_stdin_is_capped_and_marked_truncated(self, monkeypatch):
        big = "x" * (cli_main._STDIN_MAX_CHARS + 10)
        monkeypatch.setattr(cli_main.sys, "stdin", io.StringIO(big))
        text = cli_main._read_stdin()
        assert text.startswith("x" * cli_main._STDIN_MAX_CHARS)
        assert text.endswith("[Content truncated — input exceeded limit]")

    def test_write_alias_keeps_tone(self, monkeypatch):
        captured: dict = {}
        monkeypatch.setattr(cli_main, "_ask", lambda prompt, **kw: captured.update(prompt=prompt, **kw))
        CliRunner().invoke(cli_main.app, ["write", "a haiku", "--tone", "casual"])
        assert captured["focus"] == "write"
        assert "casual" in captured["system"]

    def test_pipe_alias_is_raw_ask(self, monkeypatch):
        captured: dict = {}
        monkeypatch.setattr(cli_main, "_ask", lambda prompt, **kw: captured.update(prompt=prompt, **kw))
        CliRunner().invoke(cli_main.app, ["pipe", "summarize", "-a", "groq", "--json"])
        assert captured["output"] == "raw" and captured["quiet"] is True
        assert captured["provider"] == "groq"
        assert "valid JSON only" in captured["system"]

    def test_provider_alias_forwards_to_ask(self, monkeypatch):
        captured: dict = {}
        monkeypatch.setattr(cli_main, "_ask", lambda prompt, **kw: captured.update(prompt=prompt, **kw))
        result = CliRunner().invoke(cli_main.app, ["groq", "hello", "--raw"])
        assert result.exit_code == 0, result.output
        assert (captured["prompt"], captured["provider"], captured["output"], captured["quiet"]) == ("hello", "groq", "raw", True)
        # Any `nvh ask` flag works through the alias — nothing is re-declared.
        CliRunner().invoke(cli_main.app, ["groq", "hello", "--focus", "code", "--no-stream"])
        assert captured["focus"] == "code" and captured["stream"] is False

    def test_provider_alias_without_question_is_the_login_flow(self, monkeypatch):
        logins: list = []
        monkeypatch.setattr(cli_main, "advisor_login", lambda name, headless: logins.append((name, headless)))
        monkeypatch.setattr(cli_main, "_ask", lambda *a, **kw: pytest.fail("no question → no query"))
        for argv in (["groq"], ["groq", "-m", "llama-3.3"]):
            assert CliRunner().invoke(cli_main.app, argv).exit_code == 0, argv
        assert logins == [("groq", False), ("groq", False)]

    def test_clip_and_code_translate_their_legacy_flags(self, monkeypatch):
        captured: dict = {}
        monkeypatch.setattr(cli_main, "_ask", lambda prompt, **kw: captured.update(prompt=prompt, **kw))
        CliRunner().invoke(cli_main.app, ["clip", "explain", "-a", "groq", "-c"])
        assert captured["prompt"] == cli_main._CLIP_ACTIONS["explain"]
        assert captured["provider"] == "groq" and captured["clipboard"] and captured["copy"]
        result = CliRunner().invoke(cli_main.app, ["clip", "bogus"])
        assert result.exit_code == 1 and "Unknown action" in result.output
        captured.clear()
        CliRunner().invoke(cli_main.app, ["code", "fix it", "-a", "openai", "-f", "x.py"])
        assert (captured["focus"], captured["provider"], captured["file"]) == ("code", "openai", "x.py")

    def test_alias_help_is_the_targets_help(self, runner: CliRunner):
        result = runner.invoke(cli_main.app, ["doctor", "--help"])
        assert result.exit_code == 0
        text = _plain(result.output)
        assert "--deep" in text and "--fix" in text and "nvh doctor" in text

    def _engine_that_raises(self, monkeypatch):
        from nvh.providers.base import ProviderUnavailableError

        class FakeRegistry:
            def has(self, name):
                return True

        class FakeEngine:
            registry = FakeRegistry()

            def __init__(self, config=None):
                pass

            async def initialize(self):
                return ["groq"]

            async def query(self, **kwargs):
                raise ProviderUnavailableError("groq is down")

        import nvh.core.engine as engine_mod

        monkeypatch.setattr(engine_mod, "Engine", FakeEngine)
        monkeypatch.setattr(cli_main, "_read_stdin", lambda: "")

    def test_pipe_errors_go_to_stderr(self, runner: CliRunner, monkeypatch):
        self._engine_that_raises(monkeypatch)
        result = runner.invoke(cli_main.app, ["pipe", "summarize this"])
        assert result.exit_code == 1
        assert "Provider unavailable" not in result.stdout
        assert "Provider unavailable" in result.stderr and "groq is down" in result.stderr

    def _council_engine(self, monkeypatch, enabled, seen, router_pick="perplexity"):
        class FakeRegistry:
            def has(self, name):
                return name in enabled

        class FakeRouter:
            # What the engine's router would pick for an unpinned turn; defaults to
            # the one advisor a grounded research prompt must never land on.
            def route(self, query, **kwargs):
                seen["route"] = {"query": query, **kwargs}
                return types.SimpleNamespace(provider=router_pick, model="m", reason="fake", scores={})

        class FakeEngine:
            registry = FakeRegistry()
            router = FakeRouter()

            def __init__(self, config=None):
                pass

            async def initialize(self):
                return list(enabled)

            async def query(self, **kwargs):
                seen["query"] = kwargs
                chosen = kwargs.get("provider") or "router"
                return types.SimpleNamespace(
                    content=f"answer from {chosen}", metadata={}, fallback_from=None, provider=chosen,
                    model="m", cost_usd=0, latency_ms=1, cache_hit=False,
                    usage=types.SimpleNamespace(total_tokens=0, input_tokens=0, output_tokens=0),
                )

            async def run_council(self, **kwargs):
                seen["council"] = kwargs
                return types.SimpleNamespace(
                    synthesis=types.SimpleNamespace(content="council synthesis"),
                    member_responses={}, agents_used=["Historian", "Economist"],
                    total_cost_usd=Decimal("0.0123"), total_latency_ms=1500,
                    confidence_score=0.8, agreement_summary="strong agreement",
                )

        import nvh.core.engine as engine_mod

        monkeypatch.setattr(engine_mod, "Engine", FakeEngine)
        monkeypatch.setattr(cli_main, "_read_stdin", lambda: "")

    _HITS = {
        "ok": True, "backend": "searxng", "query": "state of fusion power",
        "results": [
            {"title": "ITER schedule update", "url": "https://iter.org/news", "snippet": "First plasma now 2034."},
            {"title": "Helion signs PPA", "url": "https://example.com/helion", "snippet": "Microsoft deal for 2028."},
            {"title": "NIF ignition", "url": "https://llnl.gov/nif", "snippet": "Repeat ignition shots in 2025."},
        ],
    }
    _SEARCH_DOWN = {"ok": False, "backend": "duckduckgo", "error": "ConnectError: refused"}

    def _fake_web_search(self, monkeypatch, envelope):
        """Replace the web_search backend dispatch with a canned envelope; returns the calls."""
        import nvh.integrations.web_search as web_search_pkg

        calls: list[dict] = []

        async def fake_web_search(query, *, top_k=5, timeout=10.0):
            calls.append({"query": query, "top_k": top_k})
            return envelope

        monkeypatch.setattr(web_search_pkg, "web_search", fake_web_search)
        return calls

    def test_research_focus_grounds_the_chosen_advisor_on_web_sources(self, monkeypatch, capsys):
        seen: dict = {}
        self._council_engine(monkeypatch, ["groq", "openai"], seen)
        calls = self._fake_web_search(monkeypatch, self._HITS)
        cli_main._ask("state of fusion power", focus="research", stream=False)
        assert "council" not in seen
        assert calls == [{"query": "state of fusion power", "top_k": 6}]
        sent = seen["query"]["prompt"]
        assert "[1] ITER schedule update\n    https://iter.org/news\n    First plasma now 2034." in sent
        assert "[2] Helion signs PPA" in sent and "[3] NIF ignition" in sent
        assert sent.endswith("Question: state of fusion power")
        assert seen["query"]["provider"] == "openai"  # preference order; perplexity no longer in it
        assert seen["query"]["system_prompt"].startswith("You are a thorough research assistant")
        out = " ".join(_plain(capsys.readouterr().out).split())
        assert "[research → grounded on 3 web sources via searxng]" in out
        # Both banners must be escaped: Rich reads a bare `[focus: …]` as a style tag and prints nothing.
        assert "[focus: research → openai]" in out
        assert "answer from openai" in out

    def test_research_focus_searches_the_question_but_grounds_the_full_prompt(self, monkeypatch):
        seen: dict = {}
        self._council_engine(monkeypatch, ["openai"], seen)
        calls = self._fake_web_search(monkeypatch, self._HITS)
        monkeypatch.setattr(cli_main, "_read_stdin", lambda: "PASTED CONTEXT")
        cli_main._ask("what changed", focus="research", output="raw", quiet=True)
        assert calls[0]["query"] == "what changed"
        sent = seen["query"]["prompt"]
        assert "[1] ITER schedule update" in sent
        assert sent.endswith("Question: what changed\n\nPASTED CONTEXT")

    def test_research_focus_no_longer_prefers_perplexity(self, monkeypatch):
        assert "perplexity" not in cli_main.FOCUS_MODES["research"][1]
        seen: dict = {}
        self._council_engine(monkeypatch, ["perplexity", "openai"], seen)
        self._fake_web_search(monkeypatch, self._HITS)
        cli_main._ask("state of fusion power", focus="research", output="raw", quiet=True)
        assert seen["query"]["provider"] == "openai"

    def test_research_focus_pinned_perplexity_skips_grounding(self, monkeypatch, capsys):
        seen: dict = {}
        self._council_engine(monkeypatch, ["perplexity", "groq"], seen)
        calls = self._fake_web_search(monkeypatch, self._HITS)
        cli_main._ask(
            "state of fusion power", provider="perplexity", focus="research", output="raw", quiet=True,
        )
        assert calls == [] and "council" not in seen
        assert seen["query"]["provider"] == "perplexity"
        assert seen["query"]["prompt"] == "state of fusion power"  # Sonar searches on its own
        assert "answer from perplexity" in capsys.readouterr().out

    def test_research_focus_falls_back_to_the_council_when_search_is_down(self, monkeypatch, capsys):
        seen: dict = {}
        self._council_engine(monkeypatch, ["groq", "openai"], seen)
        self._fake_web_search(monkeypatch, self._SEARCH_DOWN)
        cli_main._ask("state of fusion power", focus="research")
        assert "query" not in seen
        assert seen["council"]["auto_agents"] is True and seen["council"]["synthesize"] is True
        assert seen["council"]["prompt"] == "state of fusion power"
        assert seen["council"]["system_prompt"].startswith("You are a thorough research assistant")
        out = " ".join(_plain(capsys.readouterr().out).split())
        assert (
            "[research → web search unavailable (ConnectError: refused),"
            " synthesizing from multiple advisors]"
        ) in out
        assert "council synthesis" in out
        assert "Historian, Economist" in out and "80%" in out and "strong agreement" in out

    def test_research_focus_empty_results_also_fall_back_to_the_council(self, monkeypatch, capsys):
        seen: dict = {}
        self._council_engine(monkeypatch, ["openai"], seen)
        self._fake_web_search(monkeypatch, {"ok": True, "backend": "duckduckgo", "results": []})
        cli_main._ask("state of fusion power", focus="research")
        assert "query" not in seen and "council" in seen
        out = " ".join(_plain(capsys.readouterr().out).split())
        assert "web search unavailable (no results)" in out

    def test_research_focus_pinned_advisor_answers_ungrounded_when_search_is_down(self, monkeypatch, capsys):
        seen: dict = {}
        self._council_engine(monkeypatch, ["groq", "openai"], seen)
        self._fake_web_search(monkeypatch, self._SEARCH_DOWN)
        cli_main._ask("state of fusion power", provider="groq", focus="research", stream=False)
        assert "council" not in seen  # -p pins the advisor; the council would ignore it
        assert seen["query"]["provider"] == "groq"
        assert seen["query"]["prompt"] == "state of fusion power"
        out = " ".join(_plain(capsys.readouterr().out).split())
        assert "web search unavailable (ConnectError: refused), asking groq without sources" in out

    def test_research_focus_local_skips_web_search_and_says_why(self, monkeypatch, capsys):
        # --local promises nothing leaves this machine, so the question never
        # reaches a search engine. The banners must also render: Rich reads a
        # bare `[local mode — ...]` as a style tag and prints an empty line.
        seen: dict = {}
        self._council_engine(monkeypatch, ["ollama", "openai"], seen)
        calls = self._fake_web_search(monkeypatch, self._HITS)
        cli_main._ask("state of fusion power", focus="research", local=True, stream=False)
        assert calls == [] and "council" not in seen
        assert seen["query"]["provider"] == "ollama" and seen["query"]["privacy"] is True
        assert seen["query"]["prompt"] == "state of fusion power"
        out = " ".join(_plain(capsys.readouterr().out).split())
        assert "[research → local mode, web search skipped so nothing leaves this machine]" in out
        assert "[local mode — Ollama only, nothing leaves this machine, nothing stored]" in out
        assert "[focus: research → ollama]" in out

    def test_research_focus_privacy_skips_web_search(self, monkeypatch, capsys):
        seen: dict = {}
        self._council_engine(monkeypatch, ["openai"], seen)
        calls = self._fake_web_search(monkeypatch, self._HITS)
        cli_main._ask("state of fusion power", focus="research", privacy=True, stream=False)
        assert calls == [] and "council" not in seen
        assert seen["query"]["provider"] == "openai"
        assert seen["query"]["prompt"] == "state of fusion power"
        out = " ".join(_plain(capsys.readouterr().out).split())
        assert "[research → privacy mode, web search skipped" in out
        assert "[privacy mode — no data stored]" in out

    def test_research_focus_folds_rag_and_web_sources_under_one_instruction(self, monkeypatch, capsys):
        # The RAG block used to sit above "answer using only the numbered sources
        # below", which told the advisor to ignore the local documents.
        import nvh.integrations.rag as rag_pkg
        from nvh.integrations.web_search.grounding import GROUNDING_INSTRUCTIONS

        seen: dict = {}
        self._council_engine(monkeypatch, ["openai"], seen)
        self._fake_web_search(monkeypatch, self._HITS)
        rag_queries: list[str] = []

        async def fake_rag_ask(query, **kwargs):
            rag_queries.append(query)
            return {"ok": True, "chunks": [{
                "source": "notes/fusion.md", "text": "Our lab note: tokamak budget doubled.",
                "chunk_index": 0, "score": 0.9,
            }]}

        monkeypatch.setattr(rag_pkg, "ask", fake_rag_ask)
        cli_main._ask("state of fusion power", focus="research", knowledge=True, stream=False)
        assert rag_queries == ["state of fusion power"]  # retrieval sees the user's words, not web snippets
        sent = seen["query"]["prompt"]
        assert GROUNDING_INSTRUCTIONS not in sent  # one instruction, not two
        head = sent.split("\n\n", 1)[0]
        assert "local documents" in head and "numbered web sources" in head
        assert "Cite [n] for web sources" in head
        assert (
            sent.index("Local documents:")
            < sent.index("Our lab note: tokamak budget doubled.")
            < sent.index("Web sources:")
            < sent.index("[1] ITER schedule update")
        )
        assert sent.endswith("Question: state of fusion power")
        out = " ".join(_plain(capsys.readouterr().out).split())
        assert "[rag context injected]" in out
        assert "[research → grounded on 3 web sources via searxng]" in out

    def test_research_focus_rag_block_leads_the_prompt_when_search_is_skipped(self, monkeypatch):
        import nvh.integrations.rag as rag_pkg

        seen: dict = {}
        self._council_engine(monkeypatch, ["openai"], seen)
        self._fake_web_search(monkeypatch, self._SEARCH_DOWN)

        async def fake_rag_ask(query, **kwargs):
            return {"ok": True, "chunks": [{"source": "n.md", "text": "local fact", "chunk_index": 0, "score": 1}]}

        monkeypatch.setattr(rag_pkg, "ask", fake_rag_ask)
        cli_main._ask("state of fusion power", provider="openai", focus="research", knowledge=True, output="raw", quiet=True)
        sent = seen["query"]["prompt"]
        assert sent.startswith("Retrieved context from your indexed folder:")
        assert "local fact" in sent and sent.endswith("\n\nstate of fusion power")

    def test_research_focus_searches_the_first_line_of_a_pasted_file_when_no_prompt(self, monkeypatch, tmp_path):
        # With no positional prompt the query used to be the fenced body itself,
        # opening with ``` -- junk to a search engine and a spurious council fallback.
        seen: dict = {}
        self._council_engine(monkeypatch, ["openai"], seen)
        calls = self._fake_web_search(monkeypatch, self._HITS)
        doc = tmp_path / "fusion.md"
        doc.write_text("\n\nFusion power roadmap 2030\n\nITER first plasma slips again.\n", encoding="utf-8")
        cli_main._ask(None, focus="research", file=str(doc), output="raw", quiet=True)
        assert calls == [{"query": "Fusion power roadmap 2030", "top_k": 6}]
        sent = seen["query"]["prompt"]
        assert "[1] ITER schedule update" in sent
        assert sent.endswith(
            "Question: ```\n\n\nFusion power roadmap 2030\n\nITER first plasma slips again.\n\n```"
        )

    def test_research_focus_skips_grounding_when_the_pasted_text_has_nothing_to_search(self, monkeypatch, capsys):
        seen: dict = {}
        self._council_engine(monkeypatch, ["openai"], seen)
        calls = self._fake_web_search(monkeypatch, self._HITS)
        monkeypatch.setattr(cli_main, "_read_stdin", lambda: "```\n---\n```")
        cli_main._ask(None, focus="research", stream=False)
        assert calls == [] and "council" not in seen  # nothing was searched, so nothing "failed"
        assert seen["query"]["provider"] == "openai" and seen["query"]["prompt"] == "```\n---\n```"
        out = " ".join(_plain(capsys.readouterr().out).split())
        assert "[research → nothing searchable in the input, answering without web sources]" in out

    def test_research_focus_never_routes_a_grounded_prompt_to_perplexity(self, monkeypatch):
        # No research-preferred advisor is enabled, so the router would pick; it
        # must not land the already-grounded prompt on Perplexity, which searches again.
        seen: dict = {}
        self._council_engine(monkeypatch, ["perplexity", "groq"], seen, router_pick="perplexity")
        self._fake_web_search(monkeypatch, self._HITS)
        cli_main._ask("state of fusion power", focus="research", output="raw", quiet=True)
        assert seen["route"]["query"] == "state of fusion power"  # routed on the user's words, not the sources
        assert seen["query"]["provider"] == "groq"
        assert "[1] ITER schedule update" in seen["query"]["prompt"]

    def test_research_focus_keeps_the_router_pick_when_it_is_not_search_native(self, monkeypatch):
        seen: dict = {}
        self._council_engine(monkeypatch, ["perplexity", "groq", "deepseek"], seen, router_pick="deepseek")
        self._fake_web_search(monkeypatch, self._HITS)
        cli_main._ask("state of fusion power", focus="research", output="raw", quiet=True)
        assert seen["query"]["provider"] == "deepseek"

    def test_research_focus_with_only_perplexity_enabled_lets_it_search_itself(self, monkeypatch, capsys):
        seen: dict = {}
        self._council_engine(monkeypatch, ["perplexity"], seen)
        calls = self._fake_web_search(monkeypatch, self._HITS)
        cli_main._ask("state of fusion power", focus="research", stream=False)
        assert calls == [] and "council" not in seen
        assert seen["query"]["provider"] == "perplexity"
        assert seen["query"]["prompt"] == "state of fusion power"
        out = " ".join(_plain(capsys.readouterr().out).split())
        assert "[research → perplexity is the only advisor enabled and searches on its own]" in out


# ---------------------------------------------------------------------------
# status tiers (registry mocked)
# ---------------------------------------------------------------------------

def _row(id, title, status, detail="", fix="", **data):
    return diag.CheckResult(id, title, status, detail, fix, data)


class TestStatusTiers:
    def test_glance_renders_registry_rows(self, runner: CliRunner, monkeypatch):
        calls = _fake_checks(monkeypatch, {diag.GLANCE: [
            _row("gpu", "GPU", "pass", gpus=[{"name": "RTX 6000", "vram_gb": 48.0, "utilization_pct": 3}]),
            _row("ollama", "Ollama", "pass", models=["gemma3:4b", "qwen3:8b"]),
            _row("provider_health", "Advisor groq", "pass", "80ms", provider="groq", healthy=True),
            _row("provider_health", "Advisor llm7", "warn", "timeout", provider="llm7", healthy=False),
            _row("budget", "Budget", "info", "$0.10 spent today | $1.00 spent this month"),
            _row("services", "Services", "warn", "Ollama :11434 running · API :8000 not running · WebUI :3000 not running"),
        ]})
        result = runner.invoke(cli_main.app, ["status"])
        assert result.exit_code == 0, result.output
        assert calls == [diag.GLANCE]
        out = _plain(result.output)
        assert "RTX 6000 (48 GB) — 3% utilized" in out
        assert "gemma3:4b (loaded), qwen3:8b (loaded)" in out
        assert "1/2 online" in out
        assert "$0.10 spent today" in out
        assert "API :8000 not running" in out

    def test_multiple_tiers_is_a_usage_error(self, runner: CliRunner):
        result = runner.invoke(cli_main.app, ["status", "--deep", "--smoke"])
        assert result.exit_code == 2
        assert "Pick one tier" in result.output

    def test_deep_json_is_pure_json_with_check_first(self, runner: CliRunner, monkeypatch):
        _fake_checks(monkeypatch, {diag.DEEP: [
            _row("python", "Python version", "pass", "3.12.1"),
            _row("legacy_knowledge", "Legacy knowledge base", "warn", "2 docs", "Run `nvh rag import-legacy`"),
        ]})
        result = runner.invoke(cli_main.app, ["status", "--deep", "--json"])
        assert result.exit_code == 0, result.stderr
        report = json.loads(result.stdout)
        assert report["schema_version"] == 2
        assert (report["passed"], report["warned"], report["failed"]) == (1, 1, 0)
        assert report["fixes"] == ["Run `nvh rag import-legacy`"]
        assert list(report["checks"][0])[:2] == ["check", "status"]
        assert "running diagnostics" in result.stderr

    def test_deep_table_exits_1_on_failure(self, runner: CliRunner, monkeypatch):
        _fake_checks(monkeypatch, {diag.DEEP: [
            _row("config", "Config file exists", "fail", "/nope", "Run `nvh config init`"),
        ]})
        result = runner.invoke(cli_main.app, ["status", "--deep"])
        assert result.exit_code == 1
        out = _plain(result.output)
        assert "Diagnostic Results" in out
        assert "1 failures" in out
        assert "Suggested fixes" in out

    def test_doctor_alias_forwards_flags(self, runner: CliRunner, monkeypatch):
        seen: dict = {}
        monkeypatch.setattr(cli_main, "_run_status", lambda tier, **kw: seen.update(tier=tier, **kw))
        result = runner.invoke(cli_main.app, ["doctor", "--json", "--fix", "--home-dir", "/x"])
        assert result.exit_code == 0, result.output
        assert seen["tier"] == "deep" and seen["json_output"] and seen["fix"] and seen["home_dir"] == "/x"

    def test_alias_flags_are_reparsed_by_status(self, runner: CliRunner, monkeypatch):
        """`nvh debug --live` is `nvh status --report --live`: aliases copy no option lists."""
        seen: dict = {}
        monkeypatch.setattr(cli_main, "_run_status", lambda tier, **kw: seen.update(tier=tier, **kw))
        assert runner.invoke(cli_main.app, ["debug", "--live"]).exit_code == 0
        assert seen["tier"] == "report" and seen["live"] is True
        seen.clear()
        result = runner.invoke(cli_main.app, ["selfcheck", "--no-live-query", "--query", "ping", "--quiet", "-o", "b.json"])
        assert result.exit_code == 0, result.output
        assert seen["tier"] == "report" and seen["imports"] is True and seen["live"] is False
        assert seen["live_prompt"] == "ping" and seen["quiet"] is True and seen["output"] == "b.json"
        seen.clear()
        assert runner.invoke(cli_main.app, ["selfcheck"]).exit_code == 0
        assert seen["live"] is True and seen["live_prompt"] == "Say hello in one sentence"
        for name in ("health", "why"):
            runner.invoke(cli_main.app, [name])
            assert seen["tier"] == cli_main.DEPRECATED_ALIASES[name].split("--")[1]

    def test_test_alias_keeps_the_old_flags_for_one_release(self, runner: CliRunner, monkeypatch):
        seen: dict = {}
        monkeypatch.setattr(cli_main, "_run_status", lambda tier, **kw: seen.update(tier=tier, **kw))
        monkeypatch.delenv("NVH_API_URL", raising=False)
        result = runner.invoke(cli_main.app, [
            "test", "--no-webui", "--strict", "--api", "http://box:8000", "--webui=http://box:3000",
            "--no-providers", "--fix", "--quick", "--json",
        ])
        assert result.exit_code == 0, result.output
        assert seen["tier"] == "smoke" and seen["strict"] is True and seen["json_output"] is True
        assert cli_main.os.environ["NVH_API_URL"] == "http://box:8000"
        note = _plain(result.output)
        assert "--no-webui" in note and "--quick" in note and "no longer apply" in note
        assert "http://box" not in note

    def test_test_alias_with_legacy_flags_never_exits_2(self, runner: CliRunner, nvh_home):
        result = runner.invoke(cli_main.app, ["test", "--no-webui", "--strict"])
        assert result.exit_code in (0, 1), result.output

    def test_status_json_serialises_check_data(self, runner: CliRunner, monkeypatch):
        budget = {"daily_spend": Decimal("0.10"), "daily_limit": Decimal("0"), "by_provider": {"groq": Decimal("0.10")}}
        _fake_checks(monkeypatch, {
            diag.GLANCE: [_row("budget", "Budget", "info", "$0.10 spent today", **budget)],
            diag.DEEP: [_row("storage", "Rootless storage", "pass", "/x", home=Path("/x"), **budget)],
        })
        result = runner.invoke(cli_main.app, ["status", "--json"])
        assert result.exit_code == 0, result.output
        report = _json_payload(result.stdout)
        assert report["checks"][0]["data"]["daily_spend"] == "0.10"
        assert report["checks"][0]["data"]["by_provider"] == {"groq": "0.10"}

        result = runner.invoke(cli_main.app, ["status", "--deep", "--json"])
        assert result.exit_code == 0, result.output
        report = _json_payload(result.stdout)
        assert report["checks"][0]["data"]["home"] == str(Path("/x"))
        assert report["checks"][0]["data"]["daily_spend"] == "0.10"

    def test_providers_tier_table(self, runner: CliRunner, monkeypatch):
        _fake_checks(monkeypatch, {diag.PROVIDERS: [
            _row("provider_health", "Advisor groq", "pass", "80ms", provider="groq", healthy=True, score=0.95, chain_position=1),
            _row("provider_health", "Advisor llm7", "warn", "429", provider="llm7", healthy=False, score=0.3, chain_position=2),
            _row("fallback_chain", "Fallback chain", "info", "groq → llm7", chain=["groq", "llm7"]),
        ]})
        result = runner.invoke(cli_main.app, ["health"])
        assert result.exit_code == 0, result.output
        out = _plain(result.output)
        assert "Healthy" in out and "Unhealthy" in out
        assert "1/2" in out and "Vulnerable" in out
        assert "groq → llm7" in out

    def test_smoke_tier_is_the_old_test_command(self, runner: CliRunner, nvh_home):
        result = runner.invoke(cli_main.app, ["status", "--smoke", "--json"])
        report = json.loads(result.stdout)
        assert {"summary", "tests", "failed"} <= set(report)
        assert result.exit_code == (1 if report["failed"] else 0)

    def test_report_tier_writes_a_bundle(self, runner: CliRunner, monkeypatch, nvh_home, tmp_path):
        _fake_checks(monkeypatch, {diag.REPORT: [
            _row("python", "Python version", "pass", "3.12"),
            _row("disk", "Disk space", "warn", "3GB free", "Free up disk space"),
        ]})
        import importlib

        smoke_mod = importlib.import_module("nvh.integrations.diagnostics.smoke_tests")
        passport_mod = importlib.import_module("nvh.integrations.workspace.passport")

        monkeypatch.setattr(
            smoke_mod, "smoke_test_report",
            lambda home_dir=None, imports=False: {"summary": "ok", "ready": True, "passed": 1, "warnings": 0, "failed": 0, "tests": []},
        )
        seeds: dict = {}

        def fake_snapshot(home_dir=None, include_logs=True, **report_kwargs):
            seeds.update(report_kwargs)
            return {"path": "snap.json", "passport": {"workspace_id": "w", "rootless": True}, "excludes": []}

        monkeypatch.setattr(passport_mod, "support_snapshot", fake_snapshot)
        out = tmp_path / "bundle.json"
        result = runner.invoke(cli_main.app, ["status", "--report", "-o", str(out)])
        assert result.exit_code == 0, result.output
        bundle = json.loads(out.read_text(encoding="utf-8"))
        assert bundle["schema_version"] == 2
        assert bundle["components"]["checks"]["warned"] == 1
        assert bundle["components"]["wizard_live_turn"] == {"skipped": True}
        assert bundle["components"]["support_snapshot"]["workspace_id"] == "w"
        # The snapshot embeds the sections the bundle already ran, not a second run.
        assert seeds["registry_checks"]["warned"] == 1
        assert seeds["smoke_tests"]["summary"] == "ok"
        assert bundle["status"]["warnings"] == ["checks: 1 warning(s)"]
        assert "Bundle OK" in result.output and "Needs attention" in result.output

        strict = runner.invoke(cli_main.app, ["status", "--report", "-o", str(out), "--strict"])
        assert strict.exit_code == 1

    def test_routing_tier_without_a_query(self, runner: CliRunner, monkeypatch, tmp_path):
        monkeypatch.setattr(cli_main.Path, "home", classmethod(lambda cls: tmp_path))
        result = runner.invoke(cli_main.app, ["status", "--routing"])
        assert result.exit_code == 0
        assert "nvh status --routing" in result.output


# ---------------------------------------------------------------------------
# checks registry
# ---------------------------------------------------------------------------

class TestChecksRegistry:
    def test_tiers_select_subsets(self):
        ids = {tier: {c.id for c in diag.checks_for(tier)} for tier in diag.TIERS}
        assert ids[diag.GLANCE] == {"provider_health", "ollama", "gpu", "cloud_session", "budget", "services"}
        assert {"provider_health", "fallback_chain"} == ids[diag.PROVIDERS]
        assert ids[diag.DEEP] > {"python", "config", "database", "provider_keys", "provider_health", "ollama", "disk", "gpu", "nvh_on_path"}
        assert ids[diag.REPORT] > ids[diag.DEEP]
        assert {"system", "dependencies", "network", "routing_probe"} <= ids[diag.REPORT]

    def test_run_check_wraps_exceptions(self):
        import asyncio

        def boom(ctx):
            raise RuntimeError("kaput")

        rows = asyncio.run(diag.run_check(diag.Check("x", "X", frozenset({diag.DEEP}), boom), diag.CheckContext()))
        assert rows == [diag.CheckResult("x", "X", "warn", "check failed: kaput")]

    def test_summarize_counts_and_fixes(self):
        rows = [
            _row("a", "A", "pass"), _row("b", "B", "warn", fix="do b"),
            _row("c", "C", "fail", fix="do c"), _row("d", "D", "info"),
        ]
        summary = diag.summarize(rows)
        assert (summary["total"], summary["passed"], summary["warned"], summary["failed"]) == (4, 1, 1, 1)
        assert summary["fixes"] == ["do b", "do c"]
        assert summary["checks"][0]["check"] == "A"

    def test_context_probes_ollama_once(self, monkeypatch):
        calls = []
        monkeypatch.setattr(diag, "probe_ollama_models", lambda timeout=5.0: calls.append(1) or ["m1"])
        ctx = diag.CheckContext()
        assert ctx.ollama_models == ["m1"] and ctx.ollama_models == ["m1"]
        assert len(calls) == 1
        ctx.reset_ollama()
        ctx.ollama_models
        assert len(calls) == 2


# ---------------------------------------------------------------------------
# main() dispatcher
# ---------------------------------------------------------------------------

class _AppProxy:
    """The real Typer for registry lookups, a marker instead of Click for `app()`."""

    def __init__(self, real, events):
        self._real, self._events = real, events

    def __getattr__(self, name):
        return getattr(self._real, name)

    def __call__(self, *args, **kwargs):
        self._events.append("app")


@pytest.fixture()
def dispatch(monkeypatch):
    """Run main() with argv; a real `app()` or LLM path is replaced by a marker."""
    events: list[str] = []

    def fake_run(coro):
        if hasattr(coro, "close"):  # a real _smart_default(...) coroutine
            coro.close()
            events.append("smart_default")

    monkeypatch.setattr(cli_main, "app", _AppProxy(cli_main.app, events))
    monkeypatch.setattr(cli_main, "_run", fake_run)
    monkeypatch.setattr(cli_main, "_launch_default_repl", lambda: events.append("repl"))
    monkeypatch.setattr(cli_main.sys, "stdin", types.SimpleNamespace(isatty=lambda: True))

    def run(*argv: str) -> tuple[list[str], int | None]:
        monkeypatch.setattr(cli_main.sys, "argv", ["nvh", *argv])
        events.clear()
        try:
            cli_main.main()
        except SystemExit as exc:
            return list(events), exc.code
        return list(events), None

    return run


class TestDispatcher:
    def test_known_command_goes_to_typer(self, dispatch):
        assert dispatch("status") == (["app"], None)
        assert dispatch("test-gen", "x.py") == (["app"], None)
        assert dispatch("groq", "hi") == (["app"], None)  # hidden alias still dispatches

    def test_typo_prints_did_you_mean_and_exits_2(self, dispatch, capsys):
        events, code = dispatch("statsu")
        assert events == [] and code == 2
        err = _plain(capsys.readouterr().err)
        assert "Did you mean" in err and "nvh status" in err
        assert "nvh ask" in err

    def test_typo_with_flag_or_subcommand(self, dispatch, capsys):
        assert dispatch("statsu", "--deep")[1] == 2
        assert dispatch("confg", "set", "defaults.mode", "convene")[1] == 2
        assert "nvh config" in _plain(capsys.readouterr().err)

    @pytest.mark.parametrize("argv, replacement", [
        (["docter", "--fix"], "status --deep"),
        (["helth"], "status --providers"),
        (["selfchek"], "status --report --live --imports"),
        (["quik", "hi"], "ask --fast --raw"),
        (["gorq", "hi"], "ask -p groq"),
        (["reserch", "x"], "ask --focus research"),
    ])
    def test_typo_of_a_deprecated_alias_suggests_its_replacement(self, dispatch, capsys, argv, replacement):
        assert cli_main._suggest_commands(argv) == [replacement]
        events, code = dispatch(*argv)
        assert events == [] and code == 2
        assert f"nvh {replacement}?" in " ".join(_plain(capsys.readouterr().err).split())

    def test_suggestions_are_case_insensitive_and_deduplicated(self):
        assert cli_main._suggest_commands(["STATSU"]) == ["status"]
        # `test` and `smoke` both forward to the same spelling.
        assert cli_main._suggest_commands(["tset"]) == ["status --smoke"]
        assert cli_main._suggest_commands(["smok"]) == ["status --smoke"]

    def test_prompt_starting_with_a_near_miss_word_is_still_a_prompt(self, dispatch):
        assert dispatch("explain", "quantum", "computing") == (["smart_default"], None)
        assert dispatch("tests", "are", "failing,", "why?") == (["smart_default"], None)

    def test_task_shaped_prompt_requires_explicit_do(self, dispatch, capsys):
        events, code = dispatch("install", "comfyui", "on", "this", "box")
        assert events == [] and code == 2
        err = _plain(capsys.readouterr().err)
        assert 'nvh do "install comfyui on this box"' in err
        assert 'nvh ask "install comfyui on this box"' in err

    def test_question_goes_to_smart_default(self, dispatch):
        assert dispatch("what", "is", "the", "CAP", "theorem?") == (["smart_default"], None)

    def test_no_args_opens_repl(self, dispatch):
        assert dispatch() == (["repl"], None)


# ---------------------------------------------------------------------------
# GPU rows the CLI renders through gpu.format_gpu_memory
# ---------------------------------------------------------------------------

def _gpu_row(name: str, vram_mb: int, *, unified: bool = False):
    from nvh.utils.gpu import GPUInfo

    return GPUInfo(
        name=name, vram_mb=vram_mb, vram_gb=round(vram_mb / 1024, 1), driver_version="580.65",
        cuda_version="13.0", utilization_pct=0, memory_used_mb=0, memory_free_mb=vram_mb, index=0,
        unified_memory=unified,
    )


class TestGpuMemoryRendering:
    """`nvh setup` (Step 3/3) and `nvh bench` (header panel) printed a GPU whose memory could not
    be read as '(0GB VRAM)' / '(0 GB VRAM)' — a real, empty card. Both sites now spell the row
    through gpu.format_gpu_memory; readable rows keep their exact old text."""

    @pytest.fixture(autouse=True)
    def _offline(self, monkeypatch, tmp_path):
        import httpx

        from nvh.cli import setup as setup_mod

        def no_network(*args, **kwargs):
            raise httpx.ConnectError("offline under test")

        monkeypatch.setenv("COLUMNS", "200")  # no Rich wrapping of the asserted lines
        monkeypatch.setattr(httpx, "get", no_network)
        monkeypatch.setattr(setup_mod, "_ollama_running", lambda: (False, []))
        monkeypatch.setattr(setup_mod, "_write_config", lambda configured, ollama_enabled: tmp_path / "config.yaml")

    @staticmethod
    def _setup(runner: CliRunner, monkeypatch, gpus) -> str:
        stub_keyring = types.SimpleNamespace(get_password=lambda *a, **k: None, set_password=lambda *a, **k: None)
        monkeypatch.setitem(sys.modules, "keyring", stub_keyring)
        monkeypatch.setattr("nvh.providers.registry.resolve_provider_key", lambda name: (None, None))
        monkeypatch.setattr(typer, "confirm", lambda *a, **k: False)              # no provider signups
        monkeypatch.setattr(typer, "prompt", lambda *a, **k: k.get("default", ""))  # email / model choice: defaults
        monkeypatch.setattr("nvh.utils.gpu.detect_gpus", lambda: gpus)
        result = runner.invoke(cli_main.app, ["setup", "--accept-terms"])
        assert result.exit_code == 0, result.output
        return _plain(result.output)

    def test_setup_names_an_unreadable_gpu(self, runner: CliRunner, monkeypatch):
        out = self._setup(runner, monkeypatch, [_gpu_row("NVIDIA GeForce RTX 4090", 0)])
        assert "Detected: NVIDIA GeForce RTX 4090 (memory unreadable)" in out
        assert "0GB VRAM" not in out and "0 GB VRAM" not in out

    def test_setup_readable_gpu_line_is_unchanged(self, runner: CliRunner, monkeypatch):
        out = self._setup(runner, monkeypatch, [_gpu_row("NVIDIA GeForce RTX 4090", 24576)])
        assert "Detected: NVIDIA GeForce RTX 4090 (24GB VRAM)" in out

    @staticmethod
    def _bench(runner: CliRunner, monkeypatch, gpus) -> str:
        import httpx

        from nvh.core import benchmark as bench_mod

        monkeypatch.setattr("nvh.utils.gpu.detect_gpus", lambda: gpus)
        tags = types.SimpleNamespace(raise_for_status=lambda: None, json=lambda: {"models": [{"name": "qwen3:8b"}]})
        monkeypatch.setattr(httpx, "get", lambda *a, **k: tags)

        async def fake_single(provider, model, prompt, max_tokens=512):
            return bench_mod.BenchmarkResult(
                model=model, gpu_name=gpus[0].name, vram_gb=gpus[0].vram_gb, prompt_tokens=10,
                output_tokens=64, time_to_first_token_ms=120, total_time_ms=1000,
                tokens_per_second=64.0, prompt_eval_rate=200.0,
            )

        monkeypatch.setattr(bench_mod, "run_single_benchmark", fake_single)
        result = runner.invoke(cli_main.app, ["bench", "--quick"])
        assert result.exit_code == 0, result.output
        return _plain(result.output)

    def test_bench_header_names_an_unreadable_gpu(self, runner: CliRunner, monkeypatch):
        out = self._bench(runner, monkeypatch, [_gpu_row("NVIDIA GeForce RTX 4090", 0)])
        assert "GPU:   NVIDIA GeForce RTX 4090 (memory unreadable)" in out
        assert "0 GB VRAM" not in out

    def test_bench_header_readable_gpu_is_unchanged(self, runner: CliRunner, monkeypatch):
        out = self._bench(runner, monkeypatch, [_gpu_row("NVIDIA GeForce RTX 4090", 24576)])
        assert "GPU:   NVIDIA GeForce RTX 4090 (24 GB VRAM)" in out
