"""Final coverage push D — config settings, agentic deep, openai_provider."""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from nvh.providers.base import Message, Usage

# -- nvh/config/settings.py deep paths --

class TestSettingsDeep:
    def test_load_config_defaults(self):
        from nvh.config.settings import load_config
        config = load_config()
        assert config is not None
        assert hasattr(config, "defaults")
        assert hasattr(config, "providers")

    def test_defaults_config_fields(self):
        from nvh.config.settings import DefaultsConfig
        d = DefaultsConfig()
        assert hasattr(d, "provider")
        assert hasattr(d, "temperature")
        assert hasattr(d, "max_tokens")

    def test_budget_config_defaults(self):
        from nvh.config.settings import BudgetConfig
        b = BudgetConfig()
        assert b.daily_limit_usd > 0
        assert b.monthly_limit_usd > 0

    def test_cache_config_defaults(self):
        from nvh.config.settings import CacheConfig
        c = CacheConfig()
        assert c.max_size > 0
        assert c.ttl_seconds > 0

    def test_routing_config_defaults(self):
        from nvh.config.settings import RoutingConfig
        r = RoutingConfig()
        assert r is not None

    def test_council_mode_config(self):
        from nvh.config.settings import CouncilModeConfig
        c = CouncilModeConfig()
        assert c.quorum >= 1
        assert c.timeout > 0

    def test_get_config_dir_creates(self, tmp_path):
        from nvh.config.settings import get_config_dir
        d = get_config_dir()
        assert d.exists()

    def test_provider_config(self):
        from nvh.config.settings import ProviderConfig
        p = ProviderConfig(enabled=True, default_model="test")
        assert p.enabled is True
        assert p.default_model == "test"

    def test_load_config_with_profile(self, tmp_path):
        from nvh.config.settings import load_config
        # Loading with a nonexistent profile should fall back to defaults
        config = load_config(profile="nonexistent_profile_xyz")
        assert config is not None


# -- nvh/core/agentic.py deep paths --

class TestAgenticDeep:
    def test_coding_system_prompt_has_tools(self):
        from nvh.core.agentic import CODING_SYSTEM_PROMPT
        assert "{tool_descriptions}" in CODING_SYSTEM_PROMPT
        assert "read" in CODING_SYSTEM_PROMPT.lower()
        assert "write" in CODING_SYSTEM_PROMPT.lower()

    def test_tier_descriptions_all_tiers(self):
        from nvh.core.agentic import TIER_DESCRIPTIONS, AgentTier
        for tier in AgentTier:
            assert tier in TIER_DESCRIPTIONS
            assert len(TIER_DESCRIPTIONS[tier]) > 10

    def test_agent_mode_enum(self):
        from nvh.core.agentic import AgentMode
        assert AgentMode.AUTO == "auto"
        assert AgentMode.SINGLE == "single"
        assert AgentMode.MULTI == "multi"

    def test_build_changes_summary_empty(self):
        from nvh.core.agent_loop import AgentResult
        from nvh.core.agentic import _build_changes_summary
        result = AgentResult(
            task="test", final_response="done",
            steps=[], total_iterations=0, total_tool_calls=0, completed=True,
        )
        summary = _build_changes_summary(result)
        assert "no tool calls" in summary.lower()

    def test_extract_commands_empty(self):
        from nvh.core.agent_loop import AgentResult
        from nvh.core.agentic import _extract_commands
        result = AgentResult(
            task="test", final_response="done",
            steps=[], total_iterations=0, total_tool_calls=0, completed=True,
        )
        cmds = _extract_commands(result)
        assert cmds == []

    def test_coding_result_defaults(self):
        from nvh.core.agentic import AgentTier, CodingResult
        r = CodingResult(task="test", plan="plan", final_summary="done")
        assert r.completed is False
        assert r.total_cost_usd == Decimal("0")
        assert r.tier == AgentTier.TIER_0
        assert r.quality_gate_passed is None


# -- nvh/providers/openai_provider.py deep --

class TestOpenAIProviderDeep:
    def test_build_messages_no_system(self):
        from nvh.providers.openai_provider import _build_messages
        msgs = _build_messages([Message(role="user", content="hi")], None)
        assert len(msgs) == 1
        assert msgs[0]["role"] == "user"

    def test_build_messages_with_system(self):
        from nvh.providers.openai_provider import _build_messages
        msgs = _build_messages(
            [Message(role="user", content="hi")],
            "You are helpful",
        )
        assert len(msgs) == 2
        assert msgs[0]["role"] == "system"
        assert msgs[0]["content"] == "You are helpful"

    def test_calc_cost_returns_decimal(self):
        from nvh.providers.openai_provider import _calc_cost
        usage = Usage(input_tokens=1000, output_tokens=500, total_tokens=1500)
        cost = _calc_cost("gpt-4o", usage)
        assert isinstance(cost, Decimal)
        assert cost >= 0

    def test_map_error_generic(self):
        from nvh.providers.base import ProviderError
        from nvh.providers.openai_provider import _map_error
        err = _map_error(Exception("something broke"), "openai")
        assert isinstance(err, ProviderError)

    @pytest.mark.asyncio
    async def test_health_check_success_mock(self):
        from nvh.providers.openai_provider import OpenAIProvider
        fake = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="ok"), finish_reason="stop")],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1, total_tokens=2),
            model="gpt-4o",
        )
        with patch("nvh.providers.openai_provider.litellm.acompletion",
                   new=AsyncMock(return_value=fake)):
            p = OpenAIProvider()
            status = await p.health_check()
            assert status.healthy is True
            assert status.latency_ms >= 0

    @pytest.mark.asyncio
    async def test_health_check_failure_mock(self):
        from nvh.providers.openai_provider import OpenAIProvider
        with patch("nvh.providers.openai_provider.litellm.acompletion",
                   new=AsyncMock(side_effect=Exception("down"))):
            p = OpenAIProvider()
            status = await p.health_check()
            assert status.healthy is False
            assert status.error is not None


# -- nvh/core/rate_limiter.py deep --

class TestRateLimiterDeep:
    def test_token_bucket_consume_full(self):
        from nvh.core.rate_limiter import TokenBucket
        tb = TokenBucket(capacity=10, refill_rate=1.0)
        assert tb.consume(5) is True
        assert tb.consume(5) is True
        assert tb.consume(1) is False  # empty

    def test_circuit_breaker_trip_and_recover(self):
        from nvh.core.rate_limiter import CircuitBreaker, CircuitState
        cb = CircuitBreaker(provider="test", failure_threshold=2, initial_cooldown=0.1)
        assert cb.state == CircuitState.CLOSED
        cb.record_failure()
        cb.record_failure()
        assert cb.state == CircuitState.OPEN
        # After cooldown it should transition to HALF_OPEN
        import time
        time.sleep(0.15)
        assert cb.allow_request() is True
        assert cb.state == CircuitState.HALF_OPEN

    def test_health_score_healthy(self):
        from nvh.core.rate_limiter import ProviderRateManager
        rm = ProviderRateManager()
        score = rm.get_health_score("test_provider")
        assert score == 1.0  # no failures = fully healthy

    def test_health_score_after_failure(self):
        from nvh.core.rate_limiter import ProviderRateManager
        rm = ProviderRateManager()
        rm.record_failure("test_p", Exception("err"))
        score = rm.get_health_score("test_p")
        assert 0 < score < 1.0
