"""Tests for smart agent-to-LLM matching."""

from __future__ import annotations

from decimal import Decimal

from nvh.core.agent_matching import (
    _ROLE_TO_TASK_TYPES,
    AgentAssignment,
    _explain_match,
    _score_provider_for_role,
    format_team_report,
    match_agents_to_providers,
)
from nvh.core.agents import generate_agents
from nvh.providers.base import CompletionResponse, HealthStatus, ModelInfo, Usage
from nvh.providers.registry import ProviderRegistry


class _FakeProvider:
    def __init__(self, name):
        self._name = name

    @property
    def name(self):
        return self._name

    async def complete(self, messages, **kw):
        return CompletionResponse(
            content="ok", model="m", provider=self._name,
            usage=Usage(total_tokens=5), cost_usd=Decimal("0"), latency_ms=1,
        )

    async def list_models(self):
        return [ModelInfo(model_id="m", provider=self._name)]

    async def health_check(self):
        return HealthStatus(provider=self._name, healthy=True, latency_ms=1)

    def estimate_tokens(self, t):
        return 1


class _FakeEngine:
    def __init__(self, providers):
        self.registry = ProviderRegistry()
        for p in providers:
            self.registry.register(p, _FakeProvider(p))
        self.config = type("C", (), {
            "providers": {p: type("PC", (), {"default_model": "m"})() for p in providers}
        })()
        self.learning = None

        # Add rate_manager
        from nvh.core.rate_limiter import ProviderRateManager
        self.rate_manager = ProviderRateManager()


class TestRoleMapping:
    def test_all_persona_roles_have_task_types(self):
        from nvh.core.agents import _PERSONA_POOL
        for template in _PERSONA_POOL:
            assert template.role in _ROLE_TO_TASK_TYPES, (
                f"Missing task type mapping for {template.role}"
            )

    def test_task_types_are_lists(self):
        for role, types in _ROLE_TO_TASK_TYPES.items():
            assert isinstance(types, list)
            assert len(types) >= 1


class TestScoring:
    def test_score_known_provider_known_type(self):
        engine = _FakeEngine(["openai"])
        score = _score_provider_for_role("openai", ["reasoning"], engine)
        assert score > 0.5  # OpenAI is strong at reasoning

    def test_score_unknown_provider(self):
        engine = _FakeEngine(["unknown_provider"])
        score = _score_provider_for_role("unknown_provider", ["reasoning"], engine)
        assert score > 0  # baseline score

    def test_prefer_local_boosts_ollama(self):
        engine = _FakeEngine(["ollama"])
        normal = _score_provider_for_role("ollama", ["code_generation"], engine)
        boosted = _score_provider_for_role("ollama", ["code_generation"], engine, prefer_local=True)
        assert boosted > normal

    def test_explain_match_with_strengths(self):
        reason = _explain_match("openai", ["reasoning", "analysis"], 0.85)
        assert "reasoning" in reason.lower() or "analysis" in reason.lower()
        assert "0.85" in reason


class TestMatchAgentsToProviders:
    def test_basic_matching(self):
        engine = _FakeEngine(["openai", "groq", "anthropic"])
        agents = generate_agents("Design a database schema for an e-commerce platform")
        assignments = match_agents_to_providers(agents, engine)
        assert len(assignments) == len(agents)
        for a in assignments:
            assert isinstance(a, AgentAssignment)
            assert a.provider in ("openai", "groq", "anthropic")
            assert len(a.reason) > 0
            assert a.score > 0

    def test_diversity_avoids_all_same_provider(self):
        engine = _FakeEngine(["openai", "groq", "anthropic"])
        agents = generate_agents("Build a full-stack web application with auth and database", num_agents=5)
        assignments = match_agents_to_providers(agents, engine)
        providers_used = {a.provider for a in assignments}
        # With 5 agents and 3 providers, should use at least 2 different ones
        assert len(providers_used) >= 2

    def test_single_provider(self):
        engine = _FakeEngine(["groq"])
        agents = generate_agents("What is quicksort?")
        assignments = match_agents_to_providers(agents, engine)
        assert all(a.provider == "groq" for a in assignments)

    def test_excludes_providers(self):
        engine = _FakeEngine(["openai", "groq", "anthropic"])
        agents = generate_agents("Database design question")
        assignments = match_agents_to_providers(
            agents, engine, exclude_providers={"openai"},
        )
        assert all(a.provider != "openai" for a in assignments)

    def test_empty_providers(self):
        engine = _FakeEngine([])
        agents = generate_agents("hello")
        assignments = match_agents_to_providers(agents, engine)
        assert assignments == []


class TestFormatReport:
    def test_format_team_report(self):
        assignments = [
            AgentAssignment(role="CTO", provider="openai", model="gpt-4o",
                            reason="Strong at reasoning", score=0.9),
            AgentAssignment(role="DBA", provider="groq", model="llama-70b",
                            reason="Strong at database", score=0.8),
        ]
        report = format_team_report(assignments)
        assert "CTO" in report
        assert "DBA" in report
        assert "openai" in report
        assert "groq" in report


class TestEndToEnd:
    def test_generate_and_match(self):
        """Full flow: generate agents from a prompt, match to LLMs."""
        engine = _FakeEngine(["openai", "groq", "anthropic", "ollama"])
        agents = generate_agents(
            "Design a secure microservices architecture for a fintech platform "
            "with real-time analytics and mobile app support",
            num_agents=5,
        )
        assignments = match_agents_to_providers(agents, engine)
        assert len(assignments) == 5
        # Should have diversity — fintech query triggers security, backend, mobile, data roles
        roles = {a.role for a in assignments}
        assert len(roles) == 5  # all unique roles

        report = format_team_report(assignments)
        assert len(report) > 100  # substantial report
