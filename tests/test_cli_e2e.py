"""End-to-end CLI tests — run actual nvh commands and verify output.

These tests execute the real CLI via subprocess and check that
commands produce the expected output without errors.
"""

import subprocess
import sys

import pytest

PYTHON = sys.executable
NVH = [PYTHON, "-m", "nvh.cli.main"]
TIMEOUT = 60


def run_nvh(*args: str, timeout: int = TIMEOUT) -> subprocess.CompletedProcess:
    """Run an nvh command and return the result.

    Forces UTF-8 decoding so rich/typer's unicode box-drawing output
    doesn't crash the subprocess reader thread on Windows (where the
    default is cp1252 and any non-ASCII byte raises UnicodeDecodeError,
    leaving stdout=None).

    Forces ``stdin=DEVNULL`` so ``nvh``'s pipe-detection logic never
    sees an inherited-but-not-closed pytest stdin and hangs in
    ``sys.stdin.read()``. Windows/macOS CI runners happened to close
    the child stdin automatically, but ubuntu-latest inherited a
    non-TTY pipe that stayed open forever — the entire test job wedged
    for 26+ minutes on that path.
    """
    return subprocess.run(
        [*NVH, *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdin=subprocess.DEVNULL,
        timeout=timeout,
    )


# ---------------------------------------------------------------------------
# Basic commands
# ---------------------------------------------------------------------------

class TestCLIBasic:
    def test_version(self):
        r = run_nvh("version")
        assert r.returncode == 0
        assert "NVHive v" in r.stdout

    def test_help(self):
        r = run_nvh("--help")
        assert r.returncode == 0
        assert "nvh" in r.stdout.lower() or "nvhive" in r.stdout.lower()

    def test_status(self):
        try:
            r = run_nvh("status", timeout=60)
            assert r.returncode == 0
            assert len(r.stdout) > 0
        except subprocess.TimeoutExpired:
            pass  # OK on CI — status connects to providers which may hang

    def test_keys(self):
        r = run_nvh("keys")
        assert r.returncode == 0
        assert "free" in r.stdout.lower() or "signup" in r.stdout.lower() or "key" in r.stdout.lower()


# ---------------------------------------------------------------------------
# Config commands
# ---------------------------------------------------------------------------

class TestCLIConfig:
    def test_config_help(self):
        r = run_nvh("config", "--help")
        assert r.returncode == 0
        assert "config" in r.stdout.lower()

    def test_config_get_provider(self):
        r = run_nvh("config", "get", "defaults.orchestration_mode")
        # May succeed or show a value
        assert r.returncode == 0 or "not found" in r.stdout.lower() or len(r.stderr) > 0


# ---------------------------------------------------------------------------
# Integration guide commands
# ---------------------------------------------------------------------------

class TestCLIGuides:
    def test_nemoclaw_guide(self):
        r = run_nvh("nemoclaw")
        assert r.returncode == 0
        assert "nemoclaw" in r.stdout.lower() or "nvhive" in r.stdout.lower()

    def test_nemoclaw_mcp(self):
        r = run_nvh("nemoclaw", "--mcp")
        assert r.returncode == 0
        assert "mcp" in r.stdout.lower() or "tool" in r.stdout.lower()

    def test_openclaw_guide(self):
        r = run_nvh("openclaw")
        assert r.returncode == 0
        assert "openclaw" in r.stdout.lower() or "connect" in r.stdout.lower()

    def test_integrate_scan(self):
        r = run_nvh("integrate", "--scan")
        assert r.returncode == 0
        # Should show detected platforms or "no platforms"
        assert len(r.stdout) > 0


# ---------------------------------------------------------------------------
# Doctor
# ---------------------------------------------------------------------------

class TestCLIDoctor:
    def test_status_deep(self):
        r = run_nvh("status", "--deep", timeout=60)
        # Exit 1 when a check fails (like missing config) — that's OK
        assert r.returncode in (0, 1)
        assert "Diagnostic" in r.stdout or "Results" in r.stdout
        assert len(r.stdout) > 100  # the deep tier produces verbose output

    def test_doctor_alias_still_answers(self):
        r = run_nvh("doctor", "--json", timeout=60)
        assert r.returncode in (0, 1)
        assert r.stdout.lstrip().startswith("{")


# ---------------------------------------------------------------------------
# Dispatcher — a typo must not become a metered LLM call
# ---------------------------------------------------------------------------

class TestCLIDispatch:
    def test_typo_prints_did_you_mean_and_exits_2(self):
        r = run_nvh("statsu", timeout=30)
        assert r.returncode == 2
        assert "Did you mean" in r.stderr
        assert "nvh status" in r.stderr
        assert r.stdout == ""


# ---------------------------------------------------------------------------
# Query commands (require a running provider)
# ---------------------------------------------------------------------------

class TestCLIQuery:
    """These tests require at least LLM7 to be reachable."""

    # 2026-06-10 audit: these three spawn `nvh` against live LLM providers
    # over the network with a hard 45s subprocess timeout. On a slow or
    # offline CI runner the provider call hangs past 45s, TimeoutExpired
    # propagates uncaught, and the test ERRORs — the documented flake that
    # randomly reddened one matrix leg on half the dependabot PRs. Skip on
    # timeout (same pattern as test_status above): zero behavior change when
    # providers are healthy, deterministic skip when the network isn't.

    def test_bare_prompt(self):
        """nvh 'question' should work."""
        try:
            r = run_nvh("What is 2+2? Answer with just the number.", timeout=45)
        except subprocess.TimeoutExpired:
            pytest.skip("provider unreachable/slow — network-dependent test")
        # May succeed or fail depending on provider availability
        if r.returncode == 0:
            assert len(r.stdout) > 0
        else:
            # Acceptable failure: no providers available
            assert "error" in r.stderr.lower() or "provider" in r.stderr.lower() or len(r.stderr) > 0

    def test_ask_command(self):
        try:
            r = run_nvh("ask", "Say hello", timeout=45)
        except subprocess.TimeoutExpired:
            pytest.skip("provider unreachable/slow — network-dependent test")
        if r.returncode == 0:
            assert len(r.stdout) > 0

    def test_quick_command(self):
        try:
            r = run_nvh("quick", "Say hi", timeout=45)
        except subprocess.TimeoutExpired:
            pytest.skip("provider unreachable/slow — network-dependent test")
        if r.returncode == 0:
            assert len(r.stdout) > 0

    def test_safe_no_ollama(self):
        """Safe mode without Ollama should fail gracefully."""
        r = run_nvh("safe", "Hello", timeout=15)
        # Either works (if Ollama is running) or fails gracefully
        assert r.returncode == 0 or len(r.stderr) > 0 or len(r.stdout) > 0


# ---------------------------------------------------------------------------
# Completions
# ---------------------------------------------------------------------------

class TestCLICompletions:
    def test_bash_completions(self):
        r = run_nvh("completions", "bash")
        assert r.returncode == 0

    def test_zsh_completions(self):
        r = run_nvh("completions", "zsh")
        assert r.returncode == 0


# ---------------------------------------------------------------------------
# Test command itself
# ---------------------------------------------------------------------------

class TestCLITest:
    def test_smoke_tier_runs_offline(self):
        """nvh status --smoke should run without crashing (exit 1 = a check failed)."""
        r = run_nvh("status", "--smoke", timeout=60)
        assert r.returncode in (0, 1)
        assert "passed" in r.stdout.lower()

    def test_test_alias_still_answers(self):
        r = run_nvh("test", "--json", timeout=60)
        assert r.returncode in (0, 1)
        assert '"tests"' in r.stdout


# ---------------------------------------------------------------------------
# Subcommand `--help` smoke tests
#
# These exercise nvh/cli/main.py without making any external calls,
# providing broad coverage of the CLI argument-parsing surface and
# every command's help text generation. Each subcommand's --help
# walks through Typer's option processing and the function signature
# binding, which collectively touches a couple hundred lines per
# command. nvh/cli/main.py was at 0% coverage before this — adding
# these takes it to ~20% in one shot.
#
# We deliberately use --help (not the actual subcommand) so the tests
# don't depend on network, advisor configuration, GPU, ollama, or
# anything else that varies across CI runners.
# ---------------------------------------------------------------------------


class TestCLISubcommandHelp:
    """Each subcommand must respond to --help with a non-empty help text."""

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
        "knowledge",  # 0.42 hidden alias of rag
        "schedule",
        "template",
        "webhook",
        "workflow",
        "integrate",
        "mcp",
        "nemoclaw",
        "openclaw",
        "status",
        "code",   # 0.42 hidden alias
        "health",  # 0.42 hidden alias
    ])
    def test_subcommand_help(self, subcommand):
        """`nvh <subcommand> --help` must exit 0 with non-empty stdout."""
        r = run_nvh(subcommand, "--help", timeout=15)
        # Some subcommands are typer groups (config, model, agent...)
        # — typer prints to either stdout or stderr depending on
        # whether it's a group or a leaf command. Accept either.
        output = (r.stdout or "") + (r.stderr or "")
        assert r.returncode == 0, (
            f"`nvh {subcommand} --help` exited {r.returncode}.\n"
            f"stdout: {(r.stdout or '')[:200]}\n"
            f"stderr: {(r.stderr or '')[:200]}"
        )
        assert len(output) > 20, (
            f"`nvh {subcommand} --help` produced empty output: {output!r}"
        )
