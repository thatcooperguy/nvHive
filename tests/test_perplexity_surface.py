"""Perplexity on LiteLLM's Responses surface (``ProviderSpec.api_surface``).

Sonar Chat Completions retires 2026-09-27; the perplexity row now routes
through ``litellm.aresponses`` and its Agent API presets. These tests fake
``aresponses`` at the module boundary and pin the conversion into nvHive's
``CompletionResponse`` / ``StreamChunk`` shapes, the cost source, error
mapping, and that chat-surface specs are untouched.
"""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from nvh.providers.base import (
    AuthenticationError,
    FinishReason,
    Message,
    ProviderError,
    RateLimitError,
    Usage,
)
from nvh.providers.openai_compatible import (
    PROVIDER_SPECS,
    OpenAICompatibleProvider,
    _build_input,
    _responses_finish,
    _responses_text,
    _responses_usage,
)
from nvh.providers.specs import ProviderSpec

_ACOMPLETION = "nvh.providers.openai_compatible.litellm.acompletion"
_ARESPONSES = "nvh.providers.openai_compatible.litellm.aresponses"
_ASYNC_CLIENT = "nvh.providers.openai_compatible.httpx.AsyncClient"


def _perplexity(**kw) -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider(PROVIDER_SPECS["perplexity"], api_key="pplx-k", **kw)


def _response(
    text: str = "Hello from pplx",
    status: str = "completed",
    cost=None,
    model: str = "low",
    reason: str | None = None,
    output=None,
):
    """A ``ResponsesAPIResponse`` stand-in mirroring LiteLLM 1.99.0's field names."""
    if output is None:
        output = [
            # Perplexity emits search_results items ahead of the message.
            SimpleNamespace(type="search_results", results=[{"title": "t", "url": "u"}]),
            SimpleNamespace(
                type="message",
                content=[SimpleNamespace(type="output_text", text=text, annotations=[])],
            ),
        ]
    return SimpleNamespace(
        id="resp_1",
        status=status,
        model=model,
        output=output,
        error=None,
        incomplete_details=SimpleNamespace(reason=reason) if reason else None,
        usage=SimpleNamespace(input_tokens=12, output_tokens=7, total_tokens=19, cost=cost),
    )


async def _events(final_type: str = "response.completed", final=None):
    yield SimpleNamespace(type="response.created", response=_response(status="in_progress"))
    yield SimpleNamespace(type="response.output_item.added", output_index=0)
    yield SimpleNamespace(type="response.output_text.delta", delta="Hello ")
    yield SimpleNamespace(type="response.output_text.delta", delta="from pplx")
    yield SimpleNamespace(type="response.output_text.done", text="Hello from pplx")
    yield SimpleNamespace(type=final_type, response=final or _response(cost={"total_cost": 0.0053}))


# ---------------------------------------------------------------------------
# Spec table
# ---------------------------------------------------------------------------


def test_api_surface_defaults_to_chat_and_perplexity_opts_into_responses():
    assert ProviderSpec("x", "x/a", "x/b").api_surface == "chat"
    spec = PROVIDER_SPECS["perplexity"]
    assert spec.api_surface == "responses"
    assert spec.sunset_date is None and spec.sunset_note == ""
    assert (spec.default_model, spec.fallback_model) == (
        "perplexity/preset/low", "perplexity/preset/fast",
    )
    assert all(s.api_surface == "chat" for n, s in PROVIDER_SPECS.items() if n != "perplexity")


def test_preset_ids_route_to_perplexity_in_litellm():
    import litellm

    spec = PROVIDER_SPECS["perplexity"]
    assert spec.route("preset/low") == "perplexity/preset/low"
    for model in (spec.default_model, spec.fallback_model):
        assert litellm.get_llm_provider(model)[1] == "perplexity"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def test_build_input_drops_name_and_keeps_roles():
    items = _build_input([
        Message(role="user", content="hi", name="alice"),
        Message(role="assistant", content="yo"),
    ])
    assert items == [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "yo"}]


def test_text_extraction_accepts_dicts_and_objects_and_skips_non_message_items():
    resp = {
        "output": [
            {"type": "search_results", "results": []},
            {"type": "message", "content": [
                {"type": "output_text", "text": "a"},
                {"type": "refusal", "refusal": "no"},
                {"type": "output_text", "text": "b"},
            ]},
        ]
    }
    assert _responses_text(resp) == "ab"
    assert _responses_text(_response("obj")) == "obj"
    assert _responses_text(SimpleNamespace(output=None)) == ""


@pytest.mark.parametrize(
    ("cost", "expected"),
    [
        ({"currency": "USD", "total_cost": 0.0053}, Decimal("0.0053")),
        (0.0053, Decimal("0.0053")),
        (None, None),
        (True, None),
    ],
)
def test_usage_and_billed_cost_extraction(cost, expected):
    usage, billed = _responses_usage(_response(cost=cost))
    assert usage == Usage(input_tokens=12, output_tokens=7, total_tokens=19)
    assert billed == expected
    assert _responses_usage(SimpleNamespace(usage=None)) == (Usage(), None)


@pytest.mark.parametrize(
    ("status", "reason", "expected"),
    [
        ("completed", None, FinishReason.STOP),
        ("incomplete", "max_output_tokens", FinishReason.LENGTH),
        ("incomplete", "content_filter", FinishReason.CONTENT_FILTER),
        ("incomplete", None, FinishReason.LENGTH),
    ],
)
def test_finish_reason_from_status_and_incomplete_details(status, reason, expected):
    assert _responses_finish(_response(status=status, reason=reason)) == expected


# ---------------------------------------------------------------------------
# complete()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_complete_uses_aresponses_not_acompletion_and_shapes_the_request():
    p = _perplexity()
    with patch(_ARESPONSES, new=AsyncMock(return_value=_response(cost={"total_cost": 0.0053}))) as ar, \
            patch(_ACOMPLETION, new=AsyncMock()) as ac:
        resp = await p.complete(
            [Message(role="user", content="hi")],
            temperature=0.2, max_tokens=64, system_prompt="be brief",
        )
    ac.assert_not_awaited()
    assert ar.await_args.kwargs == {
        "input": [{"role": "user", "content": "hi"}],
        "instructions": "be brief",
        "temperature": 0.2,
        "max_output_tokens": 64,
        "timeout": 600,
        "model": "perplexity/preset/low",
        "api_key": "pplx-k",
    }
    assert resp.content == "Hello from pplx"
    assert resp.provider == "perplexity"
    assert resp.model == "low"  # litellm's reported model wins, as on the chat path
    assert resp.usage == Usage(input_tokens=12, output_tokens=7, total_tokens=19)
    assert resp.cost_usd == Decimal("0.0053")  # Perplexity's own usage.cost
    assert resp.finish_reason == FinishReason.STOP
    assert resp.latency_ms >= 0


@pytest.mark.asyncio
async def test_complete_without_system_prompt_sends_no_instructions_and_routes_bare_model():
    p = _perplexity()
    with patch(_ARESPONSES, new=AsyncMock(return_value=_response())) as ar:
        await p.complete([Message(role="user", content="hi")], model="preset/fast")
    kw = ar.await_args.kwargs
    assert "instructions" not in kw
    assert kw["model"] == "perplexity/preset/fast"


@pytest.mark.asyncio
async def test_complete_falls_back_to_cost_per_token_when_no_billed_cost():
    p = _perplexity()
    with patch(_ARESPONSES, new=AsyncMock(return_value=_response(cost=None))), \
            patch("litellm.cost_per_token", return_value=(0.001, 0.002)) as cpt:
        resp = await p.complete([Message(role="user", content="hi")])
    assert resp.cost_usd == Decimal("0.003")
    cpt.assert_called_once_with(model="perplexity/preset/low", prompt_tokens=12, completion_tokens=7)


@pytest.mark.asyncio
async def test_complete_maps_incomplete_to_length():
    p = _perplexity()
    fake = _response(status="incomplete", reason="max_output_tokens")
    with patch(_ARESPONSES, new=AsyncMock(return_value=fake)):
        resp = await p.complete([Message(role="user", content="hi")])
    assert resp.finish_reason == FinishReason.LENGTH


@pytest.mark.asyncio
async def test_complete_raises_on_failed_status():
    p = _perplexity()
    failed = _response(status="failed")
    failed.error = {"message": "preset unavailable", "type": "server_error"}
    with patch(_ARESPONSES, new=AsyncMock(return_value=failed)):
        with pytest.raises(ProviderError, match="preset unavailable") as ei:
            await p.complete([Message(role="user", content="hi")])
    assert ei.value.provider == "perplexity"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("exc_name", "message", "expected"),
    [
        ("AuthenticationError", "401 bad key", AuthenticationError),
        ("RateLimitError", "429 slow down", RateLimitError),
        ("APIConnectionError", "boom", ProviderError),
    ],
)
async def test_complete_maps_litellm_errors(exc_name, message, expected):
    exc = type(exc_name, (Exception,), {})(message)
    with patch(_ARESPONSES, new=AsyncMock(side_effect=exc)), \
            patch("nvh.providers.quota_info.get_quota_info", return_value=SimpleNamespace(upgrade_url="")), \
            patch("nvh.providers.quota_info.format_rate_limit_message", return_value="slow"), \
            patch("nvh.providers.quota_info.parse_retry_after", return_value=None):
        with pytest.raises(expected) as ei:
            await _perplexity().complete([Message(role="user", content="hi")])
    assert ei.value.provider == "perplexity"
    assert ei.value.original_error is exc


# ---------------------------------------------------------------------------
# stream()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stream_yields_deltas_then_final_with_usage_and_billed_cost():
    p = _perplexity()
    with patch(_ARESPONSES, new=AsyncMock(return_value=_events())) as ar, \
            patch(_ACOMPLETION, new=AsyncMock()) as ac:
        chunks = [c async for c in p.stream([Message(role="user", content="hi")], max_tokens=32)]
    ac.assert_not_awaited()
    assert ar.await_args.kwargs["stream"] is True
    assert ar.await_args.kwargs["max_output_tokens"] == 32
    assert [c.delta for c in chunks] == ["Hello ", "from pplx", ""]
    assert [c.is_final for c in chunks] == [False, False, True]
    assert chunks[-1].accumulated_content == "Hello from pplx"
    assert chunks[-1].usage == Usage(input_tokens=12, output_tokens=7, total_tokens=19)
    assert chunks[-1].cost_usd == Decimal("0.0053")
    assert chunks[-1].finish_reason == FinishReason.STOP
    assert all(c.provider == "perplexity" and c.model == "perplexity/preset/low" for c in chunks)


@pytest.mark.asyncio
async def test_stream_incomplete_event_is_final_with_length():
    final = _response(status="incomplete", reason="max_output_tokens", cost=0.01)
    with patch(_ARESPONSES, new=AsyncMock(return_value=_events("response.incomplete", final))):
        chunks = [c async for c in _perplexity().stream([Message(role="user", content="hi")])]
    assert chunks[-1].is_final and chunks[-1].finish_reason == FinishReason.LENGTH
    assert chunks[-1].cost_usd == Decimal("0.01")


@pytest.mark.asyncio
async def test_stream_accepts_str_enum_event_types():
    from litellm.types.llms.openai import ResponsesAPIStreamEvents as E

    async def events():
        yield SimpleNamespace(type=E.OUTPUT_TEXT_DELTA, delta="typed")
        yield SimpleNamespace(type=E.RESPONSE_COMPLETED, response=_response(cost=None))

    with patch(_ARESPONSES, new=AsyncMock(return_value=events())):
        chunks = [c async for c in _perplexity().stream([Message(role="user", content="hi")])]
    assert [c.delta for c in chunks] == ["typed", ""] and chunks[-1].is_final


@pytest.mark.asyncio
async def test_stream_without_terminal_event_estimates_usage():
    async def events():
        yield SimpleNamespace(type="response.output_text.delta", delta="partial")

    with patch(_ARESPONSES, new=AsyncMock(return_value=events())), \
            patch("litellm.cost_per_token", return_value=(0.0, 0.0)):
        chunks = [c async for c in _perplexity().stream([Message(role="user", content="hi")])]
    assert chunks[-1].is_final and chunks[-1].accumulated_content == "partial"
    assert chunks[-1].usage.output_tokens >= 1 and chunks[-1].cost_usd == Decimal("0")


@pytest.mark.asyncio
async def test_stream_failed_and_error_events_raise_provider_error():
    async def failed():
        yield SimpleNamespace(type="response.output_text.delta", delta="x")
        bad = _response(status="failed")
        bad.error = SimpleNamespace(message="quota exhausted")
        yield SimpleNamespace(type="response.failed", response=bad)

    async def error_event():
        yield SimpleNamespace(type="error", error=SimpleNamespace(message="stream broke", code="x"))

    for source, text in ((failed(), "quota exhausted"), (error_event(), "stream broke")):
        with patch(_ARESPONSES, new=AsyncMock(return_value=source)):
            with pytest.raises(ProviderError, match=text):
                async for _ in _perplexity().stream([Message(role="user", content="hi")]):
                    pass


@pytest.mark.asyncio
async def test_stream_maps_litellm_errors_at_open_and_mid_stream():
    exc = type("RateLimitError", (Exception,), {})("429")
    with patch(_ARESPONSES, new=AsyncMock(side_effect=exc)), \
            patch("nvh.providers.quota_info.format_rate_limit_message", return_value="slow"), \
            patch("nvh.providers.quota_info.parse_retry_after", return_value=None):
        with pytest.raises(RateLimitError):
            async for _ in _perplexity().stream([Message(role="user", content="hi")]):
                pass

    async def explode():
        yield SimpleNamespace(type="response.output_text.delta", delta="x")
        raise type("ServiceUnavailableError", (Exception,), {})("503")

    with patch(_ARESPONSES, new=AsyncMock(return_value=explode())):
        with pytest.raises(ProviderError) as ei:
            async for _ in _perplexity().stream([Message(role="user", content="hi")]):
                pass
    assert ei.value.provider == "perplexity"


# ---------------------------------------------------------------------------
# Zero-cost specs and the chat surface stay as they were
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_zero_cost_responses_spec_ignores_billed_cost():
    spec = ProviderSpec("freeco", "freeco/a", "freeco/b", zero_cost=True, api_surface="responses")
    p = OpenAICompatibleProvider(spec, api_key="k")
    with patch(_ARESPONSES, new=AsyncMock(return_value=_response(cost={"total_cost": 9.9}))):
        resp = await p.complete([Message(role="user", content="hi")])
    assert resp.cost_usd == Decimal("0")


@pytest.mark.asyncio
async def test_chat_surface_spec_still_uses_acompletion():
    p = OpenAICompatibleProvider(PROVIDER_SPECS["groq"], api_key="k")
    chat = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="ok"), finish_reason="stop")],
        usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1, total_tokens=2),
        model="served",
    )
    with patch(_ACOMPLETION, new=AsyncMock(return_value=chat)) as ac, \
            patch(_ARESPONSES, new=AsyncMock()) as ar:
        resp = await p.complete([Message(role="user", content="hi")])
    ar.assert_not_awaited()
    assert ac.await_args.kwargs["model"] == "groq/openai/gpt-oss-120b"
    assert resp.content == "ok"


# ---------------------------------------------------------------------------
# health_check()
# ---------------------------------------------------------------------------


def _fake_http(status_code: int):
    class Client:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url, headers=None):
            return SimpleNamespace(status_code=status_code, json=lambda: {"data": []})

    return Client


@pytest.mark.asyncio
async def test_health_ping_uses_the_responses_surface_when_models_is_missing():
    p = _perplexity()
    with patch(_ASYNC_CLIENT, _fake_http(404)), \
            patch(_ARESPONSES, new=AsyncMock(return_value=_response())) as ar, \
            patch(_ACOMPLETION, new=AsyncMock()) as ac:
        status = await p.health_check()
    assert status.healthy is True
    ac.assert_not_awaited()
    ar.assert_awaited_once_with(
        input="ping", max_output_tokens=16, timeout=15,
        model="perplexity/preset/low", api_key="pplx-k",
    )


@pytest.mark.asyncio
async def test_health_ping_failure_is_unhealthy():
    with patch(_ASYNC_CLIENT, _fake_http(404)), \
            patch(_ARESPONSES, new=AsyncMock(side_effect=Exception("down"))):
        status = await _perplexity().health_check()
    assert status.healthy is False and "down" in status.error


# ---------------------------------------------------------------------------
# Multimodal content parts -> Responses input parts
# ---------------------------------------------------------------------------


def test_build_input_converts_chat_content_parts_to_responses_parts():
    # The shape nvh/api/server.py builds for image attachments.
    parts = [
        {"type": "text", "text": "what is in this picture?"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
        {"type": "image_url", "image_url": {"url": "https://x.example/p.png", "detail": "high"}},
        {"type": "image_url", "image_url": "https://y.example/q.png"},
        "a bare string part",
        {"type": "input_text", "text": "already converted"},
    ]
    items = _build_input([Message(role="user", content=parts)])
    assert items == [{"role": "user", "content": [
        {"type": "input_text", "text": "what is in this picture?"},
        {"type": "input_image", "image_url": "data:image/png;base64,AAAA", "detail": "auto"},
        {"type": "input_image", "image_url": "https://x.example/p.png", "detail": "high"},
        {"type": "input_image", "image_url": "https://y.example/q.png", "detail": "auto"},
        {"type": "input_text", "text": "a bare string part"},
        {"type": "input_text", "text": "already converted"},
    ]}]


def test_build_input_uses_output_text_for_assistant_parts():
    items = _build_input([Message(role="assistant", content=[{"type": "text", "text": "earlier answer"}])])
    assert items[0]["content"] == [{"type": "output_text", "text": "earlier answer"}]


@pytest.mark.asyncio
async def test_complete_sends_converted_parts_not_chat_parts():
    p = _perplexity()
    content = [
        {"type": "text", "text": "describe"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
    ]
    with patch(_ARESPONSES, new=AsyncMock(return_value=_response())) as ar:
        await p.complete([Message(role="user", content=content)])
    sent = ar.await_args.kwargs["input"][0]["content"]
    assert [part["type"] for part in sent] == ["input_text", "input_image"]


# ---------------------------------------------------------------------------
# No usage.cost and no LiteLLM price: the capabilities.yaml rates, not $0
# ---------------------------------------------------------------------------


def test_litellm_has_no_price_for_the_presets():
    import litellm

    for model in ("perplexity/preset/low", "perplexity/preset/fast"):
        with pytest.raises(Exception):
            litellm.cost_per_token(model=model, prompt_tokens=1, completion_tokens=1)


@pytest.mark.asyncio
async def test_complete_without_billed_cost_prices_at_the_catalog_rates():
    # 12 in at $3.00/M + 7 out at $15.00/M (capabilities.yaml, perplexity/preset/low).
    p = _perplexity()
    with patch(_ARESPONSES, new=AsyncMock(return_value=_response(cost=None))):
        resp = await p.complete([Message(role="user", content="hi")])
    assert resp.cost_usd == Decimal("0.000141")


@pytest.mark.asyncio
async def test_stream_final_without_billed_cost_prices_at_the_catalog_rates():
    final = _response(cost=None, model="fast")
    with patch(_ARESPONSES, new=AsyncMock(return_value=_events(final=final))):
        chunks = [
            c async for c in _perplexity().stream([Message(role="user", content="hi")], model="preset/fast")
        ]
    # 12 in + 7 out at $1.00/M each (perplexity/preset/fast).
    assert chunks[-1].cost_usd == Decimal("0.000019")


# ---------------------------------------------------------------------------
# capabilities.yaml: only the Agent API presets remain for perplexity
# ---------------------------------------------------------------------------


def test_catalog_lists_only_the_agent_api_presets_for_perplexity():
    from pathlib import Path

    import yaml

    from nvh.cli.setup import rename_retired_model

    root = Path(__file__).resolve().parents[1]
    catalog = yaml.safe_load((root / "nvh" / "config" / "capabilities.yaml").read_text(encoding="utf-8"))
    rows = {k for k, v in catalog["models"].items() if v.get("provider") == "perplexity"}
    assert rows == {"perplexity/preset/low", "perplexity/preset/fast"}
    assert not any("sonar" in k for k in catalog["models"])
    # Every retired Sonar id a 0.42 config may still hold renames onto a row that exists.
    for legacy in (
        "perplexity/llama-3.1-sonar-large-128k-online",
        "perplexity/llama-3.1-sonar-small-128k-online",
        "perplexity/sonar-pro",
        "perplexity/sonar",
    ):
        assert rename_retired_model("perplexity", legacy) in rows
