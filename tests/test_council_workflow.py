"""Behavioral regressions: real critique, honest votes and complete accounting."""

import json
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from nvh.config.settings import CouncilConfig, CouncilModeConfig, ProviderConfig
from nvh.core.engine import Engine
from nvh.providers.base import CompletionResponse, FinishReason, StreamChunk, Usage
from nvh.providers.registry import ProviderRegistry


class RecordingProvider:
    def __init__(self, name, tape, clock, answer=None):
        self.name, self.tape, self.clock, self.answer = name, tape, clock, answer

    async def complete(self, messages, **kwargs):
        prompt = messages[-1].content
        self.tape.append((self.name, prompt))
        self.clock[0] += 1
        if prompt.startswith("Given these"):
            text = "SCORE: 8\nSUMMARY: Shared evidence."
        elif "Critique and improve these answers" in prompt:
            text = f"{self.name}: critique found an unsupported claim"
        else:
            text = self.answer or f"{self.name}: original evidence"
        return CompletionResponse(
            content=text, provider=self.name, model="test",
            usage=Usage(input_tokens=10, output_tokens=5, total_tokens=15),
            cost_usd=Decimal("0.1"), latency_ms=1000,
        )

    async def stream(self, messages, **kwargs):
        response = await self.complete(messages, **kwargs)
        yield StreamChunk(
            delta=response.content, accumulated_content=response.content,
            model="test", provider=self.name, usage=response.usage,
            cost_usd=response.cost_usd, is_final=True, finish_reason=FinishReason.STOP,
        )


def make_engine(monkeypatch, answers=None):
    clock, tape = [0.0], []
    names = list(answers) if answers else ["alpha", "beta"]
    cfg = CouncilConfig(
        providers={name: ProviderConfig(enabled=True, default_model="test") for name in names},
        council=CouncilModeConfig(
            quorum=2, default_weights={name: 1 for name in names}, synthesis_provider=names[0],
        ),
    )
    reg = ProviderRegistry()
    for name in names:
        reg.register(name, RecordingProvider(name, tape, clock, (answers or {}).get(name)))
    engine = Engine(cfg, reg)
    engine._initialized = True
    engine._check_budget = AsyncMock()
    engine._log_query = AsyncMock()
    engine.webhooks.emit = AsyncMock()

    async def final_query(**kwargs):
        tape.append(("final", kwargs["prompt"]))
        clock[0] += 2
        return CompletionResponse(
            content="Final answer resolved the critiques.", provider="alpha", model="test",
            usage=Usage(input_tokens=20, output_tokens=10, total_tokens=30),
            cost_usd=Decimal("0.2"), latency_ms=2000,
        )

    engine.query = AsyncMock(side_effect=final_query)
    fake_time = SimpleNamespace(monotonic=lambda: clock[0])
    monkeypatch.setattr("nvh.core.council.time", fake_time)
    monkeypatch.setattr("nvh.core.throwdown.time", fake_time)
    return engine, tape, clock


@pytest.mark.asyncio
async def test_throwdown_critiques_first_round_before_final_and_accounts_all_work(monkeypatch):
    engine, tape, _ = make_engine(monkeypatch)
    result = await engine.run_council("Which design?", strategy="throwdown")
    assert len(result.member_responses) == 4
    critiques = [p for _, p in tape if p.startswith("Original question:")]
    assert len(critiques) == 3  # two independent critics and the final answer
    assert all("alpha: original evidence" in p and "beta: original evidence" in p for p in critiques)
    final_prompt = engine.query.await_args.kwargs["prompt"]
    assert "alpha: critique found" in final_prompt and "beta: critique found" in final_prompt
    assert tape[-1][0] == "final"
    assert result.total_cost_usd == Decimal("1.0")  # 8 round calls + final
    assert result.total_usage.total_tokens == 150
    assert result.total_latency_ms == 10000
    assert engine._log_query.await_count == 8  # rounds once, final query owns its own log
    assert engine.query.await_args.kwargs["use_cache"] is False


@pytest.mark.asyncio
async def test_throwdown_stops_after_failed_quorum(monkeypatch):
    engine, _, _ = make_engine(monkeypatch)
    engine.registry.get("beta").complete = AsyncMock(side_effect=RuntimeError("offline"))
    result = await engine.run_council("Which design?", strategy="throwdown")
    assert not result.quorum_met
    assert result.synthesis is None
    assert "_throwdown" in result.failed_members
    engine.query.assert_not_awaited()
    assert len(result.member_responses) == 1
    assert result.total_cost_usd == Decimal("0.1")


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_full_accounting_includes_agreement_and_synthesis(monkeypatch, streaming):
    engine, _, clock = make_engine(monkeypatch)
    events = []

    async def emit(event):
        events.append(event)

    if streaming:
        result = await engine.council.run_council_streaming("Question", emit)
        assert events[-1]["total_latency_ms"] == 4000
        assert events[-1]["total_cost"] == "0.4"
    else:
        result = await engine.run_council("Question")
        assert engine._log_query.await_count == 4
    assert clock[0] == 4
    assert result.total_cost_usd == Decimal("0.4")
    assert result.total_usage.total_tokens == 60
    assert result.total_latency_ms == 4000


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_majority_outvotes_high_weight_minority(monkeypatch, streaming):
    engine, _, _ = make_engine(monkeypatch, {"alpha": "Python", "beta": "  PYTHON  ", "gamma": "Java"})
    weights = {"alpha": 0.05, "beta": 0.05, "gamma": 0.9}
    options = {"strategy": "majority_vote", "weights_override": weights}
    if streaming:
        result = await engine.council.run_council_streaming("Pick one", AsyncMock(), **options)
    else:
        result = await engine.council.run_council("Pick one", **options)
    assert result.synthesis.content.endswith("Python")
    assert result.synthesis.metadata["votes"] == 2
    assert result.synthesis.metadata["outcome"] == "majority"
    assert result.synthesis.usage.total_tokens == 0


@pytest.mark.asyncio
async def test_vote_tie_is_labelled_and_uses_persona_weights(monkeypatch):
    from nvh.core.council import CouncilMember

    engine, _, _ = make_engine(monkeypatch)
    responses = {
        "alpha:Engineer": CompletionResponse(content="A", provider="alpha", model="test", usage=Usage()),
        "alpha:Reviewer": CompletionResponse(content="B", provider="alpha", model="test", usage=Usage()),
    }
    result = engine.council._majority_vote(responses, [
        CouncilMember("alpha", "test", 0.1, "Engineer"),
        CouncilMember("alpha", "test", 0.9, "Reviewer"),
    ])
    assert result.metadata["outcome"] == "tie"
    assert result.metadata["selected_from"] == "alpha:Reviewer"
    assert "weight tie-break" in result.content


@pytest.mark.asyncio
async def test_mcp_throwdown_runs_actual_critique(monkeypatch):
    from nvh import mcp_server

    engine, tape, _ = make_engine(monkeypatch)
    monkeypatch.setattr(mcp_server, "_get_engine", AsyncMock(return_value=engine))
    result = await mcp_server.create_server().call_tool("throwdown", {"prompt": "Which design?"})
    assert "Final answer resolved" in str(result)
    assert any("First-round answers (evidence to critique" in prompt for _, prompt in tape)


@pytest.mark.parametrize("endpoint,stream", [
    ("/v1/proxy/chat/completions", False),
    ("/v1/proxy/chat/completions", True),
    ("/v1/anthropic/messages", False),
])
def test_compatible_apis_execute_same_two_pass_workflow(monkeypatch, endpoint, stream):
    import nvh.api.server as server

    engine, tape, _ = make_engine(monkeypatch)
    monkeypatch.setattr(server, "_engine", engine)
    client = TestClient(server.app, raise_server_exceptions=True)
    response = client.post(endpoint, json={
        "model": "throwdown", "messages": [{"role": "user", "content": "Which design?"}],
        "stream": stream,
    })
    assert response.status_code == 200
    assert tape[-1][0] == "final"
    assert len(tape) == 9
    if not stream:
        usage = response.json()["usage"]
        assert usage.get("prompt_tokens", usage.get("input_tokens")) == 100
        assert usage.get("completion_tokens", usage.get("output_tokens")) == 50
    else:
        assert "[DONE]" in response.text
        chunks = [json.loads(line[6:]) for line in response.text.splitlines()
                  if line.startswith("data: {")]
        assert chunks[-1]["usage"]["total_tokens"] == 150


@pytest.mark.asyncio
async def test_throwdown_checks_next_round_budget_after_recording_first_round(monkeypatch):
    engine, tape, _ = make_engine(monkeypatch)
    engine._check_budget.side_effect = [None, RuntimeError("budget exhausted")]
    with pytest.raises(RuntimeError, match="budget exhausted"):
        await engine.run_council("Which design?", strategy="throwdown")
    assert len(tape) == 4
    assert engine._log_query.await_count == 4
    engine.query.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_synthesis_does_not_buy_an_agreement_call(monkeypatch):
    engine, tape, _ = make_engine(monkeypatch)
    result = await engine.council.run_council_streaming("Question", AsyncMock(), synthesize=False)
    assert len(tape) == 2
    assert not result.auxiliary_responses
    assert result.total_cost_usd == Decimal("0.2")


@pytest.mark.asyncio
async def test_malformed_agreement_still_counts_its_charge(monkeypatch):
    engine, _, _ = make_engine(monkeypatch)
    provider = engine.registry.get("alpha")
    complete = provider.complete

    async def malformed(messages, **kwargs):
        response = await complete(messages, **kwargs)
        if messages[-1].content.startswith("Given these"):
            response.content = "not a valid score"
        return response

    provider.complete = malformed
    result = await engine.run_council("Question")
    assert result.total_cost_usd == Decimal("0.4")
    assert len(result.auxiliary_responses) == 1


@pytest.mark.asyncio
async def test_failed_stream_usage_is_retained_without_voting(monkeypatch):
    engine, _, _ = make_engine(monkeypatch)

    async def partial(*args, **kwargs):
        yield StreamChunk(delta="partial", model="test", provider="beta",
                          usage=Usage(input_tokens=10, output_tokens=1, total_tokens=11),
                          cost_usd=Decimal("0.05"))
        yield StreamChunk(delta=" more", model="test", provider="beta")
        raise RuntimeError("connection lost")

    engine.registry.get("beta").stream = partial
    result = await engine.council.run_council_streaming("Question", AsyncMock())
    assert not result.quorum_met
    assert "beta" not in result.member_responses
    assert result.total_cost_usd == Decimal("0.15")
    assert result.total_usage.total_tokens == 26


@pytest.mark.asyncio
async def test_synthesis_retry_retains_reported_failed_attempt_charge(monkeypatch):
    engine, _, _ = make_engine(monkeypatch)
    provider = engine.registry.get("alpha")
    stream = provider.stream

    async def fail_synthesis(messages, **kwargs):
        if messages[-1].content.startswith("You are a synthesis engine"):
            yield StreamChunk(delta="draft", model="test", provider="alpha",
                              usage=Usage(input_tokens=10, output_tokens=1, total_tokens=11),
                              cost_usd=Decimal("0.07"))
            yield StreamChunk(delta=" more", model="test", provider="alpha")
            raise RuntimeError("lost synthesis connection")
        async for chunk in stream(messages, **kwargs):
            yield chunk

    provider.stream = fail_synthesis
    result = await engine.council.run_council_streaming("Question", AsyncMock())
    assert result.synthesis.provider == "beta"
    assert result.total_cost_usd == Decimal("0.47")
    assert result.total_usage.total_tokens == 71
