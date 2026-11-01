"""Tests for Wizard cost/latency surfacing + fallback signal (Tier 3)."""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _engine_with_response(cost: str, latency: int, fallback_from: str | None = None):
    from nvh.providers.base import CompletionResponse, FinishReason, Usage

    resp = CompletionResponse(
        content="ok",
        model="ollama/x",
        provider="ollama",
        usage=Usage(input_tokens=42, output_tokens=21, total_tokens=63),
        cost_usd=Decimal(cost),
        latency_ms=latency,
        finish_reason=FinishReason.STOP,
        fallback_from=fallback_from,
    )
    fake_provider = MagicMock()
    fake_provider.complete = AsyncMock(return_value=resp)
    fake_decision = MagicMock()
    fake_decision.provider = "ollama"
    fake_decision.model = "ollama/x"
    fake_engine = MagicMock()
    fake_engine.initialize = AsyncMock()
    fake_engine._check_budget = AsyncMock()
    fake_engine._log_query = AsyncMock()
    fake_engine.router.route = MagicMock(return_value=fake_decision)
    fake_engine.registry.get = MagicMock(return_value=fake_provider)
    fake_engine.config.defaults.temperature = 0.7
    fake_engine.config.defaults.max_tokens = 256
    return fake_engine


_EMPTY = {
    "gpu": {"detected": False},
    "storage": {"available": False},
    "providers": [],
    "ollama_models": [],
    "recent_jobs": [],
    "receipts": {},
    "vault": {},
}


@pytest.mark.asyncio
async def test_chat_envelope_includes_cost_and_latency(monkeypatch) -> None:
    """cost_usd / latency_ms / token counts ride in the response envelope."""
    from nvh.integrations.wizard import chat as chat_mod

    monkeypatch.setenv("NVH_WIZARD_AUTOFOLD_VAULT", "0")
    engine = _engine_with_response("0.012345", 850)
    with (
        patch("nvh.api.server.get_engine", return_value=engine),
        patch("nvh.integrations.wizard.context.wizard_context", return_value=_EMPTY),
    ):
        result = await chat_mod.wizard_chat("hello world")

    assert result["mode"] == "llm"
    assert pytest.approx(result["cost_usd"], abs=1e-6) == 0.012345
    assert result["latency_ms"] == 850
    assert result["input_tokens"] == 42
    assert result["output_tokens"] == 21
    assert result["fallback_from"] is None


@pytest.mark.asyncio
async def test_chat_envelope_surfaces_fallback_from(monkeypatch) -> None:
    """When the provider response reports a fallback_from, the envelope carries it."""
    from nvh.integrations.wizard import chat as chat_mod

    monkeypatch.setenv("NVH_WIZARD_AUTOFOLD_VAULT", "0")
    engine = _engine_with_response("0", 200, fallback_from="openai")
    with (
        patch("nvh.api.server.get_engine", return_value=engine),
        patch("nvh.integrations.wizard.context.wizard_context", return_value=_EMPTY),
    ):
        result = await chat_mod.wizard_chat("anything")

    assert result["fallback_from"] == "openai"


@pytest.mark.asyncio
async def test_chat_envelope_zero_when_provider_omits_meter(monkeypatch) -> None:
    """Providers like Ollama report 0 cost; envelope should still surface
    consistent fields so the UI can hide them gracefully."""
    from nvh.integrations.wizard import chat as chat_mod

    monkeypatch.setenv("NVH_WIZARD_AUTOFOLD_VAULT", "0")
    engine = _engine_with_response("0", 0)
    with (
        patch("nvh.api.server.get_engine", return_value=engine),
        patch("nvh.integrations.wizard.context.wizard_context", return_value=_EMPTY),
    ):
        result = await chat_mod.wizard_chat("anything")

    assert result["cost_usd"] == 0.0
    assert result["latency_ms"] == 0


# ── cost_known: the catalog's verdict rides beside cost_usd (0.46) ──────────


def _model_info(cost_known: bool):
    from nvh.providers.base import ModelInfo

    return ModelInfo(model_id="ollama/x", provider="ollama", cost_known=cost_known)


def _catalog(engine, info) -> None:
    """Make the fake engine's catalog answer ``info`` (a ModelInfo or None)."""
    engine.registry.get_model_info = MagicMock(return_value=info)


def test_model_cost_known_is_the_catalog_lookup_not_a_table() -> None:
    """The helper every envelope uses: a non-zero bill is known; else the
    catalog row's ``cost_known``; else the provider spec's ``zero_cost``."""
    from nvh.core.agentic import model_cost_known
    from nvh.providers.specs import PROVIDER_FACTS

    class Reg:
        def __init__(self, info):
            self.info = info

        def get_model_info(self, model):
            return self.info

    assert PROVIDER_FACTS["ollama"].zero_cost and not PROVIDER_FACTS["openai"].zero_cost
    # A non-zero bill was priced by something (LiteLLM prices models the YAML
    # catalog does not list): known whatever the row says.
    assert model_cost_known(Reg(None), "x", provider="openai", cost_usd=Decimal("0.001")) is True
    assert model_cost_known(Reg(_model_info(False)), "x", provider="openai", cost_usd=0.5) is True
    # A row answers for itself, even on a zero-cost provider.
    assert model_cost_known(Reg(_model_info(False)), "x", provider="ollama", cost_usd=0) is False
    assert model_cost_known(Reg(_model_info(True)), "x", provider="openai", cost_usd=0) is True
    # No row: a zero_cost provider is a known $0, anything else is unknown.
    assert model_cost_known(Reg(None), "x", provider="ollama", cost_usd=0) is True
    assert model_cost_known(Reg(None), "x", provider="openai", cost_usd=0) is False
    assert model_cost_known(Reg(None), "", provider="hf", cost_usd=None) is False
    # No registry at all (nothing to ask): unknown, never a silent "priced".
    assert model_cost_known(None, "x", provider="openai", cost_usd=0) is False
    # Unmetered (no usage arrived): a paid model's $0 was billed upstream and is
    # unknown; a free model's $0 (a $0 row, or a zero_cost provider) stays known.
    from nvh.providers.base import ModelInfo

    paid = ModelInfo(model_id="x", provider="openai", cost_known=True,
                     input_cost_per_1m_tokens=Decimal("2.5"), output_cost_per_1m_tokens=Decimal("10"))
    assert model_cost_known(Reg(paid), "x", provider="openai", cost_usd=0, metered=False) is False
    assert model_cost_known(Reg(paid), "x", provider="openai", cost_usd=0, metered=True) is True
    assert model_cost_known(Reg(_model_info(True)), "x", provider="ollama", cost_usd=0, metered=False) is True
    assert model_cost_known(Reg(None), "x", provider="ollama", cost_usd=0, metered=False) is True
    # The routed id is asked when the echoed one has no row.

    class RoutedReg:
        def get_model_info(self, model):
            return paid if model == "asked-for" else None

    assert model_cost_known(RoutedReg(), "echoed-snapshot", provider="openai", cost_usd=0, routed_model="asked-for") is True
    assert model_cost_known(RoutedReg(), "echoed-snapshot", provider="openai", cost_usd=0) is False
    # No row anywhere: LiteLLM's own table (the bill's source) prices an id the
    # YAML catalog does not list -- derived from the table, never typed here.
    from nvh.providers.registry import _litellm_table

    table = _litellm_table() or {}
    litellm_openai = next(
        (k for k, v in table.items() if isinstance(v, dict) and v.get("litellm_provider") == "openai" and v.get("input_cost_per_token")),
        None,
    )
    if litellm_openai is not None:
        assert model_cost_known(Reg(None), litellm_openai, provider="openai", cost_usd=0) is True
        assert model_cost_known(Reg(None), litellm_openai, provider="openai", cost_usd=0, metered=False) is False


@pytest.mark.asyncio
async def test_chat_envelope_cost_known_is_the_catalog_rows_verdict(monkeypatch) -> None:
    """A $0 reply from a model whose catalog row says cost_known=False is
    reported as cost_known=False (the UI shows "cost unknown"); a priced
    row's $0 is a real $0. The lookup is by the model that answered."""
    from nvh.integrations.wizard import chat as chat_mod

    monkeypatch.setenv("NVH_WIZARD_AUTOFOLD_VAULT", "0")
    for known in (True, False):
        engine = _engine_with_response("0", 10)
        _catalog(engine, _model_info(cost_known=known))
        with (
            patch("nvh.api.server.get_engine", return_value=engine),
            patch("nvh.integrations.wizard.context.wizard_context", return_value=_EMPTY),
        ):
            result = await chat_mod.wizard_chat("hello")
        assert result["mode"] == "llm"
        assert result["cost_usd"] == 0.0
        assert result["cost_known"] is known
        engine.registry.get_model_info.assert_called_with("ollama/x")


@pytest.mark.asyncio
async def test_chat_envelope_non_zero_cost_is_known_whatever_the_row_says(monkeypatch) -> None:
    from nvh.integrations.wizard import chat as chat_mod

    monkeypatch.setenv("NVH_WIZARD_AUTOFOLD_VAULT", "0")
    engine = _engine_with_response("0.012345", 850)
    _catalog(engine, _model_info(cost_known=False))
    with (
        patch("nvh.api.server.get_engine", return_value=engine),
        patch("nvh.integrations.wizard.context.wizard_context", return_value=_EMPTY),
    ):
        result = await chat_mod.wizard_chat("hello")
    assert pytest.approx(result["cost_usd"], abs=1e-6) == 0.012345
    assert result["cost_known"] is True


@pytest.mark.asyncio
async def test_chat_envelope_no_catalog_row_asks_the_provider_spec(monkeypatch) -> None:
    """No row for the model: a zero_cost provider's $0 is known, a paid
    provider's $0 is unknown — PROVIDER_FACTS decides, not a table here."""
    from nvh.integrations.wizard import chat as chat_mod

    monkeypatch.setenv("NVH_WIZARD_AUTOFOLD_VAULT", "0")
    for provider, expected in (("ollama", True), ("openai", False)):
        engine = _engine_with_response("0", 10)
        engine.router.route.return_value.provider = provider
        _catalog(engine, None)
        with (
            patch("nvh.api.server.get_engine", return_value=engine),
            patch("nvh.integrations.wizard.context.wizard_context", return_value=_EMPTY),
        ):
            result = await chat_mod.wizard_chat("hello")
        assert result["used_provider"] == provider
        assert result["cost_known"] is expected


@pytest.mark.asyncio
async def test_persisted_wizard_meta_carries_cost_known(monkeypatch) -> None:
    """The wizard-meta tail a reload hydrates from carries cost_known beside
    cost_usd, so a reloaded turn renders the same pill the live one did."""
    from nvh.integrations.wizard import chat as chat_mod

    monkeypatch.setenv("NVH_WIZARD_AUTOFOLD_VAULT", "0")
    engine = _engine_with_response("0", 10)
    _catalog(engine, _model_info(cost_known=False))
    persist = AsyncMock()
    with (
        patch("nvh.api.server.get_engine", return_value=engine),
        patch("nvh.integrations.wizard.context.wizard_context", return_value=_EMPTY),
        patch.object(chat_mod, "_persist_wizard_turn", persist),
    ):
        result = await chat_mod.wizard_chat("hello", conversation_id="conv-1")
    assert result["cost_known"] is False
    persist.assert_awaited_once()
    meta = persist.await_args.kwargs["metadata"]
    assert meta["cost_usd"] == 0.0 and meta["cost_known"] is False
