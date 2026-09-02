"""Tests for nvh.core.advisor_profiles."""

from __future__ import annotations

from nvh.core.advisor_profiles import (
    ADVISOR_PROFILES,
    format_advisor_card,
    get_advisor_profile,
)


class TestAdvisorProfiles:
    def test_get_known_profile(self):
        profile = get_advisor_profile("openai")
        assert profile is not None
        assert profile.name == "openai"
        assert profile.display_name == "OpenAI"

    def test_get_unknown_profile_returns_none(self):
        assert get_advisor_profile("nonexistent_provider") is None

    def test_list_profiles_has_expected_providers(self):
        expected = {"openai", "anthropic", "google", "ollama", "groq"}
        assert expected.issubset(set(ADVISOR_PROFILES.keys()))

    def test_format_advisor_card_known(self):
        card = format_advisor_card("openai")
        assert "OpenAI" in card
        assert "Best for:" in card
        assert "Avoid for:" in card

    def test_format_advisor_card_unknown(self):
        card = format_advisor_card("does_not_exist")
        assert "Unknown advisor" in card
