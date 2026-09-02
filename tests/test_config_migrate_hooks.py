"""`nvh config migrate` drops the dead top-level ``hooks:`` key.

``nvh/core/hooks.py`` and the ``hooks`` config field were deleted in 0.42; a
leftover key in an existing config.yaml is ignored at load time, and the
migration removes it so the file stops advertising a feature that is gone.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

import nvh.cli.main as cli_main
from nvh.cli.setup import migrate_config_data

CONFIG_WITH_HOOKS = """\
version: "1"
advisors:
  openai:
    api_key: ${OPENAI_API_KEY}
    default_model: gpt-5.6-terra
    enabled: true
hooks:
  pre_query:
    - echo hi
webhooks:
  - url: https://example.invalid/hook
    events: [query_complete]
"""


@pytest.fixture()
def runner() -> CliRunner:
    return CliRunner()


def test_hooks_key_removed_and_reported():
    raw = yaml.safe_load(CONFIG_WITH_HOOKS)
    out, changes = migrate_config_data(raw)
    assert "hooks" not in out
    assert changes == ["hooks: removed (nvh.core.hooks was deleted in 0.42)"]
    # Unrelated top-level keys, including the similarly named webhooks, survive.
    assert out["webhooks"] == raw["webhooks"]
    assert out["advisors"] == raw["advisors"]
    assert "hooks" in raw  # input not mutated


def test_config_without_hooks_is_untouched():
    raw = {"advisors": {"openai": {"default_model": "gpt-5.6-terra"}}}
    out, changes = migrate_config_data(raw)
    assert changes == []
    assert out == raw


def test_cli_rewrites_file_without_hooks(runner: CliRunner, tmp_path: Path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(CONFIG_WITH_HOOKS)

    result = runner.invoke(cli_main.app, ["config", "migrate", "--file", str(cfg)])
    assert result.exit_code == 0, result.output
    assert "hooks: removed" in result.output

    migrated = yaml.safe_load(cfg.read_text())
    assert "hooks" not in migrated
    assert migrated["webhooks"][0]["url"] == "https://example.invalid/hook"
    assert cfg.with_suffix(".yaml.bak").read_text() == CONFIG_WITH_HOOKS

    result = runner.invoke(cli_main.app, ["config", "migrate", "--file", str(cfg)])
    assert result.exit_code == 0, result.output
    assert "nothing to migrate" in result.output
