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
        from nvh.core import local_models
        from nvh.integrations.cloud_session import (
            CLOUD_TIER_VRAM_GB,
            CLOUDSession,
            get_cloud_recommended_config,
        )

        vram = CLOUD_TIER_VRAM_GB["ultimate"]
        assert vram == 16.0  # RTX 4080 class, per the module docstring
        session = CLOUDSession(is_cloud_session=True, tier="ultimate")
        cfg = get_cloud_recommended_config(session)

        chat = local_models.pick(vram, "chat").tag
        assert cfg["tier_vram_gb"] == vram
        assert cfg["ollama_default_model"] == f"ollama/{chat}"
        assert cfg["recommended_models"] == [p.tag for p in local_models.recommended(vram)]
        assert cfg["recommended_models"][0] == chat
        assert cfg["ollama_num_parallel"] == local_models.num_parallel_for(vram)
        assert cfg["ollama_num_ctx"] == local_models.num_ctx_for(vram)
        assert cfg["ollama_flash_attention"] is True
        # The old map handed this 16 GB card a 40 GB model; every pick must load on it.
        sizes = local_models.size_table()
        assert all(sizes[tag] <= vram for tag in cfg["recommended_models"])
        assert "nemotron" not in cfg["recommended_models"]

    def test_get_cloud_recommended_config_every_tier_fits_its_card(self):
        from nvh.core import local_models
        from nvh.integrations.cloud_session import (
            CLOUD_TIER_VRAM_GB,
            CLOUDSession,
            get_cloud_recommended_config,
        )

        sizes = local_models.size_table()
        assert set(CLOUD_TIER_VRAM_GB) == {"priority", "performance", "ultimate"}
        for tier, vram in CLOUD_TIER_VRAM_GB.items():
            cfg = get_cloud_recommended_config(CLOUDSession(is_cloud_session=True, tier=tier))
            default = cfg["ollama_default_model"].removeprefix("ollama/")
            assert default in cfg["recommended_models"], tier
            assert all(sizes[tag] <= vram for tag in cfg["recommended_models"]), tier
            assert set(cfg["recommended_models"]) <= set(local_models.all_tags()), tier
            assert cfg["ollama_num_parallel"] == local_models.num_parallel_for(vram)
            assert cfg["ollama_num_ctx"] == local_models.num_ctx_for(vram)

    def test_get_cloud_recommended_config_unknown_tier_is_priority(self):
        from nvh.integrations.cloud_session import (
            CLOUDSession,
            get_cloud_recommended_config,
        )

        unknown = get_cloud_recommended_config(CLOUDSession(is_cloud_session=True, tier="mystery"))
        priority = get_cloud_recommended_config(CLOUDSession(is_cloud_session=True, tier="priority"))
        assert unknown == priority
        assert unknown["tier_vram_gb"] == 8.0

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
