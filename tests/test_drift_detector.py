"""Tests for nvh.core.drift_detector."""

from __future__ import annotations

from types import SimpleNamespace

from nvh.core.drift_detector import (
    DRIFT_THRESHOLD,
    RECENT_WINDOW,
    DriftAlert,
    auto_reroute,
    check_for_drift,
    format_drift_alerts,
)


def _make_engine(history: dict, weights: dict | None = None):
    """Build a minimal mock engine with score_history and optional weights."""
    ns = SimpleNamespace(score_history=history)
    if weights is not None:
        ns.provider_weights = weights
    return ns


class TestCheckForDrift:
    def test_no_drift_when_scores_stable(self):
        scores = [0.9] * 20
        engine = _make_engine({("groq", "code"): scores})
        alerts = check_for_drift(engine)
        assert alerts == []

    def test_drift_detected_on_significant_drop(self):
        historical = [0.9] * 15
        recent = [0.5] * RECENT_WINDOW
        engine = _make_engine({("groq", "code"): historical + recent})
        alerts = check_for_drift(engine)
        assert len(alerts) == 1
        assert alerts[0].provider == "groq"
        assert alerts[0].task_type == "code"
        assert alerts[0].drop_pct > DRIFT_THRESHOLD * 100

    def test_no_drift_when_drop_below_threshold(self):
        historical = [0.9] * 15
        # 10% drop is below 20% threshold
        recent = [0.82] * RECENT_WINDOW
        engine = _make_engine({("openai", "qa"): historical + recent})
        alerts = check_for_drift(engine)
        assert alerts == []

    def test_skips_short_history(self):
        engine = _make_engine({("groq", "code"): [0.9, 0.1]})
        alerts = check_for_drift(engine)
        assert alerts == []

    def test_skips_zero_historical_average(self):
        scores = [0.0] * 20
        engine = _make_engine({("groq", "code"): scores})
        alerts = check_for_drift(engine)
        assert alerts == []

    def test_multiple_providers_multiple_alerts(self):
        historical = [0.9] * 15
        recent_bad = [0.3] * RECENT_WINDOW
        engine = _make_engine({
            ("groq", "code"): historical + recent_bad,
            ("openai", "qa"): historical + recent_bad,
        })
        alerts = check_for_drift(engine)
        assert len(alerts) == 2
        providers = {a.provider for a in alerts}
        assert providers == {"groq", "openai"}

    def test_no_score_history_attribute(self):
        engine = SimpleNamespace()  # no score_history at all
        alerts = check_for_drift(engine)
        assert alerts == []

    def test_alert_has_recommendation(self):
        historical = [0.9] * 15
        recent = [0.4] * RECENT_WINDOW
        engine = _make_engine({("anthropic", "review"): historical + recent})
        alerts = check_for_drift(engine)
        assert len(alerts) == 1
        assert "anthropic" in alerts[0].recommendation
        assert "review" in alerts[0].recommendation


class TestFormatDriftAlerts:
    def test_no_alerts_message(self):
        assert "No drift" in format_drift_alerts([])

    def test_formats_alert_details(self):
        alert = DriftAlert(
            provider="groq", task_type="code",
            previous_score=0.9, current_score=0.5,
            drop_pct=44.4, recommendation="Route away from groq",
        )
        output = format_drift_alerts([alert])
        assert "groq" in output
        assert "code" in output
        assert "44.4" in output
        assert "Route away" in output
        assert "Drift Alerts" in output


class TestAutoReroute:
    def test_halves_weight_for_degraded_provider(self):
        weights = {"groq": 1.0, "openai": 1.0}
        engine = _make_engine({}, weights=weights)
        alert = DriftAlert(
            provider="groq", task_type="code",
            previous_score=0.9, current_score=0.5,
            drop_pct=44.4,
        )
        actions = auto_reroute(engine, [alert])
        assert len(actions) == 1
        assert engine.provider_weights["groq"] == 0.5
        assert engine.provider_weights["openai"] == 1.0  # untouched

    def test_skips_when_no_weight_entry(self):
        weights = {"openai": 1.0}
        engine = _make_engine({}, weights=weights)
        alert = DriftAlert(
            provider="groq", task_type="code",
            previous_score=0.9, current_score=0.5,
            drop_pct=44.4,
        )
        actions = auto_reroute(engine, [alert])
        assert "skipped" in actions[0].lower()

    def test_no_weights_attribute(self):
        engine = SimpleNamespace()  # no provider_weights
        alert = DriftAlert(
            provider="groq", task_type="code",
            previous_score=0.9, current_score=0.5,
            drop_pct=44.4,
        )
        actions = auto_reroute(engine, [alert])
        assert len(actions) == 1
        assert "skipped" in actions[0].lower()
