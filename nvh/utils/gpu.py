"""NVIDIA GPU detection and model recommendation.

Uses pynvml (NVML Python bindings) when available for direct GPU access.
Falls back to nvidia-smi subprocess if pynvml is not installed.

pynvml advantages over nvidia-smi:
- No subprocess spawn (faster, ~1ms vs ~100ms)
- No output parsing (more reliable)
- More data: temperature, power draw, clock speeds, PCIe info, processes
- Works in containers where nvidia-smi may not be on PATH

Install: pip install nvidia-ml-py3
(Optional — nvidia-smi fallback always works)
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field


@dataclass
class GPUInfo:
    name: str             # e.g. "NVIDIA GeForce RTX 4090"
    vram_mb: int          # e.g. 24576
    vram_gb: float        # e.g. 24.0
    driver_version: str   # e.g. "535.129.03"
    cuda_version: str     # e.g. "12.2"
    utilization_pct: int  # GPU utilization percentage
    memory_used_mb: int   # Currently used VRAM
    memory_free_mb: int   # Available VRAM
    index: int            # GPU index (for multi-GPU systems)
    # Extended info (from pynvml, may be empty with nvidia-smi fallback)
    temperature_c: int = 0       # GPU temperature in Celsius
    power_draw_w: float = 0.0    # Current power draw in watts
    power_limit_w: float = 0.0   # Power limit in watts
    clock_gpu_mhz: int = 0       # Current GPU clock speed
    clock_mem_mhz: int = 0       # Current memory clock speed
    pcie_gen: int = 0            # PCIe generation
    pcie_width: int = 0          # PCIe lane width
    compute_capability: tuple[int, int] = (0, 0)  # e.g. (8, 9) for Ada
    processes: list[dict] = field(default_factory=list)  # running GPU processes


@dataclass
class ModelRecommendation:
    model: str              # e.g. "nemotron-small"
    reason: str             # e.g. "8GB+ VRAM available — good quality/speed balance"
    vram_required_gb: float
    tier: str               # "mini", "small", "full", "multi-gpu"


def detect_gpus() -> list[GPUInfo]:
    """Detect NVIDIA GPUs. Tries pynvml first (fast, rich data), falls back to nvidia-smi.

    Returns a list of GPUInfo objects — one per GPU.  Returns an empty list if
    no NVIDIA GPU is found or accessible.
    """
    # Try pynvml first (direct NVML library — faster and more data)
    gpus = _detect_gpus_pynvml()
    if gpus:
        return gpus

    # Fall back to nvidia-smi subprocess
    return _detect_gpus_smi()


def _detect_gpus_pynvml() -> list[GPUInfo]:
    """Detect GPUs via pynvml (NVML Python bindings)."""
    try:
        import pynvml
    except ImportError:
        return []  # pynvml not installed — fall back to nvidia-smi

    try:
        pynvml.nvmlInit()
    except Exception:
        return []

    try:
        driver_version = pynvml.nvmlSystemGetDriverVersion()
        cuda_version = "unknown"
        try:
            cuda_ver_int = pynvml.nvmlSystemGetCudaDriverVersion_v2()
            major = cuda_ver_int // 1000
            minor = (cuda_ver_int % 1000) // 10
            cuda_version = f"{major}.{minor}"
        except Exception:
            pass

        device_count = pynvml.nvmlDeviceGetCount()
        gpus: list[GPUInfo] = []

        for i in range(device_count):
            handle = pynvml.nvmlDeviceGetHandleByIndex(i)

            name = pynvml.nvmlDeviceGetName(handle)
            if isinstance(name, bytes):
                name = name.decode()

            try:
                mem_info = pynvml.nvmlDeviceGetMemoryInfo(handle)
                vram_mb = mem_info.total // (1024 * 1024)
                memory_used = mem_info.used // (1024 * 1024)
                memory_free = mem_info.free // (1024 * 1024)
            except Exception:
                # Unified memory GPUs (GB10/DGX Spark) may not
                # report VRAM via nvmlDeviceGetMemoryInfo.
                # Fall back to system RAM as the memory pool.
                import os
                try:
                    total_ram = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
                    vram_mb = total_ram // (1024 * 1024)
                    memory_free = vram_mb  # conservative
                    memory_used = 0
                except Exception:
                    vram_mb = 0
                    memory_free = 0
                    memory_used = 0

            try:
                util = pynvml.nvmlDeviceGetUtilizationRates(handle)
                utilization = util.gpu
            except Exception:
                utilization = 0

            # Extended info
            temperature = 0
            try:
                temperature = pynvml.nvmlDeviceGetTemperature(handle, pynvml.NVML_TEMPERATURE_GPU)
            except Exception:
                pass

            power_draw = 0.0
            power_limit = 0.0
            try:
                power_draw = pynvml.nvmlDeviceGetPowerUsage(handle) / 1000.0  # mW to W
                power_limit = pynvml.nvmlDeviceGetPowerManagementLimit(handle) / 1000.0
            except Exception:
                pass

            clock_gpu = 0
            clock_mem = 0
            try:
                clock_gpu = pynvml.nvmlDeviceGetClockInfo(handle, pynvml.NVML_CLOCK_GRAPHICS)
                clock_mem = pynvml.nvmlDeviceGetClockInfo(handle, pynvml.NVML_CLOCK_MEM)
            except Exception:
                pass

            pcie_gen = 0
            pcie_width = 0
            try:
                pcie_gen = pynvml.nvmlDeviceGetCurrPcieLinkGeneration(handle)
                pcie_width = pynvml.nvmlDeviceGetCurrPcieLinkWidth(handle)
            except Exception:
                pass

            cc = (0, 0)
            try:
                major, minor = pynvml.nvmlDeviceGetCudaComputeCapability(handle)
                cc = (major, minor)
            except Exception:
                pass

            processes: list[dict] = []
            try:
                procs = pynvml.nvmlDeviceGetComputeRunningProcesses(handle)
                for p in procs[:10]:
                    processes.append({
                        "pid": p.pid,
                        "memory_mb": (p.usedGpuMemory or 0) // (1024 * 1024),
                    })
            except Exception:
                pass

            gpus.append(GPUInfo(
                name=name,
                vram_mb=vram_mb,
                vram_gb=round(vram_mb / 1024, 1),
                driver_version=driver_version if isinstance(driver_version, str) else driver_version.decode(),
                cuda_version=cuda_version,
                utilization_pct=utilization,
                memory_used_mb=memory_used,
                memory_free_mb=memory_free,
                index=i,
                temperature_c=temperature,
                power_draw_w=power_draw,
                power_limit_w=power_limit,
                clock_gpu_mhz=clock_gpu,
                clock_mem_mhz=clock_mem,
                pcie_gen=pcie_gen,
                pcie_width=pcie_width,
                compute_capability=cc,
                processes=processes,
            ))

        return gpus
    except Exception:
        return []
    finally:
        try:
            pynvml.nvmlShutdown()
        except Exception:
            pass


def _detect_gpus_smi() -> list[GPUInfo]:
    """Fallback: detect GPUs via nvidia-smi subprocess."""
    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=index,name,memory.total,memory.used,memory.free,"
                "utilization.gpu,driver_version",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except FileNotFoundError:
        return []
    except Exception:
        return []

    if result.returncode != 0 or not result.stdout.strip():
        return []

    cuda_ver = _get_cuda_version()

    gpus: list[GPUInfo] = []
    for line in result.stdout.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 7:
            continue
        try:
            index         = int(parts[0])
            name          = parts[1]
            vram_mb       = int(float(parts[2]))
            memory_used   = int(float(parts[3]))
            memory_free   = int(float(parts[4]))
            utilization   = int(float(re.sub(r"[^\d.]", "", parts[5]) or "0"))
            driver_ver    = parts[6]
            vram_gb       = round(vram_mb / 1024, 1)

            gpus.append(
                GPUInfo(
                    name=name,
                    vram_mb=vram_mb,
                    vram_gb=vram_gb,
                    driver_version=driver_ver,
                    cuda_version=cuda_ver,
                    utilization_pct=utilization,
                    memory_used_mb=memory_used,
                    memory_free_mb=memory_free,
                    index=index,
                )
            )
        except (ValueError, IndexError):
            continue

    return gpus


def _get_cuda_version() -> str:
    """Return CUDA version string reported by nvidia-smi, or 'unknown'."""
    try:
        subprocess.run(
            ["nvidia-smi", "--query-gpu=compute_cap", "--format=csv,noheader"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        # nvidia-smi doesn't directly expose the CUDA runtime version, but we
        # can parse it from the human-readable output header.
        header_result = subprocess.run(
            ["nvidia-smi"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        match = re.search(r"CUDA Version:\s*([\d.]+)", header_result.stdout)
        if match:
            return match.group(1)
    except Exception:
        pass
    return "unknown"


def get_total_vram_mb() -> int:
    """Return total VRAM in MB across all detected GPUs.

    Returns 0 when no GPU is detected.
    """
    gpus = detect_gpus()
    return sum(g.vram_mb for g in gpus)


def recommend_models(gpus: list[GPUInfo] | None = None) -> list[ModelRecommendation]:
    """Recommend local models based on available GPU VRAM.

    Recommends both Nemotron and Gemma 4 (NVIDIA-optimized) models
    so users can run local council with two different architectures.

    Tier rules (total VRAM across all GPUs):
    - No GPU or < 4 GB : nemotron-mini + gemma4:e2b (CPU mode)
    - 4 – 6 GB         : nemotron-mini + gemma4:e4b
    - 6 – 12 GB        : nemotron-small + gemma4:e4b
    - 12 – 24 GB       : nemotron-small + gemma4:26b
    - 24 – 48 GB       : nemotron (70B) + gemma4:31b
    - 48 – 80 GB       : nemotron (70B) + gemma4:31b
    - 80 GB+           : nemotron:120b + gemma4:31b
    - Multi-GPU        : Ollama uses all GPUs automatically
    """
    if gpus is None:
        gpus = detect_gpus()

    total_vram_mb  = sum(g.vram_mb for g in gpus)
    total_vram_gb  = total_vram_mb / 1024
    multi_gpu      = len(gpus) > 1

    # Factor in system RAM for CPU offload — but be conservative.
    # On gaming/student rigs RAM is often 16-32GB, and the OS + apps use ~4-6GB.
    # CPU offloaded layers are 5-10x slower than GPU, so only helpful for
    # "barely doesn't fit" scenarios, not as a primary strategy.
    sys_mem = detect_system_memory()
    cpu_offload_gb = min(sys_mem.effective_for_llm_gb, 16.0)  # cap benefit at 16GB

    recommendations: list[ModelRecommendation] = []

    if not gpus or total_vram_gb < 4:
        # CPU-only: system RAM is the constraint
        if sys_mem.available_ram_gb >= 8:
            recommendations.append(
                ModelRecommendation(
                    model="nemotron-small",
                    reason=(
                        f"No GPU but {sys_mem.available_ram_gb:.0f} GB RAM available — "
                        f"nemotron-small runs on CPU (slow but functional)"
                    ),
                    vram_required_gb=0.0,
                    tier="small",
                )
            )
        recommendations.append(
            ModelRecommendation(
                model="nemotron-mini",
                reason=(
                    f"No GPU detected or < 4 GB VRAM — running on CPU "
                    f"({sys_mem.available_ram_gb:.0f} GB RAM available)"
                ),
                vram_required_gb=0.0,
                tier="mini",
            )
        )
        recommendations.append(
            ModelRecommendation(
                model="gemma4:e2b",
                reason="Gemma 4 E2B — Google/NVIDIA optimized, tiny edge model",
                vram_required_gb=0.0,
                tier="mini",
            )
        )
    elif total_vram_gb < 6:
        recommendations.append(
            ModelRecommendation(
                model="nemotron-mini",
                reason=f"{total_vram_gb:.0f} GB VRAM — nemotron-mini fits with GPU acceleration",
                vram_required_gb=2.0,
                tier="mini",
            )
        )
        recommendations.append(
            ModelRecommendation(
                model="gemma4:e4b",
                reason="Gemma 4 E4B — NVIDIA-optimized, fits alongside nemotron-mini",
                vram_required_gb=3.0,
                tier="mini",
            )
        )
    elif total_vram_gb < 12:
        recommendations.append(
            ModelRecommendation(
                model="nemotron-small",
                reason=f"{total_vram_gb:.0f} GB VRAM — recommended sweet spot for quality/speed",
                vram_required_gb=5.0,
                tier="small",
            )
        )
        recommendations.append(
            ModelRecommendation(
                model="gemma4:e4b",
                reason="Gemma 4 E4B — pair with Nemotron for local council",
                vram_required_gb=3.0,
                tier="small",
            )
        )
    elif total_vram_gb < 24:
        recommendations.append(
            ModelRecommendation(
                model="nemotron-small",
                reason=f"{total_vram_gb:.0f} GB VRAM — great quality/speed balance",
                vram_required_gb=5.0,
                tier="small",
            )
        )
        if total_vram_gb >= 20:
            recommendations.append(
                ModelRecommendation(
                    model="gemma4:26b",
                    reason=(
                        "Gemma 4 26B — NVIDIA-optimized"
                        " reasoning + code + multimodal"
                    ),
                    vram_required_gb=16.0,
                    tier="medium",
                )
            )
        else:
            recommendations.append(
                ModelRecommendation(
                    model="gemma4:e4b",
                    reason=(
                        "Gemma 4 E4B — fits alongside"
                        " Nemotron for local council"
                    ),
                    vram_required_gb=3.0,
                    tier="small",
                )
            )
    elif total_vram_gb < 48:
        recommendations.append(
            ModelRecommendation(
                model="nemotron",
                reason=f"{total_vram_gb:.0f} GB VRAM — full Nemotron 70B (quantized) fits",
                vram_required_gb=40.0,
                tier="full",
            )
        )
        # Gemma 4 26B fits alongside Nemotron 70B only on 40GB+
        if total_vram_gb >= 40:
            recommendations.append(
                ModelRecommendation(
                    model="gemma4:26b",
                    reason=(
                        "Gemma 4 26B — fits alongside"
                        " Nemotron 70B for local council"
                    ),
                    vram_required_gb=16.0,
                    tier="medium",
                )
            )
        else:
            recommendations.append(
                ModelRecommendation(
                    model="gemma4:e4b",
                    reason=(
                        "Gemma 4 E4B — lightweight"
                        " companion for local council"
                    ),
                    vram_required_gb=3.0,
                    tier="small",
                )
            )
    elif total_vram_gb < 80:
        recommendations.append(
            ModelRecommendation(
                model="nemotron",
                reason=f"{total_vram_gb:.0f} GB VRAM — full Nemotron 70B at high quality",
                vram_required_gb=40.0,
                tier="full",
            )
        )
    elif total_vram_gb < 240:
        # 80-240 GB VRAM — can run Nemotron 120B quantized (Q4_K_M ~67GB)
        # Full FP16 (240GB) requires multi-GPU at this tier
        recommendations.append(
            ModelRecommendation(
                model="nemotron:120b",
                reason=(
                    f"{total_vram_gb:.0f} GB VRAM available — Nemotron 120B runs in Q4_K_M "
                    f"(~67 GB). Full FP16 (240 GB) requires more VRAM or multi-GPU"
                ),
                vram_required_gb=67.0,
                tier="flagship",
            )
        )
        recommendations.append(
            ModelRecommendation(
                model="nemotron",
                reason="Nemotron 70B also available as a faster alternative",
                vram_required_gb=40.0,
                tier="full",
            )
        )
    else:
        # 240GB+ VRAM (multi-GPU or GB200 class) — Nemotron 120B at full FP16
        recommendations.append(
            ModelRecommendation(
                model="nemotron:120b",
                reason=f"{total_vram_gb:.0f} GB VRAM available — Nemotron 120B at full FP16 precision",
                vram_required_gb=240.0,
                tier="flagship-fp16",
            )
        )
        recommendations.append(
            ModelRecommendation(
                model="nemotron",
                reason="Nemotron 70B at full FP16 for lower-latency tasks",
                vram_required_gb=140.0,
                tier="full",
            )
        )

    # Check if CPU offload could unlock a larger model
    # Only suggest if the model is "close" (within CPU offload budget)
    combined_gb = total_vram_gb + cpu_offload_gb
    if recommendations:
        top_tier = recommendations[0].tier
        # If we're on "small" tier but combined VRAM+RAM could fit 70B Q4 (~45GB)
        if top_tier in ("small",) and combined_gb >= 45 and total_vram_gb >= 12:
            recommendations.append(
                ModelRecommendation(
                    model="nemotron",
                    reason=(
                        f"Partial CPU offload: {total_vram_gb:.0f} GB VRAM + "
                        f"{cpu_offload_gb:.0f} GB RAM = {combined_gb:.0f} GB combined. "
                        f"70B fits but ~30-50% slower than full GPU"
                    ),
                    vram_required_gb=45.0,
                    tier="full-hybrid",
                )
            )
        # If we're on "full" tier but combined could fit 120B Q4 (~67GB)
        elif top_tier in ("full",) and combined_gb >= 67 and total_vram_gb >= 48:
            recommendations.append(
                ModelRecommendation(
                    model="nemotron:120b",
                    reason=(
                        f"Partial CPU offload: {total_vram_gb:.0f} GB VRAM + "
                        f"{cpu_offload_gb:.0f} GB RAM = {combined_gb:.0f} GB combined. "
                        f"120B Q4 fits but noticeably slower than full GPU"
                    ),
                    vram_required_gb=67.0,
                    tier="flagship-hybrid",
                )
            )

    if multi_gpu and recommendations:
        last = recommendations[0]
        recommendations[0] = ModelRecommendation(
            model=last.model,
            reason=last.reason + f" (Ollama will use all {len(gpus)} GPUs automatically)",
            vram_required_gb=last.vram_required_gb,
            tier="multi-gpu",
        )

    return recommendations


@dataclass
class SystemMemoryInfo:
    total_ram_gb: float
    available_ram_gb: float
    effective_for_llm_gb: float  # what's usable for CPU offloaded layers


def detect_system_memory() -> SystemMemoryInfo:
    """Detect system RAM (free/available). Used for CPU offload decisions and OOM prevention.

    Uses platform-specific methods to get *available* (free) RAM, not just total.
    On gaming/student rigs this is typically 8-20GB free out of 16-32GB total.
    """
    total_gb = 0.0
    avail_gb = 0.0

    try:
        # Try /proc/meminfo first (Linux — most reliable for free RAM)
        with open("/proc/meminfo") as f:
            meminfo = {}
            for line in f:
                parts = line.split()
                if len(parts) >= 2:
                    meminfo[parts[0].rstrip(":")] = int(parts[1])  # value in kB
            total_gb = meminfo.get("MemTotal", 0) / (1024 ** 2)
            # MemAvailable is the best metric — accounts for cache that can be freed
            avail_gb = meminfo.get("MemAvailable", meminfo.get("MemFree", 0)) / (1024 ** 2)
    except FileNotFoundError:
        pass

    if total_gb == 0:
        try:
            # macOS / other Unix fallback
            import os
            page_size = os.sysconf("SC_PAGE_SIZE")
            total_pages = os.sysconf("SC_PHYS_PAGES")
            total_gb = (page_size * total_pages) / (1024 ** 3)
            # macOS doesn't have SC_AVPHYS_PAGES — estimate 60% free as conservative default
            try:
                avail_pages = os.sysconf("SC_AVPHYS_PAGES")
                avail_gb = (page_size * avail_pages) / (1024 ** 3)
            except (ValueError, OSError):
                avail_gb = total_gb * 0.6
        except Exception:
            pass

    if total_gb == 0:
        try:
            # Last resort: subprocess
            result = subprocess.run(["free", "-b"], capture_output=True, text=True, timeout=3)
            for line in result.stdout.splitlines():
                if line.startswith("Mem:"):
                    parts = line.split()
                    total_gb = int(parts[1]) / (1024 ** 3)
                    avail_gb = int(parts[6]) / (1024 ** 3) if len(parts) > 6 else total_gb * 0.6
        except Exception:
            pass

    # For LLM CPU offload, cap at 70% of free RAM — leave headroom for OS/apps
    effective = avail_gb * 0.7
    return SystemMemoryInfo(
        total_ram_gb=round(total_gb, 1),
        available_ram_gb=round(avail_gb, 1),
        effective_for_llm_gb=round(effective, 1),
    )


def check_oom_risk(model_vram_gb: float, gpus: list[GPUInfo] | None = None) -> dict:
    """Check if loading a model would risk OOM on GPU or system.

    Returns a dict with:
      safe: bool — True if model fits safely
      fits_gpu: bool — model fits entirely in GPU VRAM
      fits_hybrid: bool — model fits with CPU offload
      gpu_free_gb: float — current free VRAM
      ram_free_gb: float — current free system RAM
      recommendation: str — what to do
    """
    if gpus is None:
        gpus = detect_gpus()

    sys_mem = detect_system_memory()

    # GPU free VRAM (use memory_free_mb from nvidia-smi)
    gpu_free_gb = sum(g.memory_free_mb for g in gpus) / 1024 if gpus else 0.0
    # Reserve 15% for KV cache and overhead
    gpu_usable_gb = gpu_free_gb * 0.85

    ram_free_gb = sys_mem.available_ram_gb
    ram_usable_gb = sys_mem.effective_for_llm_gb

    result = {
        "safe": False,
        "fits_gpu": False,
        "fits_hybrid": False,
        "gpu_free_gb": round(gpu_free_gb, 1),
        "ram_free_gb": round(ram_free_gb, 1),
        "recommendation": "",
    }

    if model_vram_gb <= gpu_usable_gb:
        result["safe"] = True
        result["fits_gpu"] = True
        result["recommendation"] = (
            f"Model fits in GPU VRAM ({model_vram_gb:.0f} GB needed, "
            f"{gpu_free_gb:.0f} GB free) — full GPU acceleration"
        )
    elif model_vram_gb <= gpu_usable_gb + ram_usable_gb:
        result["safe"] = True
        result["fits_hybrid"] = True
        overflow = model_vram_gb - gpu_usable_gb
        result["recommendation"] = (
            f"Model needs hybrid mode: {gpu_usable_gb:.0f} GB on GPU + "
            f"{overflow:.0f} GB on CPU RAM. Expect 30-50% slower than full GPU"
        )
    else:
        needed = model_vram_gb - gpu_usable_gb - ram_usable_gb
        result["recommendation"] = (
            f"OOM RISK: Model needs {model_vram_gb:.0f} GB but only "
            f"{gpu_usable_gb:.0f} GB GPU + {ram_usable_gb:.0f} GB RAM available. "
            f"Short by {needed:.0f} GB. Use a smaller model or lower quantization"
        )

    return result


@dataclass
class OllamaOptimization:
    """GPU-architecture-aware settings for Ollama."""
    flash_attention: bool
    num_parallel: int
    recommended_ctx: int
    recommended_quant: str
    architecture: str
    compute_capability: tuple[int, int]
    notes: list[str]


def get_ollama_optimizations(gpus: list[GPUInfo] | None = None) -> OllamaOptimization:
    """Return architecture-aware Ollama settings based on detected GPU.

    Parses compute capability to determine:
    - Flash Attention support (CC >= 8.0)
    - Recommended parallelism
    - Context window sizing based on VRAM
    - Best quantization format for the architecture
    """
    if gpus is None:
        gpus = detect_gpus()

    if not gpus:
        return OllamaOptimization(
            flash_attention=False,
            num_parallel=1,
            recommended_ctx=2048,
            recommended_quant="Q4_K_M",
            architecture="CPU",
            compute_capability=(0, 0),
            notes=["No GPU detected — running on CPU. Inference will be slow."],
        )

    # Use primary GPU for architecture decisions
    gpu = gpus[0]
    total_vram_gb = sum(g.vram_mb for g in gpus) / 1024
    cc = _parse_compute_capability(gpu.name)

    notes: list[str] = []

    # Flash Attention: CC >= 8.0 (Ampere+)
    flash_attention = cc >= (8, 0)
    if cc >= (9, 0):
        notes.append("Flash Attention 3 available (Hopper+)")
    elif cc >= (8, 0):
        notes.append("Flash Attention 2 enabled")
    else:
        notes.append("Flash Attention not supported (Turing) — using standard attention")

    # Architecture name
    if cc >= (10, 0):
        arch = "Blackwell"
        notes.append("FP4 Tensor Cores available (not yet used by Ollama)")
        notes.append("GDDR7 provides high memory bandwidth")
    elif cc >= (9, 0):
        arch = "Hopper"
        notes.append("Transformer Engine available (not used by Ollama — use vLLM for FP8)")
    elif cc >= (8, 9):
        arch = "Ada Lovelace"
        notes.append("FP8 Tensor Cores present (not yet leveraged by Ollama)")
    elif cc >= (8, 0):
        arch = "Ampere"
        notes.append("BF16 Tensor Cores active")
    else:
        arch = "Turing"
        notes.append("No BF16 support — avoid BF16 models, use Q4_K_M/Q8_0")

    # Parallelism based on VRAM tier
    if total_vram_gb >= 48:
        num_parallel = 4
    elif total_vram_gb >= 24:
        num_parallel = 2
    else:
        num_parallel = 1

    # Context window based on VRAM
    if total_vram_gb >= 96:
        ctx = 131072
    elif total_vram_gb >= 48:
        ctx = 65536
    elif total_vram_gb >= 24:
        ctx = 32768
    elif total_vram_gb >= 16:
        ctx = 16384
    elif total_vram_gb >= 12:
        ctx = 8192
    else:
        ctx = 4096

    # Quantization recommendation
    if cc >= (9, 0) and total_vram_gb >= 80:
        quant = "Q8_0 or F16"  # Hopper+ with HBM bandwidth can handle it
        notes.append("High bandwidth — Q8_0 or F16 recommended for best quality")
    elif cc >= (10, 0):
        quant = "Q4_K_M"  # Blackwell consumer (GDDR7) — Q4 is optimal balance
        notes.append("Future: FP4 GGUF format will leverage Blackwell natively")
    elif cc < (8, 0):
        quant = "Q4_K_M"  # Turing — avoid BF16, stick with integer quants
    else:
        quant = "Q4_K_M"  # Ampere/Ada — standard recommendation

    # System RAM for CPU offload
    sys_mem = detect_system_memory()
    if sys_mem.total_ram_gb > 0:
        notes.append(f"System RAM: {sys_mem.total_ram_gb:.0f} GB total, "
                     f"~{sys_mem.effective_for_llm_gb:.0f} GB usable for CPU offload")

    return OllamaOptimization(
        flash_attention=flash_attention,
        num_parallel=num_parallel,
        recommended_ctx=ctx,
        recommended_quant=quant,
        architecture=arch,
        compute_capability=cc,
        notes=notes,
    )


def _parse_compute_capability(gpu_name: str) -> tuple[int, int]:
    """Infer compute capability from GPU name.

    This is a heuristic — ideally we'd query CUDA directly, but nvidia-smi
    doesn't expose CC. This covers the common consumer/pro/datacenter GPUs.
    """
    name = gpu_name.upper()

    # Blackwell (CC 10.0)
    if any(x in name for x in ["RTX 50", "RTX PRO 6000", "B100", "B200", "GB200"]):
        return (10, 0)

    # Hopper (CC 9.0)
    if any(x in name for x in ["H100", "H200", "H800"]):
        return (9, 0)

    # Ada Lovelace (CC 8.9)
    if any(x in name for x in ["RTX 40", "RTX 6000 ADA", "RTX A6000 ADA", "RTX 6000 PRO", "L4", "L40"]):
        return (8, 9)

    # Ampere (CC 8.0/8.6)
    if any(x in name for x in ["A100", "A30"]):
        return (8, 0)
    if any(x in name for x in ["RTX 30", "RTX A", "A10", "A40", "A16"]):
        return (8, 6)

    # Turing (CC 7.5)
    if any(x in name for x in ["RTX 20", "GTX 16", "T4", "TITAN RTX"]):
        return (7, 5)

    # Older or unrecognized — assume Ampere as safe default
    return (8, 0)


def get_gpu_summary() -> str:
    """Return a human-readable GPU summary suitable for CLI / UI display.

    Examples::

        "NVIDIA GeForce RTX 4070 (12.0 GB VRAM) — driver 535.54.03 / CUDA 12.2"
        "2x GPU: NVIDIA A100 80GB PCIe (80.0 GB each, 160.0 GB total)"
        "No NVIDIA GPU detected (CPU mode)"
    """
    gpus = detect_gpus()

    if not gpus:
        return "No NVIDIA GPU detected (CPU mode)"

    if len(gpus) == 1:
        g = gpus[0]
        return (
            f"{g.name} ({g.vram_gb:.1f} GB VRAM) — "
            f"driver {g.driver_version} / CUDA {g.cuda_version}"
        )

    # Multi-GPU
    total_gb = sum(g.vram_gb for g in gpus)
    names = {g.name for g in gpus}
    if len(names) == 1:
        name = next(iter(names))
        return (
            f"{len(gpus)}x GPU: {name} "
            f"({gpus[0].vram_gb:.1f} GB each, {total_gb:.1f} GB total) — "
            f"driver {gpus[0].driver_version} / CUDA {gpus[0].cuda_version}"
        )
    # Mixed GPU types
    gpu_list = ", ".join(f"{g.name} ({g.vram_gb:.1f} GB)" for g in gpus)
    return f"{len(gpus)}x GPU: {gpu_list} — {total_gb:.1f} GB total VRAM"
