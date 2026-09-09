"""The upward project-config search must never pick up the user's own
config as a project overlay.

Before the guard, running from any directory under ``$HOME`` deep-merged
the pre-NVH_HOME ``~/.hive/config.yaml`` over the real user config; a leftover
``providers: {}`` then won against an ``advisors:``-style config and every
provider vanished. Since 0.44 the user config is ``$NVH_HOME/config/config.yaml``
and the project names are ``.nvh.yaml`` / ``.nvh/config.yaml`` (the old
``.hive`` names are read as a fallback), so the guard covers the layout's
``config_dir``, the ``NVH_HOME`` root and the legacy ``~/.hive``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import nvh.config.settings as settings


@pytest.fixture()
def home(tmp_path: Path, monkeypatch) -> Path:
    """A fake OS home whose ``NVH_HOME`` is ``<home>/.nvh`` (the default)."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    for var in ("NVH_HOME", "NVHIVE_HOME", "NVH_CONFIG", "HIVE_CONFIG_HOME"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("NVH_HOME", str(home / ".nvh"))
    settings.reset_default_paths()
    yield home
    settings.reset_default_paths()


def test_default_config_dir_is_the_layout_config_dir(home: Path) -> None:
    assert settings.DEFAULT_CONFIG_DIR == home / ".nvh" / "config"
    assert settings.DEFAULT_CONFIG_PATH == home / ".nvh" / "config" / "config.yaml"


def test_layout_config_is_not_a_project_config(home: Path, monkeypatch) -> None:
    cfg = home / ".nvh" / "config" / "config.yaml"
    cfg.parent.mkdir(parents=True)
    cfg.write_text("providers: {}\n")
    # ``.nvh/config.yaml`` is also a project name: running from $HOME must not
    # treat the layout root as a project.
    (home / ".nvh" / "config.yaml").write_text("providers: {}\n")
    project = home / "work" / "proj"
    project.mkdir(parents=True)
    monkeypatch.chdir(project)

    assert settings._find_project_config() is None

    monkeypatch.chdir(home)
    assert settings._find_project_config() is None


def test_legacy_home_hive_config_is_not_a_project_config(home: Path, monkeypatch) -> None:
    (home / ".hive").mkdir()
    (home / ".hive" / "config.yaml").write_text("providers: {}\n")
    project = home / "work" / "proj"
    project.mkdir(parents=True)
    monkeypatch.chdir(project)

    assert settings._find_project_config() is None

    monkeypatch.chdir(home)
    assert settings._find_project_config() is None


def test_real_project_config_under_home_is_still_found(home: Path, monkeypatch) -> None:
    (home / ".hive").mkdir()
    (home / ".hive" / "config.yaml").write_text("providers: {}\n")
    project = home / "work" / "proj"
    (project / ".nvh").mkdir(parents=True)
    project_cfg = project / ".nvh" / "config.yaml"
    project_cfg.write_text("defaults:\n  temperature: 0.1\n")
    (project / "src").mkdir()
    monkeypatch.chdir(project / "src")

    assert settings._find_project_config() == project_cfg


def test_legacy_project_names_are_read_as_a_fallback(home: Path, monkeypatch) -> None:
    project = home / "work" / "proj"
    (project / ".hive").mkdir(parents=True)
    legacy_cfg = project / ".hive" / "config.yaml"
    legacy_cfg.write_text("defaults:\n  temperature: 0.1\n")
    monkeypatch.chdir(project)

    assert settings._find_project_config() == legacy_cfg

    # The .nvh name wins over the .hive one at the same level.
    new_cfg = project / ".nvh.yaml"
    new_cfg.write_text("defaults:\n  temperature: 0.3\n")
    assert settings._find_project_config() == new_cfg


def test_nvh_yaml_at_home_root_is_still_a_project_config(home: Path, monkeypatch) -> None:
    # Only the config *directories* at $HOME are user-config locations; a
    # dotfile ``~/.nvh.yaml`` is a deliberate project-style overlay.
    dotfile = home / ".nvh.yaml"
    dotfile.write_text("defaults:\n  temperature: 0.2\n")
    monkeypatch.chdir(home)

    assert settings._find_project_config() == dotfile


def test_project_overlay_merges_over_user_config(home: Path, monkeypatch) -> None:
    cfg = home / ".nvh" / "config" / "config.yaml"
    cfg.parent.mkdir(parents=True)
    cfg.write_text("defaults:\n  timeout: 10\n  provider: openai\n")
    project = home / "work" / "proj"
    project.mkdir(parents=True)
    (project / ".nvh.yaml").write_text("defaults:\n  timeout: 99\n")
    monkeypatch.chdir(project)

    loaded = settings.load_config()
    assert loaded.defaults.timeout == 99
    assert loaded.defaults.provider == "openai"
