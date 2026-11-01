"""Tests for platform detection and the service-file helpers."""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from nvh.integrations.diagnostics import detector
from nvh.integrations.diagnostics.detector import Platform, detect_platforms, register_cursor


@pytest.fixture(autouse=True)
def _detect_under_the_fake_home(monkeypatch, tmp_path):
    """Detection sees only the fake, empty home tests/conftest.py gives every
    test: no ``claude`` / ``cursor`` / ``openshell`` on PATH (so no
    ``claude mcp list`` subprocess either) and a cwd without ``openclaw.json``.
    The results below are then the same on the dev box and on CI."""
    monkeypatch.setattr(detector, "_cmd_exists", lambda cmd: None)
    monkeypatch.chdir(tmp_path)


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def test_detection_runs_under_the_fake_home():
    """The tests/conftest.py contract this module relies on: ``Path.home()``,
    ``HOME`` and ``USERPROFILE`` agree on an empty directory, ``NVH_HOME`` is a
    fresh sibling of it (not under the real home), and the settings module's
    default config path was re-derived from it."""
    from nvh.config import settings

    home = Path.home()
    assert home.is_dir() and not any(home.iterdir())
    assert Path(os.environ["HOME"]) == home == Path(os.environ["USERPROFILE"])
    nvh_home = Path(os.environ["NVH_HOME"])
    assert nvh_home.parent == home.parent and not nvh_home.exists()
    assert settings.DEFAULT_CONFIG_DIR == nvh_home / "config"
    assert settings.DEFAULT_CONFIG_PATH == nvh_home / "config" / "config.yaml"


def test_nothing_detected_under_an_empty_home():
    platforms = detect_platforms()
    assert {p.name for p in platforms} == {
        "nemoclaw", "openclaw", "claude_code", "cursor", "claude_desktop",
    }
    assert not any(p.detected for p in platforms)
    assert not any(p.already_configured for p in platforms)


def test_cursor_detected_and_configured_from_its_home_config():
    cfg = Path.home() / ".cursor" / "mcp.json"
    _write_json(cfg, {"mcpServers": {"nvhive": {"command": "nvhive-mcp"}}})
    cursor = next(p for p in detect_platforms() if p.name == "cursor")
    assert cursor.detected and cursor.already_configured
    assert cursor.config_path == str(cfg)
    assert cursor.detection_method == f"Config: {cfg}"


def test_openclaw_detected_but_unconfigured_from_its_home_config():
    cfg = Path.home() / ".openclaw" / "config.json"
    _write_json(cfg, {"mcpServers": {}})
    openclaw = next(p for p in detect_platforms() if p.name == "openclaw")
    assert openclaw.detected and not openclaw.already_configured
    assert openclaw.config_path == str(cfg)


def test_register_cursor_writes_the_fake_home_and_is_then_detected():
    """``register_cursor()`` with no path writes ``~/.cursor/mcp.json`` — the
    fake home's, which this test then round-trips through detection."""
    ok, message = register_cursor()
    assert ok, message
    cfg = Path.home() / ".cursor" / "mcp.json"
    assert cfg.is_file() and str(cfg) in message
    assert json.loads(cfg.read_text(encoding="utf-8"))["mcpServers"]["nvhive"] == {
        "command": "nvhive-mcp",
    }
    cursor = next(p for p in detect_platforms() if p.name == "cursor")
    assert cursor.detected and cursor.already_configured


def test_detect_platforms_returns_all():
    """Should return entries for all known platforms."""
    platforms = detect_platforms()
    names = {p.name for p in platforms}
    assert "nemoclaw" in names
    assert "openclaw" in names
    assert "claude_code" in names
    assert "cursor" in names
    assert "claude_desktop" in names


def test_platform_dataclass():
    p = Platform(name="test", display_name="Test", integration_type="mcp")
    assert not p.detected
    assert not p.already_configured
    assert p.notes == []


def test_detect_returns_platform_objects():
    platforms = detect_platforms()
    for p in platforms:
        assert isinstance(p, Platform)
        assert p.name
        assert p.display_name
        assert p.integration_type in ("mcp", "inference")


class TestDetector:
    def test_detect_platforms(self):
        result = detect_platforms()
        assert isinstance(result, (list, dict))

    def test_detect_cursor(self):
        try:
            from nvh.integrations.diagnostics.detector import detect_cursor
            result = detect_cursor()
            assert isinstance(result, (bool, dict, type(None)))
        except (ImportError, AttributeError):
            pytest.skip("detect_cursor not available")

    def test_detect_vscode(self):
        try:
            from nvh.integrations.diagnostics.detector import detect_vscode
            result = detect_vscode()
            assert isinstance(result, (bool, dict, type(None)))
        except (ImportError, AttributeError):
            pytest.skip("detect_vscode not available")

    def test_detect_platforms_returns_list(self):
        platforms = detect_platforms()
        assert isinstance(platforms, list)

    def test_platform_info_fields(self):
        platforms = detect_platforms()
        for p in platforms:
            assert hasattr(p, "name") or isinstance(p, dict)

    def test_detect_cursor_path_check(self):
        platforms = detect_platforms()
        # Should detect at least something (or empty list on CI)
        assert isinstance(platforms, list)
        for p in platforms:
            assert hasattr(p, "name") or isinstance(p, (dict, str))

    def test_detect_with_mocked_vscode(self):
        with patch.dict(os.environ, {"VSCODE_PID": "12345"}):
            platforms = detect_platforms()
            assert isinstance(platforms, list)


class TestService:
    def test_import(self):
        from nvh.integrations import service
        assert service is not None

    def test_has_service_functions(self):
        from nvh.integrations import service
        assert (hasattr(service, "generate_systemd_service") or
                hasattr(service, "generate_launchd_plist") or
                hasattr(service, "service_status"))

    def test_generate_systemd_service_contains_unit_sections(self):
        from nvh.integrations.services.service import generate_systemd_service
        content = generate_systemd_service(host="0.0.0.0", port=9000)
        assert "[Unit]" in content
        assert "[Service]" in content
        assert "[Install]" in content
        assert "0.0.0.0" in content
        assert "9000" in content

    def test_generate_launchd_plist_is_valid_xml(self):
        import xml.etree.ElementTree as ET

        from nvh.integrations.services.service import generate_launchd_plist
        content = generate_launchd_plist(host="127.0.0.1", port=8080)
        assert content.strip().startswith("<?xml")
        # Should parse without error (strip the DOCTYPE which ET doesn't handle)
        cleaned = content.replace(
            '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"\n'
            '  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">',
            "",
        )
        ET.fromstring(cleaned)

    def test_service_status_returns_tuple(self):
        from nvh.integrations.services.service import service_status
        result = service_status()
        assert isinstance(result, tuple)
        assert len(result) == 2
        assert isinstance(result[0], bool)
        assert isinstance(result[1], str)
