"""Tests for the smart query pure functions (confidence & verification parsing)."""

import pytest

from nvh.core.smart_query import _parse_verification, assess_confidence


class TestAssessConfidence:
    def test_assess_confidence_high(self):
        """Clear, direct response yields high confidence (>0.7)."""
        text = (
            "Python is a high-level, interpreted programming language created by "
            "Guido van Rossum and first released in 1991. It emphasizes code "
            "readability with its use of significant indentation. Python supports "
            "multiple programming paradigms including structured, object-oriented, "
            "and functional programming."
        )
        assert assess_confidence(text) > 0.7

    def test_assess_confidence_low_hedging(self):
        """Response with hedging phrases yields low confidence (<0.6)."""
        text = (
            "I'm not sure about this, but I think it might be related to the "
            "configuration. It could be a network issue, possibly caused by a "
            "firewall. I believe the answer is somewhere in the docs."
        )
        assert assess_confidence(text) < 0.6

    def test_assess_confidence_short(self):
        """Very short response (<20 words) is penalized."""
        short_text = "Yes, that is correct."
        long_text = (
            "Yes, that is correct. The function returns a boolean value indicating "
            "whether the operation was successful. It checks the input parameters, "
            "validates the configuration, and then proceeds with the execution of "
            "the underlying system call."
        )
        short_score = assess_confidence(short_text)
        long_score = assess_confidence(long_text)
        assert short_score < long_score

    def test_assess_confidence_questions(self):
        """Response with multiple question marks is penalized."""
        text_no_questions = (
            "The answer is straightforward. You need to configure the environment "
            "variable and restart the service. The documentation covers this topic "
            "in the deployment section."
        )
        text_with_questions = (
            "Are you asking about the configuration? Did you check the logs? "
            "Have you tried restarting? What version are you running?"
        )
        assert assess_confidence(text_with_questions) < assess_confidence(text_no_questions)

    def test_assess_confidence_error_signals(self):
        """Responses with error signals like 'I apologize' are penalized."""
        text_clean = (
            "The solution involves updating the configuration file to include the "
            "new provider endpoint. Set the API key and restart the service."
        )
        text_error = (
            "I apologize, but I'm unable to provide a definitive answer. "
            "The documentation is unclear on this specific point."
        )
        assert assess_confidence(text_error) < assess_confidence(text_clean)


class TestParseVerification:
    def test_parse_verification_correct(self):
        """Parse a correct verdict with high confidence and no issues."""
        text = (
            "VERDICT: correct\n"
            "CONFIDENCE: 9\n"
            "ISSUES: none\n"
            "CORRECTION: none"
        )
        result = _parse_verification(text, "openai")
        assert result.verdict == "correct"
        assert result.confidence == pytest.approx(0.9)
        assert result.issues == []
        assert result.correction is None
        assert result.verifier_provider == "openai"

    def test_parse_verification_incorrect(self):
        """Parse an incorrect verdict with issues listed."""
        text = (
            "VERDICT: incorrect\n"
            "CONFIDENCE: 3\n"
            "ISSUES: factual error, missing context\n"
            "CORRECTION: The correct answer is X"
        )
        result = _parse_verification(text, "groq")
        assert result.verdict == "incorrect"
        assert result.confidence == pytest.approx(0.3)
        assert result.issues == ["factual error", "missing context"]
        assert result.correction == "The correct answer is X"
        assert result.verifier_provider == "groq"

    def test_parse_verification_partial(self):
        """Parse a partially_correct verdict with minimal fields."""
        text = (
            "VERDICT: partially_correct\n"
            "CONFIDENCE: 6"
        )
        result = _parse_verification(text, "google")
        assert result.verdict == "partially_correct"
        assert result.confidence == pytest.approx(0.6)
        assert result.issues == []
        assert result.correction is None
        assert result.verifier_provider == "google"
