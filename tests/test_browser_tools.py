"""Tests for browser + OS tools."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nvh.core.tools import ToolRegistry


def _registry():
    reg = ToolRegistry(include_system=False)
    from nvh.core.browser_tools import register_browser_tools
    register_browser_tools(reg)
    return reg


class TestRegistration:
    def test_all_tools_registered(self):
        reg = _registry()
        names = {t.name for t in reg.list_tools()}
        assert "browser_navigate" in names
        assert "http_request" in names
        assert "process_list" in names
        assert "docker_ps" in names

    def test_safe_flags(self):
        reg = _registry()
        for t in reg.list_tools():
            if t.name in ("browser_navigate", "http_request", "process_list", "docker_ps"):
                assert t.safe is True
            elif t.name in ("mouse_click", "keyboard_type", "process_kill", "docker_run"):
                assert t.safe is False


@pytest.mark.asyncio
async def test_http_request_get():
    reg = _registry()
    mock_resp = MagicMock(status_code=200, text="hello world")
    with patch("httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.request = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_cls.return_value = mock_client
        result = await reg.execute("http_request", {"method": "GET", "url": "https://example.com"})
    assert result.success
    assert "200" in result.output


@pytest.mark.asyncio
async def test_process_list_returns_string():
    reg = _registry()
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(stdout="PID TTY TIME CMD\n1 ? 00:00 init", stderr="")
        result = await reg.execute("process_list", {})
    assert result.success


@pytest.mark.asyncio
async def test_docker_ps_not_installed():
    reg = _registry()
    with patch("subprocess.run", side_effect=FileNotFoundError):
        result = await reg.execute("docker_ps", {})
    assert result.success
    assert "not available" in result.output.lower() or "docker" in result.output.lower()


@pytest.mark.asyncio
async def test_browser_navigate_no_playwright():
    reg = _registry()
    with patch.dict("sys.modules", {"playwright": None, "playwright.sync_api": None}):
        result = await reg.execute("browser_navigate", {"url": "https://example.com"})
    assert result.success
    assert "not installed" in result.output.lower() or "playwright" in result.output.lower()
