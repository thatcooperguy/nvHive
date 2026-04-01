"""Tests for the routing engine and task classifier."""


from nvh.core.router import classify_task
from nvh.providers.base import TaskType


class TestTaskClassifier:
    def test_code_generation(self):
        result = classify_task("Write a Python function to sort a list")
        assert result.task_type == TaskType.CODE_GENERATION
        assert result.confidence > 0

    def test_code_debug(self):
        result = classify_task("I'm getting a TypeError exception, can you fix this bug?")
        assert result.task_type == TaskType.CODE_DEBUG

    def test_math(self):
        result = classify_task("Solve this equation: 2x + 5 = 15")
        assert result.task_type == TaskType.MATH

    def test_creative_writing(self):
        result = classify_task("Write a short story about a dragon")
        assert result.task_type == TaskType.CREATIVE_WRITING

    def test_summarization(self):
        result = classify_task("Summarize this article for me")
        assert result.task_type == TaskType.SUMMARIZATION

    def test_translation(self):
        result = classify_task("Translate this paragraph to Spanish")
        assert result.task_type == TaskType.TRANSLATION

    def test_question(self):
        result = classify_task("What is the capital of France?")
        assert result.task_type == TaskType.QUESTION_ANSWERING

    def test_conversation(self):
        result = classify_task("Hello, how are you?")
        assert result.task_type == TaskType.CONVERSATION

    def test_multimodal(self):
        result = classify_task("Look at this image and tell me what's in the photo")
        assert result.task_type == TaskType.MULTIMODAL

    def test_returns_all_scores(self):
        result = classify_task("Write and debug a Python sort function")
        assert len(result.all_scores) > 0
        assert all(0 <= s <= 1 for s in result.all_scores.values())

    def test_fallback_for_ambiguous(self):
        result = classify_task("hmm")
        assert result.confidence < 1.0
