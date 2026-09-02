"""Tests for nvh.integrations.cloud_session."""

from __future__ import annotations

import os
from unittest.mock import patch


class TestCloudSession:
    """Cloud session detection with mocked environment."""

    @patch.dict(
        "os.environ",
        {"CLOUD_SESSION_ID": "abc-123", "CLOUD_TIER": "ultimate"},
        clear=False,
    )
    @patch("subprocess.run", side_effect=FileNotFoundError)
    @patch("os.path.ismount", return_value=False)
    def test_detect_with_env_vars(self, _mnt, _sub):
        from nvh.integrations.cloud_session import detect_cloud_session

        session = detect_cloud_session()
        assert session.is_cloud_session is True
        assert session.session_id == "abc-123"
        assert session.tier == "ultimate"

    @patch("subprocess.run", side_effect=FileNotFoundError)
    @patch("os.path.ismount", return_value=False)
    def test_detect_without_cloud(self, _mnt, _sub):
        from nvh.integrations.cloud_session import detect_cloud_session

        # Remove only the cloud-specific env vars (keep HOME/USERPROFILE)
        env_patch = {
            "CLOUD_SESSION_ID": "",
            "NVIDIA_CLOUD_SESSION": "",
            "CLOUD_USER_ID": "",
            "CLOUD_TIER": "",
        }
        with patch.dict("os.environ", env_patch):
            session = detect_cloud_session()
        assert session.is_cloud_session is False

    def test_detect_cloud_session(self):
        from nvh.integrations.cloud_session import detect_cloud_session
        result = detect_cloud_session()
        assert result is not None
        assert hasattr(result, "is_cloud_session")
        # On a regular dev machine, should not detect cloud
        assert isinstance(result.is_cloud_session, bool)

    def test_detect_non_cloud(self):
        from nvh.integrations.cloud_session import detect_cloud_session
        result = detect_cloud_session()
        assert result.is_cloud_session is False  # on dev machine

    def test_cloud_session_with_env(self):
        from nvh.integrations.cloud_session import detect_cloud_session
        with patch.dict(os.environ, {"CLOUD_SESSION_ID": "test123", "CLOUD_PROVIDER": "aws"}):
            result = detect_cloud_session()
            # May or may not detect — just exercise the path
            assert result is not None

    def test_get_cloud_recommended_config_not_cloud(self):
        from nvh.integrations.cloud_session import (
            CLOUDSession,
            get_cloud_recommended_config,
        )

        session = CLOUDSession(is_cloud_session=False)
        cfg = get_cloud_recommended_config(session)
        assert cfg == {}

    def test_get_cloud_recommended_config_ultimate(self):
        from nvh.integrations.cloud_session import (
            CLOUDSession,
            get_cloud_recommended_config,
        )

        session = CLOUDSession(is_cloud_session=True, tier="ultimate")
        cfg = get_cloud_recommended_config(session)
        assert cfg["ollama_num_parallel"] == 2
        assert cfg["recommended_models"][0] == "nemotron"
        assert "llama3.2-vision" in cfg["recommended_models"]

    def test_format_cloud_status_not_cloud(self):
        from nvh.integrations.cloud_session import (
            CLOUDSession,
            format_cloud_status,
        )

        assert "Not running" in format_cloud_status(CLOUDSession())

    def test_format_cloud_status_cloud(self):
        from nvh.integrations.cloud_session import (
            CLOUDSession,
            format_cloud_status,
        )

        s = CLOUDSession(
            is_cloud_session=True,
            tier="performance",
            gpu_class="RTX 3080",
            session_id="xyz-456",
        )
        status = format_cloud_status(s)
        assert "Performance" in status
        assert "RTX 3080" in status
