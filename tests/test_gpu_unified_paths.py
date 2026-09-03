"""Unified-memory (GB10 / DGX Spark) vs discrete-GPU paths in nvh.utils.gpu.

Every test here runs against fakes — a fake ``pynvml`` module injected into
``sys.modules``, a faked ``nvidia-smi`` subprocess, and a patched
``detect_system_memory`` — so the host GPU is never consulted.

Regression coverage for the review findings on the Spark diff:

* G2 — only a GB10 *name* switches a pynvml row to the system-RAM pool; an
  NVML memory failure on an RTX/A100 no longer flips the stack into Spark mode.
* G3 — the nvidia-smi fallback tolerates ``[N/A]`` only for GB10, and a pool
  that cannot be measured is reported as an issue, never as a ready 0 GB GPU.
* G4 — GB10 (CC 12.1) gets unified-memory quantization guidance, not the
  Hopper/HBM "Q8_0 or F16" tier.
* G5 — one GB10 predicate (``nvh.utils.hw_ids.is_gb10_name``).
* G6 — ``detect_gpu_status()['summary']`` and ``recommend_models`` budgets on
  GB10 vs an x86 RTX 4090; picks, sizes, reasons and the num_ctx / num_parallel
  / quant ladder are derived from ``nvh.core.local_models`` so they cannot drift.
* R4 — a row whose pool cannot be read is *kept* (0 GB, ``memory-unavailable``
  carrying the row's ``index``) so its name still reaches the platform
  classifier; when *no* row is sized ``detect_gpu_status`` reports ``blocked``
  with a summary naming it — never ``ready, 0 GB``; the budget helpers treat
  the row as no usable VRAM.
* R5 — one unreadable row next to sized ones does not block the machine:
  status stays ``ready`` (as when detection dropped the bad row), the summary
  names it as memory unreadable, and VRAM totals / the multi-GPU note count
  only sized rows.
* R6 — detection issues are scoped to the source whose rows won: NVML's
  ``memory-unavailable`` warnings do not survive into a ``ready`` nvidia-smi
  result, and a GPU both sources fail to size is reported once, not twice.
* Reserve curve — a unified pool loses ``local_models.unified_os_reserve_gb``
  of itself (4 GB at 16 GB, 16 GB at 128 GB), not the GB10's flat 16 GB, so a
  16 GB pool keeps a 12 GB budget instead of none.
"""

from __future__ import annotations

import sys
import types
from dataclasses import replace
from types import SimpleNamespace

import pytest

from nvh.core import local_models as lm
from nvh.utils import gpu, hw_ids
from nvh.utils.gpu import GPUInfo, SystemMemoryInfo

GiB = 1024 ** 3

SPARK_RAM = SystemMemoryInfo(total_ram_gb=128.0, available_ram_gb=90.0, effective_for_llm_gb=63.0)
X86_RAM = SystemMemoryInfo(total_ram_gb=64.0, available_ram_gb=48.0, effective_for_llm_gb=33.6)


def _gpu(
    name: str,
    vram_mb: int,
    *,
    unified: bool = False,
    cc: tuple[int, int] = (0, 0),
    free_mb: int | None = None,
    index: int = 0,
) -> GPUInfo:
    return GPUInfo(
        name=name,
        vram_mb=vram_mb,
        vram_gb=round(vram_mb / 1024, 1),
        driver_version="580.65",
        cuda_version="13.0",
        utilization_pct=0,
        memory_used_mb=vram_mb - (free_mb if free_mb is not None else vram_mb),
        memory_free_mb=free_mb if free_mb is not None else vram_mb,
        index=index,
        compute_capability=cc,
        unified_memory=unified,
    )


def _ram_unreadable() -> SystemMemoryInfo:
    raise OSError("/proc/meminfo unreadable")


# ---------------------------------------------------------------------------
# Fake pynvml
# ---------------------------------------------------------------------------


def _fake_pynvml(devices: list[dict]) -> tuple[types.ModuleType, dict[str, int]]:
    """A ``pynvml`` stand-in. ``devices`` rows: ``{"name", "mem", "cc"}`` where
    ``mem`` is ``(total, used, free)`` in bytes or an Exception to raise."""
    mod = types.ModuleType("pynvml")
    calls = {"memory_info": 0}

    mod.NVML_TEMPERATURE_GPU = 0
    mod.NVML_CLOCK_GRAPHICS = 0
    mod.NVML_CLOCK_MEM = 1
    mod.nvmlInit = lambda: None
    mod.nvmlShutdown = lambda: None
    mod.nvmlSystemGetDriverVersion = lambda: "580.65"
    mod.nvmlSystemGetCudaDriverVersion_v2 = lambda: 13000
    mod.nvmlDeviceGetCount = lambda: len(devices)
    mod.nvmlDeviceGetHandleByIndex = lambda i: i
    mod.nvmlDeviceGetName = lambda h: devices[h]["name"]
    mod.nvmlDeviceGetUtilizationRates = lambda h: SimpleNamespace(gpu=devices[h].get("util", 0))
    mod.nvmlDeviceGetCudaComputeCapability = lambda h: devices[h]["cc"]

    def memory_info(h):
        calls["memory_info"] += 1
        mem = devices[h]["mem"]
        if isinstance(mem, Exception):
            raise mem
        return SimpleNamespace(total=mem[0], used=mem[1], free=mem[2])

    mod.nvmlDeviceGetMemoryInfo = memory_info

    def unsupported(*_a, **_k):
        raise RuntimeError("Not Supported")

    for fn in (
        "nvmlDeviceGetTemperature",
        "nvmlDeviceGetPowerUsage",
        "nvmlDeviceGetPowerManagementLimit",
        "nvmlDeviceGetClockInfo",
        "nvmlDeviceGetCurrPcieLinkGeneration",
        "nvmlDeviceGetCurrPcieLinkWidth",
        "nvmlDeviceGetComputeRunningProcesses",
    ):
        setattr(mod, fn, unsupported)
    return mod, calls


@pytest.fixture
def pynvml_with(monkeypatch):
    def install(devices: list[dict]) -> dict[str, int]:
        mod, calls = _fake_pynvml(devices)
        monkeypatch.setitem(sys.modules, "pynvml", mod)
        return calls

    return install


@pytest.fixture(autouse=True)
def _fresh_smi_memo():
    """detect_gpu_status memoises the nvidia-smi fallback for SMI_FALLBACK_TTL_S; each test starts cold."""
    gpu.clear_gpu_detection_cache()
    yield
    gpu.clear_gpu_detection_cache()


# ---------------------------------------------------------------------------
# G2 — pynvml path
# ---------------------------------------------------------------------------


def test_pynvml_gb10_uses_system_ram_pool(monkeypatch, pynvml_with) -> None:
    """(a) GB10 → unified; total = MemTotal, free = MemAvailable; NVML memory never asked."""
    monkeypatch.setattr(gpu, "detect_system_memory", lambda: SPARK_RAM)
    calls = pynvml_with([{"name": "NVIDIA GB10", "mem": RuntimeError("Not Supported"), "cc": (12, 1)}])
    issues: list[dict] = []

    gpus = gpu._detect_gpus_pynvml(issues=issues)

    assert len(gpus) == 1
    g = gpus[0]
    assert g.unified_memory is True
    assert g.vram_mb == 128 * 1024
    assert g.vram_gb == 128.0
    assert g.memory_free_mb == 90 * 1024          # MemAvailable, not NVML's free
    assert g.memory_used_mb == (128 - 90) * 1024
    assert g.compute_capability == (12, 1)
    assert calls["memory_info"] == 0, "RAM is authoritative on GB10 — NVML memory must not be queried"
    assert issues == []


def test_pynvml_gb10_prefers_ram_even_when_nvml_reports_numbers(monkeypatch, pynvml_with) -> None:
    monkeypatch.setattr(gpu, "detect_system_memory", lambda: SPARK_RAM)
    pynvml_with([{"name": "NVIDIA GB10", "mem": (120 * GiB, 60 * GiB, 60 * GiB), "cc": (12, 1)}])

    (g,) = gpu._detect_gpus_pynvml(issues=[])

    assert g.unified_memory is True
    assert (g.vram_mb, g.memory_free_mb) == (128 * 1024, 90 * 1024)


def test_pynvml_rtx4090_memory_failure_is_not_unified_and_gets_no_ram(monkeypatch, pynvml_with) -> None:
    """(b) A transient NVML error on a discrete GPU must not flip the stack into Spark mode."""
    monkeypatch.setattr(gpu, "detect_system_memory", lambda: X86_RAM)
    pynvml_with([{"name": "NVIDIA GeForce RTX 4090", "mem": RuntimeError("NVML_ERROR_UNKNOWN"), "cc": (8, 9)}])
    issues: list[dict] = []

    gpus = gpu._detect_gpus_pynvml(issues=issues)

    assert not any(g.unified_memory for g in gpus)
    assert not any(g.vram_mb == 64 * 1024 for g in gpus), "system RAM must never be substituted for VRAM"
    # The row is kept — name, index and CC intact — at 0 GB, and the failure is explained.
    assert [(g.name, g.index, g.vram_mb, g.memory_free_mb, g.compute_capability) for g in gpus] == [
        ("NVIDIA GeForce RTX 4090", 0, 0, 0, (8, 9)),
    ]
    (issue,) = issues
    assert issue["source"] == "pynvml"
    assert issue["code"] == "memory-unavailable"
    assert "RTX 4090" in issue["message"]
    assert "NVML_ERROR_UNKNOWN" in issue["detail"]


def test_pynvml_zero_total_on_discrete_gpu_is_not_unified(monkeypatch, pynvml_with) -> None:
    monkeypatch.setattr(gpu, "detect_system_memory", lambda: X86_RAM)
    pynvml_with([{"name": "NVIDIA GeForce RTX 4090", "mem": (0, 0, 0), "cc": (8, 9)}])
    issues: list[dict] = []

    gpus = gpu._detect_gpus_pynvml(issues=issues)

    assert [(g.vram_mb, g.unified_memory) for g in gpus] == [(0, False)]
    assert [i["code"] for i in issues] == ["memory-unavailable"]


def test_pynvml_a100_numbers_unchanged(monkeypatch, pynvml_with) -> None:
    """(c) A normal discrete GPU: NVML's figures pass through, system RAM is never read."""
    monkeypatch.setattr(gpu, "detect_system_memory", _ram_unreadable)
    pynvml_with([{"name": "NVIDIA A100 80GB PCIe", "mem": (80 * GiB, 10 * GiB, 70 * GiB), "cc": (8, 0), "util": 7}])
    issues: list[dict] = []

    (g,) = gpu._detect_gpus_pynvml(issues=issues)

    assert g.unified_memory is False
    assert (g.vram_mb, g.memory_used_mb, g.memory_free_mb) == (81920, 10240, 71680)
    assert g.vram_gb == 80.0
    assert g.compute_capability == (8, 0)
    assert g.utilization_pct == 7
    assert g.driver_version == "580.65" and g.cuda_version == "13.0"
    assert issues == []


def test_pynvml_multi_gpu_keeps_healthy_rows_and_the_unreadable_one_at_zero(monkeypatch, pynvml_with) -> None:
    monkeypatch.setattr(gpu, "detect_system_memory", lambda: X86_RAM)
    pynvml_with([
        {"name": "NVIDIA A100 80GB PCIe", "mem": (80 * GiB, 0, 80 * GiB), "cc": (8, 0)},
        {"name": "NVIDIA A100 80GB PCIe", "mem": RuntimeError("GPU is lost"), "cc": (8, 0)},
    ])
    issues: list[dict] = []

    gpus = gpu._detect_gpus_pynvml(issues=issues)

    assert [(g.index, g.vram_mb, g.unified_memory) for g in gpus] == [(0, 81920, False), (1, 0, False)]
    assert [i["code"] for i in issues] == ["memory-unavailable"]
    assert "(GPU 1)" in issues[0]["message"]
    assert issues[0]["index"] == 1          # pairs the issue with its row without parsing the message


def test_pynvml_gb10_with_unreadable_ram_falls_back_to_nvml_figures(monkeypatch, pynvml_with) -> None:
    monkeypatch.setattr(gpu, "detect_system_memory", _ram_unreadable)
    pynvml_with([{"name": "NVIDIA GB10", "mem": (120 * GiB, 20 * GiB, 100 * GiB), "cc": (12, 1)}])

    (g,) = gpu._detect_gpus_pynvml(issues=[])

    assert g.unified_memory is True                 # still a shared pool for budgeting
    assert (g.vram_mb, g.memory_free_mb) == (120 * 1024, 100 * 1024)


def test_pynvml_gb10_with_no_pool_at_all_is_an_issue_and_an_unsized_row(monkeypatch, pynvml_with) -> None:
    monkeypatch.setattr(gpu, "detect_system_memory", _ram_unreadable)
    pynvml_with([{"name": "NVIDIA GB10", "mem": RuntimeError("Not Supported"), "cc": (12, 1)}])
    issues: list[dict] = []

    gpus = gpu._detect_gpus_pynvml(issues=issues)

    # Kept by name (the classifier still recognises a GB10) but with no pool to budget:
    # 0 GB and unified_memory False — "unified" describes figures, and there are none.
    assert [(g.name, g.vram_mb, g.unified_memory, g.compute_capability) for g in gpus] == [
        ("NVIDIA GB10", 0, False, (12, 1)),
    ]
    (issue,) = issues
    assert issue["code"] == "memory-unavailable"
    assert "system RAM could not be measured" in issue["message"]


# ---------------------------------------------------------------------------
# G3 — nvidia-smi fallback
# ---------------------------------------------------------------------------


def _fake_smi(monkeypatch, stdout: str, compute_caps: list[tuple[int, int]] | None = None) -> None:
    monkeypatch.setattr(gpu.shutil, "which", lambda command: "/usr/bin/nvidia-smi")
    monkeypatch.setattr(gpu, "_get_cuda_version", lambda: "13.0")
    monkeypatch.setattr(gpu, "_get_compute_capabilities", lambda: compute_caps or [])
    fake = SimpleNamespace(returncode=0, stdout=stdout, stderr="")
    monkeypatch.setattr(gpu.subprocess, "run", lambda *a, **k: fake)


def test_smi_na_memory_on_discrete_gpu_is_kept_at_zero_with_issue(monkeypatch) -> None:
    """A non-GB10 card printing [N/A] is not a *ready* 0 GB GPU: the row stays (name, index,
    CC) at 0 GB with the failure explained, and detect_gpu_status reports it as blocked."""
    monkeypatch.setattr(gpu, "detect_system_memory", lambda: X86_RAM)
    _fake_smi(monkeypatch, "0, NVIDIA GeForce RTX 4090, [N/A], [N/A], [N/A], 0 %, 580.65\n", [(8, 9)])
    issues: list[dict] = []

    gpus = gpu._detect_gpus_smi(issues=issues)

    assert [(g.name, g.index, g.vram_mb, g.unified_memory, g.compute_capability) for g in gpus] == [
        ("NVIDIA GeForce RTX 4090", 0, 0, False, (8, 9)),
    ]
    (issue,) = issues
    assert issue["source"] == "nvidia-smi"
    assert issue["code"] == "memory-unavailable"
    assert "RTX 4090" in issue["message"]


def test_smi_discrete_row_survives_next_to_an_unreadable_one(monkeypatch) -> None:
    monkeypatch.setattr(gpu, "detect_system_memory", lambda: X86_RAM)
    _fake_smi(
        monkeypatch,
        "0, NVIDIA GeForce RTX 4090, [N/A], [N/A], [N/A], 0 %, 580.65\n"
        "1, NVIDIA GeForce RTX 3090, 24576, 1024, 23552, 3 %, 580.65\n",
        [(8, 9), (8, 6)],
    )
    issues: list[dict] = []

    gpus = gpu._detect_gpus_smi(issues=issues)

    assert [(g.index, g.vram_mb, g.memory_free_mb, g.unified_memory) for g in gpus] == [
        (0, 0, 0, False),            # kept, unsized
        (1, 24576, 23552, False),
    ]
    assert [g.compute_capability for g in gpus] == [(8, 9), (8, 6)]     # still aligned to their own rows
    assert [(i["code"], i["source"], i["index"]) for i in issues] == [("memory-unavailable", "nvidia-smi", 0)]


def test_smi_gb10_uses_ram_pool_and_tolerates_na(monkeypatch) -> None:
    monkeypatch.setattr(gpu, "detect_system_memory", lambda: SPARK_RAM)
    _fake_smi(monkeypatch, "0, NVIDIA GB10, [N/A], [N/A], [N/A], 0 %, 580.65\n")
    issues: list[dict] = []

    (g,) = gpu._detect_gpus_smi(issues=issues)

    assert g.unified_memory is True
    assert (g.vram_mb, g.memory_used_mb, g.memory_free_mb) == (128 * 1024, 38 * 1024, 90 * 1024)
    assert issues == []


def test_smi_gb10_with_unreadable_ram_is_an_issue_and_an_unsized_row(monkeypatch) -> None:
    monkeypatch.setattr(gpu, "detect_system_memory", _ram_unreadable)
    _fake_smi(monkeypatch, "0, NVIDIA GB10, [N/A], [N/A], [N/A], 0 %, 580.65\n")
    issues: list[dict] = []

    gpus = gpu._detect_gpus_smi(issues=issues)

    assert [(g.name, g.vram_mb, g.unified_memory) for g in gpus] == [("NVIDIA GB10", 0, False)]
    (issue,) = issues
    assert issue["code"] == "memory-unavailable"
    assert issue["severity"] == "warning"
    assert "NVIDIA GB10" in issue["message"] and "system RAM could not be measured" in issue["message"]


def test_smi_gb10_with_unreadable_ram_falls_back_to_printed_figures(monkeypatch) -> None:
    monkeypatch.setattr(gpu, "detect_system_memory", _ram_unreadable)
    _fake_smi(monkeypatch, "0, NVIDIA GB10, 122880, 20480, 102400, 0 %, 580.65\n")

    (g,) = gpu._detect_gpus_smi(issues=[])

    assert g.unified_memory is True
    assert (g.vram_mb, g.memory_used_mb, g.memory_free_mb) == (122880, 20480, 102400)


def test_smi_discrete_numbers_unchanged(monkeypatch) -> None:
    monkeypatch.setattr(gpu, "detect_system_memory", _ram_unreadable)
    _fake_smi(monkeypatch, "0, NVIDIA GeForce RTX 4090, 24564, 1000, 23564, 5 %, 550.54\n", [(8, 9)])

    (g,) = gpu._detect_gpus_smi(issues=[])

    assert g.unified_memory is False
    assert (g.vram_mb, g.memory_used_mb, g.memory_free_mb, g.utilization_pct) == (24564, 1000, 23564, 5)
    assert g.compute_capability == (8, 9)


# ---------------------------------------------------------------------------
# detect_gpu_status()['summary']
# ---------------------------------------------------------------------------


def test_status_summary_gb10(monkeypatch) -> None:
    gb10 = _gpu("NVIDIA GB10", 128 * 1024, unified=True, cc=(12, 1))
    monkeypatch.setattr(gpu, "_detect_gpus_pynvml", lambda *, issues=None: [gb10])
    monkeypatch.setattr(gpu, "_detect_gpus_smi", lambda *, issues=None: [])
    monkeypatch.setattr(gpu, "_nvidia_device_files_present", lambda: True)
    monkeypatch.setattr(gpu.shutil, "which", lambda command: "/usr/bin/nvidia-smi")

    status = gpu.detect_gpu_status()

    assert status["status"] == "ready" and status["source"] == "pynvml"
    assert status["summary"] == "1 GPU ready: NVIDIA GB10, 128 GB unified"


def test_status_summary_no_gpu(monkeypatch) -> None:
    monkeypatch.setattr(gpu, "_detect_gpus_pynvml", lambda *, issues=None: [])
    monkeypatch.setattr(gpu, "_detect_gpus_smi", lambda *, issues=None: [])
    monkeypatch.setattr(gpu, "_nvidia_device_files_present", lambda: False)
    monkeypatch.setattr(gpu.shutil, "which", lambda command: None)

    status = gpu.detect_gpu_status()

    assert status["status"] == "not-detected"
    assert status["gpus"] == []
    assert status["summary"] == "no NVIDIA GPU detected (nvidia-smi missing)"


def test_status_gb10_with_unknown_pool_is_blocked_with_reason(monkeypatch) -> None:
    """End to end: pynvml missing, nvidia-smi prints [N/A], RAM unreadable → the GB10 is
    listed (so the platform classifier sees it) but the status is 'blocked', never 'ready, 0 GB'."""
    monkeypatch.setitem(sys.modules, "pynvml", None)      # import pynvml → ImportError
    monkeypatch.setattr(gpu, "detect_system_memory", _ram_unreadable)
    monkeypatch.setattr(gpu, "_nvidia_device_files_present", lambda: False)
    _fake_smi(monkeypatch, "0, NVIDIA GB10, [N/A], [N/A], [N/A], 0 %, 580.65\n")

    status = gpu.detect_gpu_status()

    assert status["status"] == "blocked"
    assert status["source"] == "nvidia-smi"
    assert [(g.name, g.vram_mb) for g in status["gpus"]] == [("NVIDIA GB10", 0)]
    # The source-level NVML issue (why it was skipped) plus the winning source's row issue — once.
    assert [(i["source"], i["code"]) for i in status["issues"]] == [
        ("pynvml", "module-missing"), ("nvidia-smi", "memory-unavailable"),
    ]
    assert not any(i["code"] == "devices-present-no-query" for i in status["issues"])  # it *was* queried
    assert status["summary"].startswith("1 GPU visible but its memory could not be read: NVIDIA GB10 (GPU 0)")
    assert "system RAM could not be measured" in status["summary"]


def test_status_unsized_discrete_row_is_blocked_and_named(monkeypatch) -> None:
    """R4: NVML enumerates an RTX 4090 but cannot read its memory; nvidia-smi is absent.
    The row stays (its name feeds the platform classifier), the status is 'blocked', and
    both summaries say so instead of 'ready, 0.0 GB'."""
    monkeypatch.setattr(gpu, "detect_system_memory", lambda: X86_RAM)
    monkeypatch.setattr(gpu, "_nvidia_device_files_present", lambda: True)
    monkeypatch.setattr(gpu.shutil, "which", lambda command: None)
    monkeypatch.setitem(sys.modules, "pynvml", _fake_pynvml([
        {"name": "NVIDIA GeForce RTX 4090", "mem": RuntimeError("NVML_ERROR_UNKNOWN"), "cc": (8, 9)},
    ])[0])

    status = gpu.detect_gpu_status()

    assert status["status"] == "blocked" and status["source"] == "pynvml"
    assert [(g.name, g.vram_mb) for g in status["gpus"]] == [("NVIDIA GeForce RTX 4090", 0)]
    assert status["summary"] == (
        "1 GPU visible but its memory could not be read: NVIDIA GeForce RTX 4090 (GPU 0) — "
        "pynvml could not report memory for NVIDIA GeForce RTX 4090 (GPU 0)."
    )
    assert gpu.get_gpu_summary(status["gpus"]) == (
        "NVIDIA GeForce RTX 4090 (memory unreadable) — driver 580.65 / CUDA 13.0"
    )


def test_status_mixed_rows_one_unsized_is_ready_and_names_the_unreadable_one(monkeypatch) -> None:
    """R5: a healthy A100 next to one whose memory cannot be read is a usable machine — 'ready'
    on the sized row (as when detection dropped the bad row), the unreadable row kept at 0 GB and
    named in issues and both summaries; VRAM totals count only the sized row."""
    monkeypatch.setattr(gpu, "detect_system_memory", lambda: X86_RAM)
    monkeypatch.setattr(gpu, "_nvidia_device_files_present", lambda: True)
    monkeypatch.setattr(gpu.shutil, "which", lambda command: None)
    monkeypatch.setitem(sys.modules, "pynvml", _fake_pynvml([
        {"name": "NVIDIA A100 80GB PCIe", "mem": (80 * GiB, 0, 80 * GiB), "cc": (8, 0)},
        {"name": "NVIDIA A100 80GB PCIe", "mem": RuntimeError("GPU is lost"), "cc": (8, 0)},
    ])[0])

    status = gpu.detect_gpu_status()

    assert status["status"] == "ready" and status["source"] == "pynvml"
    assert [(g.index, g.vram_mb) for g in status["gpus"]] == [(0, 81920), (1, 0)]
    assert status["summary"] == (
        "1 of 2 GPUs ready: NVIDIA A100 80GB PCIe, 80 GB VRAM; "
        "memory unreadable: NVIDIA A100 80GB PCIe (GPU 1)"
    )
    assert [(i["source"], i["code"], i["index"]) for i in status["issues"]] == [("pynvml", "memory-unavailable", 1)]
    assert gpu.get_gpu_summary(status["gpus"]) == (
        "2x GPU: NVIDIA A100 80GB PCIe (80.0 GB each, 80.0 GB total) — driver 580.65 / CUDA 13.0"
        " (1 GPU(s) memory unreadable)"
    )
    assert gpu._memory_budget(status["gpus"], X86_RAM).total_vram_gb == 80.0


def test_status_is_blocked_only_when_no_row_is_sized(monkeypatch) -> None:
    """R5: two visible GPUs, neither sizable, nvidia-smi absent → blocked, both named."""
    monkeypatch.setattr(gpu, "detect_system_memory", lambda: X86_RAM)
    monkeypatch.setattr(gpu, "_nvidia_device_files_present", lambda: True)
    monkeypatch.setattr(gpu.shutil, "which", lambda command: None)
    monkeypatch.setitem(sys.modules, "pynvml", _fake_pynvml([
        {"name": "NVIDIA A100 80GB PCIe", "mem": RuntimeError("GPU is lost"), "cc": (8, 0)},
        {"name": "NVIDIA A100 80GB PCIe", "mem": (0, 0, 0), "cc": (8, 0)},
    ])[0])

    status = gpu.detect_gpu_status()

    assert status["status"] == "blocked" and status["source"] == "pynvml"
    assert [g.vram_mb for g in status["gpus"]] == [0, 0]
    assert status["summary"] == (
        "2 GPUs visible but memory unreadable: NVIDIA A100 80GB PCIe (GPU 0), NVIDIA A100 80GB PCIe (GPU 1)"
        " — pynvml could not report memory for NVIDIA A100 80GB PCIe (GPU 0)."
    )
    assert [i["index"] for i in status["issues"] if i["code"] == "memory-unavailable"] == [0, 1]


def test_status_smi_gets_a_turn_when_nvml_sized_nothing(monkeypatch) -> None:
    """The pre-R4 fallback survives: NVML names the GPU but cannot size it; nvidia-smi can."""
    monkeypatch.setattr(gpu, "detect_system_memory", lambda: X86_RAM)
    monkeypatch.setattr(gpu, "_nvidia_device_files_present", lambda: True)
    monkeypatch.setitem(sys.modules, "pynvml", _fake_pynvml([
        {"name": "NVIDIA GeForce RTX 4090", "mem": RuntimeError("NVML_ERROR_UNKNOWN"), "cc": (8, 9)},
    ])[0])
    _fake_smi(monkeypatch, "0, NVIDIA GeForce RTX 4090, 24564, 1000, 23564, 5 %, 580.65\n", [(8, 9)])

    status = gpu.detect_gpu_status()

    assert status["status"] == "ready" and status["source"] == "nvidia-smi"
    assert [(g.vram_mb, g.memory_free_mb) for g in status["gpus"]] == [(24564, 23564)]
    assert status["summary"] == "1 GPU ready: NVIDIA GeForce RTX 4090, 24 GB VRAM"
    # R6: NVML's memory-unavailable warning described a row that lost; it does not survive.
    assert not any(i["code"] == "memory-unavailable" for i in status["issues"])


def test_status_both_sources_unsized_reports_the_gpu_once(monkeypatch) -> None:
    """R6: NVML and nvidia-smi both enumerate the RTX 4090 and both fail to size it — NVML's
    rows stay (source pynvml), and the memory issue is not doubled per GPU."""
    monkeypatch.setattr(gpu, "detect_system_memory", lambda: X86_RAM)
    monkeypatch.setattr(gpu, "_nvidia_device_files_present", lambda: True)
    monkeypatch.setitem(sys.modules, "pynvml", _fake_pynvml([
        {"name": "NVIDIA GeForce RTX 4090", "mem": RuntimeError("NVML_ERROR_UNKNOWN"), "cc": (8, 9)},
    ])[0])
    _fake_smi(monkeypatch, "0, NVIDIA GeForce RTX 4090, [N/A], [N/A], [N/A], 0 %, 580.65\n", [(8, 9)])

    status = gpu.detect_gpu_status()

    assert status["status"] == "blocked" and status["source"] == "pynvml"
    assert [(g.name, g.vram_mb) for g in status["gpus"]] == [("NVIDIA GeForce RTX 4090", 0)]
    assert [(i["source"], i["code"], i["index"]) for i in status["issues"]] == [("pynvml", "memory-unavailable", 0)]
    assert status["summary"].endswith("— pynvml could not report memory for NVIDIA GeForce RTX 4090 (GPU 0).")


def test_budget_helpers_treat_an_unsized_row_as_no_usable_vram(monkeypatch) -> None:
    """recommend_models / check_oom_risk / get_ollama_optimizations on a kept 0 GB row:
    no division by zero, no VRAM tier, no unified claim — the table's 0-4 GB tier, as for 'no GPU'."""
    monkeypatch.setattr(gpu, "detect_system_memory", lambda: X86_RAM)
    unsized = _gpu("NVIDIA GH200 480GB", 0, cc=(9, 0))

    budget = gpu._memory_budget([unsized], X86_RAM)
    assert (budget.total_vram_gb, budget.model_budget_gb, budget.unified_memory) == (0.0, 0.0, False)
    assert (budget.total_gpus, budget.sized_gpus, budget.gpu_memory_unreadable) == (1, 0, True)
    assert lm.tier_for(budget) is lm.LOCAL_MODEL_TIERS[0]

    recs = gpu.recommend_models(gpus=[unsized])
    assert [r.model for r in recs] == [r.model for r in gpu.recommend_models(gpus=[])]
    assert [r.vram_required_gb for r in recs] == [lm.pick_for_tag(r.model).runtime_gb for r in recs]
    assert not any(r.tier.endswith("-hybrid") for r in recs)  # no sized GPU to offload from
    assert "GPU detected but its memory could not be read" in recs[0].reason
    assert "no GPU detected" in gpu.recommend_models(gpus=[])[0].reason

    oom = gpu.check_oom_risk(8.0, gpus=[unsized])
    assert oom["gpu_free_gb"] == 0.0 and oom["fits_gpu"] is False and oom["unified_memory"] is False
    assert oom == gpu.check_oom_risk(8.0, gpus=[])

    opt = gpu.get_ollama_optimizations(gpus=[unsized])
    assert opt.architecture == "Hopper" and opt.compute_capability == (9, 0)
    assert (opt.num_parallel, opt.recommended_ctx) == (lm.num_parallel_for(budget), lm.num_ctx_for(budget))
    assert (opt.num_parallel, opt.recommended_ctx) == (lm.LOCAL_MODEL_TIERS[0].num_parallel, lm.LOCAL_MODEL_TIERS[0].num_ctx)
    assert opt.recommended_ctx != lm.CPU_ONLY_NUM_CTX, "a listed card is not 'no GPU'"
    assert any("memory could not be read" in n for n in opt.notes)


def test_budget_helpers_count_only_sized_rows(monkeypatch) -> None:
    """R5: an unreadable row next to healthy ones is not a GPU Ollama can use — VRAM totals and
    the multi-GPU note count sized rows only, exactly as at HEAD when the bad row was dropped."""
    monkeypatch.setattr(gpu, "detect_system_memory", lambda: X86_RAM)
    healthy = _gpu("NVIDIA A100 80GB PCIe", 80 * 1024, cc=(8, 0))
    lost = _gpu("NVIDIA A100 80GB PCIe", 0, cc=(8, 0), index=1)

    budget = gpu._memory_budget([healthy, lost], X86_RAM)
    assert (budget.total_vram_gb, budget.model_budget_gb, budget.unified_memory) == (80.0, 80.0, False)
    assert (budget.total_gpus, budget.sized_gpus) == (2, 1)

    recs = gpu.recommend_models(gpus=[healthy, lost])
    assert [(r.model, r.tier) for r in recs] == [(r.model, r.tier) for r in gpu.recommend_models(gpus=[healthy])]
    assert "Ollama will use all" not in recs[0].reason

    second = _gpu("NVIDIA A100 80GB PCIe", 80 * 1024, cc=(8, 0), index=2)
    recs = gpu.recommend_models(gpus=[healthy, second, lost])
    assert recs[0].tier == "multi-gpu"
    assert all("Ollama will use all 2 GPUs automatically" in r.reason for r in recs)

    # A 0 GB row listed first must not decide the memory model for the sized GB10 behind it
    # (its unified_memory is always False, which would add a CPU-offload bonus on top of RAM).
    monkeypatch.setattr(gpu, "detect_system_memory", lambda: SPARK_RAM)
    gb10 = _gpu("NVIDIA GB10", 128 * 1024, unified=True, cc=(12, 1), index=1)
    budget = gpu._memory_budget([_gpu("NVIDIA GB10", 0, cc=(12, 1)), gb10], SPARK_RAM)
    assert (budget.unified_memory, budget.model_budget_gb, budget.cpu_offload_gb) == (True, 112.0, 0.0)
    opt = gpu.get_ollama_optimizations(gpus=[_gpu("NVIDIA GB10", 0, cc=(12, 1)), gb10])
    assert opt.recommended_quant == lm.quant_for(budget) == "Q4_K_M" and opt.architecture == "Blackwell"
    assert any("memory could not be read" in n for n in opt.notes)


# ---------------------------------------------------------------------------
# recommend_models budgets — picks, sizes and reasons come from the table
# ---------------------------------------------------------------------------


def _expected_hybrid(budget: lm.TierBudget, listed: set[str]) -> lm.LocalModelPick | None:
    """recommend_models' hybrid rule restated from the table: a card of HYBRID_MIN_BUDGET_GB or more,
    the tier combined_gb reaches, its chat (else code) pick, when it needs more than VRAM, fits VRAM +
    the capped RAM bonus and leaves at most HYBRID_MAX_RAM_SHARE of itself in RAM."""
    home = lm.tier_for(budget)
    reach = replace(budget, budget_gb=budget.combined_gb, offload_gb=0.0)
    if budget.unified or budget.sized_gpus == 0 or home.min_gb < gpu.HYBRID_MIN_BUDGET_GB or lm.tier_for(reach) is home:
        return None
    ceiling = min(budget.combined_gb, budget.budget_gb / (1 - gpu.HYBRID_MAX_RAM_SHARE))
    for use_case in ("chat", "code"):
        pick = lm.pick(reach, use_case)
        if pick is not None and pick.tag not in listed and budget.budget_gb < pick.runtime_gb <= ceiling:
            return pick
    return None


def test_recommend_models_gb10_budget_is_pool_minus_reserve_with_note_on_primary_only(monkeypatch) -> None:
    """(i) 128 GB unified, MemAvailable 90 GB → cpu_offload 0, budget 112 GB, note on the primary rec only."""
    monkeypatch.setattr(gpu, "detect_system_memory", lambda: SPARK_RAM)
    gb10 = _gpu("NVIDIA GB10", 128 * 1024, unified=True, cc=(12, 1), free_mb=90 * 1024)

    budget = gpu._memory_budget([gb10], SPARK_RAM)
    recs = gpu.recommend_models(gpus=[gb10])

    assert budget.unified_memory is True
    assert budget.cpu_offload_gb == 0.0
    assert budget.model_budget_gb == 128.0 - gpu.UNIFIED_MEMORY_OS_RESERVE_GB == 112.0
    assert budget.combined_gb == 112.0
    assert lm.moe_first(budget) is True

    table = lm.recommended(budget)
    primary = recs[0]
    assert primary.model == table[0].tag and table[0].moe, "a MoE model leads on a unified pool"
    assert primary.tier == gpu.recommendation_tier(table[0]) == "full"
    assert primary.reason == lm.reason_for(budget, table[0])
    assert primary.reason.startswith(f"{primary.model} — ")
    assert "unified memory: ~112 GB of 128 GB usable after the 16 GB OS reserve" in primary.reason
    assert primary.note == gpu.unified_memory_note(budget)
    assert f"~{gpu.UNIFIED_MEMORY_BANDWIDTH_GBPS} GB/s" in primary.note and "MoE" in primary.note
    for pick in table:
        if pick.moe:
            assert pick.tag in primary.note
    assert all(r.note == "" for r in recs[1:]), "the unified note is attached to the primary rec only"
    assert not any(r.tier.endswith("-hybrid") for r in recs)
    vision = lm.pick(budget, "vision")
    assert [(r.model, r.tier) for r in recs if r.use_case == "vision"] == [(vision.tag, "vision")]
    assert {r.model for r in recs} == {p.tag for p in table}


def test_recommend_models_x86_rtx4090_reads_the_table(monkeypatch) -> None:
    """(ii) 24 GB RTX 4090 + 64 GB RAM: raw VRAM tiering, 16 GB capped offload, one hybrid pick —
    the whole list, in order, derived from the table."""
    monkeypatch.setattr(gpu, "detect_system_memory", lambda: X86_RAM)
    rtx = _gpu("NVIDIA GeForce RTX 4090", 24576)

    budget = gpu._memory_budget([rtx], X86_RAM)
    recs = gpu.recommend_models(gpus=[rtx])

    assert budget.unified_memory is False
    assert budget.model_budget_gb == 24.0
    assert budget.cpu_offload_gb == 16.0           # min(33.6, 16.0)
    assert budget.combined_gb == 40.0
    assert budget.compute_capability == (8, 9)      # read off the name: NVML gave (0, 0)

    vision, embed = lm.pick(budget, "vision"), lm.pick(budget, "embed")
    text = [p for p in lm.recommended(budget) if p.tag not in (vision.tag, embed.tag)]
    hybrid = _expected_hybrid(budget, {p.tag for p in text})
    assert hybrid is not None
    expected = [
        *((p.tag, gpu.recommendation_tier(p), p.runtime_gb, lm.reason_for(budget, p)) for p in text),
        (hybrid.tag, gpu.recommendation_tier(hybrid) + "-hybrid", hybrid.runtime_gb, None),
        (vision.tag, "vision", vision.runtime_gb, lm.reason_for(budget, vision)),
        (embed.tag, "embed", embed.runtime_gb, lm.reason_for(budget, embed)),
    ]
    got = [(r.model, r.tier, r.vram_required_gb, None if r.tier.endswith("-hybrid") else r.reason) for r in recs]
    assert got == expected
    assert [r.tier for r in recs[:len(text)]] == ["full", "full", "mini"]

    hybrid_rec = next(r for r in recs if r.tier.endswith("-hybrid"))
    assert hybrid_rec.reason.startswith(lm.reason_for(budget, hybrid))
    assert "partial CPU offload: 24 GB VRAM + 16 GB RAM = 40 GB combined" in hybrid_rec.reason
    spill = hybrid.runtime_gb - budget.budget_gb
    assert f"~{round(spill, 1):g} GB ({spill / hybrid.runtime_gb:.0%}) in RAM" in hybrid_rec.reason
    assert all(r.note == "" for r in recs)
    assert all(r.model in lm.all_tags() for r in recs)


def test_recommend_models_16gb_unified_pool_keeps_a_real_budget(monkeypatch) -> None:
    """(iii) 16 GB unified (an Apple Silicon Mac through `nvh models tiers --unified-gb`, or a small
    GB10-class part): the reserve follows the pool -- 4 GB, not the GB10's 16 -- so the budget is
    12 GB and the primary is an 8B model, where the flat reserve used to plan against 0 GB."""
    ram = SystemMemoryInfo(total_ram_gb=16.0, available_ram_gb=10.0, effective_for_llm_gb=7.0)
    monkeypatch.setattr(gpu, "detect_system_memory", lambda: ram)
    pool = _gpu("NVIDIA GB10", 16 * 1024, unified=True, cc=(12, 1))

    budget = gpu._memory_budget([pool], ram)
    recs = gpu.recommend_models(gpus=[pool])
    opt = gpu.get_ollama_optimizations(gpus=[pool])

    assert (budget.unified_memory, budget.total_vram_gb, budget.model_budget_gb, budget.cpu_offload_gb) == (
        True, 16.0, 12.0, 0.0,
    )
    assert budget.os_reserve_gb == lm.unified_os_reserve_gb(16.0) == gpu.unified_os_reserve_gb(16.0) == 4.0
    assert lm.tier_for(budget).label == "small-plus"
    assert recs[0].model == lm.pick(budget, "chat").tag == "qwen3:8b"
    assert recs[0].tier == "small" and recs[0].use_case == "chat"
    assert "~12 GB of 16 GB usable after the 4 GB OS reserve" in recs[0].reason
    assert "~4 GB is reserved for the OS" in recs[0].note and "leaving ~12 GB for models" in recs[0].note
    assert "no MoE model fits this budget yet" in recs[0].note
    assert not any(r.tier.endswith("-hybrid") for r in recs)
    assert (opt.recommended_ctx, opt.num_parallel, opt.recommended_quant) == (8192, 1, "Q4_K_M")
    assert any("after the 4 GB OS reserve" in note for note in opt.notes)
    assert any("keep ~4 GB free for the OS" in note for note in opt.notes)
    # The 128 GB GB10 is untouched by the curve: same 16 GB reserve, same 112 GB budget.
    spark = gpu._memory_budget([_gpu("NVIDIA GB10", 128 * 1024, unified=True, cc=(12, 1))], SPARK_RAM)
    assert (spark.os_reserve_gb, spark.model_budget_gb) == (gpu.UNIFIED_MEMORY_OS_RESERVE_GB, 112.0)


# ---------------------------------------------------------------------------
# G4 — Ollama quantization on GB10
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("cc", [(12, 1), (0, 0)], ids=["nvml-cc", "name-heuristic"])
def test_ollama_optimizations_gb10_is_not_the_hbm_tier(monkeypatch, cc) -> None:
    monkeypatch.setattr(gpu, "detect_system_memory", lambda: SPARK_RAM)
    gb10 = _gpu("NVIDIA GB10", 128 * 1024, unified=True, cc=cc)
    budget = gpu._memory_budget([gb10], SPARK_RAM)

    opt = gpu.get_ollama_optimizations(gpus=[gb10])

    assert opt.architecture == "Blackwell" and opt.compute_capability == (12, 1)
    assert opt.recommended_quant == lm.quant_for(budget) == "Q4_K_M"
    assert (opt.recommended_ctx, opt.num_parallel) == (lm.num_ctx_for(budget), lm.num_parallel_for(budget))
    tier = lm.tier_for(budget)   # sized to 128 - 16 = 112 GB
    assert tier is lm.tier_for(112.0)
    assert (opt.recommended_ctx, opt.num_parallel) == (tier.num_ctx, tier.num_parallel)
    assert tier.default_quant != "Q4_K_M", "the unified pool overrides the tier's HBM quant"
    joined = " ".join(opt.notes)
    assert "Q8_0 or F16 recommended" not in joined
    assert "GDDR7" not in joined
    assert "MoE" in joined and str(opt.recommended_ctx) in joined and "16 GB OS reserve" in joined
    for pick in lm.recommended(budget):
        if pick.moe:
            assert pick.tag in joined


def test_ollama_optimizations_h100_keeps_hbm_tier(monkeypatch) -> None:
    monkeypatch.setattr(gpu, "detect_system_memory", lambda: X86_RAM)
    h100 = _gpu("NVIDIA H100 80GB HBM3", 80 * 1024, cc=(9, 0))
    budget = gpu._memory_budget([h100], X86_RAM)

    opt = gpu.get_ollama_optimizations(gpus=[h100])

    assert opt.recommended_quant == lm.quant_for(budget) == lm.tier_for(budget).default_quant == "Q8_0 or F16"
    assert any("Q8_0 or F16 recommended" in n for n in opt.notes)


# ---------------------------------------------------------------------------
# G5 — one GB10 predicate
# ---------------------------------------------------------------------------


def test_gb10_predicate_is_shared_with_hw_ids() -> None:
    assert not hasattr(gpu, "_GB10_RE"), "gpu.py must not keep a private GB10 regex"
    assert gpu.is_gb10_name is hw_ids.is_gb10_name
    assert gpu.is_unified_memory_gpu_name("NVIDIA GB10") is True
    assert gpu.is_unified_memory_gpu_name("NVIDIA GB100") is False
    assert gpu.is_unified_memory_gpu_name(None) is False
    assert gpu._parse_compute_capability("NVIDIA GB10") == (12, 1)
    assert gpu._parse_compute_capability("NVIDIA GB200") == (10, 0)


def test_is_unified_memory_gpu_name_delegates(monkeypatch) -> None:
    seen: list[str | None] = []
    monkeypatch.setattr(gpu, "is_gb10_name", lambda name: seen.append(name) or True)
    assert gpu.is_unified_memory_gpu_name("anything") is True
    assert seen == ["anything"]
