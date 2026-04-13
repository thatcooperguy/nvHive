"""Tests for recursive agent spawning."""

from __future__ import annotations

from decimal import Decimal

import pytest

from nvh.core.recursive_agents import (
    AgentResponse,
    RecursiveResult,
    ReferralRequest,
    detect_referrals,
    format_recursive_result,
    run_with_referrals,
)
from nvh.providers.base import CompletionResponse, Usage
from nvh.providers.registry import ProviderRegistry


class TestDetectReferrals:
    def test_single_referral(self):
        text = "The auth system looks good. REFER: Need a Database Expert for the session storage design."
        refs = detect_referrals(text, "Backend Engineer", depth=0)
        assert len(refs) == 1
        assert refs[0].requested_role == "Database Expert"
        assert "session storage" in refs[0].context
        assert refs[0].depth == 1

    def test_multiple_referrals(self):
        text = (
            "REFER: Need a Security Engineer for the auth token design.\n"
            "Also, REFER: Consult a Frontend Developer for the login flow."
        )
        refs = detect_referrals(text, "CTO", depth=0)
        assert len(refs) == 2

    def test_no_referrals(self):
        text = "Everything looks fine. No additional expertise needed."
        refs = detect_referrals(text, "QA", depth=0)
        assert refs == []

    def test_loop_in_format(self):
        text = "REFER: Loop in a DevOps Engineer for the deployment pipeline."
        refs = detect_referrals(text, "Backend", depth=1)
        assert len(refs) == 1
        assert refs[0].requested_role == "DevOps Engineer"
        assert refs[0].depth == 2  # parent was depth 1


class TestReferralRequest:
    def test_construction(self):
        rr = ReferralRequest(
            requesting_agent="Backend",
            requested_role="DBA",
            context="sharding strategy",
            depth=1,
        )
        assert rr.requesting_agent == "Backend"
        assert rr.depth == 1


class TestAgentResponse:
    def test_with_referrals(self):
        refs = [ReferralRequest("A", "B", "help", 1)]
        ar = AgentResponse(
            role="Backend", provider="groq", model="llama",
            content="response text", referrals=refs,
            spawned_from="", depth=0,
        )
        assert len(ar.referrals) == 1

    def test_spawned_agent(self):
        ar = AgentResponse(
            role="DBA", provider="openai", model="gpt-4o",
            content="sharding advice", spawned_from="Backend", depth=1,
        )
        assert ar.spawned_from == "Backend"
        assert ar.depth == 1


class TestRunWithReferrals:
    @pytest.mark.asyncio
    async def test_no_referrals_returns_initial(self):
        class FakeEngine:
            registry = ProviderRegistry()

        initial = [
            AgentResponse(role="Backend", provider="groq", model="m",
                          content="All good, no referrals needed.", depth=0),
        ]
        result = await run_with_referrals("build API", FakeEngine(), initial)
        assert result.total_agents == 1
        assert result.spawned_agents == 0

    @pytest.mark.asyncio
    async def test_spawns_referred_agent(self):
        class FakeP:
            name = "groq"
            async def complete(self, messages, **kw):
                return CompletionResponse(
                    content="Here's my expert opinion on databases.",
                    model="m", provider="groq",
                    usage=Usage(total_tokens=10), cost_usd=Decimal("0"),
                    latency_ms=100,
                )
            async def list_models(self): return []
            async def health_check(self): pass
            def estimate_tokens(self, t): return 1

        class FakeEngine:
            registry = ProviderRegistry()
            config = type("C", (), {"providers": {"groq": type("P", (), {"default_model": "m"})()}})()
            rate_manager = type("RM", (), {"get_health_score": lambda s, p: 1.0})()
            async def query(self, prompt="", **kw):
                return CompletionResponse(
                    content="Database sharding recommendation.",
                    model="m", provider="groq",
                    usage=Usage(total_tokens=20), cost_usd=Decimal("0.001"),
                    latency_ms=50,
                )

        FakeEngine.registry.register("groq", FakeP())

        initial = [
            AgentResponse(
                role="Backend", provider="groq", model="m",
                content="REFER: Need a Database Expert for the sharding strategy.",
                depth=0,
            ),
        ]
        result = await run_with_referrals("design data layer", FakeEngine(), initial)
        assert result.total_agents == 2
        assert result.spawned_agents == 1
        assert any(r.role == "Database Expert" for r in result.responses)
        assert any(r.spawned_from == "Backend" for r in result.responses)

    @pytest.mark.asyncio
    async def test_respects_max_depth(self):
        class FakeEngine:
            registry = ProviderRegistry()

        initial = [
            AgentResponse(
                role="A", provider="x", model="m",
                content="REFER: Need a B for help.", depth=2,
            ),
        ]
        # max_depth=2 means depth 2 agents can't refer (depth 3 blocked)
        result = await run_with_referrals("task", FakeEngine(), initial, max_depth=2)
        assert result.spawned_agents == 0  # referral at depth 3 was skipped


class TestFormatResult:
    def test_format_non_empty(self):
        result = RecursiveResult(
            task="Build API",
            responses=[
                AgentResponse(role="Backend", provider="groq", model="m",
                              content="Built the endpoints.", depth=0),
                AgentResponse(role="DBA", provider="openai", model="gpt-4o",
                              content="Schema design.", spawned_from="Backend", depth=1),
            ],
            total_agents=2, spawned_agents=1, max_depth_reached=1,
            duration_ms=5000, total_cost_usd=Decimal("0.02"),
        )
        output = format_recursive_result(result)
        assert "Backend" in output
        assert "DBA" in output
        assert "referred by Backend" in output
        assert "1 spawned" in output
