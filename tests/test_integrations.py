"""Tests for platform detection and integration helpers."""

from nvh.integrations.detector import Platform, detect_platforms


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
