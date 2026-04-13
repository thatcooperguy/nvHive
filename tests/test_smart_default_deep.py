"""Deep tests for _smart_default routing logic and _classify_intent edge cases.

_smart_default is the universal entry point for `nvh "prompt"`. These tests
verify the routing decisions (action -> iterative -> coding -> review ->
testgen -> council -> simple) with mocked engine/providers.

No real API calls, no real filesystem changes.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nvh.cli.main import _classify_intent

# ---------------------------------------------------------------------------
# _classify_intent — edge cases not covered by test_intent_classifier.py
# ---------------------------------------------------------------------------


class TestClassifyIntentEdgeCases:

    def test_iterative_coding_architect(self):
        assert _classify_intent("architect a new microservices system") == "iterative_coding"

    def test_iterative_coding_rewrite(self):
        assert _classify_intent("rewrite the entire authentication module") == "iterative_coding"

    def test_iterative_coding_refactor_entire(self):
        assert _classify_intent("refactor the entire codebase to use async") == "iterative_coding"

    def test_iterative_coding_fix_multiple(self):
        assert _classify_intent("fix multiple bugs across the project") == "iterative_coding"

    def test_iterative_coding_design_and_implement(self):
        assert _classify_intent("design and implement a notification service") == "iterative_coding"

    def test_coding_add_endpoint(self):
        assert _classify_intent("add a new endpoint for user profiles") == "coding"

    def test_testgen_coverage_gaps(self):
        assert _classify_intent("find the coverage gaps in our test suite") == "testgen"

    def test_testgen_need_tests_for(self):
        assert _classify_intent("I need tests for the router module") == "testgen"

    def test_review_look_at_my_diff(self):
        assert _classify_intent("look at my diff before I push") == "review"

    def test_review_is_this_code_safe(self):
        assert _classify_intent("is this code safe for production?") == "review"

    def test_complex_tradeoff(self):
        assert _classify_intent("what are the trade-offs between monolith and microservices?") == "complex"

    def test_complex_whats_best_way(self):
        assert _classify_intent("what's the best way to scale a database?") == "complex"

    def test_simple_greeting(self):
        assert _classify_intent("hi there") == "simple"

    def test_simple_basic_question(self):
        assert _classify_intent("what time is it?") == "simple"

    def test_whitespace_only(self):
        assert _classify_intent("   ") == "simple"

    def test_testgen_takes_priority_over_coding(self):
        """'add tests' should match testgen, not coding (even though 'add' is a coding verb)."""
        assert _classify_intent("add tests for the auth module") == "testgen"

    def test_review_takes_priority_over_complex(self):
        """'review my code' is review, not complex."""
        assert _classify_intent("review my code changes") == "review"


# ---------------------------------------------------------------------------
# _smart_default routing — mocked integration tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_smart_default_routes_system_action():
    """If detect_action returns an action, _smart_default executes it directly."""
    mock_action = MagicMock()
    mock_action.tool_name = "shell"
    mock_action.description = "list processes"
    mock_action.requires_confirm = False
    mock_action.arguments = {"command": "ps"}

    with patch("nvh.core.action_detector.detect_action", return_value=mock_action), \
         patch("nvh.cli.main._execute_action", new_callable=AsyncMock) as mock_exec:
        from nvh.cli.main import _smart_default
        await _smart_default("list all processes")
        mock_exec.assert_awaited_once_with(mock_action)


@pytest.mark.asyncio
async def test_smart_default_routes_to_coding_agent():
    """A coding prompt should trigger the agent coding path."""
    with patch("nvh.core.action_detector.detect_action", return_value=None), \
         patch("nvh.config.settings.load_config") as mock_config, \
         patch("nvh.core.engine.Engine") as MockEngine, \
         patch("nvh.core.agentic.run_coding_agent", new_callable=AsyncMock) as mock_agent, \
         patch("nvh.core.agentic.auto_detect_config") as mock_detect_config, \
         patch("nvh.cli.main.console"):

        # Setup mock engine — use MagicMock for sync methods, AsyncMock for async
        mock_engine_inst = MagicMock()
        MockEngine.return_value = mock_engine_inst
        mock_engine_inst.initialize = AsyncMock()
        mock_engine_inst.registry = MagicMock()
        mock_engine_inst.registry.list_enabled.return_value = ["groq"]
        mock_engine_inst.rate_manager = MagicMock()
        mock_engine_inst.rate_manager.get_health_score.return_value = 1.0

        mock_config.return_value = MagicMock()
        mock_config.return_value.defaults.mode = "performant"

        mock_agent_config = MagicMock()
        mock_detect_config.return_value = mock_agent_config

        mock_result = MagicMock()
        mock_result.error = ""
        mock_result.final_response = "Done"
        mock_agent.return_value = mock_result

        from nvh.cli.main import _smart_default
        await _smart_default("fix the bug in main.py")
        mock_agent.assert_awaited_once()


@pytest.mark.asyncio
async def test_smart_default_routes_simple_to_engine_query():
    """A simple question should route to engine.query (single advisor)."""
    with patch("nvh.core.action_detector.detect_action", return_value=None), \
         patch("nvh.config.settings.load_config") as mock_config, \
         patch("nvh.core.engine.Engine") as MockEngine, \
         patch("nvh.cli.main.console") as mock_console:

        mock_engine_inst = MagicMock()
        MockEngine.return_value = mock_engine_inst
        mock_engine_inst.initialize = AsyncMock()
        mock_engine_inst.registry = MagicMock()
        mock_engine_inst.registry.list_enabled.return_value = ["groq"]
        mock_engine_inst.rate_manager = MagicMock()
        mock_engine_inst.rate_manager.get_health_score.return_value = 1.0

        mock_config.return_value = MagicMock()
        mock_config.return_value.defaults.mode = "performant"

        mock_response = MagicMock()
        mock_response.content = "42"
        mock_response.provider = "groq"
        mock_response.model = "llama3"
        mock_response.usage = MagicMock()
        mock_response.usage.total_tokens = 10
        mock_response.cost_usd = 0
        mock_response.latency_ms = 50
        mock_response.fallback_from = None
        mock_response.cache_hit = False
        mock_engine_inst.query = AsyncMock(return_value=mock_response)

        from nvh.cli.main import _smart_default
        await _smart_default("What is the meaning of life?")
        mock_engine_inst.query.assert_awaited_once()


@pytest.mark.asyncio
async def test_smart_default_iterative_with_flag():
    """force_iterative=True should route to iterative_solve regardless of intent."""
    with patch("nvh.core.action_detector.detect_action", return_value=None), \
         patch("nvh.config.settings.load_config") as mock_config, \
         patch("nvh.core.engine.Engine") as MockEngine, \
         patch("nvh.core.iterative_loop.iterative_solve", new_callable=AsyncMock) as mock_iter, \
         patch("nvh.core.iterative_loop.format_iterative_result", return_value="result"), \
         patch("nvh.cli.main.console"):

        mock_engine_inst = MagicMock()
        MockEngine.return_value = mock_engine_inst
        mock_engine_inst.initialize = AsyncMock()
        mock_engine_inst.registry = MagicMock()
        mock_engine_inst.registry.list_enabled.return_value = ["groq", "openai"]
        mock_engine_inst.rate_manager = MagicMock()
        mock_engine_inst.rate_manager.get_health_score.return_value = 1.0

        mock_config.return_value = MagicMock()
        mock_config.return_value.defaults.mode = "performant"

        mock_result = MagicMock()
        mock_iter.return_value = mock_result

        from nvh.cli.main import _smart_default
        await _smart_default("hello world", force_iterative=True)
        mock_iter.assert_awaited_once()
