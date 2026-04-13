"""Integration tests for drift detection wired into the Engine."""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nvh.core.drift_detector import RECENT_WINDOW

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_engine():
    """Build a minimal Engine with mocked internals.

    Patches the heavy dependencies so we can instantiate Engine cheaply
    without touching real providers, DB, or GPU detection.
    """
    with patch("nvh.core.engine.ProviderRateManager"), \
         patch("nvh.core.engine.RoutingEngine"), \
         patch("nvh.core.engine.CouncilOrchestrator"), \
         patch("nvh.core.engine.ConversationManager"), \
         patch("nvh.core.engine.WebhookManager") as mock_wh, \
         patch("nvh.core.orchestrator.LocalOrchestrator") as mock_orch, \
         patch("nvh.core.context_files.find_context_files", return_value=[]):
        cfg = MagicMock()
        cfg.cache.max_size = 100
        cfg.cache.ttl_seconds = 60
        cfg.webhooks = []
        cfg.defaults.orchestration_mode = "off"

        reg = MagicMock()

        from nvh.core.engine import Engine
        engine = Engine(config=cfg, registry=reg)
    return engine


# ---------------------------------------------------------------------------
# Tests: check_drift
# ---------------------------------------------------------------------------

class TestEngineCheckDrift:
    """Engine.check_drift() delegates to drift_detector.check_for_drift."""

    def test_no_drift_on_empty_history(self):
        engine = _make_engine()
        assert engine.check_drift() == []

    def test_no_drift_with_stable_scores(self):
        engine = _make_engine()
        engine.score_history[("groq", "code")] = [0.9] * 25
        assert engine.check_drift() == []

    def test_detects_drift_on_quality_drop(self):
        engine = _make_engine()
        historical = [0.9] * 15
        recent = [0.4] * RECENT_WINDOW
        engine.score_history[("groq", "code")] = historical + recent
        alerts = engine.check_drift()
        assert len(alerts) == 1
        assert alerts[0].provider == "groq"
        assert alerts[0].task_type == "code"

    def test_returns_multiple_alerts(self):
        engine = _make_engine()
        good = [0.9] * 15
        bad = [0.3] * RECENT_WINDOW
        engine.score_history[("groq", "code")] = good + bad
        engine.score_history[("openai", "qa")] = good + bad
        alerts = engine.check_drift()
        assert len(alerts) == 2


# ---------------------------------------------------------------------------
# Tests: auto_reroute
# ---------------------------------------------------------------------------

class TestEngineAutoReroute:
    """Engine.auto_reroute() detects drift and adjusts provider_weights."""

    def test_halves_degraded_provider_weight(self):
        engine = _make_engine()
        engine.provider_weights = {"groq": 1.0, "openai": 1.0}
        engine.score_history[("groq", "code")] = [0.9] * 15 + [0.3] * RECENT_WINDOW
        actions = engine.auto_reroute()
        assert len(actions) == 1
        assert engine.provider_weights["groq"] == pytest.approx(0.5)
        assert engine.provider_weights["openai"] == 1.0  # unchanged

    def test_no_actions_when_no_drift(self):
        engine = _make_engine()
        engine.provider_weights = {"groq": 1.0}
        engine.score_history[("groq", "code")] = [0.9] * 25
        actions = engine.auto_reroute()
        assert actions == []

    def test_skips_provider_not_in_weights(self):
        engine = _make_engine()
        engine.provider_weights = {"openai": 1.0}  # groq not present
        engine.score_history[("groq", "code")] = [0.9] * 15 + [0.3] * RECENT_WINDOW
        actions = engine.auto_reroute()
        assert len(actions) == 1
        assert "skipped" in actions[0].lower()


# ---------------------------------------------------------------------------
# Tests: score_history accumulation via _record_learning
# ---------------------------------------------------------------------------

class TestScoreHistoryAccumulation:
    """Verify that _record_learning appends to score_history."""

    @pytest.mark.asyncio
    async def test_record_learning_appends_quality_score(self):
        engine = _make_engine()
        engine.learning = MagicMock()
        engine.learning.record_outcome = AsyncMock()
        engine.learning.get_score_map.return_value = {}

        decision = MagicMock()
        decision.task_type.value = "code"
        decision.confidence = 0.9
        decision.scores = {"composite": 0.8, "capability": 0.7}

        response = MagicMock()
        response.provider = "groq"
        response.model = "llama3"
        response.latency_ms = 100
        response.cost_usd = Decimal("0.001")
        response.finish_reason = "stop"
        response.usage.input_tokens = 10
        response.usage.output_tokens = 20
        response.fallback_from = None

        await engine._record_learning(decision, response, quality_score=0.85)

        assert ("groq", "code") in engine.score_history
        assert engine.score_history[("groq", "code")] == [0.85]

    @pytest.mark.asyncio
    async def test_record_learning_defaults_to_1_on_success(self):
        engine = _make_engine()
        engine.learning = MagicMock()
        engine.learning.record_outcome = AsyncMock()
        engine.learning.get_score_map.return_value = {}

        from nvh.providers.base import FinishReason

        decision = MagicMock()
        decision.task_type.value = "qa"
        decision.confidence = 0.8
        decision.scores = {"composite": 0.7, "capability": 0.6}

        response = MagicMock()
        response.provider = "openai"
        response.model = "gpt-4"
        response.latency_ms = 200
        response.cost_usd = Decimal("0.01")
        response.finish_reason = FinishReason.STOP
        response.usage.input_tokens = 50
        response.usage.output_tokens = 100
        response.fallback_from = None

        await engine._record_learning(decision, response, quality_score=None)

        assert engine.score_history[("openai", "qa")] == [1.0]
