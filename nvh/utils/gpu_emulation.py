"""GPU performance emulation — predict LLM inference speed on any NVIDIA GPU.

Uses memory bandwidth as the primary predictor of tokens/second,
with architecture-specific IPC multipliers and compute capability
adjustments. Memory bandwidth is the bottleneck for LLM inference
(low arithmetic intensity — few ops per byte fetched).

Based on:
- NVIDIA architecture whitepapers (Ampere, Ada, Hopper, Blackwell)
- Measured baselines from DGX Spark (GB10)
- Memory bandwidth scaling research from NVIDIA/Databricks

Sources:
- https://acecloud.ai/blog/nvidia-ada-ampere-hopper-blackwell-comparison/
- https://www.hardware-corner.net/memory-bandwidth-llm-speed/
- https://developer.nvidia.com/blog/mastering-llm-techniques-inference-optimization/
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class GPUSpec:
    """Specifications for an NVIDIA GPU."""
    name: str
    architecture: str
    cuda_cores: int
    vram_gb: float
    memory_bandwidth_gbps: float  # GB/s
    tdp_watts: int
    sm_count: int
    process_nm: int  # manufacturing process
    compute_capability: tuple[int, int]


# Architecture IPC multipliers relative to Ampere (1.0)
# Ada Lovelace: ~1.5x IPC improvement over Ampere
# Hopper: ~1.7x for AI workloads (Transformer Engine)
# Blackwell/GB10: ~2.0x (2nd gen Transformer Engine, FP4)
_ARCH_IPC_MULTIPLIER: dict[str, float] = {
    "pascal": 0.5,       # GTX 10xx
    "volta": 0.7,        # V100
    "turing": 0.8,       # RTX 20xx
    "ampere": 1.0,       # RTX 30xx, A100
    "ada_lovelace": 1.3, # RTX 40xx, L40
    "hopper": 1.7,       # H100, H200
    "blackwell": 2.0,    # RTX 50xx, B100, B200, GB10
}


# Known GPU specifications
GPU_DATABASE: dict[str, GPUSpec] = {
    # Consumer — Ampere
    "rtx_3060": GPUSpec(
        "RTX 3060", "ampere", 3584, 12, 360, 170, 28, 8,
        (8, 6),
    ),
    "rtx_3070": GPUSpec(
        "RTX 3070", "ampere", 5888, 8, 448, 220, 46, 8,
        (8, 6),
    ),
    "rtx_3080": GPUSpec(
        "RTX 3080", "ampere", 8704, 10, 760, 320, 68, 8,
        (8, 6),
    ),
    "rtx_3090": GPUSpec(
        "RTX 3090", "ampere", 10496, 24, 936, 350, 82, 8,
        (8, 6),
    ),
    # Consumer — Ada Lovelace
    "rtx_4060": GPUSpec(
        "RTX 4060", "ada_lovelace", 3072, 8, 272, 115, 24,
        5, (8, 9),
    ),
    "rtx_4070": GPUSpec(
        "RTX 4070", "ada_lovelace", 5888, 12, 504, 200, 46,
        5, (8, 9),
    ),
    "rtx_4080": GPUSpec(
        "RTX 4080", "ada_lovelace", 9728, 16, 717, 320, 76,
        5, (8, 9),
    ),
    "rtx_4090": GPUSpec(
        "RTX 4090", "ada_lovelace", 16384, 24, 1008, 450,
        128, 5, (8, 9),
    ),
    # Consumer — Blackwell
    "rtx_5070": GPUSpec(
        "RTX 5070", "blackwell", 6144, 12, 672, 250, 48, 4,
        (10, 0),
    ),
    "rtx_5080": GPUSpec(
        "RTX 5080", "blackwell", 10752, 16, 960, 360, 84,
        4, (10, 0),
    ),
    "rtx_5090": GPUSpec(
        "RTX 5090", "blackwell", 21760, 32, 1792, 575, 170,
        4, (10, 0),
    ),
    # Data Center — Ampere
    "a100_40gb": GPUSpec(
        "A100 40GB", "ampere", 6912, 40, 1555, 250, 108, 7,
        (8, 0),
    ),
    "a100_80gb": GPUSpec(
        "A100 80GB", "ampere", 6912, 80, 2039, 300, 108, 7,
        (8, 0),
    ),
    # Data Center — Hopper
    "h100_sxm": GPUSpec(
        "H100 SXM", "hopper", 16896, 80, 3350, 700, 132, 4,
        (9, 0),
    ),
    "h100_pcie": GPUSpec(
        "H100 PCIe", "hopper", 14592, 80, 2039, 350, 114,
        4, (9, 0),
    ),
    # Data Center — Blackwell
    "b100": GPUSpec(
        "B100", "blackwell", 18432, 192, 8000, 700, 144, 4,
        (10, 0),
    ),
    "b200": GPUSpec(
        "B200", "blackwell", 18432, 192, 8000, 1000, 144, 4,
        (10, 0),
    ),
    # DGX Spark
    "gb10": GPUSpec(
        "DGX Spark (GB10)", "blackwell", 2048, 128, 273, 100,
        16, 4, (10, 0),
    ),
}


# Measured baselines: (gpu_key, model_name) -> tok/s
_MEASURED_BASELINES: dict[tuple[str, str], float] = {
    ("gb10", "nemotron-mini"): 86.6,
    ("gb10", "gemma3"): 73.4,
    ("gb10", "llama3.1"): 48.1,
    ("gb10", "gemma4:e4b"): 26.4,
    ("gb10", "nemotron-3-super"): 24.8,
}

# Model memory requirements (approximate, quantized)
_MODEL_MEMORY_GB: dict[str, float] = {
    "nemotron-mini": 2.7,
    "nemotron-small": 5.0,
    "gemma3": 3.3,
    "gemma4:e2b": 1.5,
    "gemma4:e4b": 9.6,
    "gemma4:26b": 16.0,
    "gemma4:31b": 20.0,
    "llama3.1": 4.9,
    "llama3.1:70b": 40.0,
    "nemotron": 40.0,
    "nemotron-3-super": 86.0,
    "codellama": 4.0,
}


@dataclass
class PerformanceEstimate:
    """Estimated performance for a model on a specific GPU."""
    gpu_name: str
    model: str
    estimated_toks: float
    fits_in_vram: bool
    confidence: str  # "measured", "high", "medium", "low"
    basis: str  # explanation of how estimate was derived
    vram_headroom_gb: float


def estimate_performance(
    gpu_key: str,
    model: str,
) -> PerformanceEstimate | None:
    """Estimate tokens/second for a model on a specific GPU.

    Uses memory bandwidth as the primary predictor, adjusted by
    architecture IPC multiplier. Calibrated against measured
    DGX Spark baselines.
    """
    gpu = GPU_DATABASE.get(gpu_key)
    if not gpu:
        return None

    model_mem = _MODEL_MEMORY_GB.get(model)
    if model_mem is None:
        return None

    fits = model_mem <= gpu.vram_gb
    headroom = gpu.vram_gb - model_mem

    # Check for measured baseline
    measured = _MEASURED_BASELINES.get((gpu_key, model))
    if measured is not None:
        return PerformanceEstimate(
            gpu_name=gpu.name,
            model=model,
            estimated_toks=measured,
            fits_in_vram=fits,
            confidence="measured",
            basis="Directly measured on this hardware",
            vram_headroom_gb=headroom,
        )

    if not fits:
        return PerformanceEstimate(
            gpu_name=gpu.name,
            model=model,
            estimated_toks=0.0,
            fits_in_vram=False,
            confidence="n/a",
            basis=f"Model needs {model_mem}GB but GPU has {gpu.vram_gb}GB",
            vram_headroom_gb=headroom,
        )

    # Estimate using bandwidth scaling from a measured baseline
    # Find the best matching measured baseline (same model,
    # different GPU — or same GPU family, different model)
    ref_key = None
    ref_toks = None

    # Priority 1: same model on a measured GPU
    for (gk, mk), toks in _MEASURED_BASELINES.items():
        if mk == model:
            ref_key = gk
            ref_toks = toks
            break

    # Priority 2: similar-sized model on a measured GPU
    if ref_key is None:
        best_diff = float("inf")
        for (gk, mk), toks in _MEASURED_BASELINES.items():
            mk_mem = _MODEL_MEMORY_GB.get(mk, 0)
            diff = abs(mk_mem - model_mem)
            if diff < best_diff:
                best_diff = diff
                ref_key = gk
                ref_toks = toks

    if ref_key is None or ref_toks is None:
        return PerformanceEstimate(
            gpu_name=gpu.name,
            model=model,
            estimated_toks=0.0,
            fits_in_vram=fits,
            confidence="low",
            basis="No reference baseline available",
            vram_headroom_gb=headroom,
        )

    ref_gpu = GPU_DATABASE[ref_key]

    # Scale by memory bandwidth ratio (dominant for token generation)
    bw_ratio = (
        gpu.memory_bandwidth_gbps
        / ref_gpu.memory_bandwidth_gbps
    )

    # Scale by compute (CUDA cores * IPC) for prompt processing
    gpu_ipc = _ARCH_IPC_MULTIPLIER.get(gpu.architecture, 1.0)
    ref_ipc = _ARCH_IPC_MULTIPLIER.get(
        ref_gpu.architecture, 1.0,
    )
    compute_ratio = (
        (gpu.cuda_cores * gpu_ipc)
        / (ref_gpu.cuda_cores * ref_ipc)
    )

    # VRAM headroom factor — models near VRAM limit perform
    # worse due to KV cache pressure
    vram_ratio = min(1.0, headroom / model_mem) if model_mem > 0 else 1.0
    vram_factor = 0.8 + 0.2 * vram_ratio  # 0.8x at full, 1.0x with headroom

    # Combined scaling:
    # - Token generation is memory-bound (60% bandwidth)
    # - Prompt processing is compute-bound (25% compute)
    # - VRAM headroom affects KV cache (15%)
    scale = (
        bw_ratio * 0.60
        + compute_ratio * 0.25
        + vram_factor * 0.15
    )
    estimated = ref_toks * scale

    # Determine confidence
    if gpu.architecture == ref_gpu.architecture:
        confidence = "high"
    elif abs(bw_ratio - 1.0) < 0.5:
        confidence = "medium"
    else:
        confidence = "medium"

    return PerformanceEstimate(
        gpu_name=gpu.name,
        model=model,
        estimated_toks=round(estimated, 1),
        fits_in_vram=fits,
        confidence=confidence,
        basis=(
            f"Scaled from {ref_gpu.name} ({ref_toks} tok/s)"
            f" — BW {bw_ratio:.2f}x,"
            f" compute {compute_ratio:.2f}x"
        ),
        vram_headroom_gb=headroom,
    )


def estimate_all_gpus(model: str) -> list[PerformanceEstimate]:
    """Estimate performance for a model across all known GPUs."""
    results = []
    for gpu_key in sorted(GPU_DATABASE.keys()):
        est = estimate_performance(gpu_key, model)
        if est and est.fits_in_vram:
            results.append(est)
    results.sort(key=lambda e: e.estimated_toks, reverse=True)
    return results


def estimate_all_models(gpu_key: str) -> list[PerformanceEstimate]:
    """Estimate performance for all known models on a specific GPU."""
    results = []
    for model in sorted(_MODEL_MEMORY_GB.keys()):
        est = estimate_performance(gpu_key, model)
        if est:
            results.append(est)
    results.sort(key=lambda e: e.estimated_toks, reverse=True)
    return results
