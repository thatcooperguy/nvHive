"""Tests for the adaptive learning engine pure functions."""

import pytest

from nvh.core.learning import (
    FULL_LEARNED_SAMPLES,
    MIN_SAMPLES_TO_BLEND,
    blend_score,
    ema_update,
    implicit_quality,
    quality_to_capability,
)


class TestEmaUpdate:
    def test_ema_update(self):
        """Basic EMA: blends current value with new observation."""
        result = ema_update(0.5, 1.0, alpha=0.15)
        expected = 0.5 * 0.85 + 1.0 * 0.15
        assert result == pytest.approx(expected)

    def test_ema_update_convergence(self):
        """After many updates with the same value, EMA converges to it."""
        value = 0.5
        target = 0.9
        for _ in range(200):
            value = ema_update(value, target)
        assert value == pytest.approx(target, abs=1e-6)

    def test_ema_update_no_change(self):
        """When observation equals current, value stays the same."""
        result = ema_update(0.7, 0.7)
        assert result == pytest.approx(0.7)

    def test_ema_update_custom_alpha(self):
        """Custom alpha controls learning rate."""
        result = ema_update(0.5, 1.0, alpha=0.5)
        assert result == pytest.approx(0.75)


class TestBlendScore:
    def test_blend_score_low_samples(self):
        """Under MIN_SAMPLES_TO_BLEND, returns static score unchanged."""
        for n in range(MIN_SAMPLES_TO_BLEND):
            result = blend_score(static=0.8, learned=0.3, sample_count=n)
            assert result == pytest.approx(0.8), f"Failed at n={n}"

    def test_blend_score_high_samples(self):
        """At FULL_LEARNED_SAMPLES or above, returns learned score."""
        result = blend_score(static=0.8, learned=0.95, sample_count=FULL_LEARNED_SAMPLES)
        assert result == pytest.approx(0.95)

        result = blend_score(static=0.8, learned=0.95, sample_count=100)
        assert result == pytest.approx(0.95)

    def test_blend_score_medium_samples(self):
        """At midpoint between MIN and FULL, approximately 50/50 blend."""
        midpoint = (MIN_SAMPLES_TO_BLEND + FULL_LEARNED_SAMPLES) // 2
        # t = (midpoint - 5) / (20 - 5)
        t = (midpoint - MIN_SAMPLES_TO_BLEND) / (
            FULL_LEARNED_SAMPLES - MIN_SAMPLES_TO_BLEND
        )
        static, learned = 0.6, 0.9
        expected = static * (1.0 - t) + learned * t
        result = blend_score(static=static, learned=learned, sample_count=midpoint)
        assert result == pytest.approx(expected)

    def test_blend_score_boundary_min(self):
        """Exactly at MIN_SAMPLES_TO_BLEND, interpolation starts at t=0."""
        result = blend_score(static=0.5, learned=1.0, sample_count=MIN_SAMPLES_TO_BLEND)
        assert result == pytest.approx(0.5)

    def test_blend_score_boundary_full_minus_one(self):
        """One sample below FULL threshold, still partially blended."""
        n = FULL_LEARNED_SAMPLES - 1
        result = blend_score(static=0.5, learned=1.0, sample_count=n)
        assert result < 1.0
        assert result > 0.5


class TestQualityToCapability:
    def test_quality_to_capability(self):
        """1-10 quality maps to 0.0-1.0 capability linearly."""
        assert quality_to_capability(1.0) == pytest.approx(0.0)
        assert quality_to_capability(10.0) == pytest.approx(1.0)
        assert quality_to_capability(5.5) == pytest.approx(0.5)

    def test_quality_to_capability_clamps_low(self):
        """Values below 1 are clamped."""
        assert quality_to_capability(0.0) == pytest.approx(0.0)
        assert quality_to_capability(-5.0) == pytest.approx(0.0)

    def test_quality_to_capability_clamps_high(self):
        """Values above 10 are clamped."""
        assert quality_to_capability(15.0) == pytest.approx(1.0)


class TestImplicitQuality:
    def test_implicit_quality_signals(self):
        """Core implicit signals map to expected values."""
        # error -> 0.1
        assert implicit_quality("error", False, None) == pytest.approx(0.1)
        # fallback -> 0.1
        assert implicit_quality("success", True, None) == pytest.approx(0.1)
        # success -> 0.7
        assert implicit_quality("success", False, None) == pytest.approx(0.7)
        # thumbs up -> 0.9
        assert implicit_quality("success", False, 1) == pytest.approx(0.9)

    def test_implicit_quality_thumbs_down(self):
        """Thumbs down feedback returns 0.3."""
        assert implicit_quality("success", False, -1) == pytest.approx(0.3)

    def test_implicit_quality_feedback_overrides_status(self):
        """User feedback takes priority over error status."""
        assert implicit_quality("error", False, 1) == pytest.approx(0.9)
        assert implicit_quality("error", True, -1) == pytest.approx(0.3)
