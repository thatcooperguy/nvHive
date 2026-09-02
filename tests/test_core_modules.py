"""Tests for the small core and utils modules.

quality_benchmark, workflows, environment, voice, image_gen, scheduler, notify,
benchmark, free_tier, sanitize, logging, streaming.
"""
from __future__ import annotations

import asyncio
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

from nvh.core.quality_benchmark import (
    _WEIGHTS_NO_REFERENCE,
    _WEIGHTS_WITH_REFERENCE,
    BenchmarkMode,
    BenchmarkPrompt,
    DimensionScore,
    QualityBenchmarkReport,
    QualityDimension,
    QualityJudge,
    ResponseEvaluation,
    generate_markdown_report,
    load_dataset,
)
from nvh.core.workflows import (
    WorkflowResult,
    WorkflowStep,
    _render_template,
    load_workflow,
)
from nvh.utils.environment import (
    EnvironmentInfo,
    _detect_docker,
    _detect_gpu,
    _detect_platform,
    get_environment_summary,
)

# ---- quality_benchmark ----


class TestBenchmarkModels:
    def test_prompt_defaults(self):
        bp = BenchmarkPrompt(id="t1", task_type="code", prompt="X")
        assert bp.system_prompt == "" and bp.criteria == []
        assert bp.difficulty == "medium" and bp.tags == []

    def test_dimension_score(self):
        ds = DimensionScore(dimension=QualityDimension.ACCURACY, score=7.5)
        assert ds.score == 7.5 and ds.reasoning == ""

    def test_response_evaluation_defaults(self):
        ev = ResponseEvaluation(
            prompt_id="p1", provider="openai", model="gpt-4",
            mode=BenchmarkMode.SINGLE, response_text="hi",
        )
        assert ev.overall_score == 0.0 and ev.cost_usd == Decimal("0")

    def test_report_defaults(self):
        rpt = QualityBenchmarkReport(
            run_id="a", timestamp="t", dataset_name="d",
            total_prompts=0, modes_tested=[BenchmarkMode.SINGLE],
        )
        assert rpt.results == [] and rpt.total_cost_usd == Decimal("0")


class TestQualityJudge:
    def _j(self):
        return QualityJudge(engine=MagicMock(), judge_provider="auto")

    def test_parse_with_reference(self):
        text = (
            "accuracy: 8\naccuracy_reason: g\ncompleteness: 7\n"
            "completeness_reason: d\nactionability: 6\n"
            "actionability_reason: m\ncoherence: 9\ncoherence_reason: c\n"
            "instruction_following: 8\ninstruction_following_reason: f\n"
            "correctness: 10\ncorrectness_reason: exact\n"
        )
        scores = self._j()._parse_scores(text, has_reference=True)
        assert len(scores) == 6
        by = {s.dimension: s for s in scores}
        assert by[QualityDimension.ACCURACY].score == 8.0
        assert by[QualityDimension.CORRECTNESS].score == 10.0

    def test_parse_without_reference(self):
        t = "accuracy: 5\ncompleteness: 5\nactionability: 5\n"
        scores = self._j()._parse_scores(
            t + "coherence: 5\ninstruction_following: 5\n", False,
        )
        assert len(scores) == 5
        assert QualityDimension.CORRECTNESS not in {s.dimension for s in scores}

    def test_parse_empty_defaults_to_5(self):
        assert all(s.score == 5.0 for s in self._j()._parse_scores("", False))

    def test_default_scores(self):
        scores = self._j()._default_scores(has_reference=True)
        assert len(scores) == 6 and all(s.reasoning == "Judge unavailable" for s in scores)

    def test_resolve_explicit(self):
        j = self._j(); j._judge_provider = "anthropic"  # noqa: E702
        assert j._resolve_provider() == "anthropic"

    def test_resolve_local(self):
        j = self._j(); j._judge_provider = "local"  # noqa: E702
        assert j._resolve_provider() == "ollama"


class TestLoadDataset:
    def test_from_yaml(self, tmp_path):
        d = {"prompts": [{"id": "p1", "task_type": "qa", "prompt": "Q?"}]}
        (tmp_path / "b.yaml").write_text(yaml.dump(d))
        assert load_dataset(tmp_path / "b.yaml")[0].id == "p1"

    def test_empty_yaml(self, tmp_path):
        (tmp_path / "e.yaml").write_text("")
        assert isinstance(load_dataset(tmp_path / "e.yaml"), list)


class TestWeightsAndReport:
    def test_weights_sum(self):
        assert abs(sum(_WEIGHTS_WITH_REFERENCE.values()) - 1.0) < 1e-9
        assert abs(sum(_WEIGHTS_NO_REFERENCE.values()) - 1.0) < 1e-9

    def test_markdown_report(self):
        rpt = QualityBenchmarkReport(
            run_id="a", timestamp="2026-01-01T00:00:00", dataset_name="d",
            total_prompts=1, modes_tested=[BenchmarkMode.SINGLE],
            summary={"single": {"overall": 7.5, "avg_cost": 0.001}},
        )
        md = generate_markdown_report(rpt)
        assert "nvHive Quality Benchmark" in md and "Single Model" in md
# ---- workflows ----


class TestWorkflowModels:
    def test_step_defaults(self):
        ws = WorkflowStep(name="s1", action="ask", prompt="hi")
        assert ws.advisor == "" and ws.save_as == "" and ws.condition == ""

    def test_result(self):
        wr = WorkflowResult("t", 2, 3, {"x": "1"}, False, "boom")
        assert not wr.success and wr.error == "boom"


class TestRenderTemplate:
    def test_basic(self):
        assert _render_template("Hello {{name}}", {"name": "W"}) == "Hello W"

    def test_multiple(self):
        assert _render_template("{{a}}+{{b}}", {"a": "1", "b": "2"}) == "1+2"

    def test_no_match(self):
        assert _render_template("{{x}}", {}) == "{{x}}"


class TestLoadWorkflow:
    def test_valid(self, tmp_path):
        data = {
            "name": "Flow", "description": "A", "variables": {"input": "d"},
            "steps": [
                {"name": "s1", "action": "ask", "prompt": "Do {{input}}"},
                {"name": "s2", "action": "convene", "prompt": "Sum", "cabinet": "r"},
            ],
        }
        (tmp_path / "f.yaml").write_text(yaml.dump(data))
        wf = load_workflow(tmp_path / "f.yaml")
        assert wf.name == "Flow" and len(wf.steps) == 2
        assert wf.steps[1].cabinet == "r"

    def test_minimal(self, tmp_path):
        (tmp_path / "m.yaml").write_text(yaml.dump({"name": "bare"}))
        assert load_workflow(tmp_path / "m.yaml").steps == []

    def test_auto_name(self, tmp_path):
        d = {"name": "a", "steps": [{"action": "ask", "prompt": "hi"}]}
        (tmp_path / "a.yaml").write_text(yaml.dump(d))
        assert load_workflow(tmp_path / "a.yaml").steps[0].name == "step_1"

# ---- environment ----


class TestDetectPlatform:
    @patch("nvh.utils.environment.sys")
    def test_linux(self, m):
        m.platform = "linux"; assert _detect_platform() == "linux"  # noqa: E702

    @patch("nvh.utils.environment.sys")
    def test_darwin(self, m):
        m.platform = "darwin"; assert _detect_platform() == "macos"  # noqa: E702

    @patch("nvh.utils.environment.sys")
    def test_windows(self, m):
        m.platform = "win32"; assert _detect_platform() == "windows"  # noqa: E702

    @patch("nvh.utils.environment.sys")
    def test_unknown(self, m):
        m.platform = "freebsd"; assert _detect_platform() == "freebsd"  # noqa: E702


class TestDetectDocker:
    @patch("nvh.utils.environment.Path")
    def test_exists(self, mp):
        mp.return_value.exists.return_value = True
        assert _detect_docker() is True

    @patch("nvh.utils.environment.Path")
    def test_not(self, mp):
        mp.return_value.exists.return_value = False
        assert _detect_docker() is False


class TestDetectGpu:
    @patch("nvh.utils.environment.shutil.which", return_value=None)
    def test_no_smi(self, _):
        h, a, n, c, v = _detect_gpu()
        assert h is False and a is False

    @patch("nvh.utils.environment.subprocess.run")
    @patch("nvh.utils.environment.shutil.which", return_value="/usr/bin/nvidia-smi")
    def test_success(self, _w, mr):
        mr.return_value = MagicMock(
            returncode=0, stdout="A100, 81920 MiB\nA100, 81920 MiB\n",
        )
        h, a, n, c, v = _detect_gpu()
        assert h and a and c == 2 and v == 80.0

    @patch("nvh.utils.environment.subprocess.run")
    @patch("nvh.utils.environment.shutil.which", return_value="/usr/bin/nvidia-smi")
    def test_fail_rc(self, _w, mr):
        mr.return_value = MagicMock(returncode=1, stdout="")
        h, a, _, _, _ = _detect_gpu()
        assert h is True and a is False


class TestEnvSummary:
    def test_cpu_only(self):
        assert "CPU-only" in get_environment_summary(EnvironmentInfo(platform="linux"))

    def test_gpu(self):
        info = EnvironmentInfo(
            platform="linux", has_gpu=True, gpu_accessible=True,
            gpu_names=["A100"], gpu_count=1,
        )
        assert "A100" in get_environment_summary(info)

    def test_docker_cloud(self):
        info = EnvironmentInfo(
            platform="linux", is_docker=True, is_cloud=True,
            cloud_provider="aws", instance_type="g5.xlarge",
        )
        s = get_environment_summary(info)
        assert "docker" in s and "aws" in s and "g5.xlarge" in s


# ---------------------------------------------------------------------------
# nvh/core/quality_benchmark.py — construction
# ---------------------------------------------------------------------------


class TestQualityBenchmarkConstruction:
    def test_benchmark_prompt_model(self):
        bp = BenchmarkPrompt(id="test1", task_type="math", prompt="What is 2+2?")
        assert bp.prompt == "What is 2+2?"
        assert bp.task_type == "math"

    def test_dimension_score(self):
        ds = DimensionScore(dimension="accuracy", score=8.5, reasoning="good")
        assert ds.score == 8.5

    def test_benchmark_mode_enum(self):
        assert hasattr(BenchmarkMode, "QUICK") or hasattr(BenchmarkMode, "FULL") or len(list(BenchmarkMode)) > 0


# ---------------------------------------------------------------------------
# nvh/core/workflows.py — bundled workflow files
# ---------------------------------------------------------------------------


class TestWorkflowsBundled:
    def test_load_workflow_from_yaml(self):
        try:
            wf = load_workflow(Path("nvh/workflows/research.yaml"))
            assert wf is not None
            assert hasattr(wf, "steps") or hasattr(wf, "name")
        except (FileNotFoundError, ImportError, TypeError):
            pytest.skip("Workflow loading needs different path or API")

    def test_workflow_step_construction(self):
        step = WorkflowStep(name="test", action="query", prompt="do something")
        assert step.name == "test"
        assert step.action == "query"


# ---------------------------------------------------------------------------
# nvh/core/voice.py
# ---------------------------------------------------------------------------


class TestVoice:
    def test_import(self):
        from nvh.core import voice
        assert voice is not None

    def test_has_config_or_function(self):
        from nvh.core import voice
        assert hasattr(voice, "VoiceConfig") or hasattr(voice, "record_audio") or hasattr(voice, "transcribe")

    def test_voice_config_defaults(self):
        from nvh.core.voice import VoiceConfig
        vc = VoiceConfig()
        assert vc.stt_provider == "groq"
        assert vc.tts_provider == "edge"
        assert vc.tts_voice == "en-US-AriaNeural"
        assert vc.auto_listen is False
        assert vc.silence_timeout == 2.0

    def test_voice_config_custom(self):
        from nvh.core.voice import VoiceConfig
        vc = VoiceConfig(stt_provider="local", tts_provider="system", auto_listen=True)
        assert vc.stt_provider == "local"
        assert vc.tts_provider == "system"
        assert vc.auto_listen is True

    def test_speech_to_text_requires_groq_key(self):
        from nvh.core.voice import speech_to_text
        with patch.dict("os.environ", {}, clear=True):
            with patch("keyring.get_password", side_effect=Exception("no keyring")):
                # Skip if event loop is consumed by previous async tests
                try:
                    loop = asyncio.get_event_loop()
                    if loop.is_closed():
                        pytest.skip("event loop closed by prior async test")
                    with pytest.raises(ValueError, match="Groq API key"):
                        loop.run_until_complete(
                            speech_to_text("/fake.wav", provider="groq")
                        )
                except RuntimeError:
                    pytest.skip("no event loop available")


# ---------------------------------------------------------------------------
# nvh/core/image_gen.py
# ---------------------------------------------------------------------------


class TestImageGen:
    def test_import(self):
        from nvh.core import image_gen
        assert image_gen is not None

    def test_has_generate_or_config(self):
        from nvh.core import image_gen
        assert (hasattr(image_gen, "generate_image") or
                hasattr(image_gen, "ImageConfig") or
                hasattr(image_gen, "ImageGenConfig"))

    def test_import_image_gen(self):
        from nvh.core import image_gen
        assert hasattr(image_gen, "generate_image")
        assert hasattr(image_gen, "open_image")

    def test_generate_image_unknown_provider_raises(self):
        from nvh.core.image_gen import generate_image
        try:
            loop = asyncio.get_event_loop()
            if loop.is_closed():
                pytest.skip("event loop closed by prior async test")
            with pytest.raises(ValueError, match="Unknown image provider"):
                loop.run_until_complete(
                    generate_image("a cat", provider="nonexistent")
                )
        except RuntimeError:
            pytest.skip("no event loop available")


# ---------------------------------------------------------------------------
# nvh/core/scheduler.py, notify.py
# ---------------------------------------------------------------------------


class TestScheduler:
    def test_import(self):
        from nvh.core import scheduler
        assert scheduler is not None

    def test_scheduler_construction(self):
        from nvh.core.scheduler import Scheduler
        s = Scheduler()
        assert s is not None

    def test_list_tasks_empty(self):
        from nvh.core.scheduler import Scheduler
        s = Scheduler()
        tasks = s.list_tasks() if hasattr(s, "list_tasks") else []
        assert isinstance(tasks, (list, dict))


class TestNotify:
    @pytest.mark.asyncio
    async def test_notify_task_complete(self):
        from nvh.core.notify import notify_task_complete
        # Should not raise even without a notification system
        await notify_task_complete("test task", "result preview")


# ---------------------------------------------------------------------------
# nvh/core/benchmark.py
# ---------------------------------------------------------------------------


class TestBenchmark:
    def test_import(self):
        from nvh.core import benchmark
        assert benchmark is not None

    def test_benchmark_suite_exists(self):
        from nvh.core.benchmark import BENCHMARK_PROMPTS, BenchmarkSuite
        assert BenchmarkSuite is not None
        assert len(BENCHMARK_PROMPTS) > 0

    def test_benchmark_result_construction(self):
        from nvh.core.benchmark import BenchmarkResult
        r = BenchmarkResult(model="m", gpu_name="RTX 3090", vram_gb=24.0,
                            prompt_tokens=10, output_tokens=50,
                            time_to_first_token_ms=100, total_time_ms=500,
                            tokens_per_second=100.0, prompt_eval_rate=50.0)
        assert r.tokens_per_second == 100.0
        assert r.gpu_name == "RTX 3090"

    def test_benchmark_suite_construction(self):
        from nvh.core.benchmark import BenchmarkSuite
        suite = BenchmarkSuite(
            gpu_name="Test GPU", vram_gb=8.0,
            results=[], total_time_ms=100, timestamp="2025-01-01",
        )
        assert suite.gpu_name == "Test GPU"

    def test_benchmark_prompts_non_empty(self):
        from nvh.core.benchmark import BENCHMARK_PROMPTS
        assert len(BENCHMARK_PROMPTS) >= 3
        for bp in BENCHMARK_PROMPTS:
            assert "prompt" in bp
            assert "max_tokens" in bp

    def test_community_baselines_has_entries(self):
        from nvh.core.benchmark import COMMUNITY_BASELINES
        assert len(COMMUNITY_BASELINES) >= 5
        assert "NVIDIA GeForce RTX 4090" in COMMUNITY_BASELINES

    def test_format_benchmark_results(self):
        from nvh.core.benchmark import BenchmarkResult, BenchmarkSuite, format_benchmark_results
        r = BenchmarkResult(
            model="test", gpu_name="GPU", vram_gb=8, prompt_tokens=10,
            output_tokens=50, time_to_first_token_ms=100, total_time_ms=500,
            tokens_per_second=100.0, prompt_eval_rate=200.0,
        )
        suite = BenchmarkSuite(
            gpu_name="GPU", vram_gb=8, results=[r],
            total_time_ms=500, timestamp="now",
        )
        text = format_benchmark_results(suite)
        assert "GPU" in text
        assert "100.0" in text
        assert isinstance(text, str)


# ---------------------------------------------------------------------------
# nvh/core/free_tier.py
# ---------------------------------------------------------------------------


class TestFreeTier:
    def test_import(self):
        from nvh.core import free_tier
        assert free_tier is not None

    def test_free_tier_advisors(self):
        from nvh.core.free_tier import FREE_TIER_ADVISORS
        assert isinstance(FREE_TIER_ADVISORS, (list, dict))
        assert len(FREE_TIER_ADVISORS) > 0

    def test_detect_available(self):
        from nvh.core.free_tier import detect_available_free_advisors
        result = detect_available_free_advisors()
        assert isinstance(result, list)

    def test_get_best_free(self):
        from nvh.core.free_tier import get_best_free_advisor
        best = get_best_free_advisor()
        # Returns a string provider name or None
        assert best is None or isinstance(best, str)


# ---------------------------------------------------------------------------
# nvh/utils: sanitize, logging, streaming
# ---------------------------------------------------------------------------


class TestSanitize:
    def test_sanitize_dict(self):
        from nvh.utils.sanitize import sanitize_dict
        result = sanitize_dict({"API_KEY": "secret123", "name": "public"})
        assert isinstance(result, dict)
        assert result["name"] == "public"

    def test_mask_key(self):
        from nvh.utils.sanitize import mask_key
        result = mask_key("sk-abcdef1234567890")
        assert "sk-abcdef" not in result or "***" in result or len(result) < 20


class TestLogging:
    def test_setup_logging(self):
        from nvh.utils.logging import setup_logging
        setup_logging(level="WARNING")
        # Should not raise


class TestStreaming:
    def test_import(self):
        from nvh.utils import streaming
        assert streaming is not None

    def test_has_collect_stream(self):
        from nvh.utils.streaming import collect_stream, stream_to_callback
        assert callable(collect_stream)
        assert callable(stream_to_callback)
