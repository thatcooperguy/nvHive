"""Tests for nvh.core.model_manager.

Model sizes come from nvh.core.local_models.size_table(), so the tags and GB
figures used here are read from that table instead of typed in.
"""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, patch

import pytest

from nvh.core import local_models
from nvh.core.model_manager import (
    DEFAULT_MODEL_VRAM_GB,
    MODEL_VRAM_GB,
    VRAM_OVERHEAD_GB,
    ModelManager,
    ModelStatus,
    SwapPlan,
    _model_size_gb,
)

SIZES = local_models.size_table()
BIGGEST = max(SIZES, key=SIZES.get)                 # gpt-oss:120b today
CODER = local_models.pick(24.0, "code").tag         # the 24 GB row's coder
CHAT_48 = local_models.pick(48.0, "chat").tag       # the 48 GB row's chat model
SMALL = local_models.pick(8.0, "chat").tag          # the 8 GB row's chat model

RETIRED_TAGS = (
    "llama3.3:70b",
    "nemotron",
    "nemotron:70b",
    "qwen2.5:72b",
    "deepseek-coder-v2:236b",
    "qwen2.5-coder:32b",
    "codellama:34b",
    "minicpm-v",
    "llava:7b",
    "qwen2.5-coder:14b",
    "llama3.1:8b",
    "deepseek-r1:8b",
    "qwen2.5-coder:7b",
    "mistral:7b",
)


# ---------------------------------------------------------------------------
# size table
# ---------------------------------------------------------------------------


class TestSizeTable:
    def test_model_vram_is_the_tier_table(self):
        assert MODEL_VRAM_GB == SIZES
        for retired in RETIRED_TAGS:
            assert retired not in MODEL_VRAM_GB

    def test_latest_suffix_resolves_through_the_table(self):
        assert _model_size_gb("moondream:latest") == SIZES["moondream"]
        assert _model_size_gb(SMALL) == SIZES[SMALL]

    def test_unknown_tag_uses_the_default(self):
        assert _model_size_gb("some-unknown-model:latest") == DEFAULT_MODEL_VRAM_GB

    def test_sizes_are_sane_for_swap_maths(self):
        # The scenarios below rely on these orderings; if the table moves, they say so here.
        assert SIZES[CHAT_48] > SIZES[CODER] > SIZES[SMALL]
        assert SIZES[BIGGEST] > 24.0 - VRAM_OVERHEAD_GB


# ---------------------------------------------------------------------------
# plan_swap tests
# ---------------------------------------------------------------------------


class TestPlanSwap:
    def test_already_loaded_returns_no_unload(self):
        mm = ModelManager(vram_gb=48)
        mm._loaded[SMALL] = ModelStatus(name=SMALL, loaded=True, size_gb=SIZES[SMALL])
        plan = mm.plan_swap(SMALL)
        assert plan.fits is True
        assert plan.unload == []
        assert "already loaded" in plan.message

    def test_fits_without_unload(self):
        mm = ModelManager(vram_gb=96)
        plan = mm.plan_swap(SMALL)
        assert plan.fits is True
        assert plan.unload == []
        assert plan.estimated_free_after >= 0

    def test_needs_unload_single_model(self):
        mm = ModelManager(vram_gb=48)
        mm._loaded[CHAT_48] = ModelStatus(
            name=CHAT_48, loaded=True, size_gb=SIZES[CHAT_48], last_used=1.0,
        )
        # available = 48 - CHAT_48 - overhead, which is less than the coder needs
        assert 48 - SIZES[CHAT_48] - VRAM_OVERHEAD_GB < SIZES[CODER]
        plan = mm.plan_swap(CODER)
        assert plan.fits is True
        assert CHAT_48 in plan.unload

    def test_needs_unload_multiple_models(self):
        mm = ModelManager(vram_gb=48)
        mm._loaded[SMALL] = ModelStatus(
            name=SMALL, loaded=True, size_gb=SIZES[SMALL], last_used=1.0,
        )
        mm._loaded[CODER] = ModelStatus(
            name=CODER, loaded=True, size_gb=SIZES[CODER], last_used=2.0,
        )
        available = 48 - SIZES[SMALL] - SIZES[CODER] - VRAM_OVERHEAD_GB
        assert SIZES[CHAT_48] - available > SIZES[SMALL]  # freeing the LRU alone is not enough
        plan = mm.plan_swap(CHAT_48)
        assert plan.fits is True
        assert plan.unload == [SMALL, CODER]

    def test_model_too_large_for_total_vram(self):
        mm = ModelManager(vram_gb=24)
        plan = mm.plan_swap(BIGGEST)
        assert plan.fits is False
        assert "exceeds" in plan.message.lower()

    def test_unknown_model_uses_default_size(self):
        mm = ModelManager(vram_gb=96)
        plan = mm.plan_swap("some-unknown-model:latest")
        assert plan.fits is True
        assert plan.estimated_free_after == pytest.approx(96 - VRAM_OVERHEAD_GB - DEFAULT_MODEL_VRAM_GB)

    def test_unload_order_is_lru(self):
        mm = ModelManager(vram_gb=30)
        mm._loaded["model_a"] = ModelStatus(
            name="model_a", loaded=True, size_gb=10.0, last_used=100.0,
        )
        mm._loaded["model_b"] = ModelStatus(
            name="model_b", loaded=True, size_gb=10.0, last_used=50.0,
        )
        # available = 30 - 20 - overhead; the coder needs more than that
        assert SIZES[CODER] > 30 - 20 - VRAM_OVERHEAD_GB
        plan = mm.plan_swap(CODER)
        # LRU (lowest last_used) should be unloaded first
        assert plan.unload[0] == "model_b"


# ---------------------------------------------------------------------------
# execute_swap tests
# ---------------------------------------------------------------------------


class TestExecuteSwap:
    @pytest.mark.asyncio
    async def test_execute_swap_already_loaded_noop(self):
        mm = ModelManager(vram_gb=96)
        mm._loaded[SMALL] = ModelStatus(name=SMALL, loaded=True, size_gb=SIZES[SMALL])
        plan = SwapPlan(
            target_model=SMALL, unload=[], estimated_free_after=88.0,
            fits=True, message="Already loaded.",
        )
        result = await mm.execute_swap(plan)
        assert result is True

    @pytest.mark.asyncio
    async def test_execute_swap_plan_does_not_fit(self):
        mm = ModelManager(vram_gb=24)
        plan = SwapPlan(
            target_model="huge:model", unload=[], estimated_free_after=0,
            fits=False, message="Won't fit.",
        )
        result = await mm.execute_swap(plan)
        assert result is False

    @pytest.mark.asyncio
    async def test_execute_swap_calls_subprocess(self):
        mm = ModelManager(vram_gb=96)
        plan = SwapPlan(
            target_model=SMALL, unload=["old:model"],
            estimated_free_after=80.0, fits=True, message="ok",
        )
        mm._loaded["old:model"] = ModelStatus(name="old:model", loaded=True, size_gb=5.0)

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stderr = ""

        with patch("nvh.core.model_manager.subprocess.run", return_value=mock_result) as mock_run:
            result = await mm.execute_swap(plan)
            assert result is True
            assert "old:model" not in mm._loaded
            assert SMALL in mm._loaded
            assert mm._loaded[SMALL].size_gb == SIZES[SMALL]
            # Should have called stop + run
            assert mock_run.call_count == 2

    @pytest.mark.asyncio
    async def test_execute_swap_progress_callback(self):
        mm = ModelManager(vram_gb=96)
        plan = SwapPlan(
            target_model=SMALL, unload=[],
            estimated_free_after=80.0, fits=True, message="ok",
        )
        messages = []

        mock_result = MagicMock()
        mock_result.returncode = 0

        with patch("nvh.core.model_manager.subprocess.run", return_value=mock_result):
            await mm.execute_swap(plan, on_progress=messages.append)
        assert any("Loading" in m for m in messages)


# ---------------------------------------------------------------------------
# get_loaded_models tests
# ---------------------------------------------------------------------------


class TestGetLoadedModels:
    @pytest.mark.asyncio
    async def test_returns_empty_on_ollama_not_found(self):
        mm = ModelManager(vram_gb=96)
        with patch(
            "nvh.core.model_manager.subprocess.run",
            side_effect=FileNotFoundError,
        ):
            result = await mm.get_loaded_models()
        assert result == []

    @pytest.mark.asyncio
    async def test_returns_empty_on_timeout(self):
        mm = ModelManager(vram_gb=96)
        with patch(
            "nvh.core.model_manager.subprocess.run",
            side_effect=subprocess.TimeoutExpired("ollama", 10),
        ):
            result = await mm.get_loaded_models()
        assert result == []

    @pytest.mark.asyncio
    async def test_parses_ps_output(self):
        mm = ModelManager(vram_gb=96)
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = f"NAME\tSIZE\tMODIFIED\n{SMALL}\t6.0GB\t2min ago\nmoondream:latest\t2GB\tnow\n"

        with patch("nvh.core.model_manager.subprocess.run", return_value=mock_result):
            models = await mm.get_loaded_models()
        assert [m.name for m in models] == [SMALL, "moondream:latest"]
        assert all(m.loaded for m in models)
        assert models[0].size_gb == SIZES[SMALL]
        assert models[1].size_gb == SIZES["moondream"]


# ---------------------------------------------------------------------------
# format_status tests
# ---------------------------------------------------------------------------


class TestFormatStatus:
    def test_empty_shows_no_models(self):
        mm = ModelManager(vram_gb=48)
        assert "No models" in mm.format_status()

    def test_with_loaded_models_shows_vram(self):
        mm = ModelManager(vram_gb=96)
        mm._loaded[CODER] = ModelStatus(
            name=CODER, loaded=True, size_gb=SIZES[CODER],
        )
        output = mm.format_status()
        assert CODER in output
        assert f"{SIZES[CODER]:.1f} GB" in output
        assert "96.0 GB" in output
