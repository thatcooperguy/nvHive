"""Tests for the council orchestrator."""

from decimal import Decimal

from nvh.core.council import CouncilMember
from nvh.providers.base import CompletionResponse, Usage


class TestCouncilMemberResolution:
    def test_normalize_weights(self):
        members = [
            CouncilMember(provider="a", model="m1", weight=0.3),
            CouncilMember(provider="b", model="m2", weight=0.3),
            CouncilMember(provider="c", model="m3", weight=0.3),
        ]
        total = sum(m.weight for m in members)
        assert abs(total - 0.9) < 0.01

    def test_council_response_cost(self):
        r1 = CompletionResponse(
            content="Response 1", model="m1", provider="a",
            usage=Usage(input_tokens=100, output_tokens=50, total_tokens=150),
            cost_usd=Decimal("0.001"),
        )
        r2 = CompletionResponse(
            content="Response 2", model="m2", provider="b",
            usage=Usage(input_tokens=100, output_tokens=60, total_tokens=160),
            cost_usd=Decimal("0.002"),
        )
        total = r1.cost_usd + r2.cost_usd
        assert total == Decimal("0.003")
