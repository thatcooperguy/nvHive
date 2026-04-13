"""Tests for the multi-agent parallel pipeline + model manager."""

from __future__ import annotations

from decimal import Decimal

from nvh.core.model_manager import (
    MODEL_VRAM_GB,
    ModelManager,
    ModelStatus,
)
from nvh.core.parallel_pipeline import (
    PipelineResult,
    SubTaskResult,
    format_pipeline_result,
)


class TestModelManager:
    def test_plan_swap_already_loaded(self):
        mm = ModelManager(vram_gb=96)
        mm._loaded["llama3.3:70b"] = ModelStatus(
            name="llama3.3:70b", loaded=True, size_gb=40.0,
        )
        plan = mm.plan_swap("llama3.3:70b")
        assert plan.fits is True
        assert plan.unload == []
        assert "already loaded" in plan.message

    def test_plan_swap_fits_in_available(self):
        mm = ModelManager(vram_gb=96)
        plan = mm.plan_swap("gemma2:9b")
        assert plan.fits is True
        assert plan.unload == []

    def test_plan_swap_needs_unload(self):
        mm = ModelManager(vram_gb=48)
        mm._loaded["llama3.3:70b"] = ModelStatus(
            name="llama3.3:70b", loaded=True, size_gb=40.0,
        )
        plan = mm.plan_swap("qwen2.5-coder:32b")
        assert plan.fits is True
        assert "llama3.3:70b" in plan.unload
        assert "context" in plan.message.lower() or "Context" in plan.message

    def test_plan_swap_too_large(self):
        mm = ModelManager(vram_gb=24)
        plan = mm.plan_swap("deepseek-coder-v2:236b")
        assert plan.fits is False
        assert "exceeds" in plan.message.lower()

    def test_format_status_no_models(self):
        mm = ModelManager(vram_gb=96)
        assert "No models" in mm.format_status()

    def test_format_status_with_models(self):
        mm = ModelManager(vram_gb=96)
        mm._loaded["llama3.3:70b"] = ModelStatus(
            name="llama3.3:70b", loaded=True, size_gb=40.0,
        )
        status = mm.format_status()
        assert "llama3.3:70b" in status
        assert "40.0 GB" in status

    def test_vram_estimates_exist(self):
        assert "llama3.3:70b" in MODEL_VRAM_GB
        assert "gemma2:27b" in MODEL_VRAM_GB
        assert MODEL_VRAM_GB["llama3.3:70b"] > MODEL_VRAM_GB["gemma2:27b"]


class TestSubTaskResult:
    def test_construction(self):
        r = SubTaskResult(
            role="Backend Engineer", provider="groq", model="llama-70b",
            is_local=False, content="Built the API endpoints.",
            success=True, duration_ms=5000, cost_usd=Decimal("0.02"),
        )
        assert r.role == "Backend Engineer"
        assert r.is_local is False
        assert r.success is True

    def test_failure(self):
        r = SubTaskResult(
            role="DBA", provider="ollama", model="llama3.3:70b",
            is_local=True, content="", success=False,
            error="Model not loaded",
        )
        assert r.success is False
        assert "not loaded" in r.error


class TestPipelineResult:
    def test_construction(self):
        r = PipelineResult(
            task="Build notification service",
            subtask_results=[
                SubTaskResult(role="Backend", provider="groq", model="m",
                              is_local=False, content="ok", success=True),
                SubTaskResult(role="Tests", provider="ollama", model="m",
                              is_local=True, content="ok", success=True),
            ],
            post_qa_verdict="PASSED",
            post_qa_summary="All good",
            suggested_improvements=["Add rate limiting", "Add monitoring"],
            models_used=["groq/m", "ollama/m"],
        )
        assert len(r.subtask_results) == 2
        assert r.post_qa_verdict == "PASSED"
        assert len(r.suggested_improvements) == 2


class TestFormatPipelineResult:
    def test_format_non_empty(self):
        r = PipelineResult(
            task="Build API",
            subtask_results=[
                SubTaskResult(role="Backend", provider="groq", model="llama",
                              is_local=False, content="done", success=True,
                              duration_ms=3000, cost_usd=Decimal("0.01")),
            ],
            post_qa_verdict="PASSED",
            post_qa_summary="Looks good",
            suggested_improvements=["Add caching"],
            total_duration_ms=5000,
            total_cost_usd=Decimal("0.01"),
            models_used=["groq/llama"],
        )
        output = format_pipeline_result(r)
        assert "Build API" in output
        assert "Backend" in output
        assert "PASSED" in output
        assert "Add caching" in output
        assert "CLOUD" in output

    def test_format_with_local_and_swaps(self):
        r = PipelineResult(
            task="Test",
            subtask_results=[
                SubTaskResult(role="Coder", provider="ollama", model="llama",
                              is_local=True, content="done", success=True),
            ],
            post_qa_verdict="PARTIAL",
            vram_swaps=2,
            models_used=["ollama/llama"],
        )
        output = format_pipeline_result(r)
        assert "LOCAL" in output
        assert "context preserved" in output.lower()
