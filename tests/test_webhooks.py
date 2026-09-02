"""Tests for nvh.core.webhooks — manager, dispatch, signing, payload formatters."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from nvh.core.webhooks import (
    WebhookConfig,
    WebhookManager,
    WebhookPayload,
    _sign_payload,
    format_budget_alert,
    format_provider_alert,
    format_query_complete,
)


class TestWebhookManager:
    def test_webhook_manager_construct(self):
        wm = WebhookManager()
        assert wm is not None

    def test_list_hooks_empty(self):
        wm = WebhookManager()
        hooks = wm.list_hooks()
        assert isinstance(hooks, (list, dict))

    def test_register_and_list_hooks(self) -> None:
        mgr = WebhookManager()
        cfg = WebhookConfig(
            url="https://example.com/hook",
            events=["query.complete"],
            secret="s3cret",
        )
        mgr.register(cfg)
        hooks = mgr.list_hooks()
        assert len(hooks) == 1
        assert hooks[0]["url"] == "https://example.com/hook"
        assert hooks[0]["secret"] == "***"  # masked

    def test_list_hooks_no_secret(self) -> None:
        mgr = WebhookManager()
        cfg = WebhookConfig(url="https://example.com/hook", events=[])
        mgr.register(cfg)
        hooks = mgr.list_hooks()
        assert hooks[0]["secret"] == ""

    @pytest.mark.asyncio()
    async def test_dispatch_success(self) -> None:
        mgr = WebhookManager()
        cfg = WebhookConfig(
            url="https://example.com/hook",
            events=["query.complete"],
            secret="abc",
            retry_count=1,
            timeout_seconds=5,
        )
        mock_response = MagicMock(status_code=200)
        mock_client = AsyncMock()
        mock_client.post.return_value = mock_response
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        payload = WebhookPayload(event="query.complete", timestamp=1.0, data={"k": "v"})
        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await mgr._dispatch(cfg, payload)
        assert result is True
        mock_client.post.assert_called_once()

    @pytest.mark.asyncio()
    async def test_dispatch_failure_retries(self) -> None:
        mgr = WebhookManager()
        cfg = WebhookConfig(
            url="https://example.com/hook",
            events=[],
            retry_count=2,
            timeout_seconds=1,
        )
        mock_client = AsyncMock()
        mock_client.post.side_effect = httpx.ConnectError("refused")
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        payload = WebhookPayload(event="test.event", timestamp=1.0, data={})
        with (
            patch("httpx.AsyncClient", return_value=mock_client),
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            result = await mgr._dispatch(cfg, payload)
        assert result is False
        assert mock_client.post.call_count == 2

    @pytest.mark.asyncio()
    async def test_emit_filters_by_event(self) -> None:
        mgr = WebhookManager()
        cfg = WebhookConfig(url="https://a.com", events=["x.y"])
        mgr.register(cfg)
        await mgr.emit("other.event", {})
        assert mgr._queue.empty()

    @pytest.mark.asyncio()
    async def test_emit_matches_event(self) -> None:
        mgr = WebhookManager()
        cfg = WebhookConfig(url="https://a.com", events=["x.y"])
        mgr.register(cfg)
        await mgr.emit("x.y", {"data": 1})
        assert not mgr._queue.empty()


class TestSignPayload:
    def test_deterministic(self) -> None:
        sig1 = _sign_payload("body", "secret")
        sig2 = _sign_payload("body", "secret")
        assert sig1 == sig2
        assert len(sig1) == 64  # hex sha256


class TestWebhookConfigAndFormatters:
    def test_load_from_config(self):
        mgr = WebhookManager()
        mgr.load_from_config([
            {"url": "https://a.com/hook", "events": ["query.complete"], "secret": "s"},
            {"url": "", "events": []},  # empty URL skipped
            {"url": "https://b.com/hook", "enabled": False},
        ])
        hooks = mgr.list_hooks()
        assert len(hooks) == 2
        assert hooks[0]["url"] == "https://a.com/hook"

    @pytest.mark.asyncio
    async def test_emit_skips_disabled_hook(self):
        mgr = WebhookManager()
        cfg = WebhookConfig(url="https://a.com", events=[], enabled=False)
        mgr.register(cfg)
        await mgr.emit("any.event", {"key": "val"})
        assert mgr._queue.empty()

    @pytest.mark.asyncio
    async def test_dispatch_non_2xx_retries(self):
        mgr = WebhookManager()
        cfg = WebhookConfig(url="https://a.com", events=[], retry_count=2, timeout_seconds=1)
        mock_resp = MagicMock(status_code=500)
        mock_client = AsyncMock()
        mock_client.post.return_value = mock_resp
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        payload = WebhookPayload(event="x", timestamp=1.0, data={})
        with (
            patch("httpx.AsyncClient", return_value=mock_client),
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            result = await mgr._dispatch(cfg, payload)
        assert result is False
        assert mock_client.post.call_count == 2

    def test_format_budget_alert(self):
        data = format_budget_alert(0.50, 1.00, 5.0, 10.0, 0.5)
        assert data["daily_spend_usd"] == 0.5
        assert data["threshold_pct"] == 50.0
        assert data["daily_pct_used"] == 50.0

    def test_format_budget_alert_zero_limits(self):
        data = format_budget_alert(0.0, 0.0, 0.0, 0.0, 0.0)
        assert data["daily_pct_used"] == 0
        assert data["monthly_pct_used"] == 0

    def test_format_provider_alert(self):
        data = format_provider_alert("openai", "error", error="timeout", latency_ms=500)
        assert data["provider"] == "openai"
        assert data["error"] == "timeout"
        assert data["latency_ms"] == 500

    def test_format_provider_alert_minimal(self):
        data = format_provider_alert("ollama", "recovered")
        assert "error" not in data
        assert "latency_ms" not in data

    def test_format_query_complete(self):
        data = format_query_complete("openai", "gpt-4", 100, 0.003, 500, "query")
        assert data["provider"] == "openai"
        assert data["total_tokens"] == 100
        assert data["mode"] == "query"
