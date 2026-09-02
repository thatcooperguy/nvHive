"""`nvh config migrate`, the doctor's retired-model check, and `nvh advisor remove`.

All three lean on the retired-model table in nvh.cli.setup, added for the
0.41.1 hotfix after 17 of 21 cloud providers retired the IDs 0.41.0 shipped.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml
from typer.testing import CliRunner

import nvh.cli.main as cli_main
from nvh.cli import setup as cli_setup

_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _plain(text: str) -> str:
    # Rich styles CLI output when colour is forced (CI), splitting phrases
    # with escape codes; substring checks must run on the de-styled text.
    return _ANSI.sub("", text)
from nvh.cli.setup import (
    RETIRED_MODEL_RENAMES,
    RETIRED_PROVIDERS,
    disable_provider_in_config,
    migrate_config_data,
    provider_config_files,
    provider_env_vars,
    remove_key,
    rename_retired_model,
    stale_default_models,
)
from nvh.config.settings import ProviderConfig

SHIPPED_CONFIG = """\
version: "1"

defaults:
  provider: github
  model: gpt-4o-mini

advisors:
  openai:
    api_key: ${OPENAI_API_KEY}
    default_model: gpt-4o
    fallback_model: gpt-4o-mini
    enabled: true
  anthropic:
    api_key: ${ANTHROPIC_API_KEY}
    default_model: claude-sonnet-4-6
    fallback_model: claude-haiku-4-5-20251001
    enabled: false
  groq:
    api_key: ${GROQ_API_KEY}
    default_model: groq/llama-3.3-70b-versatile
    fallback_model: groq/llama-3.1-8b-instant
    enabled: true
  github:
    api_key: ${GITHUB_TOKEN}
    default_model: gpt-4o-mini
    fallback_model: meta-llama-3.1-8b-instruct
    enabled: true
  llm7:
    api_key: ${LLM7_API_KEY:-anonymous}
    default_model: deepseek-r1-0528
    fallback_model: gpt-4o-mini
    enabled: true
  nvidia:
    api_key: ${NVIDIA_API_KEY:-${NIM_API_KEY}}
    default_model: meta/llama-3.1-70b-instruct
    fallback_model: meta/llama-3.1-8b-instruct
    enabled: true
  ollama:
    base_url: http://localhost:11434
    type: ollama
    default_model: ollama/gemma3:4b
    enabled: true

council:
  default_weights:
    openai: 0.5
    github: 0.5
  fallback_order: [github, openai, groq]

profiles:
  cheap:
    providers:
      openai:
        default_model: gpt-4o-mini
"""

# What the web Wizard's save-key path writes to the storage layout's config.yaml.
WIZARD_CONFIG = """\
version: "1"
defaults: {}
advisors:
  groq:
    api_key: ${GROQ_API_KEY}
    default_model: groq/openai/gpt-oss-120b
    enabled: true
"""


@pytest.fixture()
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture()
def layout_config_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the storage layout at a throwaway NVH_HOME so key scrubbing can
    never reach the developer's real ~/.nvh/config."""
    for var in ("HIVE_CONFIG_HOME", "NVHIVE_HOME"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("NVH_HOME", str(tmp_path / "nvh-home"))
    cfg_dir = tmp_path / "nvh-home" / "config"
    cfg_dir.mkdir(parents=True)
    return cfg_dir


# ---------------------------------------------------------------------------
# Rename table
# ---------------------------------------------------------------------------

class TestRenameTable:
    def test_targets_are_never_retired_themselves(self):
        for provider, table in RETIRED_MODEL_RENAMES.items():
            for old, new in table.items():
                assert new not in table, f"{provider}: {old} -> {new} chains to another rename"
                assert new != old

    def test_ids_rename_only_inside_their_provider(self):
        assert rename_retired_model("openai", "gpt-4o") == "gpt-5.6-terra"
        assert rename_retired_model("myproxy", "gpt-4o") is None
        assert rename_retired_model("groq", "groq/llama-3.3-70b-versatile") == (
            "groq/openai/gpt-oss-120b"
        )
        assert rename_retired_model("nvidia", "meta/llama-3.1-70b-instruct") == (
            "nvidia_nim/meta/llama-3.3-70b-instruct"
        )
        assert rename_retired_model("together", "meta/llama-3.1-70b-instruct") is None

    def test_llm7_maps_to_its_own_lineup(self):
        assert rename_retired_model("llm7", "gpt-4o-mini") == "gpt-oss"
        assert rename_retired_model("llm7", "deepseek-r1-0528") == "gpt-oss"
        assert rename_retired_model("llm7", "llama-3.3-70b") == "minimax-m2.7"

    def test_deepseek_reasoner_keeps_the_reasoning_tier(self):
        # The same model `nvh math` pins for deepseek — not the flash tier.
        assert rename_retired_model("deepseek", "deepseek/deepseek-reasoner") == (
            "deepseek/deepseek-v4-pro"
        )
        assert rename_retired_model("deepseek", "deepseek/deepseek-chat") == (
            "deepseek/deepseek-v4-flash"
        )

    def test_current_ids_untouched(self):
        assert rename_retired_model("anthropic", "claude-sonnet-4-6") is None
        assert rename_retired_model("mistral", "mistral/mistral-large-latest") is None
        assert rename_retired_model("openai", "gpt-5.6-terra") is None


# ---------------------------------------------------------------------------
# migrate_config_data
# ---------------------------------------------------------------------------

class TestMigrateConfigData:
    def test_rewrites_shipped_config(self):
        raw = yaml.safe_load(SHIPPED_CONFIG)
        out, changes = migrate_config_data(raw)

        advisors = out["advisors"]
        assert advisors["openai"]["default_model"] == "gpt-5.6-terra"
        assert advisors["openai"]["fallback_model"] == "gpt-5.6-luna"
        assert advisors["groq"]["default_model"] == "groq/openai/gpt-oss-120b"
        assert advisors["groq"]["fallback_model"] == "groq/openai/gpt-oss-20b"
        assert advisors["llm7"]["default_model"] == "gpt-oss"
        assert advisors["llm7"]["fallback_model"] == "gpt-oss"
        assert advisors["nvidia"]["default_model"] == "nvidia_nim/meta/llama-3.3-70b-instruct"
        assert advisors["nvidia"]["fallback_model"] == "nvidia_nim/meta/llama-3.1-8b-instruct"
        # Not retired: untouched, including disabled blocks.
        assert advisors["anthropic"]["default_model"] == "claude-sonnet-4-6"
        assert advisors["ollama"]["default_model"] == "ollama/gemma3:4b"
        # ${VAR} references survive verbatim.
        assert advisors["openai"]["api_key"] == "${OPENAI_API_KEY}"
        assert advisors["nvidia"]["api_key"] == "${NVIDIA_API_KEY:-${NIM_API_KEY}}"

        assert "github" not in advisors
        assert out["defaults"]["provider"] == ""
        assert out["defaults"]["model"] == "gpt-4o-mini"  # bare id, no provider scope
        assert out["council"]["default_weights"] == {"openai": 0.5}
        assert out["council"]["fallback_order"] == ["openai", "groq"]
        assert out["profiles"]["cheap"]["providers"]["openai"]["default_model"] == "gpt-5.6-luna"

        joined = "\n".join(changes)
        assert f"advisors.github: github provider retired {RETIRED_PROVIDERS['github']}" in joined
        assert "advisors.openai.default_model: gpt-4o → gpt-5.6-terra" in changes
        assert (
            "profiles.cheap.providers.openai.default_model: gpt-4o-mini → gpt-5.6-luna" in changes
        )

    def test_defaults_model_is_looked_up_under_defaults_provider(self):
        raw = {"defaults": {"provider": "openai", "model": "gpt-4o"}}
        out, changes = migrate_config_data(raw)
        assert out["defaults"]["model"] == "gpt-5.6-terra"
        assert changes == ["defaults.model: gpt-4o → gpt-5.6-terra"]

        raw = {"defaults": {"provider": "llm7", "model": "gpt-4o"}}
        out, _ = migrate_config_data(raw)
        assert out["defaults"]["model"] == "gpt-oss"

    def test_input_not_mutated(self):
        raw = yaml.safe_load(SHIPPED_CONFIG)
        before = yaml.safe_dump(raw)
        migrate_config_data(raw)
        assert yaml.safe_dump(raw) == before

    def test_up_to_date_config_reports_no_changes(self):
        raw = {
            "advisors": {
                "openai": {"default_model": "gpt-5.6-terra", "enabled": True},
                "ollama": {"type": "ollama", "default_model": "ollama/gemma3:4b"},
            },
            "council": {"default_weights": {"openai": 1.0}},
        }
        out, changes = migrate_config_data(raw)
        assert changes == []
        assert out == raw

    def test_providers_key_spelling_also_migrated(self):
        raw = {"providers": {"cohere": {"default_model": "command-r-plus"}}}
        out, changes = migrate_config_data(raw)
        assert out["providers"]["cohere"]["default_model"] == "command-a-03-2025"
        assert changes == ["providers.cohere.default_model: command-r-plus → command-a-03-2025"]


# ---------------------------------------------------------------------------
# nvh config migrate
# ---------------------------------------------------------------------------

class TestConfigMigrateCommand:
    def test_dry_run_prints_changes_without_writing(self, runner: CliRunner, tmp_path: Path):
        cfg = tmp_path / "config.yaml"
        cfg.write_text(SHIPPED_CONFIG)

        result = runner.invoke(cli_main.app, ["config", "migrate", "--dry-run", "--file", str(cfg)])
        assert result.exit_code == 0, result.output
        output = _plain(result.output)
        assert "Dry run" in output
        assert "gpt-4o → gpt-5.6-terra" in output
        assert "github provider retired 2026-07-30" in output
        assert cfg.read_text() == SHIPPED_CONFIG
        assert not cfg.with_suffix(".yaml.bak").exists()

    def test_migrates_user_config_with_backup(self, runner: CliRunner, tmp_path: Path):
        cfg = tmp_path / "config.yaml"
        cfg.write_text(SHIPPED_CONFIG)

        with patch("nvh.config.settings.DEFAULT_CONFIG_PATH", cfg):
            result = runner.invoke(cli_main.app, ["config", "migrate"])
        assert result.exit_code == 0, result.output
        assert "Applied" in result.output

        migrated = yaml.safe_load(cfg.read_text())
        assert migrated["advisors"]["groq"]["default_model"] == "groq/openai/gpt-oss-120b"
        assert "github" not in migrated["advisors"]
        assert migrated["advisors"]["openai"]["api_key"] == "${OPENAI_API_KEY}"
        assert cfg.with_suffix(".yaml.bak").read_text() == SHIPPED_CONFIG

        # Idempotent: a second run has nothing left to do.
        with patch("nvh.config.settings.DEFAULT_CONFIG_PATH", cfg):
            result = runner.invoke(cli_main.app, ["config", "migrate"])
        assert result.exit_code == 0, result.output
        assert "nothing to migrate" in result.output

    def test_missing_config_fails(self, runner: CliRunner, tmp_path: Path):
        result = runner.invoke(
            cli_main.app, ["config", "migrate", "--file", str(tmp_path / "absent.yaml")],
        )
        assert result.exit_code == 1
        assert "not found" in result.output

    def test_help_lists_migrate(self, runner: CliRunner):
        result = runner.invoke(cli_main.app, ["config", "--help"])
        assert result.exit_code == 0
        assert "migrate" in result.output


# ---------------------------------------------------------------------------
# Doctor: configured model superseded by the renames table
# ---------------------------------------------------------------------------

class TestStaleDefaultModels:
    def test_flags_retired_ids_on_enabled_providers_only(self):
        providers = {
            "openai": ProviderConfig(default_model="gpt-4o", fallback_model="gpt-5.6-luna"),
            "groq": ProviderConfig(default_model="groq/openai/gpt-oss-120b"),
            "cohere": ProviderConfig(default_model="command-r-plus", enabled=False),
        }
        assert stale_default_models(providers) == [("openai", "default_model", "gpt-4o")]

    def test_full_0_41_0_config_is_flagged(self):
        """Most retired IDs are still capabilities.yaml keys, so catalog
        membership could not catch them — the renames table must."""
        advisors = yaml.safe_load(SHIPPED_CONFIG)["advisors"]
        assert stale_default_models(advisors) == [
            ("openai", "default_model", "gpt-4o"),
            ("openai", "fallback_model", "gpt-4o-mini"),
            ("groq", "default_model", "groq/llama-3.3-70b-versatile"),
            ("groq", "fallback_model", "groq/llama-3.1-8b-instant"),
            ("github", "provider", "github"),
            ("llm7", "default_model", "deepseek-r1-0528"),
            ("llm7", "fallback_model", "gpt-4o-mini"),
            ("nvidia", "default_model", "meta/llama-3.1-70b-instruct"),
            ("nvidia", "fallback_model", "meta/llama-3.1-8b-instruct"),
        ]

    def test_migrated_config_is_clean(self):
        migrated, _ = migrate_config_data(yaml.safe_load(SHIPPED_CONFIG))
        assert stale_default_models(migrated["advisors"]) == []

    def test_bare_ids_only_count_inside_their_provider(self):
        providers = {
            "myproxy": {"type": "openai_compatible", "default_model": "gpt-4o", "enabled": True},
            "ollama": ProviderConfig(default_model="ollama/whatever:latest", type="ollama"),
            "triton": ProviderConfig(default_model=""),
        }
        assert stale_default_models(providers) == []

    def test_disabled_retired_provider_is_not_flagged(self):
        assert stale_default_models({"github": ProviderConfig(enabled=False)}) == []

    def test_doctor_help_still_works(self, runner: CliRunner):
        result = runner.invoke(cli_main.app, ["doctor", "--help"])
        assert result.exit_code == 0


# ---------------------------------------------------------------------------
# nvh advisor remove
# ---------------------------------------------------------------------------

class TestAdvisorRemove:
    ENV = "GROQ_API_KEY=gsk_live\nOPENAI_API_KEY=sk_live\n# comment\n"

    def test_provider_env_vars_include_aliases(self):
        assert provider_env_vars("groq") == ["GROQ_API_KEY"]
        assert provider_env_vars("huggingface") == ["HUGGINGFACE_API_KEY", "HF_TOKEN"]
        assert provider_env_vars("grok") == ["GROK_API_KEY", "XAI_API_KEY"]

    def test_remove_key_clears_keyring_env_file_and_environ(
        self, tmp_path: Path, layout_config_dir: Path, monkeypatch,
    ):
        (tmp_path / ".env").write_text(self.ENV)
        monkeypatch.setenv("GROQ_API_KEY", "gsk_live")
        mock_keyring = MagicMock()
        with (
            patch.dict("sys.modules", {"keyring": mock_keyring}),
            patch("nvh.cli.setup.DEFAULT_CONFIG_DIR", tmp_path),
        ):
            result = remove_key("groq")

        mock_keyring.delete_password.assert_called_once_with("nvhive", "groq_api_key")
        assert result["keyring"] is True
        assert result["env_file"] == ["GROQ_API_KEY"]
        assert result["env_paths"] == [tmp_path / ".env"]
        assert (tmp_path / ".env").read_text() == "OPENAI_API_KEY=sk_live\n# comment\n"
        assert "GROQ_API_KEY" not in os.environ

    def test_remove_key_scrubs_the_wizard_env_too(self, tmp_path: Path, layout_config_dir: Path):
        (tmp_path / ".env").write_text(self.ENV)
        (layout_config_dir / ".env").write_text("GROQ_API_KEY=gsk_wizard\nNVIDIA_API_KEY=nv\n")
        mock_keyring = MagicMock()
        mock_keyring.delete_password.side_effect = Exception("no such password")
        with (
            patch.dict("sys.modules", {"keyring": mock_keyring}),
            patch("nvh.cli.setup.DEFAULT_CONFIG_DIR", tmp_path),
        ):
            result = remove_key("groq")

        assert result["env_file"] == ["GROQ_API_KEY"]
        assert result["env_paths"] == [tmp_path / ".env", layout_config_dir / ".env"]
        assert "GROQ_API_KEY" not in (tmp_path / ".env").read_text()
        assert (layout_config_dir / ".env").read_text() == "NVIDIA_API_KEY=nv\n"

    def test_remove_key_reports_nothing_found(self, tmp_path: Path, layout_config_dir: Path):
        mock_keyring = MagicMock()
        mock_keyring.delete_password.side_effect = Exception("no such password")
        with (
            patch.dict("sys.modules", {"keyring": mock_keyring}),
            patch("nvh.cli.setup.DEFAULT_CONFIG_DIR", tmp_path),
        ):
            result = remove_key("groq")
        assert result["keyring"] is False
        assert result["env_file"] == []
        assert result["env_paths"] == []

    def test_disable_provider_in_config_strips_key_and_disables(self, tmp_path: Path):
        cfg = tmp_path / "config.yaml"
        cfg.write_text(SHIPPED_CONFIG)
        assert disable_provider_in_config(cfg, "groq") is True
        data = yaml.safe_load(cfg.read_text())
        assert data["advisors"]["groq"]["enabled"] is False
        assert "api_key" not in data["advisors"]["groq"]
        assert data["advisors"]["groq"]["default_model"] == "groq/llama-3.3-70b-versatile"
        assert data["advisors"]["openai"]["api_key"] == "${OPENAI_API_KEY}"
        assert cfg.with_suffix(".yaml.bak").read_text() == SHIPPED_CONFIG
        # Already disabled + no key: nothing to do, backup untouched.
        assert disable_provider_in_config(cfg, "groq") is False
        assert disable_provider_in_config(cfg, "not-configured") is False
        assert cfg.with_suffix(".yaml.bak").read_text() == SHIPPED_CONFIG
        assert disable_provider_in_config(tmp_path / "missing.yaml", "groq") is False
        assert not (tmp_path / "missing.yaml.bak").exists()

    def test_provider_config_files_adds_the_layout_config_when_distinct(
        self, tmp_path: Path, layout_config_dir: Path,
    ):
        cfg = tmp_path / "config.yaml"
        layout_cfg = layout_config_dir / "config.yaml"
        with patch("nvh.config.settings.DEFAULT_CONFIG_PATH", cfg):
            assert provider_config_files() == [cfg]
            layout_cfg.write_text(WIZARD_CONFIG)
            assert provider_config_files() == [cfg, layout_cfg]
        with patch("nvh.config.settings.DEFAULT_CONFIG_PATH", layout_cfg):
            assert provider_config_files() == [layout_cfg]

    def test_cli_removes_key_everywhere(
        self, runner: CliRunner, tmp_path: Path, layout_config_dir: Path, monkeypatch,
    ):
        cfg = tmp_path / "config.yaml"
        cfg.write_text(SHIPPED_CONFIG)
        (tmp_path / ".env").write_text(self.ENV)
        layout_cfg = layout_config_dir / "config.yaml"
        layout_cfg.write_text(WIZARD_CONFIG)
        (layout_config_dir / ".env").write_text("GROQ_API_KEY=gsk_wizard\n")
        monkeypatch.setenv("GROQ_API_KEY", "gsk_live")
        mock_keyring = MagicMock()
        with (
            patch.dict("sys.modules", {"keyring": mock_keyring}),
            patch("nvh.cli.setup.DEFAULT_CONFIG_DIR", tmp_path),
            patch("nvh.config.settings.DEFAULT_CONFIG_PATH", cfg),
        ):
            result = runner.invoke(cli_main.app, ["advisor", "remove", "groq"])

        assert result.exit_code == 0, result.output
        assert "removed from keychain" in result.output
        assert "GROQ_API_KEY" in result.output
        assert result.output.count("Disabled groq") == 2
        assert "GROQ_API_KEY" not in (tmp_path / ".env").read_text()
        assert (layout_config_dir / ".env").read_text() == ""
        for path in (cfg, layout_cfg):
            data = yaml.safe_load(path.read_text())
            assert data["advisors"]["groq"]["enabled"] is False
            assert "api_key" not in data["advisors"]["groq"]
            assert path.with_suffix(".yaml.bak").exists()
        assert "GROQ_API_KEY" not in os.environ

    def test_cli_reports_when_nothing_stored(
        self, runner: CliRunner, tmp_path: Path, layout_config_dir: Path,
    ):
        mock_keyring = MagicMock()
        mock_keyring.delete_password.side_effect = Exception("no such password")
        with (
            patch.dict("sys.modules", {"keyring": mock_keyring}),
            patch("nvh.cli.setup.DEFAULT_CONFIG_DIR", tmp_path),
            patch("nvh.config.settings.DEFAULT_CONFIG_PATH", tmp_path / "config.yaml"),
        ):
            result = runner.invoke(cli_main.app, ["advisor", "remove", "groq"])
        assert result.exit_code == 0, result.output
        assert "No stored API key found" in result.output


# ---------------------------------------------------------------------------
# Shipped defaults / retired provider references
# ---------------------------------------------------------------------------

class TestShippedDefaults:
    def test_setup_defaults_are_not_retired(self, tmp_path: Path):
        with (
            patch("nvh.config.settings.DEFAULT_CONFIG_PATH", tmp_path / "config.yaml"),
            patch("nvh.config.settings.get_config_dir", return_value=tmp_path),
        ):
            cli_setup._write_config(
                {"groq": "g", "openai": "o", "anthropic": "a", "google": "gg"},
                ollama_enabled=False,
            )
        data = yaml.safe_load((tmp_path / "config.yaml").read_text())
        for name, block in data["advisors"].items():
            if name == "ollama":
                continue
            for field in ("default_model", "fallback_model"):
                model = block.get(field, "")
                assert rename_retired_model(name, model) is None, (
                    f"{name}.{field}={model} is retired"
                )
        assert data["advisors"]["groq"]["default_model"] == "groq/openai/gpt-oss-120b"
        assert data["advisors"]["anthropic"]["default_model"] == "claude-sonnet-5"
        assert data["advisors"]["google"]["fallback_model"] == "gemini/gemini-3.5-flash-lite"

    def test_github_no_longer_a_known_advisor(self, runner: CliRunner):
        assert "github" not in cli_main.KNOWN_ADVISORS
        assert "github" not in [row[0] for row in cli_main.ACCOUNT_SIGNUP]
        result = runner.invoke(cli_main.app, ["keys"])
        assert result.exit_code == 0, result.output
        assert "GitHub Models" not in result.output
