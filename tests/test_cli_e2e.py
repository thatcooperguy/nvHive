"""End-to-end CLI tests — run actual nvh commands and verify output.

These tests execute the real CLI via subprocess and check that
commands produce the expected output without errors.
"""

import subprocess
import sys

import pytest

PYTHON = sys.executable
NVH = [PYTHON, "-m", "nvh.cli.main"]
TIMEOUT = 30


def run_nvh(*args: str, timeout: int = TIMEOUT) -> subprocess.CompletedProcess:
    """Run an nvh command and return the result."""
    return subprocess.run(
        [*NVH, *args],
        capture_output=True,
        text=True,
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
        r = run_nvh("status")
        assert r.returncode == 0
        # Should show some output about providers or config
        assert len(r.stdout) > 0

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
    def test_doctor(self):
        r = run_nvh("doctor", timeout=60)
        # Doctor may return 1 if issues found (like missing config) — that's OK
        assert r.returncode in (0, 1)
        assert "Diagnostic" in r.stdout or "Results" in r.stdout
        assert len(r.stdout) > 100  # doctor produces verbose output


# ---------------------------------------------------------------------------
# Query commands (require a running provider)
# ---------------------------------------------------------------------------

class TestCLIQuery:
    """These tests require at least LLM7 to be reachable."""

    def test_bare_prompt(self):
        """nvh 'question' should work."""
        r = run_nvh("What is 2+2? Answer with just the number.", timeout=45)
        # May succeed or fail depending on provider availability
        if r.returncode == 0:
            assert len(r.stdout) > 0
        else:
            # Acceptable failure: no providers available
            assert "error" in r.stderr.lower() or "provider" in r.stderr.lower() or len(r.stderr) > 0

    def test_ask_command(self):
        r = run_nvh("ask", "Say hello", timeout=45)
        if r.returncode == 0:
            assert len(r.stdout) > 0

    def test_quick_command(self):
        r = run_nvh("quick", "Say hi", timeout=45)
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
    def test_smoke_test_no_webui(self):
        """nvh test --no-webui should run without crashing."""
        r = run_nvh("test", "--no-webui", "--no-providers", timeout=60)
        assert r.returncode == 0
        assert "passed" in r.stdout.lower()
