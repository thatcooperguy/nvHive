"""GPU inference benchmarks — measure tokens/second on your hardware.

Think of it as '3DMark for AI' — gamers and students can see how
their GPU performs and compare with community averages.
"""

from __future__ import annotations

import time
from dataclasses import dataclass


@dataclass
class BenchmarkResult:
    model: str
    gpu_name: str
    vram_gb: float
    prompt_tokens: int
    output_tokens: int
    time_to_first_token_ms: int    # TTFT
    total_time_ms: int
    tokens_per_second: float       # output tok/s — the key metric
    prompt_eval_rate: float        # input tok/s (prompt processing speed)


@dataclass
class BenchmarkSuite:
    """Results from a full benchmark run."""
    gpu_name: str
    vram_gb: float
    results: list[BenchmarkResult]
    total_time_ms: int
    timestamp: str


# Standard benchmark prompts (consistent across all users for fair comparison)
BENCHMARK_PROMPTS = [
    {
        "name": "Short Q&A",
        "prompt": "What are the three laws of thermodynamics? Explain each briefly.",
        "max_tokens": 256,
        "description": "Tests basic generation speed on a short response",
    },
    {
        "name": "Code Generation",
        "prompt": "Write a Python implementation of merge sort with detailed comments explaining each step.",
        "max_tokens": 512,
        "description": "Tests code generation throughput",
    },
    {
        "name": "Long Response",
        "prompt": "Write a comprehensive essay on the history of artificial intelligence, from its origins in the 1950s to the transformer revolution. Cover key milestones, important researchers, and pivotal moments.",
        "max_tokens": 1024,
        "description": "Tests sustained generation speed on longer outputs",
    },
    {
        "name": "Reasoning",
        "prompt": "A farmer has 17 sheep. All but 9 die. How many sheep does the farmer have left? Show your step-by-step reasoning, then provide the answer.",
        "max_tokens": 256,
        "description": "Tests reasoning with moderate output",
    },
]


async def run_single_benchmark(
    provider,  # Provider instance (OllamaProvider or any)
    model: str,
    prompt: str,
    max_tokens: int = 512,
) -> BenchmarkResult:
    """Run a single benchmark — measure tokens/second.

    Uses streaming to measure time-to-first-token and per-token rate.
    """
    from nvh.providers.base import Message
    from nvh.utils.gpu import detect_gpus

    gpus = detect_gpus()
    gpu_name = gpus[0].name if gpus else "CPU"
    vram_gb = gpus[0].vram_gb if gpus else 0

    messages = [Message(role="user", content=prompt)]

    start = time.monotonic()
    first_token_time = None
    output_tokens = 0

    async for chunk in provider.stream(
        messages=messages,
        model=model,
        temperature=0.7,
        max_tokens=max_tokens,
    ):
        if chunk.delta and first_token_time is None:
            first_token_time = time.monotonic()
        if chunk.delta:
            output_tokens += 1  # approximate — count chunks as tokens
        if chunk.is_final and chunk.usage:
            output_tokens = chunk.usage.output_tokens or output_tokens

    end = time.monotonic()
    total_ms = int((end - start) * 1000)
    ttft_ms = int((first_token_time - start) * 1000) if first_token_time else total_ms

    # Calculate tokens/second (generation phase only, excluding TTFT)
    gen_time = (end - (first_token_time or start))
    tps = output_tokens / gen_time if gen_time > 0 else 0

    # Prompt eval rate (approximate)
    prompt_tokens = provider.estimate_tokens(prompt)
    prompt_eval_rate = prompt_tokens / ((first_token_time or end) - start) if first_token_time else 0

    return BenchmarkResult(
        model=model,
        gpu_name=gpu_name,
        vram_gb=vram_gb,
        prompt_tokens=prompt_tokens,
        output_tokens=output_tokens,
        time_to_first_token_ms=ttft_ms,
        total_time_ms=total_ms,
        tokens_per_second=round(tps, 1),
        prompt_eval_rate=round(prompt_eval_rate, 1),
    )


async def run_benchmark_suite(
    provider,
    model: str,
    prompts: list[dict] | None = None,
) -> BenchmarkSuite:
    """Run the full benchmark suite on a model."""
    from datetime import datetime

    from nvh.utils.gpu import detect_gpus

    if prompts is None:
        prompts = BENCHMARK_PROMPTS

    gpus = detect_gpus()
    gpu_name = gpus[0].name if gpus else "CPU"
    vram_gb = gpus[0].vram_gb if gpus else 0

    results = []
    suite_start = time.monotonic()

    for bp in prompts:
        result = await run_single_benchmark(
            provider=provider,
            model=model,
            prompt=bp["prompt"],
            max_tokens=bp["max_tokens"],
        )
        results.append(result)

    suite_end = time.monotonic()

    return BenchmarkSuite(
        gpu_name=gpu_name,
        vram_gb=vram_gb,
        results=results,
        total_time_ms=int((suite_end - suite_start) * 1000),
        timestamp=datetime.now().isoformat(),
    )


def format_benchmark_results(suite: BenchmarkSuite) -> str:
    """Format benchmark results as a Rich-compatible string."""
    lines = []
    lines.append(f"GPU: {suite.gpu_name} ({suite.vram_gb:.0f} GB VRAM)")
    lines.append(f"Total time: {suite.total_time_ms / 1000:.1f}s")
    lines.append("")

    avg_tps = sum(r.tokens_per_second for r in suite.results) / len(suite.results) if suite.results else 0
    lines.append(f"Average: {avg_tps:.1f} tokens/second")

    return "\n".join(lines)


# Community average baselines (for comparison)
COMMUNITY_BASELINES = {
    # GPU name -> average tok/s for 7B Q4_K_M model
    "NVIDIA GeForce RTX 3060": 55,
    "NVIDIA GeForce RTX 3070": 75,
    "NVIDIA GeForce RTX 3080": 85,
    "NVIDIA GeForce RTX 3090": 100,
    "NVIDIA GeForce RTX 4060": 70,
    "NVIDIA GeForce RTX 4070": 90,
    "NVIDIA GeForce RTX 4070 Ti": 110,
    "NVIDIA GeForce RTX 4080": 130,
    "NVIDIA GeForce RTX 4090": 160,
    "NVIDIA GeForce RTX 5070": 140,
    "NVIDIA GeForce RTX 5080": 180,
    "NVIDIA GeForce RTX 5090": 260,
    "NVIDIA A100": 190,
    "NVIDIA H100": 380,
}
