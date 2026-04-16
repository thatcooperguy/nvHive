"""Tests for nvh.utils.ollama — shared model-health helpers."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from nvh.utils.ollama import (
    _tags_match,
    list_installed_models,
    missing_models,
    required_ollama_models,
    strip_ollama_prefix,
)


class TestStripOllamaPrefix:
    def test_strips_prefix(self):
        assert strip_ollama_prefix("ollama/nemotron-small") == "nemotron-small"

    def test_leaves_unprefixed_alone(self):
        assert strip_ollama_prefix("nemotron-small") == "nemotron-small"

    def test_empty_string(self):
        assert strip_ollama_prefix("") == ""

    def test_with_tag(self):
        assert strip_ollama_prefix("ollama/llama3.2-vision:11b") == "llama3.2-vision:11b"


class TestTagsMatch:
    def test_exact(self):
        assert _tags_match("nemotron-small", "nemotron-small")

    def test_required_untagged_matches_any_tag(self):
        assert _tags_match("nemotron-small", "nemotron-small:latest")
        assert _tags_match("nemotron-small", "nemotron-small:11b")

    def test_tag_mismatch_fails(self):
        assert not _tags_match("nemotron-small:latest", "nemotron-small:11b")

    def test_base_mismatch_fails(self):
        # prevents accidental prefix over-matching
        assert not _tags_match("nemotron", "nemotron-small")

    def test_empty_inputs(self):
        assert not _tags_match("", "nemotron-small")
        assert not _tags_match("nemotron-small", "")


class TestMissingModels:
    def test_all_present(self):
        assert missing_models(
            ["nemotron-small"],
            ["nemotron-small:latest", "llama3.2-vision:latest"],
        ) == []

    def test_some_missing(self):
        assert missing_models(
            ["nemotron-small", "llama3.2-vision"],
            ["nemotron-small:latest"],
        ) == ["llama3.2-vision"]

    def test_all_missing(self):
        assert missing_models(["a", "b"], []) == ["a", "b"]

    def test_preserves_order(self):
        assert missing_models(
            ["c", "a", "b"],
            [],
        ) == ["c", "a", "b"]


class TestListInstalledModels:
    def test_returns_models_on_success(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "models": [
                {"name": "nemotron-small:latest"},
                {"name": "llama3.2-vision:latest"},
            ]
        }
        with patch("httpx.get", return_value=mock_resp):
            out = list_installed_models()
        assert out == ["nemotron-small:latest", "llama3.2-vision:latest"]

    def test_returns_empty_on_http_error(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        with patch("httpx.get", return_value=mock_resp):
            out = list_installed_models()
        assert out == []

    def test_returns_empty_on_network_error(self):
        with patch("httpx.get", side_effect=Exception("refused")):
            out = list_installed_models()
        assert out == []


class TestRequiredOllamaModels:
    def _make_config(self, providers: dict):
        """Build a minimal duck-typed config object for the helper."""
        return SimpleNamespace(providers={
            name: SimpleNamespace(**attrs) for name, attrs in providers.items()
        })

    def test_collects_from_enabled_ollama_advisors(self):
        config = self._make_config({
            "ollama": {
                "enabled": True,
                "type": "ollama",
                "default_model": "ollama/nemotron-small",
                "fallback_model": "",
            },
        })
        assert required_ollama_models(config) == ["nemotron-small"]

    def test_ignores_disabled_advisors(self):
        config = self._make_config({
            "ollama": {
                "enabled": False,
                "type": "ollama",
                "default_model": "ollama/nemotron-small",
                "fallback_model": "",
            },
        })
        assert required_ollama_models(config) == []

    def test_collects_default_and_fallback(self):
        config = self._make_config({
            "ollama": {
                "enabled": True,
                "type": "ollama",
                "default_model": "ollama/nemotron-small",
                "fallback_model": "ollama/nemotron-mini",
            },
        })
        assert set(required_ollama_models(config)) == {"nemotron-small", "nemotron-mini"}

    def test_detects_ollama_via_default_model_prefix(self):
        """Advisor without type='ollama' but with ollama/ prefix still counts."""
        config = self._make_config({
            "vision": {
                "enabled": True,
                "type": "",
                "default_model": "ollama/llama3.2-vision",
                "fallback_model": "",
            },
        })
        assert required_ollama_models(config) == ["llama3.2-vision"]

    def test_skips_non_ollama_providers(self):
        config = self._make_config({
            "groq": {
                "enabled": True,
                "type": "",
                "default_model": "groq/llama-3.3-70b",
                "fallback_model": "",
            },
        })
        assert required_ollama_models(config) == []

    def test_deduplicates(self):
        config = self._make_config({
            "a": {
                "enabled": True, "type": "ollama",
                "default_model": "ollama/nemotron", "fallback_model": "ollama/nemotron",
            },
            "b": {
                "enabled": True, "type": "ollama",
                "default_model": "ollama/nemotron", "fallback_model": "",
            },
        })
        assert required_ollama_models(config) == ["nemotron"]
