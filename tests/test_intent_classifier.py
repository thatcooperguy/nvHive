"""Tests for _classify_intent — the universal smart router.

This function is the entry point for `nvh "prompt"`. It decides
whether the user wants a coding task, code review, test generation,
complex council discussion, or a simple question. Getting this wrong
means the user's prompt goes to the wrong mode entirely.
"""

from __future__ import annotations

import pytest

# Import from the CLI module where it's defined
from nvh.cli.main import _classify_intent


class TestCodingIntent:
    """Prompts that should route to the agent coding mode."""

    @pytest.mark.parametrize("prompt", [
        "Fix the timeout bug in council.py",
        "fix the streaming hang",
        "Refactor the router to use health scores",
        "Add a /v1/ping endpoint",
        "Implement retry logic in the API client",
        "Create a new provider for Mistral",
        "Write a function that sorts by date",
        "Build a caching layer for the models endpoint",
        "Update the config parser to support YAML anchors",
        "Remove the deprecated fallback_model field",
        "Delete the old test fixtures",
        "Rename the council module to orchestrator",
        "Optimize the TF-IDF classifier for speed",
        "Fix this bug in main.py",
        "Add error handling to the webhook endpoint",
        # debug/troubleshoot/investigate
        "Help me debug this weird issue",
        "Debug the flaky websocket connection",
        "Troubleshoot the auth failures in production",
        "Investigate why memory usage keeps growing",
        # help me + coding verb
        "Help me fix this broken endpoint",
        "Help me write a retry wrapper",
        "Help me build a CLI parser",
        # why is ... broken/failing
        "Why is the API failing on large payloads?",
        "Why are my tests crashing on CI?",
        "Why does the server keep erroring out?",
        # make ... work/faster/better
        "Make the search endpoint faster",
        "Make this query work with PostgreSQL",
        "Make the parser better at handling edge cases",
        # how do I + coding verb
        "How do I implement pagination?",
        "How do I connect to the Redis cluster?",
        "How do I integrate Stripe payments?",
        "How do I deploy to AWS Lambda?",
        "How do I setup the dev environment?",
        # file extensions without a verb
        "Something is wrong with utils.py",
        "The layout.css is all messed up",
        "Check out routes.ts for the bug",
        # error/exception/crash/bug context
        "There's a TypeError exception in the handler",
        "Getting a crash when uploading large files",
        "Bug in the auth middleware after the refactor",
        "Error with the database connection on startup",
        # migrate/upgrade/convert/port
        "Migrate the codebase from Flask to FastAPI",
        "Upgrade the ORM to SQLAlchemy 2.0",
        "Convert the callback API to async/await",
        "Port the Go service to Rust",
    ])
    def test_detects_coding_tasks(self, prompt):
        result = _classify_intent(prompt)
        assert result in ("coding", "iterative_coding"), f"Expected coding intent for: {prompt}, got {result}"


class TestReviewIntent:
    """Prompts that should route to the code review mode."""

    @pytest.mark.parametrize("prompt", [
        "Review my staged changes",
        "Review this PR",
        "Check my code for bugs",
        "Review the changes I just made",
        "Audit the security of this codebase",
        "Check this code for issues",
        # look at my code/changes
        "Look at my code and tell me if it's right",
        "Look at this changes before I push",
        # what do you think of this code
        "What do you think of this code?",
        "What do you think of my implementation?",
        # is this code correct/safe/good
        "Is this code correct?",
        "Is my code safe for production?",
        "Is this implementation good enough?",
    ])
    def test_detects_review_requests(self, prompt):
        assert _classify_intent(prompt) == "review", f"Expected 'review' for: {prompt}"


class TestTestgenIntent:
    """Prompts that should route to test generation."""

    @pytest.mark.parametrize("prompt", [
        "Add tests for the auth module",
        "Write unit tests for council.py",
        "Generate tests for the router",
        "Create tests for the API endpoints",
        "Add test coverage for the streaming code",
        "coverage gaps",
        # test coverage without "add"
        "We need better test coverage",
        # how to test this
        "How to test this endpoint?",
        # need tests for
        "Need tests for the new parser",
        "I need tests for the auth module",
    ])
    def test_detects_test_requests(self, prompt):
        assert _classify_intent(prompt) == "testgen", f"Expected 'testgen' for: {prompt}"


class TestComplexIntent:
    """Prompts that should route to council mode (complex questions)."""

    @pytest.mark.parametrize("prompt", [
        "Should we use Redis or Postgres for session storage?",
        "Compare Python vs Go for microservices",
        "What are the pros and cons of monorepos?",
        "Evaluate whether we should migrate to Kubernetes",
        "Design a scalable notification system",
        "Which is better for our use case: REST or GraphQL?",
        "Recommend an architecture for real-time analytics",
        # "what's the best way to"
        "What's the best way to handle authentication?",
        "What's the best approach for caching?",
        # "how should I approach"
        "How should I approach database sharding?",
        "How should I structure the microservices?",
        # Multi-part questions with "and" + "?"
        "Should I use SQLite and how does it compare to Postgres?",
        "What framework should I pick and what ORM goes with it?",
    ])
    def test_detects_complex_questions(self, prompt):
        assert _classify_intent(prompt) == "complex", f"Expected 'complex' for: {prompt}"


class TestSimpleIntent:
    """Prompts that should route to a single advisor (simple questions)."""

    @pytest.mark.parametrize("prompt", [
        "What is a binary search tree?",
        "Hello",
        "Explain quicksort",
        "What does this error mean?",
        "How do I install pandas?",
        "What is the capital of France?",
        "Tell me a joke",
    ])
    def test_detects_simple_questions(self, prompt):
        assert _classify_intent(prompt) == "simple", f"Expected 'simple' for: {prompt}"


class TestEdgeCases:
    """Edge cases and ambiguous prompts."""

    def test_empty_prompt(self):
        result = _classify_intent("")
        assert result == "simple"

    def test_single_word(self):
        result = _classify_intent("hello")
        assert result == "simple"

    def test_case_insensitive(self):
        assert _classify_intent("FIX THE BUG") == "coding"
        assert _classify_intent("REVIEW MY CODE") == "review"
        assert _classify_intent("ADD TESTS") == "testgen"
