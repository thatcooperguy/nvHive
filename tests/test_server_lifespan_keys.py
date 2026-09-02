"""The API lifespan must expose keys saved by `nvh setup` / the web wizard.

`nvh serve` gets them for free because nvh.cli.main runs load_env_keys()
before dispatching, but a bare ``uvicorn nvh.api.server:app`` (or the
TestClient context manager) reaches the lifespan directly. These tests
run the real lifespan and the real loader against a throwaway
HIVE_CONFIG_HOME with a stub Engine and a recording stub in place of the
``keyring`` module, so no provider or SecretService daemon is touched.
"""

from __future__ import annotations

import os
import sys
import types
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import nvh.api.server as server_module
import nvh.cli.setup as nvh_setup
from nvh.api.server import app

KEY_VAR = "NVH_LIFESPAN_TEST_API_KEY"


class _StubRegistry:
    def list_enabled(self) -> list[str]:
        return []


class _StubEngine:
    webhooks = None
    _initialized = True
    registry = _StubRegistry()

    def __init__(self, *args, **kwargs) -> None:
        pass

    async def initialize(self) -> list[str]:
        return []


@pytest.fixture()
def config_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Throwaway config dir; records every keyring lookup the lifespan makes."""
    monkeypatch.setenv("HIVE_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("NVH_HOME", str(tmp_path / "nvh-home"))
    monkeypatch.setenv("NVH_BOOT_PREFLIGHT", "0")
    monkeypatch.delenv("NVH_USE_KEYRING", raising=False)
    monkeypatch.delenv(KEY_VAR, raising=False)
    monkeypatch.setattr(nvh_setup, "DEFAULT_CONFIG_DIR", tmp_path)
    monkeypatch.setattr(server_module, "Engine", _StubEngine)
    monkeypatch.setattr(server_module, "_engine", None)

    keyring_calls: list[tuple[str, str]] = []
    fake_keyring = types.ModuleType("keyring")

    def get_password(service: str, name: str) -> None:
        keyring_calls.append((service, name))
        return None

    fake_keyring.get_password = get_password
    monkeypatch.setitem(sys.modules, "keyring", fake_keyring)
    return tmp_path, keyring_calls


def test_lifespan_exposes_env_file_key(config_home) -> None:
    home, _ = config_home
    (home / ".env").write_text(
        f"# saved by nvh setup\n{KEY_VAR}=sk-from-dot-env\n", encoding="utf-8"
    )

    with TestClient(app):
        assert os.environ[KEY_VAR] == "sk-from-dot-env"


def test_lifespan_leaves_keyring_alone_unless_opted_in(config_home) -> None:
    _, keyring_calls = config_home

    with TestClient(app):
        pass

    assert keyring_calls == []


def test_lifespan_reads_keyring_when_opted_in(config_home, monkeypatch: pytest.MonkeyPatch) -> None:
    _, keyring_calls = config_home
    monkeypatch.setenv("NVH_USE_KEYRING", "1")

    with TestClient(app):
        pass

    assert keyring_calls
    assert all(service == "nvhive" for service, _ in keyring_calls)


def test_lifespan_never_overrides_shell_env(config_home, monkeypatch: pytest.MonkeyPatch) -> None:
    home, _ = config_home
    monkeypatch.setenv(KEY_VAR, "sk-from-shell")
    (home / ".env").write_text(f"{KEY_VAR}=sk-from-dot-env\n", encoding="utf-8")

    with TestClient(app):
        assert os.environ[KEY_VAR] == "sk-from-shell"


def test_lifespan_survives_missing_env_file(config_home) -> None:
    home, _ = config_home
    assert not (home / ".env").exists()

    with TestClient(app) as client:
        assert client.get("/v1/health").status_code == 200
    assert KEY_VAR not in os.environ
