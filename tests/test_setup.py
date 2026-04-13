"""Tests for nvh.cli.setup helpers (no interactive prompts)."""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

from nvh.cli.setup import (
    CORE_PROVIDERS,
    _check_provider_key,
    _detect_gpu_info,
    _get_recommended_models,
    _ollama_running,
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

    def test_96gb_gets_two_models(self):
        with patch.dict("sys.modules", {"nvh.utils.gpu": None}):
            recs = _get_recommended_models(96.0)
        assert len(recs) == 2


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
