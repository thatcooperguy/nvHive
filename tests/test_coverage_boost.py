"""Tests to boost coverage for router, settings, and tools modules."""

from __future__ import annotations

import os
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from nvh.config.settings import (
    CouncilConfig,
    DefaultsConfig,
    ProviderConfig,
    _deep_merge,
    _interpolate_env,
    load_config,
)
from nvh.core.router import (
    ClassificationResult,
    RoutingEngine,
    classify_task,
)
from nvh.core.tools import ToolRegistry
from nvh.providers.base import ModelInfo, TaskType
from nvh.providers.registry import ProviderRegistry

# ═══════════════════════════════════════════════════════════════════════
# Module 1: router.py
# ═══════════════════════════════════════════════════════════════════════


class TestTaskClassifier:
    def test_classify_code_generation(self) -> None:
        result = classify_task("Write a Python function to sort a list")
        assert result.task_type == TaskType.CODE_GENERATION

    def test_classify_math(self) -> None:
        result = classify_task("Calculate the integral of x squared from 0 to 5")
        assert result.task_type == TaskType.MATH

    def test_classify_conversation(self) -> None:
        result = classify_task("Hello how are you doing today")
        assert result.task_type == TaskType.CONVERSATION

    def test_classify_debug(self) -> None:
        result = classify_task("Fix this bug in my code, I'm getting a TypeError")
        assert result.task_type == TaskType.CODE_DEBUG

    def test_classify_summarization(self) -> None:
        result = classify_task("Summarize this article for me in three sentences")
        assert result.task_type == TaskType.SUMMARIZATION

    def test_classify_translation(self) -> None:
        result = classify_task("Translate this paragraph to Spanish")
        assert result.task_type == TaskType.TRANSLATION

    def test_classify_empty_falls_back(self) -> None:
        result = classify_task("")
        assert isinstance(result, ClassificationResult)
        assert result.task_type is not None

    def test_classify_ambiguous_question(self) -> None:
        result = classify_task("what?")
        assert isinstance(result, ClassificationResult)


def _make_engine(
    providers: dict[str, ProviderConfig] | None = None,
    enabled: list[str] | None = None,
    models: list[ModelInfo] | None = None,
    health: float = 1.0,
) -> RoutingEngine:
    """Build a RoutingEngine with mocked registry and rate manager."""
    config = CouncilConfig(
        providers=providers or {},
        defaults=DefaultsConfig(provider="openai", model="gpt-4o"),
    )
    registry = MagicMock(spec=ProviderRegistry)
    registry.list_enabled.return_value = enabled or []
    registry.has.side_effect = lambda n: n in (enabled or [])
    registry.get_models_for_provider.return_value = models or []
    registry.get_model_info.return_value = None

    rate_mgr = MagicMock()
    rate_mgr.get_health_score.return_value = health

    return RoutingEngine(config, registry, rate_mgr)


def _model(
    model_id: str = "test-model",
    provider: str = "openai",
    cap: float = 0.8,
    cost: Decimal = Decimal("1"),
    latency: int = 500,
    context: int = 128000,
) -> ModelInfo:
    return ModelInfo(
        model_id=model_id,
        provider=provider,
        input_cost_per_1m_tokens=cost,
        output_cost_per_1m_tokens=cost,
        typical_latency_ms=latency,
        context_window=context,
        capability_scores={"code_generation": cap, "conversation": cap},
    )


class TestRoutingEngine:
    def test_provider_override(self) -> None:
        engine = _make_engine()
        decision = engine.route("hello", provider_override="anthropic")
        assert decision.provider == "anthropic"
        assert "override" in decision.reason.lower()

    def test_provider_override_with_model(self) -> None:
        engine = _make_engine()
        decision = engine.route(
            "hello",
            provider_override="anthropic",
            model_override="claude-3",
        )
        assert decision.provider == "anthropic"
        assert decision.model == "claude-3"

    def test_no_providers_available(self) -> None:
        engine = _make_engine(enabled=[])
        decision = engine.route("hello")
        assert decision.reason.startswith("No providers available")

    def test_model_override_used(self) -> None:
        m = _model()
        engine = _make_engine(
            enabled=["openai"],
            models=[m],
            providers={"openai": ProviderConfig(default_model="gpt-4o")},
        )
        decision = engine.route("Write code", model_override="gpt-4o-mini")
        assert decision.model == "gpt-4o-mini"

    def test_fallback_when_unhealthy(self) -> None:
        engine = _make_engine(enabled=["openai"], models=[], health=0.0)
        decision = engine.route("hello")
        assert "filtered out" in decision.reason.lower() or "default" in decision.reason.lower()

    def test_cheapest_strategy(self) -> None:
        m = _model(cost=Decimal("0"))
        engine = _make_engine(
            enabled=["openai"],
            models=[m],
            providers={"openai": ProviderConfig(default_model="gpt-4o")},
        )
        decision = engine.route("hello", strategy="cheapest")
        assert decision.provider == "openai"

    def test_fastest_strategy(self) -> None:
        m = _model(latency=100)
        engine = _make_engine(
            enabled=["openai"],
            models=[m],
            providers={"openai": ProviderConfig(default_model="gpt-4o")},
        )
        decision = engine.route("hello", strategy="fastest")
        assert decision.provider == "openai"

    def test_cost_score_free_model(self) -> None:
        m = _model(cost=Decimal("0"))
        engine = _make_engine()
        assert engine._cost_score(m) == 1.0

    def test_latency_score_instant(self) -> None:
        m = _model(latency=0)
        engine = _make_engine()
        assert engine._latency_score(m) == 1.0


# ═══════════════════════════════════════════════════════════════════════
# Module 2: settings.py
# ═══════════════════════════════════════════════════════════════════════


class TestSettings:
    def test_default_config_values(self) -> None:
        cfg = CouncilConfig()
        assert cfg.defaults.mode == "ask"
        assert cfg.defaults.timeout == 30
        assert cfg.defaults.max_tokens == 4096
        assert cfg.defaults.temperature == 1.0
        assert cfg.defaults.stream is True
        assert cfg.cache.enabled is True
        assert cfg.budget.daily_limit_usd == Decimal("5")

    def test_load_config_from_yaml(self, tmp_path: Path) -> None:
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text(
            "version: '1'\n"
            "defaults:\n"
            "  provider: anthropic\n"
            "  timeout: 45\n"
        )
        with patch("nvh.config.settings._find_project_config", return_value=None):
            cfg = load_config(config_path=cfg_file)
        assert cfg.defaults.provider == "anthropic"
        assert cfg.defaults.timeout == 45

    def test_load_config_missing_file(self, tmp_path: Path) -> None:
        missing = tmp_path / "nope.yaml"
        with patch("nvh.config.settings._find_project_config", return_value=None):
            cfg = load_config(config_path=missing)
        assert isinstance(cfg, CouncilConfig)
        assert cfg.defaults.mode == "ask"

    def test_deep_merge(self) -> None:
        base = {"a": 1, "nested": {"x": 10, "y": 20}}
        override = {"nested": {"y": 99, "z": 30}, "b": 2}
        result = _deep_merge(base, override)
        assert result["a"] == 1
        assert result["b"] == 2
        assert result["nested"]["x"] == 10
        assert result["nested"]["y"] == 99
        assert result["nested"]["z"] == 30

    def test_env_var_interpolation(self) -> None:
        with patch.dict(os.environ, {"TEST_VAR_XYZ": "hello"}):
            assert _interpolate_env("${TEST_VAR_XYZ}") == "hello"

    def test_env_var_default(self) -> None:
        os.environ.pop("NONEXISTENT_VAR_ABC", None)
        assert _interpolate_env("${NONEXISTENT_VAR_ABC:-fallback}") == "fallback"

    def test_advisors_alias(self) -> None:
        cfg = CouncilConfig(**{
            "advisors": {"mock": {"default_model": "m"}},
        })
        assert "mock" in cfg.providers

    def test_config_merge_project_over_user(self, tmp_path: Path) -> None:
        user_cfg = tmp_path / "user.yaml"
        user_cfg.write_text("defaults:\n  timeout: 10\n  provider: openai\n")
        proj_cfg = tmp_path / ".hive.yaml"
        proj_cfg.write_text("defaults:\n  timeout: 99\n")
        with patch("nvh.config.settings._find_project_config", return_value=proj_cfg):
            cfg = load_config(config_path=user_cfg)
        assert cfg.defaults.timeout == 99
        assert cfg.defaults.provider == "openai"


# ═══════════════════════════════════════════════════════════════════════
# Module 3: tools.py
# ═══════════════════════════════════════════════════════════════════════


class TestToolRegistry:
    def test_builtins_registered(self, tmp_path: Path) -> None:
        reg = ToolRegistry(workspace=str(tmp_path), include_system=False)
        names = {t.name for t in reg.list_tools()}
        assert "read_file" in names
        assert "write_file" in names
        assert "list_files" in names
        assert "search_files" in names
        assert "shell" in names

    def test_get_tool_descriptions(self, tmp_path: Path) -> None:
        reg = ToolRegistry(workspace=str(tmp_path), include_system=False)
        desc = reg.get_tool_descriptions()
        assert "read_file" in desc
        assert len(desc) > 50

    @pytest.mark.asyncio
    async def test_read_file(self, tmp_path: Path) -> None:
        (tmp_path / "hello.txt").write_text("world")
        reg = ToolRegistry(workspace=str(tmp_path), include_system=False)
        tool = reg.get("read_file")
        assert tool is not None
        result = await tool.handler(path="hello.txt")
        assert result == "world"

    @pytest.mark.asyncio
    async def test_write_file(self, tmp_path: Path) -> None:
        reg = ToolRegistry(workspace=str(tmp_path), include_system=False)
        tool = reg.get("write_file")
        assert tool is not None
        result = await tool.handler(path="out.txt", content="data")
        assert "4 chars" in result
        assert (tmp_path / "out.txt").read_text() == "data"

    @pytest.mark.asyncio
    async def test_list_files(self, tmp_path: Path) -> None:
        (tmp_path / "a.py").write_text("x")
        (tmp_path / "b.py").write_text("y")
        reg = ToolRegistry(workspace=str(tmp_path), include_system=False)
        tool = reg.get("list_files")
        assert tool is not None
        result = await tool.handler(pattern="*.py")
        assert "a.py" in result
        assert "b.py" in result

    @pytest.mark.asyncio
    async def test_search_files(self, tmp_path: Path) -> None:
        (tmp_path / "code.py").write_text("def hello_world():\n    pass\n")
        reg = ToolRegistry(workspace=str(tmp_path), include_system=False)
        tool = reg.get("search_files")
        assert tool is not None
        result = await tool.handler(query="hello_world", pattern="*.py")
        assert "hello_world" in result

    def test_path_traversal_blocked(self, tmp_path: Path) -> None:
        reg = ToolRegistry(workspace=str(tmp_path), include_system=False)
        with pytest.raises(PermissionError, match="traversal"):
            reg._resolve_path("../../etc/passwd")

    def test_get_unknown_tool(self, tmp_path: Path) -> None:
        reg = ToolRegistry(workspace=str(tmp_path), include_system=False)
        assert reg.get("nonexistent") is None

    @pytest.mark.asyncio
    async def test_execute_unknown_tool(self, tmp_path: Path) -> None:
        reg = ToolRegistry(workspace=str(tmp_path), include_system=False)
        result = await reg.execute("no_such_tool", {})
        assert not result.success
        assert "Unknown tool" in result.error
