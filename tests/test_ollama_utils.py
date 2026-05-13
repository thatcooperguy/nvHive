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
        assert strip_ollama_prefix("ollama/llama3.2-vision") == "llama3.2-vision"

    def test_leaves_unprefixed_alone(self):
        assert strip_ollama_prefix("llama3.2-vision") == "llama3.2-vision"

    def test_empty_string(self):
        assert strip_ollama_prefix("") == ""

    def test_with_tag(self):
        assert strip_ollama_prefix("ollama/llama3.2-vision:11b") == "llama3.2-vision:11b"


class TestTagsMatch:
    def test_exact(self):
        assert _tags_match("llama3.2-vision", "llama3.2-vision")

    def test_required_untagged_matches_any_tag(self):
        assert _tags_match("llama3.2-vision", "llama3.2-vision:latest")
        assert _tags_match("llama3.2-vision", "llama3.2-vision:11b")

    def test_tag_mismatch_fails(self):
        assert not _tags_match("qwen3:8b", "qwen3:14b")

    def test_base_mismatch_fails(self):
        # prevents accidental prefix over-matching
        assert not _tags_match("llama3.2", "llama3.2-vision")

    def test_empty_inputs(self):
        assert not _tags_match("", "llama3.2-vision")
        assert not _tags_match("llama3.2-vision", "")


class TestMissingModels:
    def test_all_present(self):
        assert missing_models(
            ["llama3.2-vision"],
            ["llama3.2-vision:latest", "qwen3:8b"],
        ) == []

    def test_some_missing(self):
        assert missing_models(
            ["qwen3:8b", "llama3.2-vision"],
            ["qwen3:8b"],
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
                {"name": "qwen3:8b"},
                {"name": "llama3.2-vision:latest"},
            ]
        }
        with patch("httpx.get", return_value=mock_resp):
            out = list_installed_models()
        assert out == ["qwen3:8b", "llama3.2-vision:latest"]

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
                "default_model": "ollama/qwen3:8b",
                "fallback_model": "",
            },
        })
        assert required_ollama_models(config) == ["qwen3:8b"]

    def test_ignores_disabled_advisors(self):
        config = self._make_config({
            "ollama": {
                "enabled": False,
                "type": "ollama",
                "default_model": "ollama/qwen3:8b",
                "fallback_model": "",
            },
        })
        assert required_ollama_models(config) == []

    def test_collects_default_and_fallback(self):
        config = self._make_config({
            "ollama": {
                "enabled": True,
                "type": "ollama",
                "default_model": "ollama/qwen3:8b",
                "fallback_model": "ollama/gemma3:4b",
            },
        })
        assert set(required_ollama_models(config)) == {"qwen3:8b", "gemma3:4b"}

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
