"""Tests for the Local LLM Orchestration Engine."""

from __future__ import annotations

import pytest

from nvh.core.orchestrator import (
    LocalOrchestrator,
    OrchestrationConfig,
    OrchestrationMode,
)

# ---------------------------------------------------------------------------
# Enum values
# ---------------------------------------------------------------------------

class TestOrchestrationModeEnum:
    def test_off_value(self):
        assert OrchestrationMode.OFF == "off"

    def test_light_value(self):
        assert OrchestrationMode.LIGHT == "light"

    def test_full_value(self):
        assert OrchestrationMode.FULL == "full"

    def test_auto_value(self):
        assert OrchestrationMode.AUTO == "auto"

    def test_from_string(self):
        assert OrchestrationMode("off") == OrchestrationMode.OFF
        assert OrchestrationMode("light") == OrchestrationMode.LIGHT
        assert OrchestrationMode("full") == OrchestrationMode.FULL
        assert OrchestrationMode("auto") == OrchestrationMode.AUTO

    def test_all_modes_present(self):
        modes = {m.value for m in OrchestrationMode}
        assert modes == {"off", "light", "full", "auto"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _FakeRegistry:
    """Minimal registry stub for testing."""

    def __init__(self, has_ollama: bool = False):
        self._has_ollama = has_ollama
        self._provider = object() if has_ollama else None

    def has(self, name: str) -> bool:
        return name == "ollama" and self._has_ollama

    def get(self, name: str):
        if name == "ollama" and self._has_ollama:
            return self._provider
        raise KeyError(name)


# ---------------------------------------------------------------------------
# Auto-mode: no local provider → OFF
# ---------------------------------------------------------------------------

class TestAutoModeNoLocalProvider:
    @pytest.mark.asyncio
    async def test_auto_off_when_no_ollama(self):
        orchestrator = LocalOrchestrator(OrchestrationConfig(mode=OrchestrationMode.AUTO))
        registry = _FakeRegistry(has_ollama=False)
        mode = await orchestrator.initialize(registry, gpu_vram_gb=32)
        assert mode == OrchestrationMode.OFF

    @pytest.mark.asyncio
    async def test_mode_property_off(self):
        orchestrator = LocalOrchestrator(OrchestrationConfig(mode=OrchestrationMode.AUTO))
        registry = _FakeRegistry(has_ollama=False)
        await orchestrator.initialize(registry, gpu_vram_gb=32)
        assert orchestrator.mode == OrchestrationMode.OFF

    @pytest.mark.asyncio
    async def test_is_active_false_when_off(self):
        orchestrator = LocalOrchestrator(OrchestrationConfig(mode=OrchestrationMode.AUTO))
        registry = _FakeRegistry(has_ollama=False)
        await orchestrator.initialize(registry, gpu_vram_gb=32)
        assert orchestrator.is_active is False


# ---------------------------------------------------------------------------
# Auto-mode: LIGHT when VRAM >= 6GB but < 20GB
# ---------------------------------------------------------------------------

class TestAutoModeLight:
    @pytest.mark.asyncio
    async def test_auto_light_at_6gb(self):
        orchestrator = LocalOrchestrator(OrchestrationConfig(mode=OrchestrationMode.AUTO))
        registry = _FakeRegistry(has_ollama=True)
        mode = await orchestrator.initialize(registry, gpu_vram_gb=6)
        assert mode == OrchestrationMode.LIGHT

    @pytest.mark.asyncio
    async def test_auto_light_at_12gb(self):
        orchestrator = LocalOrchestrator(OrchestrationConfig(mode=OrchestrationMode.AUTO))
        registry = _FakeRegistry(has_ollama=True)
        mode = await orchestrator.initialize(registry, gpu_vram_gb=12)
        assert mode == OrchestrationMode.LIGHT

    @pytest.mark.asyncio
    async def test_auto_light_just_below_full(self):
        orchestrator = LocalOrchestrator(OrchestrationConfig(mode=OrchestrationMode.AUTO))
        registry = _FakeRegistry(has_ollama=True)
        mode = await orchestrator.initialize(registry, gpu_vram_gb=19)
        assert mode == OrchestrationMode.LIGHT

    @pytest.mark.asyncio
    async def test_is_active_true_when_light(self):
        orchestrator = LocalOrchestrator(OrchestrationConfig(mode=OrchestrationMode.AUTO))
        registry = _FakeRegistry(has_ollama=True)
        await orchestrator.initialize(registry, gpu_vram_gb=8)
        assert orchestrator.is_active is True


# ---------------------------------------------------------------------------
# Auto-mode: FULL when VRAM >= 20GB
# ---------------------------------------------------------------------------

class TestAutoModeFull:
    @pytest.mark.asyncio
    async def test_auto_full_at_20gb(self):
        orchestrator = LocalOrchestrator(OrchestrationConfig(mode=OrchestrationMode.AUTO))
        registry = _FakeRegistry(has_ollama=True)
        mode = await orchestrator.initialize(registry, gpu_vram_gb=20)
        assert mode == OrchestrationMode.FULL

    @pytest.mark.asyncio
    async def test_auto_full_at_80gb(self):
        orchestrator = LocalOrchestrator(OrchestrationConfig(mode=OrchestrationMode.AUTO))
        registry = _FakeRegistry(has_ollama=True)
        mode = await orchestrator.initialize(registry, gpu_vram_gb=80)
        assert mode == OrchestrationMode.FULL

    @pytest.mark.asyncio
    async def test_is_active_true_when_full(self):
        orchestrator = LocalOrchestrator(OrchestrationConfig(mode=OrchestrationMode.AUTO))
        registry = _FakeRegistry(has_ollama=True)
        await orchestrator.initialize(registry, gpu_vram_gb=24)
        assert orchestrator.is_active is True


# ---------------------------------------------------------------------------
# User override to OFF
# ---------------------------------------------------------------------------

class TestUserOverrideOff:
    @pytest.mark.asyncio
    async def test_override_off_even_with_ollama_and_high_vram(self):
        orchestrator = LocalOrchestrator(OrchestrationConfig(mode=OrchestrationMode.OFF))
        registry = _FakeRegistry(has_ollama=True)
        mode = await orchestrator.initialize(registry, gpu_vram_gb=80)
        assert mode == OrchestrationMode.OFF

    @pytest.mark.asyncio
    async def test_override_off_is_not_active(self):
        orchestrator = LocalOrchestrator(OrchestrationConfig(mode=OrchestrationMode.OFF))
        registry = _FakeRegistry(has_ollama=True)
        await orchestrator.initialize(registry, gpu_vram_gb=80)
        assert orchestrator.is_active is False


# ---------------------------------------------------------------------------
# Non-OFF mode overrides are downgraded when no local provider
# ---------------------------------------------------------------------------

class TestDowngradeWhenNoLocalProvider:
    @pytest.mark.asyncio
    async def test_light_override_downgraded_without_ollama(self):
        orchestrator = LocalOrchestrator(OrchestrationConfig(mode=OrchestrationMode.LIGHT))
        registry = _FakeRegistry(has_ollama=False)
        mode = await orchestrator.initialize(registry, gpu_vram_gb=0)
        assert mode == OrchestrationMode.OFF

    @pytest.mark.asyncio
    async def test_full_override_downgraded_without_ollama(self):
        orchestrator = LocalOrchestrator(OrchestrationConfig(mode=OrchestrationMode.FULL))
        registry = _FakeRegistry(has_ollama=False)
        mode = await orchestrator.initialize(registry, gpu_vram_gb=0)
        assert mode == OrchestrationMode.OFF


# ---------------------------------------------------------------------------
# Fallback to keywords when local LLM fails / returns empty
# ---------------------------------------------------------------------------

class TestFallbackBehavior:
    @pytest.mark.asyncio
    async def test_smart_route_returns_empty_when_off(self):
        orchestrator = LocalOrchestrator(OrchestrationConfig(mode=OrchestrationMode.OFF))
        # No initialization needed — mode is explicitly OFF
        result = await orchestrator.smart_route("Write a Python function", ["openai", "ollama"])
        assert result == {}

    @pytest.mark.asyncio
    async def test_optimize_prompt_returns_original_when_off(self):
        orchestrator = LocalOrchestrator(OrchestrationConfig(mode=OrchestrationMode.OFF))
        original = "What is machine learning?"
        result = await orchestrator.optimize_prompt(original, "openai")
        assert result == original

    @pytest.mark.asyncio
    async def test_generate_agents_returns_empty_when_off(self):
        orchestrator = LocalOrchestrator(OrchestrationConfig(mode=OrchestrationMode.OFF))
        result = await orchestrator.generate_custom_agents("Should we use Rust?")
        assert result == []

    @pytest.mark.asyncio
    async def test_evaluate_response_returns_defaults_when_off(self):
        orchestrator = LocalOrchestrator(OrchestrationConfig(mode=OrchestrationMode.OFF))
        result = await orchestrator.evaluate_response("question", "answer", "openai")
        assert result["quality"] == 7
        assert result["is_complete"] is True
        assert result["should_retry"] is False

    @pytest.mark.asyncio
    async def test_synthesize_locally_returns_empty_when_off(self):
        orchestrator = LocalOrchestrator(OrchestrationConfig(mode=OrchestrationMode.OFF))
        result = await orchestrator.synthesize_locally("question", {"openai": "answer1"})
        assert result == ""

    @pytest.mark.asyncio
    async def test_compress_context_returns_empty_when_off(self):
        orchestrator = LocalOrchestrator(OrchestrationConfig(mode=OrchestrationMode.OFF))
        result = await orchestrator.compress_context([])
        assert result == ""


# ---------------------------------------------------------------------------
# Orchestrator doesn't crash when Ollama is unavailable
# ---------------------------------------------------------------------------

class TestRobustnessWithoutOllama:
    @pytest.mark.asyncio
    async def test_initialize_without_ollama_does_not_raise(self):
        orchestrator = LocalOrchestrator()
        registry = _FakeRegistry(has_ollama=False)
        # Should not raise even with FULL requested
        mode = await orchestrator.initialize(registry, gpu_vram_gb=0)
        assert isinstance(mode, OrchestrationMode)

    @pytest.mark.asyncio
    async def test_all_methods_safe_when_no_provider(self):
        orchestrator = LocalOrchestrator(OrchestrationConfig(mode=OrchestrationMode.AUTO))
        registry = _FakeRegistry(has_ollama=False)
        await orchestrator.initialize(registry, gpu_vram_gb=0)

        # None of these should raise
        assert await orchestrator.smart_route("test", ["openai"]) == {}
        assert await orchestrator.generate_custom_agents("test") == []
        assert await orchestrator.optimize_prompt("test", "openai") == "test"
        assert (await orchestrator.evaluate_response("q", "a", "openai"))["quality"] == 7
        assert await orchestrator.synthesize_locally("q", {"openai": "a"}) == ""
        assert await orchestrator.compress_context([]) == ""

    def test_default_mode_before_initialize(self):
        """Mode property is safe to call before initialize()."""
        orchestrator = LocalOrchestrator()
        assert orchestrator.mode == OrchestrationMode.OFF

    def test_is_active_false_before_initialize(self):
        orchestrator = LocalOrchestrator()
        assert orchestrator.is_active is False


# ---------------------------------------------------------------------------
# Config defaults
# ---------------------------------------------------------------------------

class TestOrchestrationConfig:
    def test_default_mode_is_auto(self):
        config = OrchestrationConfig()
        assert config.mode == OrchestrationMode.AUTO

    def test_custom_vram_thresholds(self):
        config = OrchestrationConfig(min_model_size_for_light=8, min_model_size_for_full=30)
        assert config.min_model_size_for_light == 8
        assert config.min_model_size_for_full == 30

    @pytest.mark.asyncio
    async def test_custom_thresholds_respected(self):
        config = OrchestrationConfig(
            mode=OrchestrationMode.AUTO,
            min_model_size_for_light=8,
            min_model_size_for_full=30,
        )
        orchestrator = LocalOrchestrator(config)
        registry = _FakeRegistry(has_ollama=True)

        # 7GB VRAM — below custom light threshold of 8, so OFF
        mode = await orchestrator.initialize(registry, gpu_vram_gb=7)
        assert mode == OrchestrationMode.OFF

    @pytest.mark.asyncio
    async def test_custom_thresholds_light(self):
        config = OrchestrationConfig(
            mode=OrchestrationMode.AUTO,
            min_model_size_for_light=8,
            min_model_size_for_full=30,
        )
        orchestrator = LocalOrchestrator(config)
        registry = _FakeRegistry(has_ollama=True)

        # 10GB VRAM — meets custom light threshold, below full
        mode = await orchestrator.initialize(registry, gpu_vram_gb=10)
        assert mode == OrchestrationMode.LIGHT
