"""Native function calling degrades (0.44 D3, invariants I10/I11).

  - ``tools=`` reaches LiteLLM only when the resolved model supports function
    calling (the shipped catalog's ``supports_tools`` / a wave-2
    ``supports_function_calling`` flag), and NEVER on Perplexity's Responses
    surface;
  - LiteLLM's ``tool_calls`` are mapped back on ``complete()`` and assembled
    from deltas on ``stream()`` (final chunk);
  - Ollama sends ``tools`` to ``/api/chat`` only when ``/api/show`` lists the
    ``tools`` capability, and maps ``message.tool_calls`` back;
  - the text protocol ``TOOL_CALL: {json}`` is the fallback everywhere and the
    deprecated fenced form is still parsed;
  - the Wizard chat asks the provider before sending ``tools=`` and reads both
    channels.

Hermetic: no network — ``litellm.acompletion`` / ``aresponses`` and Ollama's
``httpx.AsyncClient`` are replaced; the catalog is the in-package YAML.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import litellm
import pytest

from nvh.core.tools import (
    Tool,
    ToolRegistry,
    normalize_tool_calls,
    parse_tool_calls,
    provider_supports_native_tools,
    wire_tool_calls,
)
from nvh.integrations.wizard import chat as chat_mod
from nvh.providers import openai_compatible as oc
from nvh.providers.base import FinishReason, Message
from nvh.providers.ollama_provider import OllamaProvider
from nvh.providers.specs import PROVIDER_SPECS


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch, tmp_path):
    monkeypatch.setenv("NVH_HOME", str(tmp_path))
    yield
    # Process singleton reset hook (I12): the catalog cache (a test may have
    # swapped ``_catalog`` for a stub; the real one is back once monkeypatch
    # unwinds, so only reset what is cached now).
    reset = getattr(oc._catalog, "cache_clear", None)
    if reset is not None:
        reset()


def _spec(name: str):
    specs = PROVIDER_SPECS
    if isinstance(specs, dict):
        return specs[name]
    return next(s for s in specs if s.name == name)


READ_FILE_TOOL = Tool(
    name="read_file", description="Read a file",
    parameters={"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
    handler=AsyncMock(),
).as_openai_tool()


def _model_response(content: str | None, tool_calls: list[dict[str, Any]] | None, finish: str = "stop"):
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return litellm.ModelResponse(
        choices=[{"message": message, "finish_reason": finish, "index": 0}],
        model="gpt-4o",
        usage={"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7},
    )


# ───────────────────────────────────────────────────────────────────────────
# The gate: catalog flag, responses surface
# ───────────────────────────────────────────────────────────────────────────


def test_catalog_decides_function_calling_support() -> None:
    assert oc.model_supports_function_calling("gpt-4o") is True
    assert oc.model_supports_function_calling("groq/llama-3.3-70b-versatile") is True
    # The Perplexity presets are catalogued without tools.
    assert oc.model_supports_function_calling("perplexity/preset/low") is False
    assert oc.model_supports_function_calling("") is False


def test_wave2_supports_function_calling_flag_wins_over_supports_tools(monkeypatch) -> None:
    """When F2's LiteLLM-derived flag lands on ModelInfo it is the authority."""
    info = SimpleNamespace(supports_function_calling=False, supports_tools=True)
    monkeypatch.setattr(oc, "_catalog", lambda: SimpleNamespace(get_model_info=lambda m: info))
    assert oc.model_supports_function_calling("anything/at-all") is False
    info.supports_function_calling = True
    assert oc.model_supports_function_calling("anything/at-all") is True


def test_model_without_a_row_falls_back_to_litellm_table(monkeypatch) -> None:
    monkeypatch.setattr(oc, "_catalog", lambda: SimpleNamespace(get_model_info=lambda m: None))
    monkeypatch.setattr(oc.litellm, "supports_function_calling", lambda m: m == "vendor/tooly")
    assert oc.model_supports_function_calling("vendor/tooly") is True
    assert oc.model_supports_function_calling("vendor/plain") is False


@pytest.mark.asyncio
async def test_supports_tools_is_false_on_the_responses_surface() -> None:
    perplexity = oc.OpenAICompatibleProvider(_spec("perplexity"), api_key="pplx-test")
    assert perplexity._responses is True
    assert await perplexity.supports_tools() is False
    assert await perplexity.supports_tools("perplexity/preset/low") is False
    openai = oc.OpenAICompatibleProvider(_spec("openai"), api_key="sk-test")
    assert await openai.supports_tools("gpt-4o") is True


# ───────────────────────────────────────────────────────────────────────────
# complete(): tools sent only when supported, tool_calls mapped back
# ───────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_complete_sends_tools_and_maps_tool_calls_back() -> None:
    provider = oc.OpenAICompatibleProvider(_spec("openai"), api_key="sk-test")
    fake = AsyncMock(return_value=_model_response(
        None,
        [{"id": "call_1", "type": "function", "function": {"name": "read_file", "arguments": '{"path": "a.py"}'}}],
        finish="tool_calls",
    ))
    with patch.object(oc.litellm, "acompletion", fake):
        response = await provider.complete(
            [Message(role="user", content="read a.py")], model="gpt-4o",
            tools=[READ_FILE_TOOL], tool_choice="auto",
        )
    sent = fake.call_args.kwargs
    assert sent["tools"] == [READ_FILE_TOOL] and sent["tool_choice"] == "auto"
    assert response.content == ""
    assert response.finish_reason == FinishReason.TOOL_CALLS
    assert response.tool_calls == [
        {"id": "call_1", "type": "function", "function": {"name": "read_file", "arguments": '{"path": "a.py"}'}},
    ]
    # The normalizer parses the arguments for the loops.
    assert normalize_tool_calls(response.tool_calls) == [{"id": "call_1", "name": "read_file", "arguments": {"path": "a.py"}}]


@pytest.mark.asyncio
async def test_complete_drops_tools_for_a_model_without_function_calling(monkeypatch) -> None:
    provider = oc.OpenAICompatibleProvider(_spec("openai"), api_key="sk-test")
    monkeypatch.setattr(oc, "model_supports_function_calling", lambda model: False)
    fake = AsyncMock(return_value=_model_response('TOOL_CALL: {"name": "read_file", "arguments": {"path": "a.py"}}', None))
    with patch.object(oc.litellm, "acompletion", fake):
        response = await provider.complete(
            [Message(role="user", content="hi")], model="gpt-4o", tools=[READ_FILE_TOOL], tool_choice="auto",
        )
    assert "tools" not in fake.call_args.kwargs and "tool_choice" not in fake.call_args.kwargs
    assert response.tool_calls is None and response.finish_reason == FinishReason.STOP
    # The text protocol is what the caller falls back to.
    _text, calls = parse_tool_calls(response.content)
    assert calls == [{"name": "read_file", "arguments": {"path": "a.py"}}]


@pytest.mark.asyncio
async def test_tools_never_reach_the_responses_surface() -> None:
    """I11: Perplexity's Agent API gets no ``tools`` whatever the caller passed."""
    provider = oc.OpenAICompatibleProvider(_spec("perplexity"), api_key="pplx-test")
    response = SimpleNamespace(
        status="completed", model="perplexity/preset/low",
        output=[{"type": "message", "content": [{"type": "output_text", "text": "answer"}]}],
        usage={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
    )
    fake = AsyncMock(return_value=response)
    with patch.object(oc.litellm, "aresponses", fake):
        out = await provider.complete(
            [Message(role="user", content="hi")], tools=[READ_FILE_TOOL], tool_choice="required",
        )
    assert "tools" not in fake.call_args.kwargs and "tool_choice" not in fake.call_args.kwargs
    assert out.content == "answer" and out.tool_calls is None

    async def events():
        yield SimpleNamespace(type="response.output_text.delta", delta="a")
        yield SimpleNamespace(type="response.completed", response=response)

    fake_stream = AsyncMock(return_value=events())
    with patch.object(oc.litellm, "aresponses", fake_stream):
        chunks = [c async for c in provider.stream([Message(role="user", content="hi")], tools=[READ_FILE_TOOL])]
    assert "tools" not in fake_stream.call_args.kwargs
    assert chunks[-1].is_final and chunks[-1].tool_calls is None


@pytest.mark.asyncio
async def test_complete_without_tools_sends_none_and_replays_tool_messages() -> None:
    provider = oc.OpenAICompatibleProvider(_spec("openai"), api_key="sk-test")
    fake = AsyncMock(return_value=_model_response("ok", None))
    wire = [{"id": "c1", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}]
    with patch.object(oc.litellm, "acompletion", fake):
        await provider.complete([
            Message(role="assistant", content="", tool_calls=wire),
            Message(role="tool", content="file body", tool_call_id="c1"),
        ], model="gpt-4o")
    sent = fake.call_args.kwargs
    assert "tools" not in sent
    assert sent["messages"][0]["tool_calls"] == wire and sent["messages"][1]["tool_call_id"] == "c1"


# ───────────────────────────────────────────────────────────────────────────
# stream(): deltas assembled on the final chunk
# ───────────────────────────────────────────────────────────────────────────


def _delta_chunk(content: str = "", tool_calls: list[dict[str, Any]] | None = None, finish: str | None = None):
    delta = SimpleNamespace(content=content, tool_calls=None)
    if tool_calls:
        delta.tool_calls = [
            SimpleNamespace(
                index=tc.get("index", 0), id=tc.get("id"),
                function=SimpleNamespace(name=tc.get("name"), arguments=tc.get("arguments")),
            )
            for tc in tool_calls
        ]
    return SimpleNamespace(choices=[SimpleNamespace(delta=delta, finish_reason=finish)], usage=None)


async def _agen(items):
    for item in items:
        yield item


@pytest.mark.asyncio
async def test_stream_sends_tools_and_assembles_tool_calls_from_deltas() -> None:
    provider = oc.OpenAICompatibleProvider(_spec("openai"), api_key="sk-test")
    chunks = [
        _delta_chunk("Let me "),
        _delta_chunk("look."),
        _delta_chunk(tool_calls=[{"index": 0, "id": "call_9", "name": "read_file", "arguments": '{"pa'}]),
        _delta_chunk(tool_calls=[{"index": 0, "arguments": 'th": "a.py"}'}]),
        _delta_chunk(tool_calls=[{"index": 1, "id": "call_10", "name": "list_files", "arguments": "{}"}]),
        _delta_chunk(finish="tool_calls"),
    ]
    fake = AsyncMock(return_value=_agen(chunks))
    with patch.object(oc.litellm, "acompletion", fake):
        out = [c async for c in provider.stream(
            [Message(role="user", content="go")], model="gpt-4o", tools=[READ_FILE_TOOL], tool_choice="auto",
        )]
    assert fake.call_args.kwargs["tools"] == [READ_FILE_TOOL] and fake.call_args.kwargs["stream"] is True
    assert "".join(c.delta for c in out) == "Let me look."
    assert all(c.tool_calls is None for c in out[:-1])
    final = out[-1]
    assert final.is_final and final.finish_reason == FinishReason.TOOL_CALLS
    assert final.tool_calls == [
        {"id": "call_9", "type": "function", "function": {"name": "read_file", "arguments": '{"path": "a.py"}'}},
        {"id": "call_10", "type": "function", "function": {"name": "list_files", "arguments": "{}"}},
    ]


@pytest.mark.asyncio
async def test_stream_closes_itself_when_tool_calls_arrive_without_a_finish_reason() -> None:
    provider = oc.OpenAICompatibleProvider(_spec("openai"), api_key="sk-test")
    chunks = [_delta_chunk(tool_calls=[{"index": 0, "id": "c", "name": "read_file", "arguments": "{}"}])]
    with patch.object(oc.litellm, "acompletion", AsyncMock(return_value=_agen(chunks))):
        out = [c async for c in provider.stream([Message(role="user", content="go")], model="gpt-4o", tools=[READ_FILE_TOOL])]
    assert out[-1].is_final and out[-1].tool_calls[0]["function"]["name"] == "read_file"


@pytest.mark.asyncio
async def test_stream_without_native_support_carries_no_tool_calls(monkeypatch) -> None:
    provider = oc.OpenAICompatibleProvider(_spec("openai"), api_key="sk-test")
    monkeypatch.setattr(oc, "model_supports_function_calling", lambda model: False)
    chunks = [_delta_chunk('TOOL_CALL: {"name": "read_file", "arguments": {}}'), _delta_chunk(finish="stop")]
    fake = AsyncMock(return_value=_agen(chunks))
    with patch.object(oc.litellm, "acompletion", fake):
        out = [c async for c in provider.stream([Message(role="user", content="go")], model="gpt-4o", tools=[READ_FILE_TOOL])]
    assert "tools" not in fake.call_args.kwargs
    assert out[-1].is_final and out[-1].tool_calls is None
    assert parse_tool_calls(out[-1].accumulated_content)[1] == [{"name": "read_file", "arguments": {}}]


# ───────────────────────────────────────────────────────────────────────────
# Ollama: /api/show capability gate, /api/chat tools, tool_calls mapped back
# ───────────────────────────────────────────────────────────────────────────


class _FakeResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload
        self.status_code = 200

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return self._payload


class _FakeClient:
    """``httpx.AsyncClient`` double: answers ``/api/show`` and ``/api/chat`` from a table."""

    def __init__(self, answers: dict[str, dict[str, Any]], calls: list[dict[str, Any]]) -> None:
        self._answers, self._calls = answers, calls

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def post(self, url: str, json: dict[str, Any] | None = None, timeout: Any = None):
        path = "/api/" + url.rsplit("/api/", 1)[1]
        self._calls.append({"path": path, "json": json})
        return _FakeResponse(self._answers[path])


@pytest.mark.asyncio
async def test_ollama_supports_tools_reads_the_show_capabilities_and_caches(monkeypatch) -> None:
    calls: list[dict[str, Any]] = []
    answers = {"/api/show": {"capabilities": ["completion", "tools"]}}
    monkeypatch.setattr("nvh.providers.ollama_provider.httpx.AsyncClient", lambda *a, **k: _FakeClient(answers, calls))
    provider = OllamaProvider()
    assert await provider.supports_tools("ollama/qwen3:8b") is True
    assert await provider.supports_tools("qwen3:8b") is True  # cached per bare tag
    assert calls == [{"path": "/api/show", "json": {"model": "qwen3:8b"}}]
    answers["/api/show"] = {"capabilities": ["completion"]}
    assert await provider.supports_tools("gemma3:4b") is False

    def boom(*a, **k):
        raise RuntimeError("daemon down")

    monkeypatch.setattr("nvh.providers.ollama_provider.httpx.AsyncClient", boom)
    assert await OllamaProvider().supports_tools("llama3.1") is False


@pytest.mark.asyncio
async def test_ollama_complete_sends_tools_only_when_capable_and_maps_tool_calls(monkeypatch) -> None:
    calls: list[dict[str, Any]] = []
    answers = {
        "/api/show": {"capabilities": ["completion", "tools"]},
        "/api/chat": {"message": {"role": "assistant", "content": "", "tool_calls": [
            {"function": {"name": "read_file", "arguments": {"path": "a.py"}}},
        ]}},
    }
    monkeypatch.setattr("nvh.providers.ollama_provider.httpx.AsyncClient", lambda *a, **k: _FakeClient(answers, calls))
    provider = OllamaProvider()
    monkeypatch.setattr(provider, "_options", AsyncMock(return_value={"temperature": 0.0, "num_predict": 8}))
    response = await provider.complete([Message(role="user", content="read a.py")], model="qwen3:8b", tools=[READ_FILE_TOOL])
    chat = [c for c in calls if c["path"] == "/api/chat"][0]
    assert chat["json"]["tools"] == [READ_FILE_TOOL]
    assert response.finish_reason == FinishReason.TOOL_CALLS
    assert response.tool_calls == [
        {"id": "call_1", "type": "function", "function": {"name": "read_file", "arguments": json.dumps({"path": "a.py"})}},
    ]

    # Without the capability the request carries no tools and text is all that comes back.
    calls.clear()
    answers["/api/show"] = {"capabilities": ["completion"]}
    answers["/api/chat"] = {"message": {"role": "assistant", "content": 'TOOL_CALL: {"name": "read_file", "arguments": {}}'}}
    provider = OllamaProvider()
    monkeypatch.setattr(provider, "_options", AsyncMock(return_value={}))
    response = await provider.complete([Message(role="user", content="hi")], model="gemma3:4b", tools=[READ_FILE_TOOL])
    chat = [c for c in calls if c["path"] == "/api/chat"][0]
    assert "tools" not in chat["json"]
    assert response.tool_calls is None and "TOOL_CALL" in response.content


@pytest.mark.asyncio
async def test_ollama_stream_delivers_tool_calls_on_the_final_chunk(monkeypatch) -> None:
    provider = OllamaProvider()
    monkeypatch.setattr(provider, "supports_tools", AsyncMock(return_value=True))
    seen: dict[str, Any] = {}

    async def fake_direct_stream(messages, model, temperature, max_tokens, *, tools=None):
        seen["tools"] = tools
        from nvh.providers.base import StreamChunk
        yield StreamChunk(delta="", is_final=True, accumulated_content="", model=model, provider="ollama",
                          finish_reason=FinishReason.TOOL_CALLS,
                          tool_calls=[{"id": "call_1", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}])

    monkeypatch.setattr(provider, "_direct_stream", fake_direct_stream)
    chunks = [c async for c in provider.stream([Message(role="user", content="go")], model="qwen3:8b", tools=[READ_FILE_TOOL])]
    assert seen["tools"] == [READ_FILE_TOOL]
    assert chunks[-1].tool_calls[0]["function"]["name"] == "read_file"


def test_ollama_wire_tool_calls_shape() -> None:
    assert OllamaProvider._wire_tool_calls([{"function": {"name": "a", "arguments": {"x": 1}}}, {"nope": 1}]) == [
        {"id": "call_1", "type": "function", "function": {"name": "a", "arguments": '{"x": 1}'}},
    ]
    assert OllamaProvider._wire_tool_calls(None) == []


# ───────────────────────────────────────────────────────────────────────────
# The shared helpers and the Wizard chat
# ───────────────────────────────────────────────────────────────────────────


def test_normalize_and_wire_round_trip() -> None:
    calls = normalize_tool_calls([
        {"id": "c1", "type": "function", "function": {"name": "a", "arguments": '{"x": 1}'}},
        {"name": "b", "arguments": {"y": 2}},
        {"tool": "c", "args": {"z": 3}},
        SimpleNamespace(id="c4", function=SimpleNamespace(name="d", arguments="not json")),
        {"function": {"arguments": "{}"}},  # nameless: skipped
    ])
    assert calls == [
        {"id": "c1", "name": "a", "arguments": {"x": 1}},
        {"id": "", "name": "b", "arguments": {"y": 2}},
        {"id": "", "name": "c", "arguments": {"z": 3}},
        {"id": "c4", "name": "d", "arguments": {}},
    ]
    assert wire_tool_calls(calls)[1] == {"id": "call_2", "type": "function", "function": {"name": "b", "arguments": '{"y": 2}'}}
    assert normalize_tool_calls(None) == [] and normalize_tool_calls("TOOL_CALL: {}") == []


def test_fenced_form_is_still_parsed_by_the_agent_loop_but_the_line_is_the_protocol() -> None:
    text, calls = parse_tool_calls(
        'a\n```tool_call\n{"tool": "x", "args": {"k": 1}}\n```\nTOOL_CALL: {"name": "y", "arguments": {}}', legacy=True,
    )
    assert calls == [{"name": "y", "arguments": {}}, {"name": "x", "arguments": {"k": 1}}]
    assert text == "a"


def test_marker_only_by_default_prose_and_quoted_objects_are_never_calls() -> None:
    """The Wizard path: only ``TOOL_CALL: {json}`` is a call. A prose mention of the
    marker stays in the answer, and a pasted / quoted ``{"tool": …, "args": …}`` or
    fenced block never runs a tool. The agent loop alone keeps the deprecated
    forms for one release (``legacy=True``)."""
    from nvh.core.agent_loop import _extract_tool_calls as agent_extract

    prose = "I would then write TOOL_CALL: followed by JSON.\nAnything else?"
    assert parse_tool_calls(prose) == (prose, [])
    assert chat_mod._extract_tool_calls(prose) == (prose, [])
    assert chat_mod._extract_tool_calls("The Tool_Call: syntax is documented below.") == ("The Tool_Call: syntax is documented below.", [])

    quoted = 'Here is the log line you pasted:\n{"tool": "refresh_models", "args": {}} was the old shape.'
    assert chat_mod._extract_tool_calls(quoted) == (quoted, [])
    fenced = 'a\n```tool_call\n{"tool": "refresh_models", "args": {}}\n```'
    assert chat_mod._extract_tool_calls(fenced) == (fenced, [])
    assert agent_extract(quoted) == [{"tool": "refresh_models", "args": {}}]
    assert agent_extract(fenced) == [{"tool": "refresh_models", "args": {}}]

    # A marker *with* a malformed or unbalanced object is still stripped and dropped.
    assert parse_tool_calls('All good.\nTOOL_CALL: {"name": "refresh_models", "arguments": {oops}}') == ("All good.", [])
    assert parse_tool_calls('x\nTOOL_CALL: {"name": "a", "arguments": {"k": 1}') == ("x", [])
    assert parse_tool_calls('x\ntool_call: {"name": "a", "arguments": {}}') == ("x", [{"name": "a", "arguments": {}}])


def test_every_adapter_writes_tool_calls_through_the_one_wire_mapping() -> None:
    """A call without an ``id`` gets ``call_<n>`` on every path — LiteLLM complete,
    LiteLLM stream, Ollama — because all three delegate to ``wire_tool_calls``."""
    litellm_message = SimpleNamespace(tool_calls=[
        SimpleNamespace(id=None, function=SimpleNamespace(name="read_file", arguments='{"path": "a.py"}')),
        SimpleNamespace(id="keep", function=SimpleNamespace(name="list_files", arguments={"pattern": "*"})),
    ])
    expected = [
        {"id": "call_1", "type": "function", "function": {"name": "read_file", "arguments": '{"path": "a.py"}'}},
        {"id": "keep", "type": "function", "function": {"name": "list_files", "arguments": '{"pattern": "*"}'}},
    ]
    assert oc._tool_calls_from_message(litellm_message) == expected
    assert oc._wire_partial_calls({
        0: {"id": "", "name": "read_file", "arguments": '{"path": "a.py"}'},
        1: {"id": "keep", "name": "list_files", "arguments": '{"pattern": "*"}'},
        2: {"id": "", "name": "", "arguments": ""},  # a delta that never named its function
    }) == expected
    assert OllamaProvider._wire_tool_calls([
        {"function": {"name": "read_file", "arguments": {"path": "a.py"}}},
        {"id": "keep", "function": {"name": "list_files", "arguments": {"pattern": "*"}}},
    ]) == expected
    assert oc._tool_calls_from_message(SimpleNamespace(tool_calls=[])) is None
    assert oc._wire_partial_calls({}) is None


# ───────────────────────────────────────────────────────────────────────────
# The engine and the agent loop hand the catalogue down as ``tools=``
# ───────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_engine_forwards_tools_to_the_provider_only_when_offered() -> None:
    import asyncio

    from nvh.core.engine import Engine, _tool_kwargs
    from nvh.providers.base import CompletionResponse, Usage

    assert _tool_kwargs(None, "auto") == {} and _tool_kwargs([], None) == {}
    assert _tool_kwargs([READ_FILE_TOOL], None) == {"tools": [READ_FILE_TOOL]}
    assert _tool_kwargs([READ_FILE_TOOL], "auto") == {"tools": [READ_FILE_TOOL], "tool_choice": "auto"}

    provider = MagicMock()
    provider.complete = AsyncMock(return_value=CompletionResponse(content="ok", model="m", provider="p", usage=Usage()))
    engine = Engine.__new__(Engine)
    engine._budget_lock = asyncio.Lock()
    engine.registry = MagicMock()
    engine.registry.has.return_value = True
    engine.registry.get.return_value = provider
    engine.rate_manager = MagicMock()
    engine.config = MagicMock()
    engine.config.providers = {}
    engine._get_fallback_chain = lambda name: [name]
    decision = SimpleNamespace(provider="p", model="m")

    await engine._execute_with_fallback([Message(role="user", content="hi")], decision, 0.0, 10, None, False)
    assert "tools" not in provider.complete.call_args.kwargs
    await engine._execute_with_fallback(
        [Message(role="user", content="hi")], decision, 0.0, 10, None, False, tools=[READ_FILE_TOOL], tool_choice="auto",
    )
    assert provider.complete.call_args.kwargs["tools"] == [READ_FILE_TOOL]
    assert provider.complete.call_args.kwargs["tool_choice"] == "auto"


@pytest.mark.asyncio
async def test_agent_loop_offers_its_catalogue_natively_and_reads_the_native_answer() -> None:
    from nvh.core.agent_loop import run_agent_loop
    from nvh.providers.base import CompletionResponse, Usage

    seen: list[dict[str, Any]] = []

    class FakeEngine:
        async def query(self, prompt="", **kwargs):
            seen.append(kwargs)
            if len(seen) == 1:
                return CompletionResponse(
                    content="", model="m", provider="p", usage=Usage(),
                    tool_calls=[{"id": "c1", "type": "function", "function": {"name": "echo", "arguments": '{"text": "hi"}'}}],
                )
            return CompletionResponse(content="done", model="m", provider="p", usage=Usage())

    tools = ToolRegistry(include_system=False, builtins=False)
    handler = AsyncMock(return_value="hi")
    tools.register(Tool(name="echo", description="Echo", parameters={"text": {"type": "string", "required": True}}, handler=handler))

    result = await run_agent_loop("say hi", FakeEngine(), tools=tools, max_iterations=3)
    assert result.completed and result.final_response == "done"
    assert seen[0]["tools"] == tools.openai_tools() and seen[0]["tool_choice"] == "auto"
    handler.assert_awaited_once_with(text="hi")
    assert result.steps[0].tool_calls == [{"tool": "echo", "args": {"text": "hi"}}]


@pytest.mark.asyncio
async def test_provider_supports_native_tools_is_conservative() -> None:
    assert await provider_supports_native_tools(MagicMock(), "gpt-4o") is False  # unknown adapter shape
    assert await provider_supports_native_tools(object(), "gpt-4o") is False

    class Yes:
        async def supports_tools(self, model):
            return True

    class Raises:
        def supports_tools(self, model):
            raise RuntimeError("no")

    class SyncNo:
        def supports_tools(self, model):
            return False

    assert await provider_supports_native_tools(Yes(), "m") is True
    assert await provider_supports_native_tools(Raises(), "m") is False
    assert await provider_supports_native_tools(SyncNo(), "m") is False


@pytest.mark.asyncio
async def test_chat_native_tool_kwargs_follow_the_provider() -> None:
    from dataclasses import fields

    blank = {f.name: None for f in fields(chat_mod._TurnSetup)}
    turn = chat_mod._TurnSetup(**{**blank, "prof": chat_mod.ProfileOverrides(), "system_prompt": "S", "user_message": "u", "history": []})
    assert await chat_mod._native_tool_kwargs(turn, MagicMock(), "gpt-4o") == {}  # nothing to offer
    turn = chat_mod._TurnSetup(**{
        **blank, "prof": chat_mod.ProfileOverrides(), "system_prompt": "S", "user_message": "u", "history": [],
        "native_tools": [READ_FILE_TOOL],
    })
    assert await chat_mod._native_tool_kwargs(turn, MagicMock(), "gpt-4o") == {}  # a mock provider vouches for nothing

    class Yes:
        async def supports_tools(self, model):
            return model == "gpt-4o"

    assert await chat_mod._native_tool_kwargs(turn, Yes(), "gpt-4o") == {"tools": [READ_FILE_TOOL], "tool_choice": "auto"}
    assert await chat_mod._native_tool_kwargs(turn, Yes(), "gpt-3") == {}


def test_chat_merges_native_and_text_calls_and_extracts_the_line() -> None:
    native = chat_mod._native_tool_calls([{"id": "c", "type": "function", "function": {"name": "refresh_models", "arguments": "{}"}}])
    text, text_calls = chat_mod._extract_tool_calls('Refreshing.\nTOOL_CALL: {"name": "refresh_models", "arguments": {}}\nTOOL_CALL: {"name": "diagnose", "arguments": {}}')
    assert text == "Refreshing."
    merged = chat_mod._merge_tool_calls(native, text_calls)
    assert merged == [{"name": "refresh_models", "arguments": {}}, {"name": "diagnose", "arguments": {}}]
    assert chat_mod._native_call_note(merged) == "(calling refresh_models, diagnose)"


@pytest.mark.asyncio
async def test_filtered_token_stream_hands_native_tool_calls_to_the_loop() -> None:
    wire = [{"id": "c", "type": "function", "function": {"name": "diagnose", "arguments": "{}"}}]

    async def chunks():
        yield SimpleNamespace(delta="Hello ", tool_calls=None)
        yield SimpleNamespace(delta="there\n", tool_calls=None)
        yield SimpleNamespace(delta="", tool_calls=wire, is_final=True)

    events = [e async for e in chat_mod._filtered_token_stream(chunks())]
    kinds = [k for k, _ in events]
    assert kinds == ["token", "token", "meter", "tool_calls", "full"]
    assert "".join(text for kind, text in events if kind == "token") == "Hello there\n"
    assert dict(events)["tool_calls"] == wire and dict(events)["full"] == "Hello there\n"

    async def plain():
        yield SimpleNamespace(delta="x")

    assert [k for k, _ in [e async for e in chat_mod._filtered_token_stream(plain())]] == ["token", "meter", "full"]


def test_wizard_registry_offers_native_specs_for_its_catalogue() -> None:
    from nvh.integrations.wizard.tools import default_registry

    reg = default_registry()
    specs = reg.openai_tools({"refresh_models", "shell"})
    assert {s["function"]["name"] for s in specs} == {"refresh_models", "shell"}
    shell = next(s for s in specs if s["function"]["name"] == "shell")
    assert shell["type"] == "function"
    assert shell["function"]["parameters"]["required"] == ["command"]
    assert set(shell["function"]["parameters"]["properties"]) == {"command", "cwd", "timeout_s"}
    # The agent registry builds the same shape from its JSON Schema.
    core = ToolRegistry(include_system=False).openai_tools(["read_file"])[0]
    assert core["function"]["parameters"] == {
        "type": "object", "properties": {"path": {"type": "string", "description": "File path to read"}}, "required": ["path"],
    }
