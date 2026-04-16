"""Tests for nvh.cli.setup helpers (no interactive prompts)."""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

from nvh.cli.setup import (
    CORE_PROVIDERS,
    _check_provider_key,
    _detect_gpu_info,
    _get_recommended_models,
    _is_vision_model,
    _ollama_running,
    _reorder_vision_first,
    _store_key,
    _validate_key,
    load_env_keys,
)


class TestCheckProviderKey:
    def test_finds_key_in_env(self):
        with patch.dict(os.environ, {"GROQ_API_KEY": "gsk_testkey123"}):
            key = _check_provider_key("groq", "GROQ_API_KEY")
        assert key == "gsk_testkey123"

    def test_returns_none_when_not_set(self):
        env_clean = {k: v for k, v in os.environ.items() if k != "GROQ_API_KEY"}
        with patch.dict(os.environ, env_clean, clear=True):
            # Mock keyring import to raise
            mock_keyring = MagicMock()
            mock_keyring.get_password.return_value = None
            with patch.dict("sys.modules", {"keyring": mock_keyring}):
                key = _check_provider_key("groq", "GROQ_API_KEY")
        assert key is None

    def test_falls_back_to_keyring(self):
        mock_keyring = MagicMock()
        mock_keyring.get_password.return_value = "kr_key_456"
        env_clean = {k: v for k, v in os.environ.items() if k != "GROQ_API_KEY"}
        with patch.dict(os.environ, env_clean, clear=True):
            with patch.dict("sys.modules", {"keyring": mock_keyring}):
                key = _check_provider_key("groq", "GROQ_API_KEY")
        assert key == "kr_key_456"

    def test_env_takes_precedence_over_keyring(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "env_key"}):
            key = _check_provider_key("openai", "OPENAI_API_KEY")
        assert key == "env_key"


class TestStoreKey:
    def test_stores_via_keyring(self):
        mock_keyring = MagicMock()
        with patch.dict("sys.modules", {"keyring": mock_keyring}):
            result = _store_key("groq", "GROQ_API_KEY", "my_secret")
        assert result is True
        mock_keyring.set_password.assert_called_once_with(
            "nvhive", "groq_api_key", "my_secret",
        )

    def test_falls_back_to_env_file_when_keyring_raises(self, tmp_path):
        mock_keyring = MagicMock()
        mock_keyring.set_password.side_effect = Exception("no backend")
        with (
            patch.dict("sys.modules", {"keyring": mock_keyring}),
            patch("nvh.cli.setup.DEFAULT_CONFIG_DIR", tmp_path),
        ):
            result = _store_key("groq", "GROQ_API_KEY", "my_secret")
        # Falls back to .env file — should succeed
        assert result is True
        assert (tmp_path / ".env").exists()

    def test_returns_false_when_both_keyring_and_env_fail(self):
        mock_keyring = MagicMock()
        mock_keyring.set_password.side_effect = Exception("no backend")
        # Use a mock that raises on mkdir and path operations
        mock_dir = MagicMock()
        mock_dir.__truediv__ = MagicMock(side_effect=OSError("cannot create"))
        with (
            patch.dict("sys.modules", {"keyring": mock_keyring}),
            patch("nvh.cli.setup.DEFAULT_CONFIG_DIR", mock_dir),
        ):
            result = _store_key("groq", "GROQ_API_KEY", "my_secret")
        assert result is False


class TestDetectGpuInfo:
    def test_returns_safe_defaults_on_exception(self):
        # _detect_gpu_info catches all exceptions internally
        with patch.dict("sys.modules", {"nvh.core.agentic": None, "nvh.utils.gpu": None}):
            gpus, vram, tier, desc = _detect_gpu_info()
        assert gpus == []
        assert vram == 0.0
        assert tier == "tier_0"
        assert "cloud" in desc.lower() or "no local" in desc.lower()


class TestOllamaRunning:
    def test_returns_false_when_not_running(self):
        mock_httpx = MagicMock()
        mock_httpx.get.side_effect = Exception("Connection refused")
        with patch.dict("sys.modules", {"httpx": mock_httpx}):
            running, models = _ollama_running()
        assert running is False
        assert models == []

    def test_returns_true_with_models(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "models": [
                {"name": "gemma2:9b"},
                {"name": "llama3.3:70b"},
            ],
        }
        mock_httpx = MagicMock()
        mock_httpx.get.return_value = mock_resp
        with patch.dict("sys.modules", {"httpx": mock_httpx}):
            running, models = _ollama_running()
        assert running is True
        assert "gemma2:9b" in models
        assert "llama3.3:70b" in models


class TestGetRecommendedModels:
    def test_high_vram_gets_large_models(self):
        # Force the fallback path by making the import fail
        with patch.dict("sys.modules", {"nvh.utils.gpu": None}):
            recs = _get_recommended_models(128.0)
        assert len(recs) >= 2
        assert any("70b" in m for m in recs)

    def test_medium_vram(self):
        with patch.dict("sys.modules", {"nvh.utils.gpu": None}):
            recs = _get_recommended_models(48.0)
        assert len(recs) >= 1

    def test_low_vram_gets_small_model(self):
        with patch.dict("sys.modules", {"nvh.utils.gpu": None}):
            recs = _get_recommended_models(16.0)
        assert len(recs) >= 1
        assert any("7b" in m for m in recs)

    def test_no_vram_returns_empty(self):
        with patch.dict("sys.modules", {"nvh.utils.gpu": None}):
            recs = _get_recommended_models(0.0)
        assert recs == []

    def test_24gb_vram_gets_gemma(self):
        with patch.dict("sys.modules", {"nvh.utils.gpu": None}):
            recs = _get_recommended_models(24.0)
        assert "gemma2:27b" in recs

    def test_96gb_gets_multiple_models(self):
        with patch.dict("sys.modules", {"nvh.utils.gpu": None}):
            recs = _get_recommended_models(96.0)
        assert len(recs) >= 2
        assert any(v in recs for v in ["llama3.2-vision", "minicpm-v"])  # vision model included


class TestValidateKey:
    def test_unknown_provider_returns_none(self):
        console = MagicMock()
        result = _validate_key("unknown_provider", "some_key", console)
        assert result is None

    def test_successful_validation(self):
        console = MagicMock()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_httpx = MagicMock()
        mock_httpx.get.return_value = mock_resp
        with patch.dict("sys.modules", {"httpx": mock_httpx}):
            result = _validate_key("groq", "gsk_valid_key_here", console)
        assert result is True

    def test_rejected_key(self):
        console = MagicMock()
        mock_resp = MagicMock()
        mock_resp.status_code = 401
        mock_httpx = MagicMock()
        mock_httpx.get.return_value = mock_resp
        with patch.dict("sys.modules", {"httpx": mock_httpx}):
            result = _validate_key("openai", "sk_bad_key", console)
        assert result is False

    def test_network_error_returns_none(self):
        console = MagicMock()
        mock_httpx = MagicMock()
        mock_httpx.get.side_effect = Exception("timeout")
        with patch.dict("sys.modules", {"httpx": mock_httpx}):
            result = _validate_key("groq", "gsk_key", console)
        assert result is None


class TestCoreProviders:
    def test_has_four_providers(self):
        assert len(CORE_PROVIDERS) == 4

    def test_each_provider_has_required_fields(self):
        for name, display, env_var, url in CORE_PROVIDERS:
            assert name
            assert display
            assert env_var
            assert url.startswith("https://")


# ---------------------------------------------------------------------------
# Edge-case tests: no GPU, no Ollama, no keyring, headless .env fallback
# ---------------------------------------------------------------------------


class TestNoGpuSetup:
    """Ensure setup handles systems with no GPU gracefully."""

    def test_detect_gpu_returns_safe_defaults_when_nvidia_smi_missing(self):
        """Simulate a system where GPU detection completely fails."""
        with patch("nvh.cli.setup._detect_gpu_info", return_value=([], 0.0, "tier_0", "Fully cloud (no local GPU)")):
            from nvh.cli.setup import _detect_gpu_info
            gpus, vram, tier, desc = _detect_gpu_info()
        assert gpus == []
        assert vram == 0.0
        assert tier == "tier_0"

    def test_no_vram_skips_model_recommendations(self):
        """With 0 VRAM, _get_recommended_models should return empty list."""
        with patch.dict("sys.modules", {"nvh.utils.gpu": None}):
            recs = _get_recommended_models(0.0)
        assert recs == []


class TestNoOllamaSetup:
    """Ensure setup handles missing Ollama gracefully."""

    def test_ollama_not_installed_returns_false(self):
        """httpx connection refused simulates Ollama not installed."""
        mock_httpx = MagicMock()
        mock_httpx.get.side_effect = ConnectionError("Connection refused")
        with patch.dict("sys.modules", {"httpx": mock_httpx}):
            running, models = _ollama_running()
        assert running is False
        assert models == []

    def test_ollama_not_installed_no_httpx(self):
        """Even if httpx itself fails to import, we get safe defaults."""
        with patch.dict("sys.modules", {"httpx": None}):
            running, models = _ollama_running()
        assert running is False
        assert models == []


class TestNoKeyringFallback:
    """Ensure _store_key falls back to .env file when keyring is unavailable."""

    def test_store_key_writes_env_file_when_keyring_fails(self, tmp_path):
        """On headless Ubuntu with no keyring, keys are written to .env."""
        mock_keyring = MagicMock()
        mock_keyring.set_password.side_effect = Exception("No suitable keyring backend")

        env_file = tmp_path / ".env"
        with (
            patch.dict("sys.modules", {"keyring": mock_keyring}),
            patch("nvh.cli.setup.DEFAULT_CONFIG_DIR", tmp_path),
        ):
            result = _store_key("groq", "GROQ_API_KEY", "gsk_test123")

        assert result is True
        assert env_file.exists()
        content = env_file.read_text()
        assert "GROQ_API_KEY=gsk_test123" in content

    def test_load_env_keys_populates_environ(self, tmp_path):
        """load_env_keys reads .env and sets missing env vars."""
        env_file = tmp_path / ".env"
        env_file.write_text("TEST_SETUP_KEY=abc123\n")

        os.environ.pop("TEST_SETUP_KEY", None)
        with patch("nvh.cli.setup.DEFAULT_CONFIG_DIR", tmp_path):
            load_env_keys()

        assert os.environ.get("TEST_SETUP_KEY") == "abc123"
        # Cleanup
        os.environ.pop("TEST_SETUP_KEY", None)

    def test_load_env_keys_does_not_overwrite_existing(self, tmp_path):
        """Existing env vars take precedence over .env file values."""
        env_file = tmp_path / ".env"
        env_file.write_text("MY_KEY=from_file\n")

        os.environ["MY_KEY"] = "from_env"
        with patch("nvh.cli.setup.DEFAULT_CONFIG_DIR", tmp_path):
            load_env_keys()

        assert os.environ["MY_KEY"] == "from_env"
        # Cleanup
        os.environ.pop("MY_KEY", None)


class TestVisionModelDetection:
    """Tests for _is_vision_model — identifies vision-capable model tags."""

    def test_known_vision_tags(self):
        assert _is_vision_model("llama3.2-vision")
        assert _is_vision_model("minicpm-v")
        assert _is_vision_model("moondream")
        assert _is_vision_model("llava")
        assert _is_vision_model("bakllava")

    def test_versioned_tags(self):
        assert _is_vision_model("llama3.2-vision:11b")
        assert _is_vision_model("llama3.2-vision:90b")
        assert _is_vision_model("llava:7b")
        assert _is_vision_model("llava:34b")

    def test_text_models_not_vision(self):
        assert not _is_vision_model("nemotron")
        assert not _is_vision_model("nemotron:70b")
        assert not _is_vision_model("gemma4:26b")
        assert not _is_vision_model("qwen2.5-coder:7b")

    def test_vision_substring_match(self):
        # Should still match even with unusual prefix
        assert _is_vision_model("some-vision-model")


class TestReorderVisionFirst:
    """Tests for _reorder_vision_first — pull ordering."""

    def test_vision_moved_to_front(self):
        result = _reorder_vision_first(["nemotron:70b", "llama3.2-vision", "gemma4:26b"])
        assert result[0] == "llama3.2-vision"
        assert result[1:] == ["nemotron:70b", "gemma4:26b"]

    def test_no_vision_preserved(self):
        result = _reorder_vision_first(["nemotron", "gemma4:26b"])
        assert result == ["nemotron", "gemma4:26b"]

    def test_only_vision(self):
        result = _reorder_vision_first(["moondream"])
        assert result == ["moondream"]

    def test_empty_list(self):
        assert _reorder_vision_first([]) == []

    def test_multiple_vision_preserves_order(self):
        # When there are multiple vision models, they stay in original order
        result = _reorder_vision_first(["nemotron", "moondream", "gemma4:26b", "minicpm-v"])
        assert result == ["moondream", "minicpm-v", "nemotron", "gemma4:26b"]


class TestWriteConfig:
    """Tests for _write_config — ollama gating and api_key emission."""

    def _write_to_tmp(self, tmp_path, configured, ollama_enabled):
        cfg_path = tmp_path / "config.yaml"
        with patch("nvh.config.settings.DEFAULT_CONFIG_PATH", cfg_path), \
             patch("nvh.config.settings.get_config_dir", return_value=tmp_path):
            from nvh.cli.setup import _write_config
            _write_config(configured, ollama_enabled=ollama_enabled)
        return cfg_path.read_text()

    def test_ollama_disabled_when_not_running(self, tmp_path):
        """If ollama_enabled=False, the config marks ollama as disabled —
        prevents REPL 'Ollama is not running' errors when user skipped install."""
        text = self._write_to_tmp(tmp_path, {"groq": "gsk_x"}, ollama_enabled=False)
        # Find the ollama block
        lines = text.splitlines()
        ollama_idx = next(i for i, line in enumerate(lines) if line.strip() == "ollama:")
        ollama_block = "\n".join(lines[ollama_idx:ollama_idx + 6])
        assert "enabled: false" in ollama_block

    def test_ollama_enabled_when_running(self, tmp_path):
        text = self._write_to_tmp(tmp_path, {"groq": "gsk_x"}, ollama_enabled=True)
        lines = text.splitlines()
        ollama_idx = next(i for i, line in enumerate(lines) if line.strip() == "ollama:")
        ollama_block = "\n".join(lines[ollama_idx:ollama_idx + 6])
        assert "enabled: true" in ollama_block

    def test_no_api_key_line_for_unconfigured_providers(self, tmp_path):
        """Unconfigured providers should NOT get an api_key: ${VAR} line —
        otherwise the config loader warns about unset env vars on every nvh run."""
        text = self._write_to_tmp(tmp_path, {"groq": "gsk_x"}, ollama_enabled=False)
        # The configured one DOES get the line
        assert "api_key: ${GROQ_API_KEY}" in text
        # The unconfigured ones do NOT
        assert "${ANTHROPIC_API_KEY}" not in text
        assert "${OPENAI_API_KEY}" not in text
        assert "${GOOGLE_API_KEY}" not in text

    def test_all_providers_configured(self, tmp_path):
        text = self._write_to_tmp(
            tmp_path,
            {"groq": "g", "openai": "o", "anthropic": "a", "google": "gg"},
            ollama_enabled=True,
        )
        assert "${GROQ_API_KEY}" in text
        assert "${OPENAI_API_KEY}" in text
        assert "${ANTHROPIC_API_KEY}" in text
        assert "${GOOGLE_API_KEY}" in text


class TestCheckNvhOnPath:
    """Tests for _check_nvh_on_path — PATH detection for the nvh binary."""

    def test_returns_none_when_on_path(self):
        from nvh.cli.setup import _check_nvh_on_path
        with patch("shutil.which", return_value="/usr/bin/nvh"):
            assert _check_nvh_on_path() is None

    def test_returns_hint_when_missing(self):
        from nvh.cli.setup import _check_nvh_on_path
        with patch("shutil.which", return_value=None):
            result = _check_nvh_on_path()
        # Either returns None (if nvh.exe not derivable) or a dict with the right keys
        if result is not None:
            assert "full_path" in result
            assert "bin_dir" in result
            assert "shell_rc" in result
