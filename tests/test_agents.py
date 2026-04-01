"""Tests for auto-agent generation."""

import pytest

from nvh.core.agents import (
    generate_agents,
    get_preset_agents,
    list_presets,
)


class TestAgentGeneration:
    def test_generates_agents_for_architecture_query(self):
        agents = generate_agents(
            "Should we migrate from a monolith to microservices?",
            num_agents=3,
        )
        assert len(agents) == 3
        roles = [a.role for a in agents]
        # Should pick architecture/engineering relevant roles
        assert any("Architect" in r or "CTO" in r for r in roles)

    def test_generates_agents_for_security_query(self):
        agents = generate_agents(
            "How should we implement OAuth authentication with JWT tokens?",
            num_agents=3,
        )
        roles = [a.role for a in agents]
        assert any("Security" in r for r in roles)

    def test_generates_agents_for_database_query(self):
        agents = generate_agents(
            "Should we use PostgreSQL or MongoDB for our user data?",
            num_agents=3,
        )
        roles = [a.role for a in agents]
        assert any("Database" in r or "Architect" in r for r in roles)

    def test_generates_agents_for_business_query(self):
        agents = generate_agents(
            "What pricing strategy should we use for our SaaS product?",
            num_agents=3,
        )
        roles = [a.role for a in agents]
        assert any("CFO" in r or "CEO" in r or "Product" in r for r in roles)

    def test_generates_agents_for_devops_query(self):
        agents = generate_agents(
            "How should we set up our CI/CD pipeline with Docker and Kubernetes?",
            num_agents=3,
        )
        roles = [a.role for a in agents]
        assert any("DevOps" in r or "SRE" in r for r in roles)

    def test_generates_minimum_agents(self):
        agents = generate_agents("hi", num_agents=3, min_agents=2)
        assert len(agents) >= 2

    def test_respects_max_agents(self):
        agents = generate_agents(
            "Design a distributed database with security, testing, monitoring, and CI/CD",
            num_agents=10,
            max_agents=5,
        )
        assert len(agents) <= 5

    def test_agent_has_system_prompt(self):
        agents = generate_agents("Build a REST API", num_agents=1, min_agents=1)
        assert len(agents) >= 1
        assert agents[0].system_prompt
        assert agents[0].role in agents[0].system_prompt

    def test_all_agents_have_unique_roles(self):
        agents = generate_agents(
            "Design a cloud-native application with database, authentication, and monitoring",
            num_agents=5,
        )
        roles = [a.role for a in agents]
        assert len(roles) == len(set(roles)), f"Duplicate roles: {roles}"

    def test_fallback_for_ambiguous_query(self):
        agents = generate_agents("hmm", num_agents=3, min_agents=2)
        assert len(agents) >= 2
        # Should get sensible defaults
        for agent in agents:
            assert agent.role
            assert agent.system_prompt


class TestPresets:
    def test_list_presets(self):
        presets = list_presets()
        assert "executive" in presets
        assert "engineering" in presets
        assert "security_review" in presets
        assert "code_review" in presets
        assert "product" in presets
        assert "data" in presets
        assert "full_board" in presets

    def test_executive_preset(self):
        agents = get_preset_agents("executive", "test query")
        roles = [a.role for a in agents]
        assert "CEO / Business Strategist" in roles
        assert "CFO / Financial Analyst" in roles
        assert "CTO" in roles

    def test_engineering_preset(self):
        agents = get_preset_agents("engineering", "test query")
        roles = [a.role for a in agents]
        assert "Software Architect" in roles
        assert "DevOps/SRE Engineer" in roles

    def test_invalid_preset(self):
        with pytest.raises(ValueError, match="Unknown preset"):
            get_preset_agents("nonexistent", "test")

    def test_preset_agents_have_system_prompts(self):
        agents = get_preset_agents("engineering", "Build a REST API")
        for agent in agents:
            assert agent.system_prompt
            assert agent.role in agent.system_prompt
