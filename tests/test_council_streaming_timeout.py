"""Regression test for the overall-timeout handler in run_council_streaming.

The handler used to read ``m.label`` from CouncilMember — a field that
doesn't exist — so a council-wide timeout raised AttributeError instead
of marking the stragglers as failed. This locks down the fixed contract:
the session returns normally and every unfinished member lands in
failed_members under the same ``provider:persona`` / ``provider`` label
convention the per-member streams use.

All tests use mock providers — no network, no real API calls.
"""

from __future__ import annotations

import asyncio

import pytest

from nvh.config.settings import (
    BudgetConfig,
    CacheConfig,
    CouncilConfig,
    CouncilModeConfig,
    DefaultsConfig,
    ProviderConfig,
    RoutingConfig,
)
from nvh.core.council import CouncilMember, CouncilOrchestrator
from nvh.providers.registry import ProviderRegistry


class _HangingProvider:
    """Mock provider whose stream never yields — a stalled advisor."""

    def __init__(self, name: str) -> None:
        self._name = name
        self.call_count = 0

    @property
    def name(self) -> str:
        return self._name

    async def stream(self, messages, model=None, temperature=1.0,
                     max_tokens=4096, system_prompt=None, **kwargs):
        self.call_count += 1
        # Sleep far longer than any reasonable timeout.
        await asyncio.sleep(3600)
        yield  # never reached — makes this an async generator

    def estimate_tokens(self, text: str) -> int:
        return max(1, len(text) // 4)


def _council_with(providers: dict[str, _HangingProvider]) -> CouncilOrchestrator:
    """Build a CouncilOrchestrator wired to the given provider mocks."""
    config = CouncilConfig(
        defaults=DefaultsConfig(provider=next(iter(providers))),
        providers={
            name: ProviderConfig(enabled=True, default_model="test-model")
            for name in providers
        },
        council=CouncilModeConfig(
            quorum=1,
            strategy="majority_vote",
            timeout=10,
            default_weights={name: 1.0 for name in providers},
            synthesis_provider="",
        ),
        routing=RoutingConfig(),
        budget=BudgetConfig(),
        cache=CacheConfig(enabled=False, ttl_seconds=1, max_size=1),
    )
    registry = ProviderRegistry()
    for name, p in providers.items():
        registry.register(name, p)
    return CouncilOrchestrator(config, registry, rate_manager=None)


class TestCouncilStreamingOverallTimeout:
    @pytest.mark.asyncio
    async def test_timeout_marks_all_members_failed_with_correct_labels(self):
        providers = {
            "alpha": _HangingProvider("alpha"),
            "beta": _HangingProvider("beta"),
        }
        council = _council_with(providers)

        # One member with a persona, one without — the timeout handler must
        # reproduce the provider:persona label used by the member streams.
        members = [
            CouncilMember(
                provider="alpha",
                model="test-model",
                weight=0.5,
                persona="Software Architect",
                system_prompt="You are a software architect.",
            ),
            CouncilMember(provider="beta", model="test-model", weight=0.5),
        ]
        council._resolve_members = (
            lambda members_override=None, weights_override=None: members
        )

        events: list[dict] = []

        async def capture(event: dict) -> None:
            events.append(event)

        # asyncio.wait_for gets council_timeout + 5s of grace, so even with
        # a tiny timeout this test takes ~5 seconds of wall clock.
        result = await council.run_council_streaming(
            query="test prompt",
            on_event=capture,
            synthesize=True,
            temperature=0.0,
            max_tokens=32,
            timeout=0.1,
        )

        assert result.failed_members == {
            "alpha:Software Architect": "timed out",
            "beta": "timed out",
        }
        assert result.member_responses == {}
        assert result.quorum_met is False

        started = {e["member"] for e in events if e.get("type") == "member_start"}
        assert started == {"alpha:Software Architect", "beta"}
        assert events[-1]["type"] == "council_complete"
