"""Final push I — proxy, engine budget/cache/fallback, council, router."""
from __future__ import annotations

import asyncio
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nvh.api.proxy import (
    is_throwdown_model,
    openai_stream_generator,
    parse_council_model,
    resolve_provider_from_model,
)
from nvh.config.settings import (
    BudgetConfig,
    CacheConfig,
    CouncilConfig,
    CouncilModeConfig,
    DefaultsConfig,
    ProviderConfig,
    RoutingConfig,
    RoutingRule,
)
from nvh.core.council import CouncilMember, CouncilOrchestrator
from nvh.core.engine import BudgetExceededError, Engine, ResponseCache
from nvh.core.router import RoutingEngine
from nvh.providers.base import (
    CompletionResponse,
    FinishReason,
    ModelInfo,
    ProviderError,
    StreamChunk,
    Usage,
)
from nvh.providers.registry import ProviderRegistry


def _cfg(**ov):
    d = dict(defaults=DefaultsConfig(provider="openai", model="gpt-4o"),
             providers={"openai": ProviderConfig(default_model="gpt-4o")},
             council=CouncilModeConfig(), routing=RoutingConfig(),
             budget=BudgetConfig(), cache=CacheConfig())
    d.update(ov)
    return CouncilConfig(**d)


def _r(content="ok", provider="openai", model="gpt-4o", **kw):
    return CompletionResponse(content=content, model=model, provider=provider,
                              usage=Usage(input_tokens=10, output_tokens=10),
                              cost_usd=Decimal("0.001"), **kw)


def _eng(**attrs):
    e = Engine.__new__(Engine)
    e._budget_lock = asyncio.Lock()
    e.webhooks = MagicMock()
    e.webhooks.emit = AsyncMock()
    for k, v in attrs.items():
        setattr(e, k, v)
    return e


# ── 1  proxy ─────────────────────────────────────────────────────────
@pytest.mark.parametrize("inp,exp", [
    ("", None), ("council:7", 7), ("council:xyz", 3), ("gpt-4o", None)])
def test_parse_council(inp, exp):
    assert parse_council_model(inp) == exp


def test_throwdown():
    assert not is_throwdown_model(None) and not is_throwdown_model("c")


@pytest.mark.parametrize("inp,exp", [
    ("auto", (None, None)), ("safe", ("ollama", None)),
    ("gpt-4o", ("openai", "gpt-4o")), ("council:5", (None, None)),
    ("throwdown", (None, None))])
def test_resolve(inp, exp):
    assert resolve_provider_from_model(inp) == exp


def test_resolve_prefix_and_unknown():
    assert resolve_provider_from_model("claude-3-5-sonnet-20241022")[0] == "anthropic"
    assert resolve_provider_from_model("mystery") == (None, "mystery")


def _meng(avail=True):
    e = MagicMock()
    e.config.defaults.temperature = 0.7
    e.config.defaults.max_tokens = 512
    e.config.defaults.system_prompt = None
    e.registry.has.return_value = avail
    return e


@pytest.mark.asyncio
async def test_stream_gen_unavail():
    eng = _meng(False)
    d = MagicMock()
    d.provider = "fake"
    d.model = "m"
    eng.router.route.return_value = d
    out = b"".join([c async for c in openai_stream_generator(
        eng, "hi", None, None, None, None, None, "auto")])
    assert b"provider_not_found" in out and b"[DONE]" in out


@pytest.mark.asyncio
async def test_stream_gen_happy():
    eng = _meng(True)
    d = MagicMock()
    d.provider = "openai"
    d.model = "gpt-4o"
    eng.router.route.return_value = d
    mp = MagicMock()
    eng.registry.get.return_value = mp

    async def _s(**kw):
        yield StreamChunk(delta="Hi", is_final=True,
                          finish_reason=FinishReason.STOP)
    mp.stream.return_value = _s()
    out = b"".join([c async for c in openai_stream_generator(
        eng, "hi", "openai", "gpt-4o", None, 0.5, 100, "gpt-4o")])
    assert b'"role": "assistant"' in out and b"Hi" in out


# ── 2  engine ────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_cache_hit_miss():
    from nvh.providers.base import Message
    c = ResponseCache(max_size=10, ttl_seconds=3600)
    msgs = [Message(role="user", content="hello")]
    assert await c.get("openai", "m", msgs, 0, 100) is None
    await c.put("openai", "m", msgs, 0, 100, _r())
    hit = await c.get("openai", "m", msgs, 0, 100)
    assert hit.cache_hit is True and hit.cost_usd == Decimal("0")


@pytest.mark.asyncio
async def test_budget_monthly_exceeded():
    e = _eng(config=_cfg(budget=BudgetConfig(
        daily_limit_usd=Decimal("0"), monthly_limit_usd=Decimal("5"),
        hard_stop=True)))
    with patch("nvh.storage.repository.get_spend", return_value=Decimal("6")):
        with pytest.raises(BudgetExceededError, match="Monthly"):
            await e._check_budget()


@pytest.mark.asyncio
async def test_budget_daily_alert_and_status():
    e = _eng(config=_cfg(budget=BudgetConfig(
        daily_limit_usd=Decimal("10"), hard_stop=True, alert_threshold=0.8)))
    with patch("nvh.storage.repository.get_spend", return_value=Decimal("9")):
        await e._check_budget()
    e.webhooks.emit.assert_called_once()
    # Also verify get_budget_status fields
    e2 = _eng(config=_cfg())
    with (patch("nvh.storage.repository.get_spend", return_value=Decimal("1")),
          patch("nvh.storage.repository.get_spend_by_provider",
                return_value={"openai": Decimal("1")}),
          patch("nvh.storage.repository.get_query_count", return_value=42)):
        s = await e2.get_budget_status()
    assert s["daily_queries"] == 42 and "by_provider" in s


@pytest.mark.asyncio
async def test_fallback_sets_fallback_from():
    from nvh.core.router import RoutingDecision
    from nvh.providers.base import Message, TaskType
    cfg = _cfg(providers={"openai": ProviderConfig(default_model="gpt-4o"),
                          "groq": ProviderConfig(default_model="llama")},
               council=CouncilModeConfig(fallback_order=["groq"]))
    reg = ProviderRegistry()
    mo = MagicMock()
    mo.complete = AsyncMock(side_effect=ProviderError("x", provider="openai"))
    mg = MagicMock()
    mg.complete = AsyncMock(return_value=_r(provider="groq"))
    reg.register("openai", mo)
    reg.register("groq", mg)
    e = _eng(config=cfg, registry=reg, rate_manager=MagicMock())
    dec = RoutingDecision(provider="openai", model="gpt-4o",
                          task_type=TaskType.CONVERSATION,
                          confidence=0.9, scores={}, reason="t")
    r = await e._execute_with_fallback(
        [Message(role="user", content="hi")], dec, 0.7, 100, None, False)
    assert r.fallback_from == "openai"


# ── 3  council ───────────────────────────────────────────────────────
_GP = {"groq": ProviderConfig(default_model="llama")}


def _orch(extra=None, quorum=2):
    provs = {"openai": ProviderConfig(default_model="gpt-4o")}
    if extra:
        provs.update(extra)
    cfg = _cfg(council=CouncilModeConfig(
        synthesis_provider="openai", quorum=quorum), providers=provs)
    reg = ProviderRegistry()
    mp = MagicMock()
    mp.complete = AsyncMock(return_value=_r(content="synth"))
    for n in provs:
        reg.register(n, mp)
    return CouncilOrchestrator(cfg, reg)


@pytest.mark.asyncio
async def test_best_of():
    o = _orch(_GP)
    r = await o._best_of("q?",
        {"openai": _r(content="A"), "groq": _r(content="B")},
        [CouncilMember("openai", "gpt-4o", 0.5),
         CouncilMember("groq", "llama", 0.5)])
    assert r.metadata.get("strategy") == "best_of"


@pytest.mark.asyncio
async def test_weighted_synthesis_two():
    o = _orch(_GP)
    r = await o._weighted_synthesis("q?",
        {"openai": _r(content="A"), "groq": _r(content="B")},
        [CouncilMember("openai", "gpt-4o", 0.6),
         CouncilMember("groq", "llama", 0.4)])
    assert r.content == "synth"


@pytest.mark.asyncio
async def test_analyze_agreement_llm():
    mp = MagicMock()
    mp.complete = AsyncMock(
        return_value=_r(content="SCORE: 8\nSUMMARY: All agree."))
    reg = ProviderRegistry()
    reg.register("openai", mp)
    o = CouncilOrchestrator(
        _cfg(council=CouncilModeConfig(synthesis_provider="openai"),
             providers={"openai": ProviderConfig(default_model="gpt-4o")}), reg)
    sc, sm = await o._analyze_agreement(
        "q?", {"a": _r(), "b": _r()}, use_llm=True)
    assert sc == pytest.approx(0.8, abs=0.01) and "agree" in sm.lower()


@pytest.mark.asyncio
async def test_run_council_auto_agents():
    o = _orch(quorum=1)
    r = await o.run_council("How to scale a DB?",
                            auto_agents=True, num_agents=1, synthesize=False)
    assert r.quorum_met and len(r.agents_used) >= 1


# ── 4  router ────────────────────────────────────────────────────────
def _rtr(rules=None):
    from nvh.core.rate_limiter import ProviderRateManager
    cfg = _cfg(routing=RoutingConfig(rules=rules or []))
    reg = ProviderRegistry()
    mp = MagicMock()
    reg.register("openai", mp)
    reg.register("groq", mp)
    return RoutingEngine(cfg, reg, ProviderRateManager())


def test_rule_match():
    r = _rtr([RoutingRule(match={"task_type": "code_generation"},
                          provider="groq", model="llama")])
    assert r.route("Write a Python sort function").provider == "groq"


def test_cost_expensive():
    m = ModelInfo(model_id="x", provider="x",
                  input_cost_per_1m_tokens=Decimal("50"),
                  output_cost_per_1m_tokens=Decimal("50"))
    assert _rtr()._cost_score(m) == 0.0


@pytest.mark.parametrize("ms,exp", [(6000, 0.0), (0, 1.0)])
def test_latency_score(ms, exp):
    assert _rtr()._latency_score(
        ModelInfo(model_id="x", provider="x", typical_latency_ms=ms)) == exp
