"""Tests for nvh.core.model_manager."""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, patch

import pytest

from nvh.core.model_manager import (
    ModelManager,
    ModelStatus,
    SwapPlan,
)

# ---------------------------------------------------------------------------
# plan_swap tests
# ---------------------------------------------------------------------------


class TestPlanSwap:
    def test_already_loaded_returns_no_unload(self):
        mm = ModelManager(vram_gb=48)
        mm._loaded["gemma2:9b"] = ModelStatus(name="gemma2:9b", loaded=True, size_gb=5.0)
        plan = mm.plan_swap("gemma2:9b")
        assert plan.fits is True
        assert plan.unload == []
        assert "already loaded" in plan.message

    def test_fits_without_unload(self):
        mm = ModelManager(vram_gb=96)
        plan = mm.plan_swap("gemma2:9b")
        assert plan.fits is True
        assert plan.unload == []
        assert plan.estimated_free_after >= 0

    def test_needs_unload_single_model(self):
        mm = ModelManager(vram_gb=48)
        mm._loaded["llama3.3:70b"] = ModelStatus(
            name="llama3.3:70b", loaded=True, size_gb=40.0, last_used=1.0,
        )
        plan = mm.plan_swap("qwen2.5-coder:32b")
        assert plan.fits is True
        assert "llama3.3:70b" in plan.unload

    def test_needs_unload_multiple_models(self):
        mm = ModelManager(vram_gb=48)
        mm._loaded["gemma2:9b"] = ModelStatus(
            name="gemma2:9b", loaded=True, size_gb=5.0, last_used=1.0,
        )
        mm._loaded["qwen2.5-coder:32b"] = ModelStatus(
            name="qwen2.5-coder:32b", loaded=True, size_gb=18.0, last_used=2.0,
        )
        # With overhead=2, used=25, available=23. llama3.3:70b needs 40 => need 17 more
        plan = mm.plan_swap("llama3.3:70b")
        assert plan.fits is True
        assert len(plan.unload) >= 1

    def test_model_too_large_for_total_vram(self):
        mm = ModelManager(vram_gb=24)
        plan = mm.plan_swap("deepseek-coder-v2:236b")
        assert plan.fits is False
        assert "exceeds" in plan.message.lower()

    def test_unknown_model_uses_default_size(self):
        mm = ModelManager(vram_gb=96)
        plan = mm.plan_swap("some-unknown-model:latest")
        # Default size is 5.0 GB, should fit in 96 GB easily
        assert plan.fits is True

    def test_unload_order_is_lru(self):
        mm = ModelManager(vram_gb=30)
        mm._loaded["model_a"] = ModelStatus(
            name="model_a", loaded=True, size_gb=10.0, last_used=100.0,
        )
        mm._loaded["model_b"] = ModelStatus(
            name="model_b", loaded=True, size_gb=10.0, last_used=50.0,
        )
        # available = 30 - 20 - 2 = 8.  Need qwen2.5-coder:32b (18 GB) => need 10 more
        plan = mm.plan_swap("qwen2.5-coder:32b")
        # LRU (lowest last_used) should be unloaded first
        assert plan.unload[0] == "model_b"


# ---------------------------------------------------------------------------
# execute_swap tests
# ---------------------------------------------------------------------------


class TestExecuteSwap:
    @pytest.mark.asyncio
    async def test_execute_swap_already_loaded_noop(self):
        mm = ModelManager(vram_gb=96)
        mm._loaded["gemma2:9b"] = ModelStatus(name="gemma2:9b", loaded=True, size_gb=5.0)
        plan = SwapPlan(
            target_model="gemma2:9b", unload=[], estimated_free_after=89.0,
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
            target_model="gemma2:9b", unload=["old:model"],
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
            assert "gemma2:9b" in mm._loaded
            # Should have called stop + run
            assert mock_run.call_count == 2

    @pytest.mark.asyncio
    async def test_execute_swap_progress_callback(self):
        mm = ModelManager(vram_gb=96)
        plan = SwapPlan(
            target_model="gemma2:9b", unload=[],
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
        mock_result.stdout = "NAME\tSIZE\tMODIFIED\ngemma2:9b\t5.0GB\t2min ago\n"

        with patch("nvh.core.model_manager.subprocess.run", return_value=mock_result):
            models = await mm.get_loaded_models()
        assert len(models) == 1
        assert models[0].name == "gemma2:9b"
        assert models[0].loaded is True


# ---------------------------------------------------------------------------
# format_status tests
# ---------------------------------------------------------------------------


class TestFormatStatus:
    def test_empty_shows_no_models(self):
        mm = ModelManager(vram_gb=48)
        assert "No models" in mm.format_status()

    def test_with_loaded_models_shows_vram(self):
        mm = ModelManager(vram_gb=96)
        mm._loaded["gemma2:27b"] = ModelStatus(
            name="gemma2:27b", loaded=True, size_gb=16.0,
        )
        output = mm.format_status()
        assert "gemma2:27b" in output
        assert "16.0 GB" in output
        assert "96.0 GB" in output
