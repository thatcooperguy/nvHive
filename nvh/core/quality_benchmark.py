"""Quality benchmark suite — prove council consensus beats single models.

Runs curated prompts through different modes (single model, free council,
premium council, throwdown) and scores responses using a blind LLM judge
on multiple quality dimensions.

Usage:
    nvh benchmark                          # run all modes
    nvh benchmark --mode council-free      # free council only ($0)
    nvh benchmark --export results.md      # export markdown report
"""

from __future__ import annotations

import logging
import re
import time
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------
# Enums
# ------------------------------------------------------------------


class QualityDimension(StrEnum):
    ACCURACY = "accuracy"
    COMPLETENESS = "completeness"
    ACTIONABILITY = "actionability"
    CORRECTNESS = "correctness"
    COHERENCE = "coherence"
    INSTRUCTION_FOLLOWING = "instruction_following"


class BenchmarkMode(StrEnum):
    SINGLE = "single"
    COUNCIL_FREE = "council_free"
    COUNCIL_PREMIUM = "council_premium"
    THROWDOWN = "throwdown"


# Weights for overall score calculation
_WEIGHTS_WITH_REFERENCE: dict[QualityDimension, float] = {
    QualityDimension.ACCURACY: 0.25,
    QualityDimension.COMPLETENESS: 0.20,
    QualityDimension.ACTIONABILITY: 0.15,
    QualityDimension.CORRECTNESS: 0.20,
    QualityDimension.COHERENCE: 0.10,
    QualityDimension.INSTRUCTION_FOLLOWING: 0.10,
}

_WEIGHTS_NO_REFERENCE: dict[QualityDimension, float] = {
    QualityDimension.ACCURACY: 0.35,
    QualityDimension.COMPLETENESS: 0.25,
    QualityDimension.ACTIONABILITY: 0.15,
    QualityDimension.COHERENCE: 0.15,
    QualityDimension.INSTRUCTION_FOLLOWING: 0.10,
}


# Default provider groups
FREE_COUNCIL = ["groq", "github", "llm7"]
PREMIUM_COUNCIL = ["anthropic", "openai", "google"]


# ------------------------------------------------------------------
# Data models
# ------------------------------------------------------------------


class BenchmarkPrompt(BaseModel):
    """A single prompt in the benchmark dataset."""
    id: str
    task_type: str
    prompt: str
    system_prompt: str = ""
    criteria: list[str] = Field(default_factory=list)
    reference_answer: str = ""
    difficulty: str = "medium"
    tags: list[str] = Field(default_factory=list)


class DimensionScore(BaseModel):
    """Score for a single quality dimension."""
    dimension: QualityDimension
    score: float = Field(ge=1.0, le=10.0)
    reasoning: str = ""


class ResponseEvaluation(BaseModel):
    """Evaluation of a single response to a benchmark prompt."""
    prompt_id: str
    provider: str
    model: str
    mode: BenchmarkMode
    response_text: str
    dimension_scores: list[DimensionScore] = Field(
        default_factory=list,
    )
    overall_score: float = 0.0
    cost_usd: Decimal = Decimal("0")
    latency_ms: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    judge_provider: str = ""
    timestamp: str = ""


class PromptResult(BaseModel):
    """All evaluations for a single prompt across modes."""
    prompt: BenchmarkPrompt
    evaluations: list[ResponseEvaluation] = Field(
        default_factory=list,
    )


class QualityBenchmarkReport(BaseModel):
    """Complete benchmark run results."""
    run_id: str
    timestamp: str
    dataset_name: str
    total_prompts: int
    modes_tested: list[BenchmarkMode]
    results: list[PromptResult] = Field(default_factory=list)
    summary: dict[str, dict[str, float]] = Field(
        default_factory=dict,
    )
    total_cost_usd: Decimal = Decimal("0")
    total_duration_ms: int = 0
    judge_config: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


# ------------------------------------------------------------------
# Dataset loading
# ------------------------------------------------------------------


def load_dataset(path: Path | None = None) -> list[BenchmarkPrompt]:
    """Load benchmark prompts from YAML file or built-in defaults."""
    if path and path.is_file():
        with open(path) as f:
            data = yaml.safe_load(f) or {}
        raw_prompts = data.get("prompts", [])
        return [BenchmarkPrompt(**p) for p in raw_prompts]
    return _builtin_prompts()


def _builtin_prompts() -> list[BenchmarkPrompt]:
    """Return the built-in benchmark dataset."""
    pkg_dir = Path(__file__).parent.parent / "config"
    yaml_path = pkg_dir / "quality_benchmarks.yaml"
    if yaml_path.is_file():
        return load_dataset(yaml_path)

    # Minimal fallback if YAML is missing
    return [
        BenchmarkPrompt(
            id="code_gen_001",
            task_type="code_generation",
            prompt=(
                "Write a Python function that finds the longest"
                " palindromic substring in a string. Include type"
                " hints, docstring, and handle edge cases."
            ),
            criteria=[
                "Correct algorithm",
                "Handles empty string and single character",
                "Includes type hints and docstring",
                "Clean, readable code",
            ],
            difficulty="medium",
            tags=["algorithms", "python"],
        ),
        BenchmarkPrompt(
            id="reasoning_001",
            task_type="reasoning",
            prompt=(
                "A startup has 10 engineers and needs to ship a"
                " product in 3 months. They can either build a"
                " monolith or start with microservices. Analyze the"
                " tradeoffs and give a clear recommendation with"
                " reasoning."
            ),
            criteria=[
                "Considers team size and timeline",
                "Addresses operational complexity",
                "Gives a clear recommendation (not just 'it depends')",
                "Acknowledges valid counterarguments",
            ],
            difficulty="medium",
            tags=["architecture", "engineering"],
        ),
        BenchmarkPrompt(
            id="math_001",
            task_type="math",
            prompt=(
                "A bag contains 5 red balls and 3 blue balls. You"
                " draw 3 balls without replacement. What is the"
                " probability that exactly 2 are red? Show your work."
            ),
            criteria=[
                "Correct probability calculation",
                "Shows clear step-by-step work",
                "Uses correct combinatorial formula",
            ],
            reference_answer="15/28 ≈ 0.5357",
            difficulty="medium",
            tags=["probability", "combinatorics"],
        ),
    ]


# ------------------------------------------------------------------
# Quality Judge
# ------------------------------------------------------------------


_JUDGE_PROMPT_TEMPLATE = """You are an expert response quality judge. \
Score the following AI response on multiple quality dimensions.

## Original Question
{prompt}

## Quality Criteria (what a good answer should include)
{criteria}

{reference_section}

## Response to Evaluate
{response}

## Instructions
Score each dimension from 1 (terrible) to 10 (excellent). \
Be strict — a 7 is good, an 8 is very good, a 9 is exceptional.

Respond in EXACTLY this format (one line per dimension):
accuracy: <1-10>
accuracy_reason: <brief explanation>
completeness: <1-10>
completeness_reason: <brief explanation>
actionability: <1-10>
actionability_reason: <brief explanation>
coherence: <1-10>
coherence_reason: <brief explanation>
instruction_following: <1-10>
instruction_following_reason: <brief explanation>{correctness_lines}"""

_CORRECTNESS_LINES = """
correctness: <1-10>
correctness_reason: <brief explanation comparing to reference>"""


class QualityJudge:
    """Scores benchmark responses using a blind LLM judge."""

    def __init__(
        self,
        engine: Any,
        judge_provider: str = "auto",
    ):
        self.engine = engine
        self._judge_provider = judge_provider
        self._resolved_provider: str | None = None

    def _resolve_provider(
        self,
        exclude: set[str] | None = None,
    ) -> str:
        """Pick the best available judge, avoiding tested providers."""
        if self._resolved_provider:
            return self._resolved_provider

        if self._judge_provider not in ("auto", "local"):
            self._resolved_provider = self._judge_provider
            return self._resolved_provider

        if self._judge_provider == "local":
            self._resolved_provider = "ollama"
            return self._resolved_provider

        # Auto: prefer a strong model not in the exclude set
        exclude = exclude or set()
        preference = [
            "anthropic", "openai", "google",
            "groq", "mistral", "ollama",
        ]
        enabled = set(self.engine.registry.list_enabled())
        for p in preference:
            if p in enabled and p not in exclude:
                self._resolved_provider = p
                return p

        # Fallback: use whatever is available
        available = enabled - exclude
        if available:
            self._resolved_provider = next(iter(available))
            return self._resolved_provider

        # Last resort
        self._resolved_provider = "groq"
        return self._resolved_provider

    async def evaluate(
        self,
        prompt: BenchmarkPrompt,
        response_text: str,
        provider: str,
        model: str,
        mode: BenchmarkMode,
        exclude_providers: set[str] | None = None,
    ) -> ResponseEvaluation:
        """Score a response across all quality dimensions."""
        judge = self._resolve_provider(exclude_providers)
        has_reference = bool(prompt.reference_answer.strip())

        criteria_text = "\n".join(
            f"- {c}" for c in prompt.criteria
        )

        reference_section = ""
        if has_reference:
            reference_section = (
                f"## Reference Answer (ground truth)\n"
                f"{prompt.reference_answer}"
            )

        correctness_lines = _CORRECTNESS_LINES if has_reference else ""

        judge_prompt = _JUDGE_PROMPT_TEMPLATE.format(
            prompt=prompt.prompt,
            criteria=criteria_text,
            reference_section=reference_section,
            response=response_text[:4000],
            correctness_lines=correctness_lines,
        )

        try:
            judge_resp = await self.engine.query(
                prompt=judge_prompt,
                provider=judge,
                temperature=0.0,
                max_tokens=500,
            )
            scores = self._parse_scores(
                judge_resp.content, has_reference,
            )
        except Exception as e:
            logger.warning("Judge evaluation failed: %s", e)
            scores = self._default_scores(has_reference)

        weights = (
            _WEIGHTS_WITH_REFERENCE
            if has_reference
            else _WEIGHTS_NO_REFERENCE
        )
        overall = sum(
            s.score * weights.get(s.dimension, 0.0)
            for s in scores
        )

        # Ground truth check: if reference answer exists,
        # verify it appears in the response. Override the
        # LLM judge's correctness score if it got it wrong.
        if has_reference:
            ref = prompt.reference_answer.strip()
            ref_variants = [ref]
            # Handle "X or Y" format in reference answers
            if " or " in ref:
                ref_variants = [
                    v.strip() for v in ref.split(" or ")
                ]
            found = any(
                v.lower() in response_text.lower()
                for v in ref_variants
            )
            if not found:
                # Override correctness score — LLM judge
                # can't verify factual truth
                for s in scores:
                    if s.dimension == QualityDimension.CORRECTNESS:
                        s.score = 2.0
                        s.reasoning = (
                            f"Ground truth check: reference"
                            f" answer '{ref}' not found in"
                            f" response"
                        )
                # Recalculate overall with corrected score
                overall = sum(
                    s.score * weights.get(s.dimension, 0.0)
                    for s in scores
                )

        return ResponseEvaluation(
            prompt_id=prompt.id,
            provider=provider,
            model=model,
            mode=mode,
            response_text=response_text,
            dimension_scores=scores,
            overall_score=round(overall, 2),
            judge_provider=judge,
            timestamp=datetime.now(UTC).isoformat(),
        )

    def _parse_scores(
        self,
        text: str,
        has_reference: bool,
    ) -> list[DimensionScore]:
        """Parse judge output into dimension scores."""
        scores: list[DimensionScore] = []
        score_map: dict[str, float] = {}
        reason_map: dict[str, str] = {}

        for line in text.strip().splitlines():
            if ":" not in line:
                continue
            key, _, value = line.partition(":")
            key = key.strip().lower()
            value = value.strip()

            if key.endswith("_reason"):
                dim_name = key.removesuffix("_reason")
                reason_map[dim_name] = value
            else:
                try:
                    num = float(re.search(r"[\d.]+", value).group())
                    score_map[key] = max(1.0, min(10.0, num))
                except (AttributeError, ValueError):
                    pass

        dimensions = [
            QualityDimension.ACCURACY,
            QualityDimension.COMPLETENESS,
            QualityDimension.ACTIONABILITY,
            QualityDimension.COHERENCE,
            QualityDimension.INSTRUCTION_FOLLOWING,
        ]
        if has_reference:
            dimensions.append(QualityDimension.CORRECTNESS)

        for dim in dimensions:
            scores.append(DimensionScore(
                dimension=dim,
                score=score_map.get(dim.value, 5.0),
                reasoning=reason_map.get(dim.value, ""),
            ))

        return scores

    def _default_scores(
        self,
        has_reference: bool,
    ) -> list[DimensionScore]:
        """Fallback scores when judge fails."""
        dimensions = [
            QualityDimension.ACCURACY,
            QualityDimension.COMPLETENESS,
            QualityDimension.ACTIONABILITY,
            QualityDimension.COHERENCE,
            QualityDimension.INSTRUCTION_FOLLOWING,
        ]
        if has_reference:
            dimensions.append(QualityDimension.CORRECTNESS)
        return [
            DimensionScore(
                dimension=d, score=5.0, reasoning="Judge unavailable",
            )
            for d in dimensions
        ]


# ------------------------------------------------------------------
# Benchmark Runner
# ------------------------------------------------------------------


class QualityBenchmarkRunner:
    """Runs the quality benchmark suite."""

    def __init__(
        self,
        engine: Any,
        judge: QualityJudge,
        dataset: list[BenchmarkPrompt],
    ):
        self.engine = engine
        self.judge = judge
        self.dataset = dataset

    async def run(
        self,
        modes: list[BenchmarkMode],
        single_providers: list[str] | None = None,
        council_free_members: list[str] | None = None,
        council_premium_members: list[str] | None = None,
        temperature: float = 0.0,
        max_tokens: int = 2048,
        task_types: list[str] | None = None,
        on_progress: Any = None,
        pace_seconds: float = 15.0,
    ) -> QualityBenchmarkReport:
        """Run the benchmark across all modes and prompts."""
        run_id = str(uuid.uuid4())[:8]
        start = time.monotonic()

        free_members = council_free_members or FREE_COUNCIL
        premium_members = council_premium_members or PREMIUM_COUNCIL
        single = single_providers or self._pick_single_baselines()

        # Filter dataset by task type if specified
        prompts = self.dataset
        if task_types:
            prompts = [
                p for p in prompts
                if p.task_type in task_types
            ]

        results: list[PromptResult] = []
        total_cost = Decimal("0")

        import asyncio as _aio

        for i, prompt in enumerate(prompts):
            if on_progress:
                on_progress(i + 1, len(prompts), prompt.id)

            # Pace between prompts to respect free-tier rate limits
            if i > 0 and pace_seconds > 0:
                await _aio.sleep(pace_seconds)

            prompt_result = PromptResult(prompt=prompt)

            for mode in modes:
                try:
                    evaluation = await self._run_mode(
                        prompt=prompt,
                        mode=mode,
                        single_providers=single,
                        free_members=free_members,
                        premium_members=premium_members,
                        temperature=temperature,
                        max_tokens=max_tokens,
                    )
                    for ev in evaluation:
                        total_cost += ev.cost_usd
                        prompt_result.evaluations.append(ev)
                except Exception as e:
                    logger.warning(
                        "Benchmark failed for %s mode=%s: %s",
                        prompt.id, mode, e,
                    )

                # Brief pause between modes for rate limit headroom
                if pace_seconds > 0:
                    await _aio.sleep(pace_seconds / 3)

            results.append(prompt_result)

        elapsed = int((time.monotonic() - start) * 1000)

        report = QualityBenchmarkReport(
            run_id=run_id,
            timestamp=datetime.now(UTC).isoformat(),
            dataset_name="default",
            total_prompts=len(prompts),
            modes_tested=modes,
            results=results,
            total_cost_usd=total_cost,
            total_duration_ms=elapsed,
            judge_config={
                "provider": self.judge._resolved_provider or "auto",
            },
        )

        # Build summary
        report.summary = self._build_summary(report)
        return report

    async def _run_mode(
        self,
        prompt: BenchmarkPrompt,
        mode: BenchmarkMode,
        single_providers: list[str],
        free_members: list[str],
        premium_members: list[str],
        temperature: float,
        max_tokens: int,
    ) -> list[ResponseEvaluation]:
        """Run a single prompt through a specific mode."""
        evaluations: list[ResponseEvaluation] = []

        if mode == BenchmarkMode.SINGLE:
            for prov in single_providers:
                ev = await self._run_single(
                    prompt, prov, temperature, max_tokens,
                )
                if ev:
                    evaluations.append(ev)

        elif mode == BenchmarkMode.COUNCIL_FREE:
            ev = await self._run_council(
                prompt, free_members, mode,
                temperature, max_tokens,
            )
            if ev:
                evaluations.append(ev)

        elif mode == BenchmarkMode.COUNCIL_PREMIUM:
            ev = await self._run_council(
                prompt, premium_members, mode,
                temperature, max_tokens,
            )
            if ev:
                evaluations.append(ev)

        elif mode == BenchmarkMode.THROWDOWN:
            ev = await self._run_throwdown(
                prompt, temperature, max_tokens,
            )
            if ev:
                evaluations.append(ev)

        return evaluations

    async def _run_single(
        self,
        prompt: BenchmarkPrompt,
        provider: str,
        temperature: float,
        max_tokens: int,
    ) -> ResponseEvaluation | None:
        """Run a single model and judge the response."""
        try:
            resp = await self.engine.query(
                prompt=prompt.prompt,
                provider=provider,
                system_prompt=prompt.system_prompt or None,
                temperature=temperature,
                max_tokens=max_tokens,
                use_cache=False,
            )
        except Exception as e:
            logger.warning(
                "Single query failed (%s): %s", provider, e,
            )
            return None

        ev = await self.judge.evaluate(
            prompt=prompt,
            response_text=resp.content,
            provider=resp.provider,
            model=resp.model,
            mode=BenchmarkMode.SINGLE,
            exclude_providers={provider},
        )
        ev.cost_usd = resp.cost_usd
        ev.latency_ms = resp.latency_ms or 0
        ev.input_tokens = resp.usage.input_tokens
        ev.output_tokens = resp.usage.output_tokens
        return ev

    async def _run_council(
        self,
        prompt: BenchmarkPrompt,
        members: list[str],
        mode: BenchmarkMode,
        temperature: float,
        max_tokens: int,
    ) -> ResponseEvaluation | None:
        """Run council mode and judge the synthesis."""
        # Filter to available members
        available = set(self.engine.registry.list_enabled())
        active = [m for m in members if m in available]
        if len(active) < 2:
            logger.warning(
                "Not enough council members available "
                "(%s of %s)", len(active), len(members),
            )
            return None

        try:
            result = await self.engine.run_council(
                prompt=prompt.prompt,
                members=active,
                system_prompt=prompt.system_prompt or None,
                temperature=temperature,
                max_tokens=max_tokens,
                synthesize=True,
                auto_agents=True,
            )
        except Exception as e:
            logger.warning("Council failed: %s", e)
            return None

        if not result.synthesis:
            logger.warning("Council produced no synthesis")
            return None

        content = result.synthesis.content
        ev = await self.judge.evaluate(
            prompt=prompt,
            response_text=content,
            provider="council",
            model=f"council({','.join(active)})",
            mode=mode,
            exclude_providers=set(active),
        )
        ev.cost_usd = result.total_cost_usd
        ev.latency_ms = result.total_latency_ms
        total_in = sum(
            r.usage.input_tokens
            for r in result.member_responses.values()
        )
        total_out = sum(
            r.usage.output_tokens
            for r in result.member_responses.values()
        )
        ev.input_tokens = total_in
        ev.output_tokens = total_out
        return ev

    async def _run_throwdown(
        self,
        prompt: BenchmarkPrompt,
        temperature: float,
        max_tokens: int,
    ) -> ResponseEvaluation | None:
        """Run throwdown (two-pass deep analysis) and judge."""
        try:
            # Pass 1: initial council
            pass1 = await self.engine.run_council(
                prompt=prompt.prompt,
                auto_agents=True,
                synthesize=True,
                temperature=temperature,
                max_tokens=max_tokens,
            )
            if not pass1.synthesis:
                return None

            # Pass 2: critique
            critique_prompt = (
                f"Original question: {prompt.prompt}\n\n"
                f"A council of AI experts produced this analysis:"
                f"\n\n{pass1.synthesis.content}\n\n"
                f"Critique this analysis. What did the experts"
                f" miss? What assumptions are wrong? Provide a"
                f" refined, improved answer."
            )
            pass2 = await self.engine.run_council(
                prompt=critique_prompt,
                auto_agents=True,
                synthesize=True,
                temperature=temperature,
                max_tokens=max_tokens,
            )

            # Final synthesis
            final_prompt = (
                f"Original question: {prompt.prompt}\n\n"
                f"Pass 1 analysis:\n"
                f"{pass1.synthesis.content}\n\n"
                f"Pass 2 critique:\n"
                f"{pass2.synthesis.content if pass2.synthesis else ''}"
                f"\n\nProduce a definitive final answer integrating"
                f" the best insights from both passes."
            )
            final = await self.engine.query(
                prompt=final_prompt,
                temperature=temperature,
                max_tokens=max_tokens,
                use_cache=False,
            )
        except Exception as e:
            logger.warning("Throwdown failed: %s", e)
            return None

        total_cost = (
            pass1.total_cost_usd
            + (pass2.total_cost_usd if pass2 else Decimal("0"))
            + (final.cost_usd if final else Decimal("0"))
        )

        ev = await self.judge.evaluate(
            prompt=prompt,
            response_text=final.content,
            provider="throwdown",
            model="throwdown",
            mode=BenchmarkMode.THROWDOWN,
        )
        ev.cost_usd = total_cost
        ev.latency_ms = (
            pass1.total_latency_ms
            + (pass2.total_latency_ms if pass2 else 0)
            + (final.latency_ms or 0)
        )
        return ev

    def _pick_single_baselines(self) -> list[str]:
        """Pick the best available single models for baseline."""
        preference = [
            "anthropic", "openai", "google", "groq",
        ]
        enabled = set(self.engine.registry.list_enabled())
        return [p for p in preference if p in enabled][:2]

    def _build_summary(
        self,
        report: QualityBenchmarkReport,
    ) -> dict[str, dict[str, float]]:
        """Build average scores per mode per dimension."""
        from collections import defaultdict

        mode_scores: dict[str, list[ResponseEvaluation]] = (
            defaultdict(list)
        )
        for pr in report.results:
            for ev in pr.evaluations:
                mode_scores[ev.mode].append(ev)

        summary: dict[str, dict[str, float]] = {}
        for mode, evals in mode_scores.items():
            dim_totals: dict[str, list[float]] = defaultdict(list)
            overall_totals: list[float] = []
            cost_totals: list[Decimal] = []

            for ev in evals:
                overall_totals.append(ev.overall_score)
                cost_totals.append(ev.cost_usd)
                for ds in ev.dimension_scores:
                    dim_totals[ds.dimension].append(ds.score)

            avg: dict[str, float] = {}
            for dim, scores in dim_totals.items():
                avg[dim] = round(sum(scores) / len(scores), 2)
            avg["overall"] = round(
                sum(overall_totals) / len(overall_totals), 2,
            )
            avg["avg_cost"] = round(
                float(sum(cost_totals) / len(cost_totals)), 6,
            )
            summary[mode] = avg

        return summary


# ------------------------------------------------------------------
# Report generation
# ------------------------------------------------------------------


_MODE_DISPLAY = {
    "single": "Single Model",
    "council_free": "Council (Free)",
    "council_premium": "Council (Premium)",
    "throwdown": "Throwdown",
}


def generate_markdown_report(
    report: QualityBenchmarkReport,
) -> str:
    """Generate a publishable markdown report."""
    lines: list[str] = []
    lines.append("# nvHive Quality Benchmark Results\n")
    lines.append(
        f"**Date:** {report.timestamp[:10]} | "
        f"**Prompts:** {report.total_prompts} | "
        f"**Total Cost:** ${report.total_cost_usd:.4f} | "
        f"**Duration:** {report.total_duration_ms / 1000:.1f}s"
    )
    judge = report.judge_config.get("provider", "auto")
    lines.append(f"**Judge:** {judge}\n")

    # Summary table
    lines.append("## Summary\n")
    dims = [
        "accuracy", "completeness", "actionability",
        "coherence", "overall",
    ]
    header = "| Mode | " + " | ".join(
        d.replace("_", " ").title() for d in dims
    ) + " | Avg Cost |"
    sep = "|------|" + "|".join(
        "--------:" for _ in dims
    ) + "|----------:|"
    lines.append(header)
    lines.append(sep)

    for mode, scores in report.summary.items():
        display = _MODE_DISPLAY.get(mode, mode)
        vals = " | ".join(
            f"{scores.get(d, 0.0):.1f}" for d in dims
        )
        cost = scores.get("avg_cost", 0.0)
        lines.append(f"| {display} | {vals} | ${cost:.4f} |")

    lines.append("")

    # Per task type
    lines.append("## By Task Type\n")
    task_groups: dict[str, list[PromptResult]] = {}
    for pr in report.results:
        tt = pr.prompt.task_type
        task_groups.setdefault(tt, []).append(pr)

    for task_type, prompt_results in task_groups.items():
        lines.append(f"### {task_type.replace('_', ' ').title()}\n")
        for pr in prompt_results:
            lines.append(f"**{pr.prompt.id}**: {pr.prompt.prompt[:80]}...\n")
            for ev in pr.evaluations:
                display = _MODE_DISPLAY.get(ev.mode, ev.mode)
                lines.append(
                    f"- {display}: **{ev.overall_score:.1f}**/10"
                    f" (${ev.cost_usd:.4f}, {ev.latency_ms}ms)"
                )
            lines.append("")

    # Footer
    lines.append("---")
    lines.append(
        "*Generated by [nvHive](https://pypi.org/project/nvhive/)"
        " quality benchmark suite.*"
    )

    return "\n".join(lines)


def generate_json_report(
    report: QualityBenchmarkReport,
) -> str:
    """Generate JSON export of full results."""
    return report.model_dump_json(indent=2)
