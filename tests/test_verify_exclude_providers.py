"""``--verify`` honours ``exclude_providers``.

:func:`nvh.core.smart_query.verify_response` runs its cross-check under the
same exclusion set the answer was produced under: an excluded advisor is
never picked as the verifier, and the verifier's own ``engine.query`` carries
the set so the engine's fallback chain cannot land there either. The CLI
threads the set its main query used (``SEARCH_NATIVE_PROVIDERS`` on a
``--focus research`` turn, grounded or in privacy mode; nothing otherwise)
into the one ``engine.query`` call that also asks for the verification, and
the engine forwards the set into both of its ``verify_response`` calls
(tests/test_engine_query.py pins the forwarding), so a grounded answer is
never re-checked by Perplexity, not even as a fallback. Verification is
independent or not at all: when the only advisor left is the answer's own,
the result is ``unverified`` rather than a self-check.
"""

from __future__ import annotations

import types
from unittest.mock import AsyncMock, MagicMock

import nvh.cli.main as cli_main
from nvh.core.smart_query import verify_response

_VERDICT = "VERDICT: correct\nCONFIDENCE: 9/10\nISSUES: none\nCORRECTION: none"


def _engine(enabled, *, health=None):
    """A fake engine with the three seams verify_response touches: the enabled
    list, the health score and ``query``."""
    scores = dict(health or {})
    engine = MagicMock()
    engine.registry.list_enabled.return_value = list(enabled)
    engine.rate_manager.get_health_score.side_effect = lambda p: scores.get(p, 1.0)
    engine.query = AsyncMock(return_value=types.SimpleNamespace(content=_VERDICT))
    return engine


class TestVerifierChoice:
    async def test_verifier_is_picked_outside_the_exclusion_and_the_query_carries_it(self):
        engine = _engine(["perplexity", "openai", "anthropic"])
        result = await verify_response(
            engine, "q", "the answer", "anthropic", exclude_providers=frozenset({"perplexity"}),
        )
        assert (result.verdict, result.verifier_provider) == ("correct", "openai")
        kwargs = engine.query.await_args.kwargs
        assert kwargs["provider"] == "openai"
        assert kwargs["exclude_providers"] == frozenset({"perplexity"})
        assert (kwargs["escalate"], kwargs["verify"]) == (False, False)

    async def test_no_exclusion_keeps_the_first_other_healthy_advisor_and_still_passes_the_set(self):
        """The default is unchanged (first enabled advisor that is not the
        answer's own); the empty set is still handed on so the engine's chain
        builder sees one call shape."""
        engine = _engine(["perplexity", "openai"])
        result = await verify_response(engine, "q", "the answer", "openai")
        assert result.verifier_provider == "perplexity"
        assert engine.query.await_args.kwargs["exclude_providers"] == frozenset()

    async def test_the_exclusion_applies_before_the_health_fallback(self):
        """Every advisor circuit-broken: the all-unhealthy fallback widens to
        the enabled list, which must already be filtered."""
        engine = _engine(["perplexity", "openai"], health={"perplexity": 0.0, "openai": 0.0})
        result = await verify_response(
            engine, "q", "the answer", "anthropic", exclude_providers={"perplexity"},
        )
        assert result.verifier_provider == "openai"
        assert engine.query.await_args.kwargs["provider"] == "openai"

    async def test_the_one_advisor_left_is_the_answers_own_so_the_result_is_unverified(self):
        """When the only advisor outside the set is the one that answered,
        nothing independent is left: the result is ``unverified`` (a model
        grading its own answer is not a verification) and the excluded
        advisor is never pulled in."""
        engine = _engine(["perplexity", "openai"])
        result = await verify_response(
            engine, "q", "the answer", "openai", exclude_providers=frozenset({"perplexity"}),
        )
        assert result.verdict == "unverified"
        assert result.issues == ["No independent verifier available"]
        assert result.verifier_provider == "none"
        engine.query.assert_not_awaited()

    async def test_a_single_provider_install_is_unverified_rather_than_self_checked(self):
        """The same policy without any exclusion: one enabled advisor cannot
        independently verify its own answer."""
        engine = _engine(["ollama"])
        result = await verify_response(engine, "q", "the answer", "ollama")
        assert (result.verdict, result.verifier_provider) == ("unverified", "none")
        assert result.issues == ["No independent verifier available"]
        engine.query.assert_not_awaited()

    async def test_nothing_left_outside_the_set_is_unverified_not_a_perplexity_check(self):
        engine = _engine(["perplexity"])
        result = await verify_response(
            engine, "q", "the answer", "openai", exclude_providers=frozenset({"perplexity"}),
        )
        assert result.verdict == "unverified"
        assert result.issues == ["No verifier available"]
        assert result.verifier_provider == "none"
        engine.query.assert_not_awaited()

    async def test_any_collection_is_accepted_and_the_engine_gets_a_frozenset(self):
        for given in (["perplexity"], {"perplexity"}, ("perplexity",)):
            engine = _engine(["perplexity", "openai", "groq"])
            await verify_response(engine, "q", "the answer", "groq", exclude_providers=given)
            sent = engine.query.await_args.kwargs["exclude_providers"]
            assert isinstance(sent, frozenset) and sent == frozenset({"perplexity"})

    async def test_a_failing_verifier_under_exclusion_stays_unverified_with_that_verifier(self):
        engine = _engine(["perplexity", "openai"])
        engine.query = AsyncMock(side_effect=RuntimeError("openai down"))
        result = await verify_response(
            engine, "q", "the answer", "anthropic", exclude_providers=frozenset({"perplexity"}),
        )
        assert (result.verdict, result.verifier_provider) == ("unverified", "openai")
        assert result.issues == ["Verification failed"]
        assert engine.query.await_count == 1
        assert engine.query.await_args.kwargs["exclude_providers"] == frozenset({"perplexity"})


class TestAskThreadsTheSetIntoTheVerifyingCall:
    """The CLI never calls verify_response itself: ``--verify`` rides on the
    same ``engine.query`` call as the answer, so the exclusion set that call
    carries is the one the cross-check runs under."""

    _HITS = {
        "ok": True, "backend": "searxng", "query": "state of fusion power",
        "results": [
            {"title": "ITER schedule update", "url": "https://iter.org/news", "snippet": "First plasma now 2034."},
        ],
    }

    def _fake_engine(self, monkeypatch, enabled, seen):
        class FakeRegistry:
            def has(self, name):
                return name in enabled

        class FakeRouter:
            def route(self, query, **kwargs):
                return types.SimpleNamespace(provider="perplexity", model="m", reason="fake", scores={})

        class FakeEngine:
            registry = FakeRegistry()
            router = FakeRouter()

            def __init__(self, config=None):
                pass

            async def initialize(self):
                return list(enabled)

            async def query(self, **kwargs):
                seen["query"] = kwargs
                chosen = kwargs.get("provider") or "router"
                return types.SimpleNamespace(
                    content=f"answer from {chosen}", metadata={}, fallback_from=None, provider=chosen,
                    model="m", cost_usd=0, latency_ms=1, cache_hit=False,
                    usage=types.SimpleNamespace(total_tokens=0, input_tokens=0, output_tokens=0),
                )

        import nvh.core.engine as engine_mod
        import nvh.integrations.web_search as web_search_pkg

        async def fake_web_search(query, *, top_k=5, timeout=10.0):
            return self._HITS

        monkeypatch.setattr(engine_mod, "Engine", FakeEngine)
        monkeypatch.setattr(web_search_pkg, "web_search", fake_web_search)
        monkeypatch.setattr(cli_main, "_read_stdin", lambda: "")

    def test_research_verify_carries_the_search_native_exclusion_on_the_verifying_call(self, monkeypatch):
        seen: dict = {}
        self._fake_engine(monkeypatch, ["openai", "perplexity"], seen)
        cli_main._ask("state of fusion power", focus="research", verify=True, output="raw", quiet=True)
        assert seen["query"]["verify"] is True
        assert seen["query"]["exclude_providers"] == cli_main.SEARCH_NATIVE_PROVIDERS == frozenset({"perplexity"})

    def test_privacy_research_verify_carries_the_same_set(self, monkeypatch):
        seen: dict = {}
        self._fake_engine(monkeypatch, ["openai", "perplexity"], seen)
        cli_main._ask(
            "state of fusion power", focus="research", privacy=True, verify=True, output="raw", quiet=True,
        )
        assert seen["query"]["verify"] is True and seen["query"]["privacy"] is True
        assert seen["query"]["exclude_providers"] == frozenset({"perplexity"})

    def test_a_plain_verify_turn_excludes_nothing(self, monkeypatch):
        seen: dict = {}
        self._fake_engine(monkeypatch, ["openai", "perplexity"], seen)
        cli_main._ask("state of fusion power", verify=True, output="raw", quiet=True)
        assert seen["query"]["verify"] is True
        assert seen["query"]["exclude_providers"] == frozenset()
