"""Tests for nvh.core.browser_tools."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from nvh.core.browser_tools import register_browser_tools
from nvh.core.tools import ToolRegistry

TOOL_NAMES = [
    "browser_navigate", "browser_screenshot", "browser_fill_form",
    "http_request", "process_list", "process_kill", "docker_ps", "docker_run",
]

def _registry() -> ToolRegistry:
    reg = ToolRegistry(include_system=False)
    register_browser_tools(reg)
    return reg


def test_register_browser_tools_adds_tools():
    reg = _registry()
    for name in TOOL_NAMES:
        assert reg.get(name) is not None, f"Missing tool: {name}"


def test_safe_flags():
    reg = _registry()
    for name in ("browser_navigate", "http_request", "process_list", "docker_ps"):
        assert reg.get(name).safe is True, f"{name} should be safe"
    for name in ("browser_screenshot", "browser_fill_form", "process_kill", "docker_run"):
        assert reg.get(name).safe is False, f"{name} should be unsafe"


def test_http_request_get():
    reg = _registry()
    mock_resp = MagicMock(status_code=200, text="hello world")
    with patch("httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.request = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_cls.return_value = mock_client
        result = asyncio.get_event_loop().run_until_complete(
            reg.execute("http_request", {"method": "GET", "url": "https://example.com"})
        )
    assert result.success
    assert "200" in result.output


def test_process_list_returns_string():
    reg = _registry()
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(stdout="PID TTY TIME CMD\n1 ? 00:00 init", stderr="")
        result = asyncio.get_event_loop().run_until_complete(
            reg.execute("process_list", {})
        )
    assert result.success
    assert "PID" in result.output


def test_docker_ps_not_installed():
    reg = _registry()
    with patch("subprocess.run", side_effect=FileNotFoundError):
        result = asyncio.get_event_loop().run_until_complete(
            reg.execute("docker_ps", {})
        )
    assert result.success
    assert "not available" in result.output


def test_browser_navigate_no_playwright():
    reg = _registry()
    with patch.dict("sys.modules", {"playwright": None, "playwright.sync_api": None}):
        result = asyncio.get_event_loop().run_until_complete(
            reg.execute("browser_navigate", {"url": "https://example.com"})
        )
    assert result.success
    assert "not installed" in result.output.lower()
