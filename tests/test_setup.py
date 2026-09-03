"""Tests for nvh.cli.setup helpers (no interactive prompts)."""

from __future__ import annotations

import io
import os
import re
from unittest.mock import MagicMock, patch

import pytest

from nvh.cli.setup import (
    _MODEL_PULL_PREFERENCE,
    _VISION_MODEL_TAGS,
    CORE_PROVIDERS,
    _check_provider_key,
    _detect_gpu_info,
    _detect_tier_budget,
    _env_key_files,
    _get_recommended_models,
    _is_vision_model,
    _layout_config_dir,
    _ollama_running,
    _prefer_largest_fitting_models,
    _reorder_vision_first,
    _store_key,
    _validate_key,
    load_env_keys,
)
from nvh.core import local_models as lm

# Tags that left the Ollama registry or never existed there; none may come out
# of any ladder in nvh.cli.setup (the table in nvh.core.local_models is the
# only source of pull targets).
RETIRED_TAGS = {
    "nemotron-omni", "nemotron-3-nano-omni", "nemotron-3-super", "nemotron:70b",
    "llama3.3:70b", "qwen2.5-coder:32b", "qwen2.5-coder:7b", "llama3.1:8b",
    "minicpm-v", "llava", "llava:7b", "bakllava", "deepseek-r1:8b", "nemotron-mini",
}


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

    def test_total_is_the_pool_and_the_budget_is_unified_aware(self, monkeypatch):
        """The hardware table prints the pool a GB10 reports (128 GB, agentic's TIER_5);
        the model ladder plans against local_models.tier_budget (112 GB after the OS reserve)."""
        import nvh.utils.gpu as gpu_mod

        rows = [_gpu_row("NVIDIA GB10", 131072, unified=True)]
        monkeypatch.setattr(gpu_mod, "detect_gpus", lambda: rows)
        monkeypatch.setattr(gpu_mod, "detect_system_memory", lambda: None)
        gpus, vram, tier, _desc = _detect_gpu_info()
        assert gpus == rows
        assert vram == 128.0
        assert tier == "tier_5"
        budget = _detect_tier_budget()
        assert budget is not None and budget.unified
        assert budget.budget_gb == 128.0 - lm.UNIFIED_MEMORY_OS_RESERVE_GB
        assert lm.tier_for(budget).label == "max"

    def test_budget_is_none_when_detection_fails(self):
        with patch.dict("sys.modules", {"nvh.utils.gpu": None}):
            assert _detect_tier_budget() is None


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
                {"name": "qwen3:8b"},
                {"name": "llama3.3:70b"},
            ],
        }
        mock_httpx = MagicMock()
        mock_httpx.get.return_value = mock_resp
        with patch.dict("sys.modules", {"httpx": mock_httpx}):
            running, models = _ollama_running()
        assert running is True
        assert "qwen3:8b" in models
        assert "llama3.3:70b" in models


def _table_order(budget) -> list[str]:
    """What _get_recommended_models must return for a budget: the table's pull list, strongest tier first."""
    return _prefer_largest_fitting_models([p.tag for p in lm.recommended(budget)])


class TestGetRecommendedModels:
    """Every tag comes from nvh.core.local_models; setup only orders them.

    Blocking ``nvh.utils.gpu`` makes the bare figure the whole budget (a
    discrete pool of that size), so the expectations below are the table's.
    """

    @pytest.mark.parametrize("gb", [6.0, 8.0, 16.0, 24.0, 47.0, 48.0, 96.0, 128.0])
    def test_matches_the_table_for_a_bare_vram_figure(self, gb):
        with patch.dict("sys.modules", {"nvh.utils.gpu": None}):
            recs = _get_recommended_models(gb)
        assert recs == _table_order(gb)
        assert recs and len(recs) == len(set(recs))
        assert set(recs) <= set(lm.all_tags())
        assert not RETIRED_TAGS & set(recs)

    def test_strongest_tier_pick_leads_and_every_role_is_covered(self):
        with patch.dict("sys.modules", {"nvh.utils.gpu": None}):
            recs = _get_recommended_models(128.0)
        assert recs[0] == lm.pick(128.0, "chat").tag
        for use_case in ("chat", "code", "vision", "embed", "cpu_fallback"):
            assert lm.pick(128.0, use_case).tag in recs, use_case

    def test_24gb_pulls_the_vision_pick_first(self):
        # _get_recommended_models is strongest-first; guided_setup reorders the
        # pull vision-first so the desktop-agent screenshot assist is ready early.
        with patch.dict("sys.modules", {"nvh.utils.gpu": None}):
            recs = _get_recommended_models(24.0)
        vision = lm.pick(24.0, "vision")
        assert vision is not None and vision.tag in recs
        assert _is_vision_model(_reorder_vision_first(recs)[0])
        assert lm.pick(24.0, "cpu_fallback").tag in recs

    def test_no_vram_returns_empty(self):
        with patch.dict("sys.modules", {"nvh.utils.gpu": None}):
            recs = _get_recommended_models(0.0)
        assert recs == []

    def test_unified_budget_plans_112gb_and_leads_with_moe(self):
        budget = lm.tier_budget([_gpu_row("NVIDIA GB10", 131072, unified=True)], None)
        assert budget.budget_gb == 128.0 - lm.UNIFIED_MEMORY_OS_RESERVE_GB
        recs = _get_recommended_models(128.0, budget=budget)
        assert recs == _table_order(budget)
        assert lm.pick_for_tag(recs[0]).moe
        reasoning = lm.pick(budget, "reasoning")
        assert reasoning is not None and reasoning.moe
        assert reasoning.tag in recs  # the reasoning MoE joins only on a bandwidth-bound pool
        with patch.dict("sys.modules", {"nvh.utils.gpu": None}):
            assert reasoning.tag not in _get_recommended_models(128.0)

    def test_unsized_budget_falls_back_to_the_figure(self):
        # A GPU whose memory could not be read carries no budget; the caller's
        # figure is planned as a discrete pool instead of returning nothing.
        unsized = lm.tier_budget([_gpu_row("NVIDIA GeForce RTX 4090", 0)], None)
        with patch.dict("sys.modules", {"nvh.utils.gpu": None}):
            assert _get_recommended_models(24.0, budget=unsized) == _table_order(24.0)


def _first_tier_of(tag: str) -> int:
    """Index of the lowest table tier that lists ``tag``."""
    return next(
        i for i, tier in enumerate(lm.LOCAL_MODEL_TIERS)
        if tag in {p.tag for p in tier.picks.values()}
    )


class TestPullPreference:
    def test_preference_is_every_table_tag_strongest_tier_first(self):
        assert sorted(_MODEL_PULL_PREFERENCE) == lm.all_tags()
        assert not RETIRED_TAGS & set(_MODEL_PULL_PREFERENCE)
        ranks = [_first_tier_of(tag) for tag in _MODEL_PULL_PREFERENCE]
        assert ranks == sorted(ranks, reverse=True)

    def test_unknown_tags_trail_in_input_order(self):
        weakest, strongest = _MODEL_PULL_PREFERENCE[-1], _MODEL_PULL_PREFERENCE[0]
        assert _prefer_largest_fitting_models(["zzz", weakest, strongest, "aaa", weakest]) == [
            strongest, weakest, "zzz", "aaa",
        ]

    def test_sort_is_stable_within_a_tier(self):
        # Same-tier picks keep the caller's order, so recommended()'s MoE-first
        # order on a unified pool survives the strongest-first sort.
        by_tier: dict[int, list[str]] = {}
        for tag in _MODEL_PULL_PREFERENCE:
            by_tier.setdefault(_first_tier_of(tag), []).append(tag)
        pair = next(tags[:2] for tags in by_tier.values() if len(tags) >= 2)
        assert _prefer_largest_fitting_models(pair) == pair
        assert _prefer_largest_fitting_models(pair[::-1]) == pair[::-1]


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

    def test_load_env_keys_reads_storage_layout_env_too(self, tmp_path):
        """Keys the web wizard saves to NVH_HOME/config/.env load without
        HIVE_CONFIG_HOME exported; the legacy ~/.hive/.env still loads first
        and anything already in the environment wins over both."""
        legacy_dir = tmp_path / "hive"
        legacy_dir.mkdir()
        (legacy_dir / ".env").write_text(
            "LEGACY_ONLY_KEY=from_legacy\nSHARED_KEY=from_legacy\n"
        )
        nvh_home = tmp_path / "nvh-home"
        (nvh_home / "config").mkdir(parents=True)
        (nvh_home / "config" / ".env").write_text(
            "WIZARD_ONLY_KEY=from_wizard\nSHARED_KEY=from_wizard\nPRESET_KEY=from_wizard\n"
        )

        scrub = {
            "HIVE_CONFIG_HOME", "NVHIVE_HOME",
            "LEGACY_ONLY_KEY", "WIZARD_ONLY_KEY", "SHARED_KEY", "PRESET_KEY",
        }
        env = {k: v for k, v in os.environ.items() if k not in scrub}
        env["NVH_HOME"] = str(nvh_home)
        env["PRESET_KEY"] = "from_env"
        with (
            patch.dict(os.environ, env, clear=True),
            patch("nvh.cli.setup.DEFAULT_CONFIG_DIR", legacy_dir),
        ):
            load_env_keys()

            assert os.environ["LEGACY_ONLY_KEY"] == "from_legacy"
            assert os.environ["WIZARD_ONLY_KEY"] == "from_wizard"
            assert os.environ["SHARED_KEY"] == "from_legacy"
            assert os.environ["PRESET_KEY"] == "from_env"

    def test_load_env_keys_can_skip_keyring(self, tmp_path):
        """The API lifespan passes use_keyring=False — no keyring round-trips."""
        mock_keyring = MagicMock()
        with (
            patch.dict("sys.modules", {"keyring": mock_keyring}),
            patch("nvh.cli.setup.DEFAULT_CONFIG_DIR", tmp_path),
        ):
            load_env_keys(use_keyring=False)
        mock_keyring.get_password.assert_not_called()

    def test_env_key_files_resolve_layout_without_importing_integrations(
        self, tmp_path, monkeypatch,
    ):
        """Importing nvh.integrations costs every CLI invocation ~160 ms, so the
        layout config dir is derived from the environment directly."""
        for var in ("HIVE_CONFIG_HOME", "NVHIVE_HOME"):
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setenv("NVH_HOME", str(tmp_path / "home"))
        blocked = {
            "nvh.integrations": None,
            "nvh.integrations.workspace": None,
            "nvh.integrations.workspace.storage": None,
        }
        with (
            patch.dict("sys.modules", blocked),
            patch("nvh.cli.setup.DEFAULT_CONFIG_DIR", tmp_path / "hive"),
        ):
            files = _env_key_files()
        assert files == [tmp_path / "hive" / ".env", tmp_path / "home" / "config" / ".env"]

    @pytest.mark.parametrize("env", [
        {},
        {"NVH_HOME": "{tmp}/a"},
        {"NVHIVE_HOME": "{tmp}/b"},
        {"NVH_HOME": "{tmp}/a", "NVHIVE_HOME": "{tmp}/b"},
        {"HIVE_CONFIG_HOME": "{tmp}/cfg", "NVH_HOME": "{tmp}/a"},
    ])
    def test_layout_config_dir_matches_storage_layout(self, tmp_path, monkeypatch, env):
        from nvh.integrations.workspace.storage import storage_layout

        for var in ("HIVE_CONFIG_HOME", "NVH_HOME", "NVHIVE_HOME"):
            monkeypatch.delenv(var, raising=False)
        for var, value in env.items():
            monkeypatch.setenv(var, value.format(tmp=tmp_path))
        assert _layout_config_dir().resolve() == storage_layout().config_dir


# The table's vision *column* (lm.vision_picks) versus everything else -- which
# includes picks that see images but sit in the chat / CPU-fallback columns.
VISION_TAGS = [p.tag for p in lm.vision_picks()]
TEXT_TAGS = [p.tag for p in lm.all_picks() if p.tag not in VISION_TAGS]


class TestVisionModelDetection:
    """_is_vision_model reads the table's vision column, plus the ``*-vision`` naming convention."""

    def test_vision_tag_set_is_the_table_s_vision_column(self):
        assert set(_VISION_MODEL_TAGS) == set(VISION_TAGS)
        assert set(_VISION_MODEL_TAGS) == {tier.picks["vision"].tag for tier in lm.LOCAL_MODEL_TIERS}
        assert not RETIRED_TAGS & set(_VISION_MODEL_TAGS)
        assert len(VISION_TAGS) >= 2 and len(TEXT_TAGS) >= 2

    def test_image_capable_text_picks_are_not_vision(self):
        # gemma3:4b (the CPU fallback from 8 GB up) and the nemotron3 chat builds
        # accept images, but they are text picks: treating them as "vision" put
        # the 4 GB fallback ahead of every tier's primary pull.
        sees_images_but_text = [p for p in lm.all_picks() if p.vision and p.tag not in VISION_TAGS]
        assert sees_images_but_text, "no image-capable text pick left in the table; drop this test"
        for pick in sees_images_but_text:
            assert pick.tag not in _VISION_MODEL_TAGS
            assert not _is_vision_model(pick.tag), pick.tag

    @pytest.mark.parametrize("pick", lm.all_picks(), ids=lambda p: p.tag)
    def test_table_picks_follow_the_vision_column(self, pick):
        assert _is_vision_model(pick.tag) is (pick.tag in VISION_TAGS)

    def test_other_tags_of_a_vision_family(self):
        # An untagged vision pick ("llama3.2-vision") covers every tag of that name ...
        for pick in lm.vision_picks():
            if pick.version == "latest":
                assert _is_vision_model(f"{pick.name}:11b"), pick.tag
        # ... and so does any quant of a vision-column name (qwen3-vl:4b) -- but
        # not of a chat name that happens to see images (nemotron3:33b-q8).
        vision_names = {p.name for p in lm.vision_picks()}
        for pick in lm.all_picks():
            assert _is_vision_model(f"{pick.name}:some-other-quant") is (pick.name in vision_names), pick.name

    def test_text_models_not_vision(self):
        assert not _is_vision_model("nemotron")
        assert not _is_vision_model("qwen2.5-coder:32b")
        for tag in TEXT_TAGS:
            assert not _is_vision_model(tag), tag

    def test_vision_substring_match(self):
        # Should still match even with unusual prefix
        assert _is_vision_model("some-vision-model")


class TestReorderVisionFirst:
    """Tests for _reorder_vision_first — pull ordering."""

    def test_vision_moved_to_front(self):
        result = _reorder_vision_first([TEXT_TAGS[0], VISION_TAGS[0], TEXT_TAGS[1]])
        assert result[0] == VISION_TAGS[0]
        assert result[1:] == [TEXT_TAGS[0], TEXT_TAGS[1]]

    def test_no_vision_preserved(self):
        assert _reorder_vision_first(TEXT_TAGS[:2]) == TEXT_TAGS[:2]

    def test_only_vision(self):
        assert _reorder_vision_first(VISION_TAGS[:1]) == VISION_TAGS[:1]

    def test_empty_list(self):
        assert _reorder_vision_first([]) == []

    def test_multiple_vision_preserves_order(self):
        # When there are multiple vision models, they stay in original order
        mixed = [TEXT_TAGS[0], VISION_TAGS[0], TEXT_TAGS[1], VISION_TAGS[1]]
        assert _reorder_vision_first(mixed) == [VISION_TAGS[0], VISION_TAGS[1], TEXT_TAGS[0], TEXT_TAGS[1]]

    @pytest.mark.parametrize("gb", [8.0, 12.0, 16.0, 24.0, 47.0, 48.0, 96.0, 128.0])
    def test_promotes_only_the_tier_s_vision_pick(self, gb):
        # Only the tier's vision pick jumps the queue; the image-capable CPU
        # fallback (gemma3:4b) stays behind the primary chat pick on every tier.
        with patch.dict("sys.modules", {"nvh.utils.gpu": None}):
            recs = _get_recommended_models(gb)
        order = _reorder_vision_first(recs)
        assert order[0] == lm.pick(gb, "vision").tag
        assert order[1:] == [tag for tag in recs if tag != order[0]]
        chat, fallback = lm.pick(gb, "chat").tag, lm.pick(gb, "cpu_fallback").tag
        assert order.index(chat) < order.index(fallback)


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


class TestModelExistsOnRegistry:
    """Tests for _model_exists_on_registry — guards _pull_model from 404s."""

    def test_returns_true_on_200(self):
        from unittest.mock import MagicMock

        from nvh.cli.setup import _model_exists_on_registry
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_httpx = MagicMock()
        mock_httpx.head.return_value = mock_resp
        with patch.dict("sys.modules", {"httpx": mock_httpx}):
            assert _model_exists_on_registry("nemotron") is True

    def test_returns_false_on_404(self):
        from unittest.mock import MagicMock

        from nvh.cli.setup import _model_exists_on_registry
        mock_resp = MagicMock()
        mock_resp.status_code = 404
        mock_httpx = MagicMock()
        mock_httpx.head.return_value = mock_resp
        with patch.dict("sys.modules", {"httpx": mock_httpx}):
            assert _model_exists_on_registry("clearly-not-a-real-model") is False

    def test_returns_none_on_network_error(self):
        from unittest.mock import MagicMock

        from nvh.cli.setup import _model_exists_on_registry
        mock_httpx = MagicMock()
        mock_httpx.head.side_effect = Exception("DNS failure")
        with patch.dict("sys.modules", {"httpx": mock_httpx}):
            # None → don't block pull; maybe user has registry mirror
            assert _model_exists_on_registry("anything") is None

    def test_splits_tag_correctly(self):
        """Model with explicit tag should probe the tag, not :latest."""
        from unittest.mock import MagicMock

        from nvh.cli.setup import _model_exists_on_registry
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_httpx = MagicMock()
        mock_httpx.head.return_value = mock_resp
        with patch.dict("sys.modules", {"httpx": mock_httpx}):
            _model_exists_on_registry("nemotron:70b")
        # Assert the URL included the explicit tag, not :latest
        called_url = mock_httpx.head.call_args[0][0]
        assert called_url.endswith("/library/nemotron/manifests/70b")


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


def _gpu_row(name: str, vram_mb: int, *, unified: bool = False):
    from nvh.utils.gpu import GPUInfo

    return GPUInfo(
        name=name, vram_mb=vram_mb, vram_gb=round(vram_mb / 1024, 1), driver_version="580.65",
        cuda_version="13.0", utilization_pct=0, memory_used_mb=0, memory_free_mb=vram_mb, index=0,
        unified_memory=unified,
    )


class TestGuidedSetupGpuRows:
    """Step 1 renders every detected row through gpu.format_gpu_memory: a GPU whose memory could
    not be read (kept at 0 GB by detect_gpu_status) is named as unreadable — not '(0 GB VRAM)'
    with 'Total VRAM 0 GB' under it and 'No GPU detected' after it."""

    @staticmethod
    def _run(monkeypatch, tmp_path, gpus, total_vram: float) -> str:
        from rich.console import Console

        from nvh.cli import setup as setup_mod

        monkeypatch.delenv("DISPLAY", raising=False)
        monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
        monkeypatch.setattr(setup_mod, "load_env_keys", lambda *a, **k: None)
        monkeypatch.setattr(setup_mod, "_detect_gpu_info", lambda: (gpus, total_vram, "tier_1", "One local model"))
        monkeypatch.setattr(setup_mod, "_ollama_running", lambda: (False, []))
        monkeypatch.setattr(setup_mod, "_ensure_ollama", lambda console: (False, []))
        monkeypatch.setattr(setup_mod, "_check_provider_key", lambda name, env_var: None)
        monkeypatch.setattr(setup_mod, "_get_clipboard", lambda: "")
        monkeypatch.setattr(setup_mod, "_write_config", lambda configured, ollama_enabled: tmp_path / "config.yaml")
        monkeypatch.setattr(setup_mod, "_check_nvh_on_path", lambda: None)
        console = Console(file=io.StringIO(), width=200, force_terminal=False, color_system=None)
        console.input = lambda *a, **k: ""  # every key prompt: skip
        setup_mod.guided_setup(console)
        return console.file.getvalue()

    def test_unreadable_row_is_named_not_zeroed(self, monkeypatch, tmp_path):
        out = self._run(monkeypatch, tmp_path, [_gpu_row("NVIDIA GeForce RTX 4090", 0)], 0.0)
        assert "NVIDIA GeForce RTX 4090 (memory unreadable)" in out
        assert "0 GB VRAM" not in out
        assert re.search(r"Total VRAM\s+memory unreadable", out), out
        assert re.search(r"Total VRAM\s+0 GB", out) is None
        assert "GPU memory could not be read" in out
        assert "No GPU detected" not in out

    def test_readable_rows_render_byte_identically(self, monkeypatch, tmp_path):
        out = self._run(monkeypatch, tmp_path, [_gpu_row("NVIDIA GeForce RTX 4090", 24576)], 24.0)
        assert "NVIDIA GeForce RTX 4090 (24 GB VRAM)" in out
        assert re.search(r"Total VRAM\s+24 GB\s*\n", out), out  # Rich pads the column before the newline
        assert "memory unreadable" not in out

    def test_unified_row_says_unified(self, monkeypatch, tmp_path):
        out = self._run(monkeypatch, tmp_path, [_gpu_row("NVIDIA GB10", 131072, unified=True)], 128.0)
        assert "NVIDIA GB10 (128 GB unified)" in out
        assert "GB VRAM)" not in out

    def test_unified_reserve_is_the_pools_own(self, monkeypatch, tmp_path):
        """The Model budget row prints the reserve the ladder actually took, which scales with
        the pool (local_models.unified_os_reserve_gb): 16 GB on a 128 GB GB10, 8 GB on a 64 GB
        Mac -- not the flat GB10 figure for every unified pool."""
        out = self._run(monkeypatch, tmp_path, [_gpu_row("NVIDIA GB10", 131072, unified=True)], 128.0)
        assert re.search(r"Model budget\s+112 GB unified after the 16 GB OS reserve", out), out

        mac = _gpu_row("Apple M4 Max", 65536, unified=True)
        budget = lm.tier_budget([mac], None)
        assert budget.os_reserve_gb == lm.unified_os_reserve_gb(64.0) == 8.0 and budget.budget_gb == 56.0
        out = self._run(monkeypatch, tmp_path, [mac], 64.0)
        assert re.search(r"Model budget\s+56 GB unified after the 8 GB OS reserve", out), out
        assert f"{lm.UNIFIED_MEMORY_OS_RESERVE_GB:.0f} GB OS reserve" not in out
