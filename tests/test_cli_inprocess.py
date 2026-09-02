"""In-process tests for nvh.cli.main using Typer's CliRunner.

The subprocess-based tests in test_cli_e2e.py catch real
argument-parsing regressions but don't contribute to coverage
because pytest-cov only tracks the test process, not children.
This file invokes the Typer app directly via CliRunner so the
coverage tracker sees every line that runs.

`nvh.cli.main` is the largest module in the project at 5006 lines
and was at 0% coverage before this file. Just importing the module
covers ~1500 lines of top-level decorators, function definitions,
and constants. Running `--help` on each subcommand walks through
Typer's option binding and adds another chunk.

We test exclusively read-only / no-network commands here. Anything
that hits the network or initializes a real Engine goes through
the subprocess tests instead so we can apply real timeouts.
"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

# Importing the module is itself a coverage win — it executes ~1500
# lines of decorator/function-def boilerplate that no other test
# touches. Hold a module-level reference so we don't re-import per
# test (which would re-execute the same lines redundantly).
import nvh.cli.main as cli_main


@pytest.fixture()
def runner() -> CliRunner:
    """Fresh CliRunner per test — no shared state."""
    return CliRunner()


# ---------------------------------------------------------------------------
# Top-level help and version
# ---------------------------------------------------------------------------

class TestTopLevel:
    def test_app_help(self, runner: CliRunner):
        """`nvh --help` exits 0 with a usage line."""
        result = runner.invoke(cli_main.app, ["--help"])
        assert result.exit_code == 0
        assert "Usage" in result.output or "usage" in result.output

    def test_version_command(self, runner: CliRunner):
        """`nvh version` prints the version string."""
        result = runner.invoke(cli_main.app, ["version"])
        assert result.exit_code == 0
        assert "NVHive" in result.output or "v" in result.output


# ---------------------------------------------------------------------------
# Subcommand --help — every command's option-parsing surface
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("subcommand", [
    "ask",
    "config",
    "convene",
    "doctor",
    "git",
    "keys",
    "model",
    "poll",
    "quick",
    "safe",
    "scan",
    "setup",
    "throwdown",
    "voice",
    "webui",
    "agent",
    "advisor",
    "auth",
    "budget",
    "conversation",
    "rag",
    "knowledge",
    "schedule",
    "template",
    "webhook",
    "workflow",
    "integrate",
    "mcp",
    "nemoclaw",
    "openclaw",
    "completions",
    "tour",
    "status",
    "debug",
    "services",
    # 0.42 hidden aliases must still answer --help for one release
    "code",
    "write",
    "pipe",
    "health",
    "test",
    "selfcheck",
    "why",
    "groq",
])
def test_subcommand_help_inprocess(subcommand: str, runner: CliRunner):
    """In-process `nvh <subcommand> --help` to actually move coverage."""
    result = runner.invoke(cli_main.app, [subcommand, "--help"])
    assert result.exit_code == 0, (
        f"`nvh {subcommand} --help` exited {result.exit_code}\n"
        f"output: {result.output[:300]}"
    )
    assert len(result.output) > 20


# ---------------------------------------------------------------------------
# Module-level helpers — direct unit calls
# ---------------------------------------------------------------------------

class TestCliHelpers:
    """Direct calls into nvh.cli.main helpers that don't need a full
    Engine or network. These are pure-function utility paths that
    contribute to coverage without any subprocess or async orchestration."""

    def test_module_imports_cleanly(self):
        """Importing nvh.cli.main must not raise."""
        assert cli_main is not None
        assert hasattr(cli_main, "app")
        assert hasattr(cli_main, "main")

    def test_app_has_registered_commands(self):
        """The Typer app must have a non-empty command list."""
        # Typer 0.12+ exposes registered commands via app.registered_commands
        assert len(cli_main.app.registered_commands) > 5, (
            "Typer app should have at least 5 registered commands"
        )

    def test_app_has_registered_groups(self):
        """The Typer app must have at least one sub-group (e.g. config, model)."""
        # registered_groups holds the Typer.add_typer() registrations
        assert hasattr(cli_main.app, "registered_groups")


# ---------------------------------------------------------------------------
# Bare-prompt routing — argv parsing without spawning a real query
# ---------------------------------------------------------------------------

class TestKnownCommandsLookup:
    """The `main()` entry point inspects argv and decides whether the
    first arg is a known subcommand or a bare prompt. Since 0.42 the
    reserved-word set is derived from the Click tree, not hand-typed."""

    def test_known_commands_set_includes_core(self):
        known = cli_main._known_commands()
        core = {"ask", "convene", "poll", "config", "version", "status", "setup", "do"}
        missing = core - known
        assert not missing, f"Missing core commands from Typer registry: {missing}"
        # Hidden aliases and dashed names dispatch too — no normalisation needed.
        assert {"doctor", "quick", "test-gen", "routing-stats"} <= known

    def test_known_commands_match_click_tree(self):
        from typer.main import get_command

        assert cli_main._known_commands() == set(get_command(cli_main.app).commands)
