"""CLI surface after issue #127: `ask` flags, `status` tiers, hidden aliases,
registry-derived dispatch, did-you-mean, and the explicit-`nvh do` gate."""

from __future__ import annotations

import io
import json
import types

import pytest
import typer
from typer.main import get_command
from typer.testing import CliRunner

import nvh.cli.main as cli_main
from nvh.integrations.diagnostics import checks as diag


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
    def test_query_mode_clones_are_hidden_aliases(self):
        root = get_command(cli_main.app)
        for name in ("code", "write", "research", "math", "quick", "safe", "pipe", "clip"):
            assert root.commands[name].hidden is True, name
            assert root.commands[name].help.startswith("(alias) "), name
        assert root.commands["ask"].hidden is False
        flags = {opt for p in root.commands["ask"].params for opt in p.opts}
        assert {"--focus", "--fast", "--local", "--clipboard", "--copy"} <= flags

    def test_diagnostic_verbs_are_hidden_aliases_of_status(self):
        root = get_command(cli_main.app)
        for name, target in (
            ("health", "--providers"), ("doctor", "--deep"), ("test", "--smoke"),
            ("smoke", "--smoke"), ("debug", "--report"), ("selfcheck", "--report"),
            ("why", "--routing"),
        ):
            assert root.commands[name].hidden is True, name
            assert target in root.commands[name].help, name
        assert root.commands["services"].commands["status"].hidden is True
        tiers = {opt for p in root.commands["status"].params for opt in p.opts}
        assert {"--providers", "--deep", "--smoke", "--report", "--routing"} <= tiers

    def test_provider_commands_are_hidden_aliases_of_ask(self):
        root = get_command(cli_main.app)
        for name in cli_main.KNOWN_ADVISORS:
            if name in ("mock", "nvidia"):
                continue
            assert root.commands[name].hidden is True, name
            assert f"nvh ask -p {name}" in root.commands[name].help
        assert root.commands["nvidia"].hidden is False  # the dashboard, not the advisor

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
        assert captured == {"prompt": "hello", "provider": "groq", "model": None, "system": None, "output": "raw", "quiet": True}


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
        out = result.output
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
        assert "Diagnostic Results" in result.output
        assert "1 failures" in result.output
        assert "Suggested fixes" in result.output

    def test_doctor_alias_forwards_flags(self, runner: CliRunner, monkeypatch):
        seen: dict = {}
        monkeypatch.setattr(cli_main, "_run_status", lambda tier, **kw: seen.update(tier=tier, **kw))
        result = runner.invoke(cli_main.app, ["doctor", "--json", "--fix", "--home-dir", "/x"])
        assert result.exit_code == 0, result.output
        assert seen["tier"] == "deep" and seen["json_output"] and seen["fix"] and seen["home_dir"] == "/x"

    def test_providers_tier_table(self, runner: CliRunner, monkeypatch):
        _fake_checks(monkeypatch, {diag.PROVIDERS: [
            _row("provider_health", "Advisor groq", "pass", "80ms", provider="groq", healthy=True, score=0.95, chain_position=1),
            _row("provider_health", "Advisor llm7", "warn", "429", provider="llm7", healthy=False, score=0.3, chain_position=2),
            _row("fallback_chain", "Fallback chain", "info", "groq → llm7", chain=["groq", "llm7"]),
        ]})
        result = runner.invoke(cli_main.app, ["health"])
        assert result.exit_code == 0, result.output
        assert "Healthy" in result.output and "Unhealthy" in result.output
        assert "1/2" in result.output and "Vulnerable" in result.output
        assert "groq → llm7" in result.output

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
        monkeypatch.setattr(
            passport_mod, "support_snapshot",
            lambda home_dir=None, include_logs=True: {"path": "snap.json", "passport": {"workspace_id": "w", "rootless": True}, "excludes": []},
        )
        out = tmp_path / "bundle.json"
        result = runner.invoke(cli_main.app, ["status", "--report", "-o", str(out)])
        assert result.exit_code == 0, result.output
        bundle = json.loads(out.read_text(encoding="utf-8"))
        assert bundle["schema_version"] == 2
        assert bundle["components"]["checks"]["warned"] == 1
        assert bundle["components"]["wizard_live_turn"] == {"skipped": True}
        assert bundle["components"]["support_snapshot"]["workspace_id"] == "w"
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
        err = capsys.readouterr().err
        assert "Did you mean" in err and "nvh status" in err
        assert "nvh ask" in err

    def test_typo_with_flag_or_subcommand(self, dispatch, capsys):
        assert dispatch("statsu", "--deep")[1] == 2
        assert dispatch("confg", "set", "defaults.mode", "convene")[1] == 2
        assert "nvh config" in capsys.readouterr().err

    def test_prompt_starting_with_a_near_miss_word_is_still_a_prompt(self, dispatch):
        assert dispatch("explain", "quantum", "computing") == (["smart_default"], None)
        assert dispatch("tests", "are", "failing,", "why?") == (["smart_default"], None)

    def test_task_shaped_prompt_requires_explicit_do(self, dispatch, capsys):
        events, code = dispatch("install", "comfyui", "on", "this", "box")
        assert events == [] and code == 2
        err = capsys.readouterr().err
        assert 'nvh do "install comfyui on this box"' in err
        assert 'nvh ask "install comfyui on this box"' in err

    def test_question_goes_to_smart_default(self, dispatch):
        assert dispatch("what", "is", "the", "CAP", "theorem?") == (["smart_default"], None)

    def test_no_args_opens_repl(self, dispatch):
        assert dispatch() == (["repl"], None)
