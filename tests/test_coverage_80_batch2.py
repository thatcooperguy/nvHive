"""Batch-2 coverage tests for openai_provider, registry, webhooks, code_graph."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

# ── Module 1: nvh/providers/openai_provider.py ─────────────────────
from nvh.providers.base import (
    AuthenticationError,
    ContentFilterError,
    InvalidRequestError,
    Message,
    ModelNotFoundError,
    ProviderError,
    ProviderUnavailableError,
    RateLimitError,
    TokenLimitError,
    Usage,
)
from nvh.providers.openai_provider import _build_messages, _calc_cost, _map_error


class TestBuildMessages:
    def test_without_system_prompt(self) -> None:
        msgs = [Message(role="user", content="hello")]
        result = _build_messages(msgs)
        assert len(result) == 1
        assert result[0] == {"role": "user", "content": "hello"}

    def test_with_system_prompt(self) -> None:
        msgs = [Message(role="user", content="hello")]
        result = _build_messages(msgs, system_prompt="You are helpful.")
        assert len(result) == 2
        assert result[0] == {"role": "system", "content": "You are helpful."}
        assert result[1] == {"role": "user", "content": "hello"}

    def test_with_name_field(self) -> None:
        msgs = [Message(role="user", content="hi", name="alice")]
        result = _build_messages(msgs)
        assert result[0]["name"] == "alice"

    def test_without_name_field(self) -> None:
        msgs = [Message(role="user", content="hi")]
        result = _build_messages(msgs)
        assert "name" not in result[0]


class TestCalcCost:
    def test_returns_decimal_on_success(self) -> None:
        usage = Usage(input_tokens=100, output_tokens=50, total_tokens=150)
        with patch("litellm.completion_cost", return_value=0.001234):
            cost = _calc_cost("gpt-4o", usage)
        assert isinstance(cost, Decimal)
        assert cost == Decimal("0.001234")

    def test_returns_zero_on_exception(self) -> None:
        usage = Usage(input_tokens=10, output_tokens=5, total_tokens=15)
        with patch("litellm.completion_cost", side_effect=Exception("nope")):
            cost = _calc_cost("gpt-4o", usage)
        assert cost == Decimal("0")


class TestMapError:
    @patch("nvh.providers.quota_info.get_quota_info")
    def test_authentication_error(self, mock_qi: MagicMock) -> None:
        mock_qi.return_value = MagicMock(upgrade_url="https://example.com")
        exc = type("AuthenticationError", (Exception,), {})("bad key")
        result = _map_error(exc, "openai")
        assert isinstance(result, AuthenticationError)

    @patch("nvh.providers.quota_info.format_rate_limit_message", return_value="slow down")
    @patch("nvh.providers.quota_info.parse_retry_after", return_value=5)
    def test_rate_limit_error(self, _pr: MagicMock, _fr: MagicMock) -> None:
        exc = type("RateLimitError", (Exception,), {})("429 too many")
        result = _map_error(exc, "openai")
        assert isinstance(result, RateLimitError)

    def test_invalid_request_token_limit(self) -> None:
        exc = type("InvalidRequestError", (Exception,), {})("context_length exceeded")
        result = _map_error(exc, "openai")
        assert isinstance(result, TokenLimitError)

    def test_invalid_request_content_filter(self) -> None:
        exc = type("InvalidRequestError", (Exception,), {})("content_filter triggered")
        result = _map_error(exc, "openai")
        assert isinstance(result, ContentFilterError)

    def test_invalid_request_generic(self) -> None:
        exc = type("InvalidRequestError", (Exception,), {})("something else 400")
        result = _map_error(exc, "openai")
        assert isinstance(result, InvalidRequestError)

    def test_not_found_error(self) -> None:
        exc = type("NotFoundError", (Exception,), {})("404 model gone")
        result = _map_error(exc, "openai")
        assert isinstance(result, ModelNotFoundError)

    def test_service_unavailable_error(self) -> None:
        exc = type("ServiceUnavailableError", (Exception,), {})("503 down")
        result = _map_error(exc, "openai")
        assert isinstance(result, ProviderUnavailableError)

    def test_generic_exception(self) -> None:
        exc = Exception("something unknown happened")
        result = _map_error(exc, "openai")
        assert isinstance(result, ProviderError)
        assert not isinstance(result, AuthenticationError)


# ── Module 2: nvh/providers/registry.py ─────────────────────────────

from nvh.providers.base import ModelInfo
from nvh.providers.registry import ProviderRegistry


class TestProviderRegistry:
    def test_register_and_get(self) -> None:
        reg = ProviderRegistry()
        mock_prov = MagicMock()
        reg.register("test", mock_prov)
        assert reg.get("test") is mock_prov

    def test_get_nonexistent_raises(self) -> None:
        reg = ProviderRegistry()
        with pytest.raises(KeyError, match="not registered"):
            reg.get("nope")

    def test_register_duplicate_overwrites(self) -> None:
        reg = ProviderRegistry()
        p1, p2 = MagicMock(), MagicMock()
        reg.register("x", p1)
        reg.register("x", p2)
        assert reg.get("x") is p2

    def test_has(self) -> None:
        reg = ProviderRegistry()
        assert not reg.has("foo")
        reg.register("foo", MagicMock())
        assert reg.has("foo")

    def test_list_models_no_filter(self) -> None:
        reg = ProviderRegistry()
        reg._model_catalog["m1"] = ModelInfo(model_id="m1", provider="a")
        reg._model_catalog["m2"] = ModelInfo(model_id="m2", provider="b")
        models = reg.list_models()
        assert len(models) == 2

    def test_list_models_with_provider_filter(self) -> None:
        reg = ProviderRegistry()
        reg._model_catalog["m1"] = ModelInfo(model_id="m1", provider="a")
        reg._model_catalog["m2"] = ModelInfo(model_id="m2", provider="b")
        models = reg.list_models(provider="a")
        assert len(models) == 1
        assert models[0].model_id == "m1"


# ── Module 3: nvh/core/webhooks.py ──────────────────────────────────

from nvh.core.webhooks import WebhookConfig, WebhookManager, _sign_payload


class TestWebhookManager:
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

        from nvh.core.webhooks import WebhookPayload

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

        from nvh.core.webhooks import WebhookPayload

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


# ── Module 4: nvh/core/code_graph.py (additional tests) ────────────

from nvh.core.code_graph import (
    _extract_imports,
    _extract_symbols,
    build_import_graph,
    format_context_for_agent,
)


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


class TestCodeGraphExtras:
    def test_relative_import(self) -> None:
        imports = _extract_imports("from . import utils\n", "pkg/mod.py")
        assert "pkg/__init__.py" in imports

    def test_relative_import_with_name(self) -> None:
        imports = _extract_imports("from .helpers import foo\n", "pkg/mod.py")
        assert "pkg/helpers.py" in imports

    def test_async_def_extraction(self) -> None:
        source = "async def my_handler():\n    pass\n"
        symbols = _extract_symbols(source)
        assert "my_handler" in symbols

    def test_no_imports(self) -> None:
        imports = _extract_imports("x = 1\n", "standalone.py")
        assert imports == []

    def test_imported_by_reverse_edges(self, tmp_path: Path) -> None:
        _write(tmp_path / "base.py", "x = 1\n")
        _write(tmp_path / "child.py", "from base import x\n")
        graph = build_import_graph(tmp_path)
        assert "child.py" in graph.nodes["base.py"].imported_by

    def test_format_context_depth_zero(self, tmp_path: Path) -> None:
        _write(tmp_path / "a.py", "from b import y\n")
        _write(tmp_path / "b.py", "y = 1\n")
        graph = build_import_graph(tmp_path)
        ctx = format_context_for_agent(graph, "a.py", depth=0)
        # depth=0 means no related files beyond the target itself
        assert "a.py" in ctx
        assert "Related files" not in ctx

    def test_format_context_missing_file(self) -> None:
        from nvh.core.code_graph import ImportGraph

        graph = ImportGraph()
        ctx = format_context_for_agent(graph, "missing.py")
        assert "not found in graph" in ctx
