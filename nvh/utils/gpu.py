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
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from nvh.utils.hw_ids import is_gb10_name

# GB10 (DGX Spark / RTX Spark class) is the first NVIDIA part whose "VRAM" is the
# system's LPDDR5x pool.  The GPU shares it with the OS, so model budgets must
# leave headroom and must never add system RAM on top as a CPU-offload bonus.
UNIFIED_MEMORY_OS_RESERVE_GB = 16.0     # OS + WebUI + desktop headroom on a unified pool
UNIFIED_MEMORY_BANDWIDTH_GBPS = 273     # GB10 LPDDR5x, per NVIDIA's DGX Spark spec sheet


def is_unified_memory_gpu_name(name: str | None) -> bool:
    """True when the GPU name is a known unified-memory part (GB10 today).

    Public alias of :func:`nvh.utils.hw_ids.is_gb10_name`, the one shared GB10
    predicate. Its callers are inside this module — ``_resolve_memory_pool``
    (system RAM as the pool) and ``_parse_compute_capability`` (sm_121) — and
    the test suites; :mod:`nvh.utils.platform_facts` imports
    ``hw_ids.is_gb10_name`` directly. Kept under this name for API stability.
    """
    return is_gb10_name(name)


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
    # True when vram_* describe a CPU/GPU-shared pool (GB10 / DGX Spark). Set
    # only from the GPU name (nvh.utils.hw_ids.is_gb10_name) — a driver that
    # fails to report memory on a discrete GPU never flips this on.
    unified_memory: bool = False


@dataclass
class ModelRecommendation:
    model: str              # e.g. "qwen3:8b"
    reason: str             # e.g. "8GB+ VRAM available — good quality/speed balance"
    vram_required_gb: float
    tier: str               # "mini", "small", "full", "multi-gpu"
    note: str = ""          # optional platform note (e.g. unified-memory bandwidth guidance)


def _append_gpu_issue(
    issues: list[dict[str, Any]] | None,
    *,
    source: str,
    code: str,
    message: str,
    severity: str = "warning",
    detail: str = "",
    index: int | None = None,
) -> None:
    if issues is None:
        return
    issue: dict[str, Any] = {
        "source": source,
        "code": code,
        "message": message,
        "severity": severity,
        "detail": detail[:300],
    }
    if index is not None:
        # Row-scoped issues (``memory-unavailable``) carry the GPU index so a
        # consumer can pair the issue with its row without parsing the message.
        issue["index"] = index
    issues.append(issue)


def _nvidia_device_files_present() -> bool:
    try:
        dev = Path("/dev")
        return (dev / "nvidiactl").exists() or any(dev.glob("nvidia[0-9]*"))
    except Exception:
        return False


def _system_ram_pool_mb() -> tuple[int, int, int]:
    """(total_mb, used_mb, free_mb) of system RAM, for unified-memory GPUs.

    GB10 exposes no dedicated VRAM, so the LPDDR5x pool *is* the GPU memory.
    ``free_mb`` comes from ``MemAvailable`` (via :func:`detect_system_memory`)
    rather than CUDA/NVML's free figure, which ignores reclaimable page cache
    on a unified pool — NVIDIA's DGX Spark guidance for "how much can I load".
    Returns zeros when RAM cannot be measured.
    """
    try:
        mem = detect_system_memory()
    except Exception:
        return 0, 0, 0
    total_mb = int(mem.total_ram_gb * 1024)
    free_mb = int(mem.available_ram_gb * 1024) if mem.available_ram_gb else total_mb
    return total_mb, max(total_mb - free_mb, 0), free_mb


def _resolve_memory_pool(
    name: str,
    index: int,
    read_driver_pool: Callable[[], tuple[int, int, int]],
    *,
    source: str,
    issues: list[dict[str, Any]] | None,
) -> tuple[tuple[int, int, int], bool]:
    """``((total_mb, used_mb, free_mb), unified)`` for one GPU row.

    One rule for both detection paths:

    * Discrete GPUs: the driver's own (total, used, free) is the pool. A read
      that raises or reports 0 MiB means the row cannot be sized — it is never
      padded with system RAM.
    * Unified-memory GPUs (GB10, by name): system RAM *is* the pool —
      ``MemTotal`` is the honest total and ``MemAvailable`` the honest "how much
      can I load" (the driver prints ``[N/A]``, or a free figure that ignores
      reclaimable page cache). If RAM cannot be measured the driver's figures
      are used when it printed any.

    A row that cannot be sized comes back as ``((0, 0, 0), False)`` with a
    ``memory-unavailable`` issue recorded (carrying the row's ``index``), and
    the caller **keeps** it: the GPU is visible, and its *name* is what
    :func:`nvh.utils.platform_facts.classify` keys on — a visible non-GB10 GPU
    on a DGX-OS arm64 box is not a Spark — so dropping the row before the
    classifier saw it turned a Grace Hopper node with an unreadable memory
    cell into ``dgx-spark`` / unified. What must not happen instead is
    treating the row as ready: :func:`detect_gpu_status` reports ``blocked``
    when *no* row is sized, names the unreadable rows in ``summary`` when
    other rows are, and the budget helpers count only sized rows — an
    unreadable row is never system RAM, never a tier, never a GPU Ollama can
    use.

    ``read_driver_pool`` is only invoked when needed, so a GB10 with readable
    RAM never touches the driver's memory query.
    """
    unified = is_unified_memory_gpu_name(name)
    pool: tuple[int, int, int] | None = _system_ram_pool_mb() if unified else None
    detail = ""
    if pool is None or pool[0] <= 0:
        try:
            pool = read_driver_pool()
        except Exception as exc:
            pool, detail = None, str(exc)
    if pool is not None and pool[0] > 0:
        return pool, unified

    if unified:
        message = (
            f"{name} (GPU {index}) shares system RAM with the CPU, "
            "but system RAM could not be measured."
        )
    else:
        message = f"{source} could not report memory for {name} (GPU {index})."
    _append_gpu_issue(
        issues,
        source=source,
        code="memory-unavailable",
        message=message,
        detail=detail or "No memory figures were reported; the GPU is listed at 0 GB as memory unreadable.",
        index=index,
    )
    return (0, 0, 0), False


def _sized_rows(gpus: list[GPUInfo]) -> list[GPUInfo]:
    """Rows with a readable memory pool — the only rows anything may budget against."""
    return [g for g in gpus if g.vram_mb > 0]


def _unsized_rows(gpus: list[GPUInfo]) -> list[GPUInfo]:
    """Rows whose memory pool could not be read (``_resolve_memory_pool`` gave 0 MiB)."""
    return [g for g in gpus if g.vram_mb <= 0]


def _primary_row(gpus: list[GPUInfo]) -> GPUInfo | None:
    """The row that architecture and memory-model decisions key on.

    The first *sized* row when there is one: a 0 GB row's ``unified_memory``
    is always False, so letting it lead would budget a GB10 listed behind it
    as a discrete card (and add a CPU-offload bonus on top of its own RAM). A
    lone unreadable GPU still has a real name and compute capability, so it
    stays the primary for architecture guidance when nothing is sized.
    """
    sized = _sized_rows(gpus)
    if sized:
        return sized[0]
    return gpus[0] if gpus else None


def _ready_label(sized: list[GPUInfo]) -> str:
    """``"NVIDIA GB10, 128 GB unified"`` / ``"NVIDIA A100 80GB PCIe x2, 160 GB VRAM total"``."""
    primary = sized[0]
    pool = "unified" if primary.unified_memory else "VRAM"
    if len(sized) == 1:
        return f"{primary.name}, {primary.vram_gb:.0f} GB {pool}"
    total_gb = sum(g.vram_gb for g in sized)
    names = {g.name for g in sized}
    label = f"{primary.name} x{len(sized)}" if len(names) == 1 else ", ".join(sorted(names))
    return f"{label}, {total_gb:.0f} GB {pool} total"


def _gpu_status_summary(status: str, gpus: list[GPUInfo], issues: list[dict[str, Any]]) -> str:
    """One line a human (or the Wizard prompt) can read without parsing issues."""
    sized = _sized_rows(gpus)
    unsized = _unsized_rows(gpus)
    unreadable = ", ".join(f"{g.name} (GPU {g.index})" for g in unsized)
    if gpus and not sized:
        reason = next((i.get("message", "") for i in issues if i.get("code") == "memory-unavailable"), "")
        if len(gpus) == 1:
            head = f"1 GPU visible but its memory could not be read: {unreadable}"
        else:
            head = f"{len(gpus)} GPUs visible but memory unreadable: {unreadable}"
        return head + (f" — {reason}" if reason else "")
    if sized:
        ready = _ready_label(sized)
        if unsized:
            # Usable machine with a bad row: say which GPU is out, and size only the rest.
            return f"{len(sized)} of {len(gpus)} GPUs ready: {ready}; memory unreadable: {unreadable}"
        if len(sized) == 1:
            return f"1 GPU ready: {ready}"
        return f"{len(sized)} GPUs ready: {ready}"
    if status == "blocked":
        return "NVIDIA device files present but the GPU could not be queried (NVML and nvidia-smi blocked)"
    if status == "unavailable":
        detail = next(
            (i.get("message", "") for i in issues if i.get("severity") not in (None, "info")),
            "",
        )
        return "nvidia-smi is present but reported no usable GPU" + (f" ({detail})" if detail else "")
    return "no NVIDIA GPU detected (nvidia-smi missing)"


def format_gpu_memory(gpu: GPUInfo, *, precision: int = 0, compact: bool = False) -> str:
    """One spelling of a GPU row's memory pool for CLI / UI labels.

    ``"24 GB VRAM"`` for a discrete card, ``"128 GB unified"`` for a GB10 (the
    pool is the system's LPDDR5x, shared with the OS), and
    ``"memory unreadable"`` for a row :func:`detect_gpu_status` kept at 0 GB
    because its pool could not be read — never ``"0 GB VRAM"``, which reads as
    a real, empty card while the recommender says there is no VRAM to budget.
    ``precision`` is the number of decimals (``1`` → ``"24.0 GB VRAM"``);
    ``compact=True`` drops the space before the unit (``"24GB VRAM"``) for the
    dense one-line CLI rows.
    """
    if gpu.vram_mb <= 0:
        return "memory unreadable"
    amount = f"{gpu.vram_gb:.{precision}f}{'' if compact else ' '}GB"
    return f"{amount} {'unified' if gpu.unified_memory else 'VRAM'}"


# nvidia-smi fallback memo: (monotonic timestamp, rows, issues). detect_gpu_status
# runs the fallback whenever NVML sized nothing — so on a box where NVML
# enumerates the GPU but cannot read its memory (or is not installed) every
# status call used to spawn three processes: the row query, bare ``nvidia-smi``
# for the CUDA header, and ``--query-gpu=compute_cap``. Status is polled in
# bursts (dashboard, Wizard turn, /v1/system/info right after /v1/system/gpu),
# so the result is kept for a few seconds; the first call in a burst is exactly
# the old call. Process-wide: the GPU set does not change between polls.
SMI_FALLBACK_TTL_S = 5.0
_smi_fallback_lock = threading.Lock()
_smi_fallback_cache: tuple[float, list[GPUInfo], list[dict[str, Any]]] | None = None


def clear_gpu_detection_cache() -> None:
    """Forget the memoised nvidia-smi fallback (tests; after a driver reload)."""
    global _smi_fallback_cache
    with _smi_fallback_lock:
        _smi_fallback_cache = None


def _smi_fallback_cached(issues: list[dict[str, Any]]) -> list[GPUInfo]:
    """:func:`_detect_gpus_smi` with its rows *and* issues memoised for :data:`SMI_FALLBACK_TTL_S`.

    The lock is held across the subprocesses so concurrent callers in a burst
    coalesce onto one spawn instead of racing to fill the memo. Callers get
    their own row objects and issue dicts; the memo is never handed out.
    """
    global _smi_fallback_cache
    with _smi_fallback_lock:
        now = time.monotonic()
        cached = _smi_fallback_cache
        if cached is None or now - cached[0] >= SMI_FALLBACK_TTL_S:
            fresh_issues: list[dict[str, Any]] = []
            rows = _detect_gpus_smi(issues=fresh_issues)
            cached = _smi_fallback_cache = (now, rows, fresh_issues)
        issues.extend(dict(issue) for issue in cached[2])
        return [replace(row) for row in cached[1]]


def detect_gpu_status() -> dict[str, Any]:
    """Detect NVIDIA GPUs and preserve rootless failure details for nvWizard.

    Returns ``{status, source, gpus, issues, device_files_present, nvidia_smi,
    summary}`` where ``summary`` is a one-line human-readable digest such as
    ``"1 GPU ready: NVIDIA GB10, 128 GB unified"`` or ``"no NVIDIA GPU detected
    (nvidia-smi missing)"``.

    ``status`` is ``ready`` when at least one row has a readable memory pool —
    the machine is usable on its sized GPUs, exactly as when detection used to
    drop the bad row. A visible GPU whose memory could not be read stays in
    ``gpus`` at 0 GB, with a ``memory-unavailable`` issue carrying its
    ``index``, and is named in ``summary`` as memory unreadable; the budget
    helpers count only sized rows. ``blocked`` means GPUs are visible but *no*
    row could be sized (or device files exist and nothing could be queried) —
    so the platform classifier still sees every GPU's name while nothing
    budgets against a pool that was never measured.
    """
    nvml_issues: list[dict[str, Any]] = []
    smi_issues: list[dict[str, Any]] = []
    gpus = _detect_gpus_pynvml(issues=nvml_issues)
    source = "pynvml" if gpus else ""
    if not _sized_rows(gpus):
        # NVML sized nothing (no rows, or only unreadable ones): nvidia-smi gets
        # a turn. Its rows win when NVML had none, or when they carry a pool.
        # The fallback is three subprocesses; within a burst of status calls
        # (dashboard poll, Wizard turn, /v1/system/info) it runs once.
        smi_gpus = _smi_fallback_cached(smi_issues)
        if smi_gpus and (not gpus or _sized_rows(smi_gpus)):
            gpus, source = smi_gpus, "nvidia-smi"
    # Row-scoped issues describe the rows of the source that produced them, and
    # only the winning source's rows are returned — so only its
    # ``memory-unavailable`` entries are kept. A losing source's memory warnings
    # would otherwise sit next to rows nvidia-smi *did* size, or double up per
    # GPU when both sources failed. Source-level issues (module missing, init
    # failed, binary missing, timeout) always stay: they explain the fallback.
    issues = [
        issue
        for issue in nvml_issues + smi_issues
        if issue.get("code") != "memory-unavailable" or issue.get("source") == source
    ]

    device_files_present = _nvidia_device_files_present()
    if _sized_rows(gpus):
        # At least one pool is readable: ready on those rows. Unreadable rows
        # stay in the list at 0 GB, named in issues and summary.
        status = "ready"
    elif gpus:
        # Visible but nothing sized: the GPUs enumerate (their names are real
        # and feed the platform classifier) but nothing can be budgeted against
        # them, so it is not "ready" — recommend_models / check_oom_risk see 0 GB.
        status = "blocked"
    elif device_files_present:
        status = "blocked"
        _append_gpu_issue(
            issues,
            source="linux-devices",
            code="devices-present-no-query",
            message="NVIDIA device files exist, but nvHive could not query the GPU.",
            detail="The base image, driver permissions, or session policy may be blocking NVML and nvidia-smi.",
        )
    elif shutil.which("nvidia-smi"):
        status = "unavailable"
    else:
        status = "not-detected"

    return {
        "status": status,
        "source": source or "none",
        "gpus": gpus,
        "issues": issues,
        "device_files_present": device_files_present,
        "nvidia_smi": shutil.which("nvidia-smi") or "",
        "summary": _gpu_status_summary(status, gpus, issues),
    }


def detect_gpus() -> list[GPUInfo]:
    """Detect NVIDIA GPUs. Tries pynvml first (fast, rich data), falls back to nvidia-smi.

    Returns a list of GPUInfo objects — one per GPU.  Returns an empty list if
    no NVIDIA GPU is found or accessible.
    """
    return detect_gpu_status()["gpus"]

def _detect_gpus_pynvml(*, issues: list[dict[str, Any]] | None = None) -> list[GPUInfo]:
    """Detect GPUs via pynvml (NVML Python bindings)."""
    try:
        import pynvml
    except ImportError:
        _append_gpu_issue(
            issues,
            source="pynvml",
            code="module-missing",
            message="Python NVML bindings are not installed.",
            severity="info",
        )
        return []  # pynvml not installed — fall back to nvidia-smi

    try:
        pynvml.nvmlInit()
    except Exception as exc:
        _append_gpu_issue(
            issues,
            source="pynvml",
            code="nvml-init-failed",
            message="NVML could not initialize in this session.",
            detail=str(exc),
        )
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
        if device_count == 0:
            _append_gpu_issue(
                issues,
                source="pynvml",
                code="no-devices",
                message="NVML initialized but reported zero NVIDIA GPUs.",
                severity="info",
            )
            return []
        gpus: list[GPUInfo] = []

        for i in range(device_count):
            handle = pynvml.nvmlDeviceGetHandleByIndex(i)

            name = pynvml.nvmlDeviceGetName(handle)
            if isinstance(name, bytes):
                name = name.decode()

            def _nvml_memory_pool(handle=handle) -> tuple[int, int, int]:
                mem_info = pynvml.nvmlDeviceGetMemoryInfo(handle)
                mib = 1024 * 1024
                return mem_info.total // mib, mem_info.used // mib, mem_info.free // mib

            # An unreadable pool keeps the row at 0 GB (issue recorded) so the
            # GPU's name still reaches the platform classifier; detect_gpu_status
            # reports it as blocked, and nvidia-smi gets a turn if nothing sized.
            (vram_mb, memory_used, memory_free), unified = _resolve_memory_pool(
                name, i, _nvml_memory_pool, source="pynvml", issues=issues,
            )

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
                unified_memory=unified,
            ))

        return gpus
    except Exception as exc:
        _append_gpu_issue(
            issues,
            source="pynvml",
            code="nvml-query-failed",
            message="NVML failed while reading GPU details.",
            detail=str(exc),
        )
        return []
    finally:
        try:
            pynvml.nvmlShutdown()
        except Exception:
            pass


def _detect_gpus_smi(*, issues: list[dict[str, Any]] | None = None) -> list[GPUInfo]:
    """Fallback: detect GPUs via nvidia-smi subprocess."""
    nvidia_smi = shutil.which("nvidia-smi")
    if not nvidia_smi:
        _append_gpu_issue(
            issues,
            source="nvidia-smi",
            code="binary-missing",
            message="nvidia-smi is not on PATH.",
            severity="info",
        )
        return []
    try:
        result = subprocess.run(
            [
                nvidia_smi,
                "--query-gpu=index,name,memory.total,memory.used,memory.free,"
                "utilization.gpu,driver_version",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except subprocess.TimeoutExpired:
        _append_gpu_issue(
            issues,
            source="nvidia-smi",
            code="timeout",
            message="nvidia-smi timed out while querying GPUs.",
        )
        return []
    except Exception as exc:
        _append_gpu_issue(
            issues,
            source="nvidia-smi",
            code="command-error",
            message="nvidia-smi could not run.",
            detail=str(exc),
        )
        return []

    if result.returncode != 0:
        _append_gpu_issue(
            issues,
            source="nvidia-smi",
            code="nonzero-exit",
            message="nvidia-smi returned an error.",
            detail=(result.stderr or result.stdout or "").strip(),
        )
        return []
    if not result.stdout.strip():
        _append_gpu_issue(
            issues,
            source="nvidia-smi",
            code="empty-output",
            message="nvidia-smi returned no GPU rows.",
            severity="info",
        )
        return []

    cuda_ver = _get_cuda_version()
    compute_caps = _get_compute_capabilities()

    gpus: list[GPUInfo] = []
    for row_index, line in enumerate(result.stdout.strip().splitlines()):
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 7:
            continue
        try:
            index         = int(parts[0])
            name          = parts[1]
            utilization   = int(float(re.sub(r"[^\d.]", "", parts[5]) or "0"))
            driver_ver    = parts[6]

            # memory.* cells are "[N/A]" on GB10 (the pool comes from system RAM)
            # and numeric on discrete GPUs; a discrete row with no readable
            # total is kept at 0 GB with an issue (status 'blocked'), never
            # listed as ready.
            (vram_mb, memory_used, memory_free), unified = _resolve_memory_pool(
                name,
                index,
                lambda cells=parts[2:5]: tuple(_smi_int(c) for c in cells),
                source="nvidia-smi",
                issues=issues,
            )
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
                    compute_capability=compute_caps[row_index] if row_index < len(compute_caps) else (0, 0),
                    unified_memory=unified,
                )
            )
        except (ValueError, IndexError):
            continue

    return gpus


def _smi_int(value: str) -> int:
    """Parse an nvidia-smi numeric cell; ``[N/A]``/``[Not Supported]`` become 0."""
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def _parse_compute_capability_value(value: str) -> tuple[int, int]:
    match = re.search(r"(\d+)(?:\.(\d+))?", value.strip())
    if not match:
        return (0, 0)
    return (int(match.group(1)), int(match.group(2) or 0))


def _get_compute_capabilities() -> list[tuple[int, int]]:
    nvidia_smi = shutil.which("nvidia-smi")
    if not nvidia_smi:
        return []
    try:
        result = subprocess.run(
            [nvidia_smi, "--query-gpu=compute_cap", "--format=csv,noheader"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception:
        return []
    if result.returncode != 0:
        return []
    return [
        cc
        for cc in (_parse_compute_capability_value(line) for line in result.stdout.splitlines())
        if cc != (0, 0)
    ]


def _get_cuda_version() -> str:
    """Return CUDA version string reported by nvidia-smi, or 'unknown'."""
    try:
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


@dataclass(frozen=True)
class MemoryBudget:
    """How much memory model recommendations may plan against.

    On discrete GPUs ``model_budget_gb`` is the raw VRAM and ``combined_gb``
    adds a capped CPU-offload bonus. On unified-memory parts (GB10 / DGX
    Spark) the pool is shared with the OS, so the budget is RAM minus
    :data:`UNIFIED_MEMORY_OS_RESERVE_GB` and no offload bonus is added — the
    same bytes must not be counted twice.
    """

    total_vram_gb: float
    model_budget_gb: float
    cpu_offload_gb: float
    combined_gb: float
    unified_memory: bool


def _memory_budget(gpus: list[GPUInfo], sys_mem: SystemMemoryInfo) -> MemoryBudget:
    # Only rows with a readable pool count; an unreadable row is 0 GB and must
    # not decide the memory model either (see _primary_row).
    sized = _sized_rows(gpus)
    total_vram_gb = sum(g.vram_mb for g in sized) / 1024
    unified = bool(sized) and bool(getattr(sized[0], "unified_memory", False))
    if unified:
        budget = max(total_vram_gb - UNIFIED_MEMORY_OS_RESERVE_GB, 0.0)
        return MemoryBudget(
            total_vram_gb=total_vram_gb,
            model_budget_gb=budget,
            cpu_offload_gb=0.0,
            combined_gb=budget,
            unified_memory=True,
        )
    # Factor in system RAM for CPU offload — but be conservative.
    # On gaming/student rigs RAM is often 16-32GB, and the OS + apps use ~4-6GB.
    # CPU offloaded layers are 5-10x slower than GPU, so only helpful for
    # "barely doesn't fit" scenarios, not as a primary strategy.
    cpu_offload_gb = min(sys_mem.effective_for_llm_gb, 16.0)  # cap benefit at 16GB
    return MemoryBudget(
        total_vram_gb=total_vram_gb,
        model_budget_gb=total_vram_gb,
        cpu_offload_gb=cpu_offload_gb,
        combined_gb=total_vram_gb + cpu_offload_gb,
        unified_memory=False,
    )


def unified_memory_note(budget: MemoryBudget) -> str:
    """Bandwidth guidance for a unified-memory (GB10-class) machine."""
    return (
        f"Unified memory: {budget.total_vram_gb:.0f} GB LPDDR5x shared by CPU and GPU at "
        f"~{UNIFIED_MEMORY_BANDWIDTH_GBPS} GB/s; ~{UNIFIED_MEMORY_OS_RESERVE_GB:.0f} GB is reserved "
        f"for the OS and WebUI, leaving ~{budget.model_budget_gb:.0f} GB for models. "
        "Dense 70B models fit but are bandwidth-bound; MoE models such as nemotron3:33b "
        "or gpt-oss:120b are the better fit. System RAM is not an extra CPU-offload pool here."
    )


def recommend_models(gpus: list[GPUInfo] | None = None) -> list[ModelRecommendation]:
    """Recommend local models based on available GPU VRAM.

    Recommends the strongest pullable local model first, then smaller
    multimodal, coding, and fallback models for resilient local routing.

    Tier rules (total VRAM across all GPUs):
    - No GPU or < 4 GB : lightweight CPU fallback
    - 4-8 GB           : gemma3:4b + moondream
    - 8-12 GB          : qwen3:8b / llama3.1:8b + small vision fallback
    - 12-24 GB         : qwen3:8b + MiniCPM-V + coding fallback
    - 24-40 GB         : llama3.2-vision + qwen/coder fallbacks
    - 40 GB+           : nemotron first, then multimodal and coding fallbacks
    - Multi-GPU        : Ollama uses all GPUs automatically

    Unified memory (GB10 / DGX Spark): the tiers above are applied to the
    shared pool minus :data:`UNIFIED_MEMORY_OS_RESERVE_GB`, no CPU-offload
    bonus is added, hybrid tiers are skipped, and the primary recommendation
    carries a ``note`` explaining the bandwidth trade-off.

    Rows whose memory could not be read (0 GB, ``memory-unavailable``) are not
    GPUs Ollama can load onto: VRAM totals and the multi-GPU note count only
    sized rows, exactly as when detection dropped such rows.
    """
    if gpus is None:
        gpus = detect_gpus()

    sized_gpus = _sized_rows(gpus)
    multi_gpu = len(sized_gpus) > 1
    sys_mem = detect_system_memory()
    budget = _memory_budget(gpus, sys_mem)
    # Tiering below runs against the model budget: identical to raw VRAM on
    # discrete GPUs, RAM minus the OS reserve on unified-memory parts.
    total_vram_gb = budget.model_budget_gb
    cpu_offload_gb = budget.cpu_offload_gb

    recommendations: list[ModelRecommendation] = []

    # NOTE on Nemotron sizes: Ollama's registry currently only ships
    #   nemotron-mini (4B)  and  nemotron / nemotron:70b  (70B)
    # Keep this list to pullable registry-backed tags.
    # Earlier versions of this function referenced both — pull attempts
    # returned 404 and the resulting "model not found" error from litellm
    # was misdiagnosed as "Ollama is not running". For mid-tier VRAM where
    # mid-tier GPUs should use real 8B/coder/vision models instead.
    if not gpus or total_vram_gb < 4:
        # CPU-only: system RAM is the constraint
        if sys_mem.available_ram_gb >= 8:
            recommendations.append(
                ModelRecommendation(
                    model="llama3.1:8b",
                    reason=(
                        f"No GPU but {sys_mem.available_ram_gb:.0f} GB RAM available — "
                        f"llama3.1:8b runs on CPU (slow but functional)"
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
                model="gemma3:4b",
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
                model="gemma3:4b",
                reason="Gemma 4 E4B — NVIDIA-optimized, fits alongside nemotron-mini",
                vram_required_gb=3.0,
                tier="mini",
            )
        )
    elif total_vram_gb < 12:
        recommendations.append(
            ModelRecommendation(
                model="qwen3:8b",
                reason=(
                    f"{total_vram_gb:.0f} GB VRAM — llama3.1:8b is the sweet spot"
                    " (no mid-sized Nemotron exists in the Ollama registry)"
                ),
                vram_required_gb=5.0,
                tier="small",
            )
        )
        recommendations.append(
            ModelRecommendation(
                model="gemma3:4b",
                reason="Gemma 4 E4B — pairs with llama3.1:8b for local council",
                vram_required_gb=3.0,
                tier="small",
            )
        )
    elif total_vram_gb < 24:
        recommendations.append(
            ModelRecommendation(
                model="minicpm-v",
                reason=(
                    f"{total_vram_gb:.0f} GB VRAM — llama3.1:8b gives good"
                    " quality/speed balance at this tier"
                ),
                vram_required_gb=5.0,
                tier="small",
            )
        )
        if total_vram_gb >= 20:
            recommendations.append(
                ModelRecommendation(
                    model="qwen3:8b",
                    reason=(
                        "Gemma 4 26B — NVIDIA-optimized"
                        " reasoning + code + multimodal"
                    ),
                    vram_required_gb=6.0,
                    tier="small",
                )
            )
        else:
            recommendations.append(
                ModelRecommendation(
                    model="qwen2.5-coder:7b",
                    reason=(
                        "Gemma 4 E4B — fits alongside"
                        " llama3.1:8b for local council"
                    ),
                    vram_required_gb=5.0,
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
                    model="llama3.2-vision",
                    reason=(
                        "Gemma 4 26B — fits alongside"
                        " Nemotron 70B for local council"
                    ),
                    vram_required_gb=7.0,
                    tier="vision",
                )
            )
        else:
            recommendations.append(
                ModelRecommendation(
                    model="qwen3:8b",
                    reason=(
                        "Gemma 4 E4B — lightweight"
                        " companion for local council"
                    ),
                    vram_required_gb=6.0,
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
    else:
        # 80GB+ VRAM. Note: Ollama's registry currently tops out at
        # keep this tier on pullable tags and pair the primary model with
        # multimodal and coding fallbacks for a stronger local council.
        recommendations.append(
            ModelRecommendation(
                model="nemotron",
                reason=(
                    f"{total_vram_gb:.0f} GB VRAM — Nemotron 70B "
                    "(largest Nemotron available in the Ollama registry)"
                ),
                vram_required_gb=40.0,
                tier="full",
            )
        )
        recommendations.append(
            ModelRecommendation(
                model="qwen2.5-coder:32b",
                reason=(
                    "Gemma 4 31B — pairs with Nemotron 70B for a strong"
                    " local council at high-VRAM tiers"
                ),
                vram_required_gb=20.0,
                tier="medium",
            )
        )

    # Check if CPU offload could unlock a larger model
    # Only suggest if the model is "close" (within CPU offload budget).
    # Skipped on unified memory: there is no second pool to offload into.
    combined_gb = budget.combined_gb
    if recommendations and not budget.unified_memory:
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
        # Qwen 2.5 Coder 32B fits as a hybrid on mid-VRAM tiers (~20GB VRAM + CPU)
        elif top_tier in ("full",) and combined_gb >= 35 and total_vram_gb >= 24:
            recommendations.append(
                ModelRecommendation(
                    model="qwen2.5-coder:32b",
                    reason=(
                        f"Partial CPU offload: {total_vram_gb:.0f} GB VRAM + "
                        f"{cpu_offload_gb:.0f} GB RAM = {combined_gb:.0f} GB combined. "
                        f"Qwen 2.5 Coder 32B fits for diversified council"
                    ),
                    vram_required_gb=20.0,
                    tier="medium-hybrid",
                )
            )

    if multi_gpu and recommendations:
        last = recommendations[0]
        recommendations[0] = ModelRecommendation(
            model=last.model,
            reason=last.reason + f" (Ollama will use all {len(sized_gpus)} GPUs automatically)",
            vram_required_gb=last.vram_required_gb,
            tier="multi-gpu",
        )

    if budget.unified_memory and recommendations:
        primary = recommendations[0]
        primary.reason += (
            f" (unified memory: ~{budget.model_budget_gb:.0f} GB of "
            f"{budget.total_vram_gb:.0f} GB usable after the OS reserve)"
        )
        primary.note = unified_memory_note(budget)

    # Vision model — appended so the desktop-agent screenshot path works.
    # Ollama loads models on demand, so co-residency with the text models is
    # not required (Ollama will swap them in/out as needed).
    vision_rec = _recommend_vision_model(total_vram_gb, gpus)
    if vision_rec is not None:
        recommendations.append(vision_rec)

    return recommendations


def _recommend_vision_model(
    total_vram_gb: float,
    gpus: list[GPUInfo],
) -> ModelRecommendation | None:
    """Pick a vision-capable Ollama model appropriate for the GPU tier.

    Tier rules (approximate Q4 sizes):
      - < 4 GB   : skip (cloud vision only)
      - 4 – 12 GB: moondream (~2 GB) — basic vision, fits alongside small text
      - 12 – 24  : minicpm-v (~5 GB) — better quality, still light
      - 24 – 80  : llama3.2-vision (~7 GB, 11B) — strong spatial grounding
      - 80 GB+   : llama3.2-vision (safer than 90B which is flaky in Ollama)

    Architecture swap: on Turing (CC < 8.0), llama3.2-vision uses BF16
    paths that degrade badly, so prefer minicpm-v there.
    """
    if total_vram_gb < 4:
        return None

    # Compute capability for arch-aware swap. Use the primary (first sized) GPU.
    primary = _primary_row(gpus)
    cc = gpu_architecture_info(primary)["compute_capability"] if primary is not None else (0, 0)
    turing_or_older = cc < (8, 0) and cc != (0, 0)

    if total_vram_gb < 12:
        return ModelRecommendation(
            model="moondream",
            reason="moondream — 2 GB vision model, fits alongside small text models",
            vram_required_gb=2.0,
            tier="vision-mini",
        )
    if total_vram_gb < 24:
        return ModelRecommendation(
            model="minicpm-v",
            reason="minicpm-v — 5 GB vision model, good quality for the tier",
            vram_required_gb=5.0,
            tier="vision-small",
        )
    # 24 GB+ tier — prefer llama3.2-vision unless Turing
    if turing_or_older:
        return ModelRecommendation(
            model="minicpm-v",
            reason=(
                "minicpm-v — Turing lacks BF16 acceleration, "
                "so llama3.2-vision would be slow here"
            ),
            vram_required_gb=5.0,
            tier="vision-small",
        )
    return ModelRecommendation(
        model="llama3.2-vision",
        reason="llama3.2-vision — 7 GB, best spatial grounding for desktop agent",
        vram_required_gb=7.0,
        tier="vision",
    )


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

    if total_gb == 0 and sys.platform == "win32":
        try:
            # Windows (incl. Windows on Arm / RTX Spark): GlobalMemoryStatusEx
            # is the only dependency-free way to read physical RAM.
            import ctypes

            class _MemoryStatusEx(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            stat = _MemoryStatusEx()
            stat.dwLength = ctypes.sizeof(stat)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
                total_gb = stat.ullTotalPhys / (1024 ** 3)
                avail_gb = stat.ullAvailPhys / (1024 ** 3)
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
      fits_hybrid: bool — model fits with CPU offload (never True on unified memory)
      gpu_free_gb: float — current free VRAM (MemAvailable on a unified pool)
      ram_free_gb: float — current free system RAM; 0 on unified memory, where
                   the bytes are already counted in gpu_free_gb
      unified_memory: bool — True when the GPU shares the system RAM pool
      recommendation: str — what to do
    """
    if gpus is None:
        gpus = detect_gpus()

    unified = any(getattr(g, "unified_memory", False) for g in gpus)

    # GPU free VRAM (use memory_free_mb from nvidia-smi)
    gpu_free_gb = sum(g.memory_free_mb for g in gpus) / 1024 if gpus else 0.0
    # Reserve 15% for KV cache and overhead
    gpu_usable_gb = gpu_free_gb * 0.85

    if unified:
        # One pool: gpu_free_gb above already *is* system RAM (MemAvailable), so
        # there is no second pool to spill into and nothing extra to report.
        ram_free_gb = ram_usable_gb = 0.0
    else:
        sys_mem = detect_system_memory()
        ram_free_gb = sys_mem.available_ram_gb
        ram_usable_gb = sys_mem.effective_for_llm_gb

    result = {
        "safe": False,
        "fits_gpu": False,
        "fits_hybrid": False,
        "gpu_free_gb": round(gpu_free_gb, 1),
        "ram_free_gb": round(ram_free_gb, 1),
        "unified_memory": unified,
        "recommendation": "",
    }

    if model_vram_gb <= gpu_usable_gb:
        result["safe"] = True
        result["fits_gpu"] = True
        pool = "unified memory" if unified else "GPU VRAM"
        result["recommendation"] = (
            f"Model fits in {pool} ({model_vram_gb:.0f} GB needed, "
            f"{gpu_free_gb:.0f} GB free) — full GPU acceleration"
        )
    elif not unified and model_vram_gb <= gpu_usable_gb + ram_usable_gb:
        result["safe"] = True
        result["fits_hybrid"] = True
        overflow = model_vram_gb - gpu_usable_gb
        result["recommendation"] = (
            f"Model needs hybrid mode: {gpu_usable_gb:.0f} GB on GPU + "
            f"{overflow:.0f} GB on CPU RAM. Expect 30-50% slower than full GPU"
        )
    elif unified:
        needed = model_vram_gb - gpu_usable_gb
        result["recommendation"] = (
            f"OOM RISK: Model needs {model_vram_gb:.0f} GB but only "
            f"{gpu_usable_gb:.0f} GB of the unified memory pool is available "
            "(system RAM is the GPU memory — no CPU-offload headroom). "
            f"Short by {needed:.0f} GB. Use a smaller model or lower quantization"
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


def architecture_from_compute_capability(cc: tuple[int, int]) -> str:
    if cc >= (10, 0):
        # Datacenter/consumer Blackwell (10.x) and GB10 (12.1, sm_121 in CUDA 13).
        return "Blackwell"
    if cc >= (9, 0):
        return "Hopper"
    if cc >= (8, 9):
        return "Ada Lovelace"
    if cc >= (8, 0):
        return "Ampere"
    if cc >= (7, 5):
        return "Turing"
    return "Unknown"


def gpu_architecture_info(gpu: GPUInfo) -> dict[str, Any]:
    observed = gpu.compute_capability != (0, 0)
    cc = gpu.compute_capability if observed else _parse_compute_capability(gpu.name)
    return {
        "architecture": architecture_from_compute_capability(cc),
        "compute_capability": cc,
        "compute_capability_source": "nvml-or-smi" if observed else "name-heuristic",
        "heuristic": not observed,
    }


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

    # Use the primary GPU (first sized row; a lone unreadable row still names
    # its architecture) for architecture and memory-model decisions.
    gpu = _primary_row(gpus) or gpus[0]
    unified = bool(getattr(gpu, "unified_memory", False))
    total_vram_gb = sum(g.vram_mb for g in gpus) / 1024
    if unified:
        # Shared pool: size parallelism/context against what's left after the OS.
        total_vram_gb = max(total_vram_gb - UNIFIED_MEMORY_OS_RESERVE_GB, 0.0)
    arch_info = gpu_architecture_info(gpu)
    cc = arch_info["compute_capability"]

    notes: list[str] = []
    if arch_info["heuristic"]:
        notes.append("Compute capability is name-based; confirm after driver/NVML access improves.")
    if _unsized_rows(gpus):
        notes.append(
            "GPU memory could not be read (see detection issues) — parallelism and "
            "context are sized as if no VRAM were available."
        )

    # Flash Attention: CC >= 8.0 (Ampere+)
    flash_attention = cc >= (8, 0)
    if cc == (0, 0):
        notes.append("Compute capability unknown - using conservative attention settings")
    elif cc >= (9, 0):
        notes.append("Flash Attention 3 available (Hopper+)")
    elif cc >= (8, 0):
        notes.append("Flash Attention 2 enabled")
    else:
        notes.append("Flash Attention not supported (Turing) — using standard attention")

    # Architecture name
    if cc == (0, 0):
        arch = "Unknown"
        notes.append("Architecture could not be confirmed from NVML or the GPU name")
    elif cc >= (10, 0):
        arch = "Blackwell"
        notes.append("FP4 Tensor Cores available (not yet used by Ollama)")
        if unified:
            notes.append(
                f"Unified LPDDR5x memory (~{UNIFIED_MEMORY_BANDWIDTH_GBPS} GB/s) shared with the CPU "
                "— dense models are bandwidth-bound; MoE models run best"
            )
        else:
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

    # Quantization recommendation — the memory pool decides before the compute
    # tier does: a unified LPDDR5x pool is bandwidth-bound regardless of how
    # new the SMs are, so it must not inherit the Hopper/HBM "Q8_0 or F16" tier.
    if unified:
        quant = "Q4_K_M"
        notes.append(
            f"Q4_K_M on the unified pool — Q8_0/F16 would be bandwidth-bound at "
            f"~{UNIFIED_MEMORY_BANDWIDTH_GBPS} GB/s; MoE models (nemotron3:33b, gpt-oss:120b) first; "
            f"context {ctx} sized to the ~{total_vram_gb:.0f} GB left after the "
            f"{UNIFIED_MEMORY_OS_RESERVE_GB:.0f} GB OS reserve"
        )
    elif cc >= (9, 0) and total_vram_gb >= 80:
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
    if unified:
        notes.append(
            f"Unified memory: {sys_mem.total_ram_gb:.0f} GB shared by CPU and GPU — no separate "
            f"CPU-offload pool; keep ~{UNIFIED_MEMORY_OS_RESERVE_GB:.0f} GB free for the OS and WebUI"
        )
    elif sys_mem.total_ram_gb > 0:
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

    # GB10 — Grace Blackwell superchip in DGX Spark / RTX Spark. sm_121 in
    # CUDA 13 (unified LPDDR5x memory, not GDDR7).
    if is_unified_memory_gpu_name(name):
        return (12, 1)

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
    return (0, 0)


def get_gpu_summary(gpus: list[GPUInfo] | None = None) -> str:
    """Return a human-readable GPU summary suitable for CLI / UI display.

    Examples::

        "NVIDIA GeForce RTX 4070 (12.0 GB VRAM) — driver 535.54.03 / CUDA 12.2"
        "2x GPU: NVIDIA A100 80GB PCIe (80.0 GB each, 160.0 GB total)"
        "No NVIDIA GPU detected (CPU mode)"
    """
    if gpus is None:
        gpus = detect_gpus()

    if not gpus:
        return "No NVIDIA GPU detected (CPU mode)"

    if len(gpus) == 1:
        g = gpus[0]
        pool = f"{g.vram_gb:.1f} GB VRAM" if g.vram_mb > 0 else "memory unreadable"
        return f"{g.name} ({pool}) — driver {g.driver_version} / CUDA {g.cuda_version}"

    # Multi-GPU
    total_gb = sum(g.vram_gb for g in gpus)
    names = {g.name for g in gpus}
    sized = _sized_rows(gpus)
    unreadable = _unsized_rows(gpus)
    suffix = f" ({len(unreadable)} GPU(s) memory unreadable)" if unreadable else ""
    if len(names) == 1:
        name = next(iter(names))
        each_gb = sized[0].vram_gb if sized else 0.0
        return (
            f"{len(gpus)}x GPU: {name} "
            f"({each_gb:.1f} GB each, {total_gb:.1f} GB total) — "
            f"driver {gpus[0].driver_version} / CUDA {gpus[0].cuda_version}"
        ) + suffix
    # Mixed GPU types
    gpu_list = ", ".join(
        f"{g.name} ({g.vram_gb:.1f} GB)" if g.vram_mb > 0 else f"{g.name} (memory unreadable)"
        for g in gpus
    )
    return f"{len(gpus)}x GPU: {gpu_list} — {total_gb:.1f} GB total VRAM" + suffix
