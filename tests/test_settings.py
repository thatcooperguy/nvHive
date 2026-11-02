"""Tests for nvh.config.settings — loading, merging, interpolation, defaults."""

from __future__ import annotations

import os
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from nvh.config.settings import (
    CacheConfig,
    CouncilConfig,
    CouncilWeights,
    ProfileConfig,
    RoutingConfig,
    _deep_merge,
    _interpolate_env,
    _load_yaml,
    generate_default_config,
    load_config,
    save_config,
)


class TestSettingsLoading:
    def test_interpolate_env_unset_no_default(self) -> None:
        """${VAR} with no default and VAR unset => empty string + warning."""
        os.environ.pop("__TOTALLY_MISSING__", None)
        result = _interpolate_env("prefix-${__TOTALLY_MISSING__}-suffix")
        assert result == "prefix--suffix"

    def test_interpolate_env_in_list(self) -> None:
        """Env-var interpolation recurses into lists."""
        with patch.dict(os.environ, {"_LIST_VAR": "item"}):
            result = _interpolate_env(["${_LIST_VAR}", "plain"])
        assert result == ["item", "plain"]

    def test_interpolate_env_non_string_passthrough(self) -> None:
        """Non-string values (int, None, etc.) pass through unchanged."""
        assert _interpolate_env(42) == 42
        assert _interpolate_env(None) is None
        assert _interpolate_env(True) is True

    def test_load_yaml_invalid_syntax(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.yaml"
        bad.write_text(":\n  - [invalid\n")
        with pytest.raises(ValueError, match="invalid YAML"):
            _load_yaml(bad)

    def test_load_yaml_non_dict(self, tmp_path: Path) -> None:
        bad = tmp_path / "list.yaml"
        bad.write_text("- one\n- two\n")
        with pytest.raises(ValueError, match="must be a YAML mapping"):
            _load_yaml(bad)

    def test_load_yaml_reads_utf8_with_optional_bom(self, tmp_path: Path) -> None:
        """A BOM (Windows editors, PS 5.1 Set-Content) and non-ASCII text load
        the same on every locale: the first key is `version`, not `\\ufeffversion`."""
        cfg = tmp_path / "config.yaml"
        cfg.write_bytes(
            "\ufeffversion: \"1\"\n# caf\u00e9 \u2014 notes\ndefaults:\n  provider: \"\u00fcber\"\n".encode("utf-8")
        )
        raw = _load_yaml(cfg)
        assert list(raw)[0] == "version"
        assert raw["defaults"]["provider"] == "\u00fcber"

    def test_load_yaml_non_utf8_is_one_actionable_error(self, tmp_path: Path) -> None:
        """A config saved in a legacy code page fails with one sentence that names
        the fix, not a UnicodeDecodeError traceback."""
        cfg = tmp_path / "config.yaml"
        cfg.write_bytes("version: \"1\"\n# caf\u00e9\n".encode("cp1252"))
        with pytest.raises(ValueError, match="not UTF-8 text") as exc:
            _load_yaml(cfg)
        assert "nvh config init --force" in str(exc.value)

    def test_save_config_writes_utf8(self, tmp_path: Path) -> None:
        """save_config writes UTF-8 whatever the locale, and load_config reads it back."""
        target = tmp_path / "config.yaml"
        config = CouncilConfig()
        config.profiles["caf\u00e9-\u00fcber"] = ProfileConfig()
        save_config(config, target)
        assert "caf\u00e9-\u00fcber" in target.read_bytes().decode("utf-8")
        assert "caf\u00e9-\u00fcber" in load_config(config_path=target).profiles

    def test_profile_merge_via_arg(self, tmp_path: Path) -> None:
        """load_config with profile= merges profile section."""
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text(yaml.dump({
            "defaults": {"timeout": 10},
            "profiles": {
                "fast": {"defaults": {"timeout": 5, "stream": False}},
            },
        }))
        with patch("nvh.config.settings._find_project_config", return_value=None):
            cfg = load_config(config_path=cfg_file, profile="fast")
        assert cfg.defaults.timeout == 5
        assert cfg.defaults.stream is False

    def test_profile_merge_via_env(self, tmp_path: Path) -> None:
        """HIVE_PROFILE env var triggers profile merge."""
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text(yaml.dump({
            "defaults": {"timeout": 10},
            "profiles": {
                "slow": {"defaults": {"timeout": 120}},
            },
        }))
        with (
            patch("nvh.config.settings._find_project_config", return_value=None),
            patch.dict(os.environ, {"HIVE_PROFILE": "slow"}),
        ):
            cfg = load_config(config_path=cfg_file)
        assert cfg.defaults.timeout == 120

    def test_council_weights_normalization(self) -> None:
        cw = CouncilWeights(weights={"a": 2.0, "b": 3.0})
        assert abs(sum(cw.weights.values()) - 1.0) < 0.01

    def test_routing_config_defaults(self) -> None:
        rc = RoutingConfig()
        assert rc.weights["capability"] == 0.4
        assert rc.rules == []

    def test_cache_config_defaults(self) -> None:
        cc = CacheConfig()
        assert cc.enabled is True
        assert cc.ttl_seconds == 86400
        assert cc.cache_nonzero_temp is False

    def test_save_and_reload(self, tmp_path: Path) -> None:
        cfg = CouncilConfig(defaults={"timeout": 77})
        path = save_config(cfg, tmp_path / "out.yaml")
        assert path.is_file()
        with patch("nvh.config.settings._find_project_config", return_value=None):
            reloaded = load_config(config_path=path)
        assert reloaded.defaults.timeout == 77

    def test_generate_default_config_is_valid_yaml(self) -> None:
        text = generate_default_config()
        parsed = yaml.safe_load(text)
        assert isinstance(parsed, dict)
        assert "defaults" in parsed

    def test_profile_config_advisors_alias(self) -> None:
        pc = ProfileConfig(**{"advisors": {"mock": {"default_model": "m"}}})
        assert "mock" in pc.providers


class TestSettings:
    def test_default_config_values(self) -> None:
        cfg = CouncilConfig()
        assert cfg.defaults.mode == "ask"
        assert cfg.defaults.timeout == 30
        assert cfg.defaults.max_tokens == 4096
        assert cfg.defaults.temperature == 1.0
        assert cfg.defaults.stream is True
        assert cfg.cache.enabled is True
        assert cfg.budget.daily_limit_usd == Decimal("5")

    def test_load_config_from_yaml(self, tmp_path: Path) -> None:
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text(
            "version: '1'\n"
            "defaults:\n"
            "  provider: anthropic\n"
            "  timeout: 45\n"
        )
        with patch("nvh.config.settings._find_project_config", return_value=None):
            cfg = load_config(config_path=cfg_file)
        assert cfg.defaults.provider == "anthropic"
        assert cfg.defaults.timeout == 45

    def test_load_config_missing_file(self, tmp_path: Path) -> None:
        missing = tmp_path / "nope.yaml"
        with patch("nvh.config.settings._find_project_config", return_value=None):
            cfg = load_config(config_path=missing)
        assert isinstance(cfg, CouncilConfig)
        assert cfg.defaults.mode == "ask"

    def test_deep_merge(self) -> None:
        base = {"a": 1, "nested": {"x": 10, "y": 20}}
        override = {"nested": {"y": 99, "z": 30}, "b": 2}
        result = _deep_merge(base, override)
        assert result["a"] == 1
        assert result["b"] == 2
        assert result["nested"]["x"] == 10
        assert result["nested"]["y"] == 99
        assert result["nested"]["z"] == 30

    def test_env_var_interpolation(self) -> None:
        with patch.dict(os.environ, {"TEST_VAR_XYZ": "hello"}):
            assert _interpolate_env("${TEST_VAR_XYZ}") == "hello"

    def test_env_var_default(self) -> None:
        os.environ.pop("NONEXISTENT_VAR_ABC", None)
        assert _interpolate_env("${NONEXISTENT_VAR_ABC:-fallback}") == "fallback"

    def test_advisors_alias(self) -> None:
        cfg = CouncilConfig(**{
            "advisors": {"mock": {"default_model": "m"}},
        })
        assert "mock" in cfg.providers

    def test_config_merge_project_over_user(self, tmp_path: Path) -> None:
        user_cfg = tmp_path / "user.yaml"
        user_cfg.write_text("defaults:\n  timeout: 10\n  provider: openai\n")
        proj_cfg = tmp_path / ".nvh.yaml"
        proj_cfg.write_text("defaults:\n  timeout: 99\n")
        with patch("nvh.config.settings._find_project_config", return_value=proj_cfg):
            cfg = load_config(config_path=user_cfg)
        assert cfg.defaults.timeout == 99
        assert cfg.defaults.provider == "openai"


@pytest.fixture()
def nvh_home(tmp_path: Path, monkeypatch) -> Path:
    """A throwaway ``NVH_HOME`` so nothing below touches the real user config."""
    import nvh.config.settings as settings

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    for var in ("NVHIVE_HOME", "NVH_CONFIG", "HIVE_CONFIG_HOME"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("NVH_HOME", str(tmp_path / "nvh"))
    # cwd under the *fake* home so the project-config walk stops there (on
    # Windows tmp_path itself sits under the real home).
    (home / "work").mkdir()
    monkeypatch.chdir(home / "work")
    settings.reset_default_paths()
    yield tmp_path / "nvh"
    settings.reset_default_paths()


class TestDefaultPaths:
    """``DEFAULT_CONFIG_DIR`` / ``DEFAULT_CONFIG_PATH`` come from ``storage_layout()``."""

    def test_default_paths_derive_from_the_storage_layout(self, nvh_home: Path) -> None:
        import nvh.config.settings as settings
        from nvh.integrations.workspace.storage import storage_layout

        assert settings.DEFAULT_CONFIG_DIR == storage_layout().config_dir == nvh_home / "config"
        assert settings.DEFAULT_CONFIG_PATH == nvh_home / "config" / "config.yaml"

    def test_default_paths_follow_the_config_override(self, nvh_home: Path, monkeypatch) -> None:
        import nvh.config.settings as settings

        monkeypatch.setenv("NVH_CONFIG", str(nvh_home.parent / "cfg"))
        settings.reset_default_paths()
        assert settings.DEFAULT_CONFIG_DIR == nvh_home.parent / "cfg"
        assert settings.DEFAULT_CONFIG_PATH == nvh_home.parent / "cfg" / "config.yaml"

        monkeypatch.delenv("NVH_CONFIG")
        monkeypatch.setenv("HIVE_CONFIG_HOME", str(nvh_home.parent / "legacy-cfg"))
        settings.reset_default_paths()
        assert settings.DEFAULT_CONFIG_DIR == nvh_home.parent / "legacy-cfg"

    def test_patched_default_paths_are_honoured(self, nvh_home: Path, monkeypatch) -> None:
        """Tests and ``activate_storage()`` set the names as plain module attributes."""
        import nvh.config.settings as settings

        monkeypatch.setattr(settings, "DEFAULT_CONFIG_DIR", nvh_home / "elsewhere")
        assert settings.get_config_dir() == nvh_home / "elsewhere"
        assert (nvh_home / "elsewhere").is_dir()
        with patch("nvh.config.settings.DEFAULT_CONFIG_PATH", nvh_home / "p" / "config.yaml"):
            (nvh_home / "p").mkdir(parents=True)
            (nvh_home / "p" / "config.yaml").write_text("defaults:\n  timeout: 42\n")
            assert settings.load_config().defaults.timeout == 42

    def test_default_paths_are_plain_module_constants(self) -> None:
        """D7 literally: ``DEFAULT_CONFIG_DIR = storage_layout().config_dir`` at import —
        no lazy ``__getattr__``, no accessor a bare in-module name could bypass."""
        import ast
        from pathlib import Path as _Path

        import nvh.config.settings as settings

        source = _Path(settings.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        top_level = {node.name for node in tree.body if isinstance(node, ast.FunctionDef)}
        assert "__getattr__" not in top_level
        assert not {"_default_config_dir", "_default_config_path"} & top_level
        assigned = {
            target.id
            for node in tree.body if isinstance(node, ast.Assign)
            for target in node.targets if isinstance(target, ast.Name)
        }
        assert {"DEFAULT_CONFIG_DIR", "DEFAULT_CONFIG_PATH"} <= assigned
        assert isinstance(settings.DEFAULT_CONFIG_DIR, Path) and isinstance(settings.DEFAULT_CONFIG_PATH, Path)


class TestSettingsDefaults:
    def test_load_config_defaults(self, nvh_home: Path):
        from nvh.config.settings import load_config
        config = load_config()
        assert config is not None
        assert hasattr(config, "defaults")
        assert hasattr(config, "providers")

    def test_defaults_config_fields(self):
        from nvh.config.settings import DefaultsConfig
        d = DefaultsConfig()
        assert hasattr(d, "provider")
        assert hasattr(d, "temperature")
        assert hasattr(d, "max_tokens")

    def test_budget_config_defaults(self):
        from nvh.config.settings import BudgetConfig
        b = BudgetConfig()
        assert b.daily_limit_usd > 0
        assert b.monthly_limit_usd > 0

    def test_cache_config_defaults(self):
        from nvh.config.settings import CacheConfig
        c = CacheConfig()
        assert c.max_size > 0
        assert c.ttl_seconds > 0

    def test_routing_config_defaults(self):
        from nvh.config.settings import RoutingConfig
        r = RoutingConfig()
        assert r is not None

    def test_council_mode_config(self):
        from nvh.config.settings import CouncilModeConfig
        c = CouncilModeConfig()
        assert c.quorum >= 1
        assert c.timeout > 0

    def test_get_config_dir_creates(self, nvh_home: Path):
        from nvh.config.settings import get_config_dir
        d = get_config_dir()
        assert d.exists()
        assert d == nvh_home / "config"

    def test_provider_config(self):
        from nvh.config.settings import ProviderConfig
        p = ProviderConfig(enabled=True, default_model="test")
        assert p.enabled is True
        assert p.default_model == "test"

    def test_load_config_with_profile(self, nvh_home: Path):
        from nvh.config.settings import load_config
        # Loading with a nonexistent profile should fall back to defaults
        config = load_config(profile="nonexistent_profile_xyz")
        assert config is not None


class TestBudgetHourlyRate:
    """``budget.hourly_rate_usd`` (0.46 §7): what the desktop costs its owner per hour.

    Optional, never guessed: ``None`` by default, parsed from YAML (``nvh config
    set budget.hourly_rate_usd 2.10`` writes a float), negative values refused,
    and the generated template documents it as a commented row so a fresh
    config still parses to ``None``.
    """

    def test_defaults_to_none(self) -> None:
        from nvh.config.settings import BudgetConfig

        assert BudgetConfig().hourly_rate_usd is None
        assert CouncilConfig().budget.hourly_rate_usd is None

    def test_parses_from_yaml(self, tmp_path: Path) -> None:
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text("version: '1'\nbudget:\n  hourly_rate_usd: 2.10\n", encoding="utf-8")
        with patch("nvh.config.settings._find_project_config", return_value=None):
            cfg = load_config(config_path=cfg_file)
        assert cfg.budget.hourly_rate_usd == Decimal("2.1")
        # ``nvh config set`` round-trips through model_dump(): the value survives a save.
        reloaded_path = save_config(cfg, tmp_path / "out.yaml")
        with patch("nvh.config.settings._find_project_config", return_value=None):
            reloaded = load_config(config_path=reloaded_path)
        assert reloaded.budget.hourly_rate_usd == Decimal("2.1")

    def test_zero_is_allowed_and_negative_is_refused(self) -> None:
        from pydantic import ValidationError

        from nvh.config.settings import BudgetConfig

        assert BudgetConfig(hourly_rate_usd=Decimal("0")).hourly_rate_usd == Decimal("0")
        with pytest.raises(ValidationError):
            BudgetConfig(hourly_rate_usd=Decimal("-0.01"))

    def test_default_template_documents_the_rate_as_a_comment(self) -> None:
        text = generate_default_config()
        assert "# hourly_rate_usd: 2.10" in text
        parsed = yaml.safe_load(text)
        assert "hourly_rate_usd" not in parsed["budget"], "a comment, not a value: the default stays None"
        assert set(parsed["budget"]) == {
            "daily_limit_usd", "monthly_limit_usd", "alert_threshold", "hard_stop", "degrade_on_limit",
        }
