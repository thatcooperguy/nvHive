"""The upward project-config search must never pick up the user's own
``~/.hive/config.yaml`` as a project overlay.

Before the guard, running from any directory under ``$HOME`` deep-merged
that file over the real user config; a leftover ``providers: {}`` then won
against an ``advisors:``-style config and every provider vanished.
"""

from __future__ import annotations

from pathlib import Path

import nvh.config.settings as settings


def _fake_home(tmp_path: Path, monkeypatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setattr(settings, "DEFAULT_CONFIG_DIR", home / ".hive")
    monkeypatch.setattr(settings, "DEFAULT_CONFIG_PATH", home / ".hive" / "config.yaml")
    return home


def test_home_hive_config_is_not_a_project_config(tmp_path: Path, monkeypatch) -> None:
    home = _fake_home(tmp_path, monkeypatch)
    (home / ".hive").mkdir()
    (home / ".hive" / "config.yaml").write_text("providers: {}\n")
    project = home / "work" / "proj"
    project.mkdir(parents=True)
    monkeypatch.chdir(project)

    assert settings._find_project_config() is None


def test_real_project_config_under_home_is_still_found(tmp_path: Path, monkeypatch) -> None:
    home = _fake_home(tmp_path, monkeypatch)
    (home / ".hive").mkdir()
    (home / ".hive" / "config.yaml").write_text("providers: {}\n")
    project = home / "work" / "proj"
    (project / ".hive").mkdir(parents=True)
    project_cfg = project / ".hive" / "config.yaml"
    project_cfg.write_text("defaults:\n  temperature: 0.1\n")
    (project / "src").mkdir()
    monkeypatch.chdir(project / "src")

    assert settings._find_project_config() == project_cfg


def test_hive_yaml_at_home_root_is_still_a_project_config(tmp_path: Path, monkeypatch) -> None:
    # Only the .hive/ *directory* at $HOME is the user-config location; a
    # dotfile ``~/.hive.yaml`` is a deliberate project-style overlay.
    home = _fake_home(tmp_path, monkeypatch)
    dotfile = home / ".hive.yaml"
    dotfile.write_text("defaults:\n  temperature: 0.2\n")
    monkeypatch.chdir(home)

    assert settings._find_project_config() == dotfile
