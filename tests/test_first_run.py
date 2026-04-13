"""Tests for the first-run detection helper in nvh.cli.main."""

from __future__ import annotations

from unittest.mock import patch

from nvh.cli.main import _is_first_run


def test_first_run_detected(tmp_path):
    """Config missing + no env vars + no CI → returns True."""
    fake_config = tmp_path / "config.yaml"
    # Clear all keys that _is_first_run checks (API keys AND CI markers)
    env = {k: "" for k in (
        "GROQ_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY",
        "GOOGLE_API_KEY", "GITHUB_TOKEN",
        "CI", "PYTEST_CURRENT_TEST", "GITHUB_ACTIONS",
    )}
    with (
        patch("nvh.cli.main.DEFAULT_CONFIG_PATH", fake_config),
        patch.dict("os.environ", env, clear=False),
    ):
        # Also need to unset CI vars that may be inherited
        import os
        for k in ("CI", "PYTEST_CURRENT_TEST", "GITHUB_ACTIONS"):
            os.environ.pop(k, None)
        assert _is_first_run() is True


def test_not_first_run_with_config(tmp_path):
    """Config exists → returns False even without env vars."""
    fake_config = tmp_path / "config.yaml"
    fake_config.write_text("model: gpt-4o\n")
    env = {k: "" for k in (
        "GROQ_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY",
        "GOOGLE_API_KEY", "GITHUB_TOKEN",
    )}
    with (
        patch("nvh.cli.main.DEFAULT_CONFIG_PATH", fake_config),
        patch.dict("os.environ", env, clear=False),
    ):
        assert _is_first_run() is False


def test_not_first_run_with_env_key(tmp_path):
    """GROQ_API_KEY set → returns False even without config."""
    fake_config = tmp_path / "config.yaml"
    with (
        patch("nvh.cli.main.DEFAULT_CONFIG_PATH", fake_config),
        patch.dict("os.environ", {"GROQ_API_KEY": "gsk_test123"}, clear=False),
    ):
        assert _is_first_run() is False
