"""Tests for quality_benchmark, workflows, and environment modules."""
from __future__ import annotations

from decimal import Decimal
from unittest.mock import MagicMock, patch

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
