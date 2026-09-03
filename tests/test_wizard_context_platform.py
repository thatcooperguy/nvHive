"""Wizard context/prompt plumbing for platform facts + GB10 unified-memory handling.

Covers the defects found by the code map:
  * ``_gpu_summary`` used to read ``GPUInfo`` dataclasses as dicts → all-null primary.
  * ``detect_gpu_status()`` had no ``summary`` key the Wizard could show.
  * GB10 had no compute-capability token → "Unknown" architecture.
  * ``recommend_models`` added CPU-offload RAM on top of a unified pool.
  * The prompt block / findings / reconnect panel had no platform awareness.
"""

from __future__ import annotations

import json
import re
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from nvh.core import local_models as lm
from nvh.integrations.wizard import context as context_module
from nvh.integrations.wizard.context import _gpu_summary, wizard_context
from nvh.integrations.wizard.findings import derive_findings
from nvh.integrations.wizard.personality import (
    _compact_platform,
    _format_context_block,
    build_system_prompt,
)
from nvh.integrations.wizard.reconnect import _label_for, _survived_facts
from nvh.utils import gpu as gpu_mod
from nvh.utils.gpu import (
    GPUInfo,
    SystemMemoryInfo,
    _memory_budget,
    _parse_compute_capability,
    gpu_architecture_info,
    recommend_models,
)


def _gpu(name: str, vram_mb: int, *, unified: bool = False, cc: tuple[int, int] = (0, 0)) -> GPUInfo:
    return GPUInfo(
        name=name,
        vram_mb=vram_mb,
        vram_gb=round(vram_mb / 1024, 1),
        driver_version="580.65",
        cuda_version="13.0",
        utilization_pct=3,
        memory_used_mb=2048,
        memory_free_mb=vram_mb - 2048,
        index=0,
        compute_capability=cc,
        unified_memory=unified,
    )


GB10 = _gpu("NVIDIA GB10", 128 * 1024, unified=True)
RTX4090 = _gpu("NVIDIA GeForce RTX 4090", 24576)


# ---------------------------------------------------------------------------
# _gpu_summary reads dataclass rows
# ---------------------------------------------------------------------------


def test_gpu_summary_reads_gpuinfo_dataclass(monkeypatch) -> None:
    status = {"status": "ready", "gpus": [GB10], "summary": "1 GPU ready: NVIDIA GB10, 128 GB unified"}

    def no_ram_probe():  # W3: the platform block owns system RAM; the GPU block must not re-read it
        raise AssertionError("_gpu_summary must not call detect_system_memory")

    monkeypatch.setattr(gpu_mod, "detect_system_memory", no_ram_probe)
    out = _gpu_summary(None, status=status)

    assert out["detected"] is True
    assert out["gpu_count"] == 1
    assert out["summary"] == "1 GPU ready: NVIDIA GB10, 128 GB unified"
    primary = out["primary"]
    assert primary["name"] == "NVIDIA GB10"
    assert primary["vram_gb"] == 128.0
    assert primary["memory_used_mb"] == 2048
    assert primary["utilization_pct"] == 3
    assert primary["driver_version"] == "580.65"
    assert primary["cuda_version"] == "13.0"
    assert primary["unified_memory"] is True
    assert primary["architecture"] == "Blackwell"
    assert primary["compute_capability"] == [12, 1]
    assert out["unified_memory"] is True
    assert "memory_total_gb" not in out  # pool size lives in the platform block only


def test_gpu_summary_discrete_gpu_carries_no_memory_fields() -> None:
    """W3: a discrete card's GPU block has neither unified_memory nor memory_total_gb."""
    out = _gpu_summary(None, status={"status": "ready", "gpus": [RTX4090], "summary": "x"})
    assert out["detected"] is True
    assert out["primary"]["unified_memory"] is False
    assert "unified_memory" not in out
    assert "memory_total_gb" not in out


def test_gpu_summary_still_accepts_dict_rows() -> None:
    status = {
        "gpus": [{"name": "RTX 5090", "vram_gb": 32.0, "architecture": "Blackwell", "compute_capability": (10, 0)}],
        "summary": "1 GPU ready: RTX 5090, 32 GB VRAM",
    }
    out = _gpu_summary(None, status=status)
    assert out["primary"]["name"] == "RTX 5090"
    assert out["primary"]["vram_gb"] == 32.0
    assert out["primary"]["architecture"] == "Blackwell"
    assert out["primary"]["compute_capability"] == [10, 0]
    assert out["primary"]["unified_memory"] is False


def test_gpu_summary_no_gpu() -> None:
    out = _gpu_summary(None, status={"status": "not-detected", "gpus": [], "summary": "no NVIDIA GPU detected (nvidia-smi missing)"})
    assert out["detected"] is False
    assert out["primary"] is None
    assert out["summary"].startswith("no NVIDIA GPU detected")


def test_gpu_summary_runs_detection_when_no_status_given(monkeypatch) -> None:
    monkeypatch.setattr(gpu_mod, "detect_gpu_status", lambda: {"status": "ready", "gpus": [RTX4090], "summary": "x"})
    out = _gpu_summary(None)
    assert out["primary"]["name"] == "NVIDIA GeForce RTX 4090"
    assert out["primary"]["architecture"] == "Ada Lovelace"


# ---------------------------------------------------------------------------
# detect_gpu_status()['summary']
# ---------------------------------------------------------------------------


def test_detect_gpu_status_has_summary_for_gb10(monkeypatch) -> None:
    monkeypatch.setattr(gpu_mod, "_detect_gpus_pynvml", lambda *, issues=None: [GB10])
    monkeypatch.setattr(gpu_mod, "_detect_gpus_smi", lambda *, issues=None: [])
    monkeypatch.setattr(gpu_mod, "_nvidia_device_files_present", lambda: True)
    status = gpu_mod.detect_gpu_status()
    assert status["status"] == "ready"
    assert status["summary"] == "1 GPU ready: NVIDIA GB10, 128 GB unified"


def test_detect_gpu_status_summary_when_nothing_detected(monkeypatch) -> None:
    monkeypatch.setattr(gpu_mod, "_detect_gpus_pynvml", lambda *, issues=None: [])
    monkeypatch.setattr(gpu_mod, "_detect_gpus_smi", lambda *, issues=None: [])
    monkeypatch.setattr(gpu_mod, "_nvidia_device_files_present", lambda: False)
    monkeypatch.setattr(gpu_mod.shutil, "which", lambda command: None)
    status = gpu_mod.detect_gpu_status()
    assert status["status"] == "not-detected"
    assert status["summary"] == "no NVIDIA GPU detected (nvidia-smi missing)"


def test_detect_gpu_status_summary_multi_gpu(monkeypatch) -> None:
    a100 = _gpu("NVIDIA A100 80GB PCIe", 81920)
    monkeypatch.setattr(gpu_mod, "_detect_gpus_pynvml", lambda *, issues=None: [a100, a100])
    monkeypatch.setattr(gpu_mod, "_detect_gpus_smi", lambda *, issues=None: [])
    status = gpu_mod.detect_gpu_status()
    assert status["summary"] == "2 GPUs ready: NVIDIA A100 80GB PCIe x2, 160 GB VRAM total"


def test_detect_gpu_status_summary_blocked(monkeypatch) -> None:
    monkeypatch.setattr(gpu_mod, "_detect_gpus_pynvml", lambda *, issues=None: [])
    monkeypatch.setattr(gpu_mod, "_detect_gpus_smi", lambda *, issues=None: [])
    monkeypatch.setattr(gpu_mod, "_nvidia_device_files_present", lambda: True)
    status = gpu_mod.detect_gpu_status()
    assert status["status"] == "blocked"
    assert "could not be queried" in status["summary"]


# ---------------------------------------------------------------------------
# GB10 recognition + nvidia-smi "[N/A]" tolerance
# ---------------------------------------------------------------------------


def test_gb10_compute_capability_and_architecture() -> None:
    assert _parse_compute_capability("NVIDIA GB10") == (12, 1)
    assert _parse_compute_capability("nvidia gb10") == (12, 1)
    # Must not match the GB100 datacenter die or GB200.
    assert _parse_compute_capability("NVIDIA GB100") == (10, 0)
    assert _parse_compute_capability("NVIDIA GB200") == (10, 0)
    arch = gpu_architecture_info(_gpu("NVIDIA GB10", 131072))
    assert arch["architecture"] == "Blackwell"
    assert arch["compute_capability"] == (12, 1)
    assert arch["heuristic"] is True


def test_is_unified_memory_gpu_name() -> None:
    assert gpu_mod.is_unified_memory_gpu_name("NVIDIA GB10") is True
    assert gpu_mod.is_unified_memory_gpu_name("NVIDIA GB100") is False
    assert gpu_mod.is_unified_memory_gpu_name("") is False


def test_smi_fallback_tolerates_na_memory_and_uses_system_ram_for_gb10(monkeypatch) -> None:
    monkeypatch.setattr(gpu_mod.shutil, "which", lambda command: "/usr/bin/nvidia-smi")
    monkeypatch.setattr(gpu_mod, "_get_cuda_version", lambda: "13.0")
    monkeypatch.setattr(gpu_mod, "_get_compute_capabilities", lambda: [])
    monkeypatch.setattr(gpu_mod, "_system_ram_pool_mb", lambda: (131072, 31072, 100000))
    fake = SimpleNamespace(
        returncode=0,
        stdout="0, NVIDIA GB10, [N/A], [N/A], [N/A], 0 %, 580.65\n",
        stderr="",
    )
    monkeypatch.setattr(gpu_mod.subprocess, "run", lambda *a, **k: fake)

    gpus = gpu_mod._detect_gpus_smi(issues=[])

    assert len(gpus) == 1
    g = gpus[0]
    assert g.name == "NVIDIA GB10"
    assert g.unified_memory is True
    assert g.vram_mb == 131072
    assert g.vram_gb == 128.0
    assert g.memory_free_mb == 100000  # MemAvailable, not nvidia-smi's N/A
    assert g.memory_used_mb == 31072


def test_smi_fallback_keeps_discrete_gpu_numbers(monkeypatch) -> None:
    monkeypatch.setattr(gpu_mod.shutil, "which", lambda command: "/usr/bin/nvidia-smi")
    monkeypatch.setattr(gpu_mod, "_get_cuda_version", lambda: "12.4")
    monkeypatch.setattr(gpu_mod, "_get_compute_capabilities", lambda: [(8, 9)])
    fake = SimpleNamespace(returncode=0, stdout="0, NVIDIA GeForce RTX 4090, 24564, 1000, 23564, 5 %, 550.54\n", stderr="")
    monkeypatch.setattr(gpu_mod.subprocess, "run", lambda *a, **k: fake)

    gpus = gpu_mod._detect_gpus_smi(issues=[])
    assert len(gpus) == 1
    assert gpus[0].unified_memory is False
    assert gpus[0].vram_mb == 24564
    assert gpus[0].memory_free_mb == 23564
    assert gpus[0].compute_capability == (8, 9)


def test_smi_int_parses_na() -> None:
    assert gpu_mod._smi_int("[N/A]") == 0
    assert gpu_mod._smi_int("[Not Supported]") == 0
    assert gpu_mod._smi_int("24564") == 24564
    assert gpu_mod._smi_int("24564.0") == 24564


# ---------------------------------------------------------------------------
# recommend_models: unified memory is ONE pool
# ---------------------------------------------------------------------------


def test_memory_budget_does_not_double_count_unified_memory() -> None:
    sys_mem = SystemMemoryInfo(total_ram_gb=128.0, available_ram_gb=100.0, effective_for_llm_gb=70.0)

    unified = _memory_budget([GB10], sys_mem)
    discrete = _memory_budget([_gpu("NVIDIA Fake 128GB", 128 * 1024)], sys_mem)

    assert unified.unified_memory is True
    assert unified.cpu_offload_gb == 0.0
    assert unified.model_budget_gb == 128.0 - gpu_mod.UNIFIED_MEMORY_OS_RESERVE_GB
    assert unified.combined_gb == unified.model_budget_gb  # no RAM bonus on top of RAM

    assert discrete.unified_memory is False
    assert discrete.cpu_offload_gb == 16.0  # capped bonus
    assert discrete.combined_gb == 128.0 + 16.0
    assert discrete.combined_gb > unified.combined_gb


def test_recommend_models_unified_has_note_and_no_hybrid_tiers() -> None:
    sys_mem = SystemMemoryInfo(128.0, 100.0, 70.0)
    with patch.object(gpu_mod, "detect_system_memory", return_value=sys_mem):
        recs = recommend_models(gpus=[GB10])
    budget = _memory_budget([GB10], sys_mem)

    assert recs, "128 GB unified pool must still yield recommendations"
    primary = recs[0]
    table = lm.recommended(budget)
    assert primary.model == table[0].tag and table[0].moe  # 112 GB budget: the table's MoE-first pick leads
    assert primary.note == gpu_mod.unified_memory_note(budget)
    assert f"~{gpu_mod.UNIFIED_MEMORY_BANDWIDTH_GBPS} GB/s" in primary.note
    assert "MoE" in primary.note
    for pick in table:
        if pick.moe:
            assert pick.tag in primary.note
    assert "unified memory" in primary.reason
    assert not any(r.tier.endswith("-hybrid") for r in recs)


def test_recommend_models_discrete_gpu_has_no_note() -> None:
    with patch.object(gpu_mod, "detect_system_memory", return_value=SystemMemoryInfo(32.0, 24.0, 16.0)):
        recs = recommend_models(gpus=[RTX4090])
    assert recs
    assert all(r.note == "" for r in recs)


def test_recommend_models_unified_reserve_can_change_tier_only_via_budget() -> None:
    """Tiering follows ``model_budget_gb = total - unified_os_reserve_gb(total)``; the table's boundaries are untouched.

    The reserve is an eighth of the pool, floored at 4 GB and capped at the GB10's 16
    (``gpu_mod.unified_os_reserve_gb``), not the flat 16 GB for every pool:
    40 GB unified -> 5 GB reserve -> 35 GB budget -> the 24-40 tier, although 40 GB raw VRAM would be the 40-48 tier.
    36 GB unified -> 4 GB reserve (4.5 rounds half-to-even) -> 32 GB budget -> the same 24-40 tier.
    """
    reserve_40 = gpu_mod.unified_os_reserve_gb(40.0)
    reserve_36 = gpu_mod.unified_os_reserve_gb(36.0)
    assert (reserve_40, reserve_36) == (5.0, 4.0)
    assert reserve_40 < gpu_mod.UNIFIED_MEMORY_OS_RESERVE_GB  # the flat 16 GB is the curve's ceiling, not its value here
    forty = _gpu("NVIDIA GB10", 40 * 1024, unified=True)
    thirty_six = _gpu("NVIDIA GB10", 36 * 1024, unified=True)
    sys_mem = SystemMemoryInfo(40.0, 30.0, 21.0)

    budget_40 = _memory_budget([forty], sys_mem)
    budget_36 = _memory_budget([thirty_six], sys_mem)
    assert budget_40.model_budget_gb == 40.0 - reserve_40 == 35.0
    assert budget_36.model_budget_gb == 36.0 - reserve_36 == 32.0
    assert (budget_40.os_reserve_gb, budget_36.os_reserve_gb) == (reserve_40, reserve_36)
    assert budget_40.combined_gb == budget_40.model_budget_gb  # no CPU-offload bonus on a unified pool
    assert budget_40.cpu_offload_gb == 0.0
    assert lm.tier_for(budget_40) is lm.tier_for(35.0)      # the budget picks the tier ...
    assert lm.tier_for(budget_40) is not lm.tier_for(40.0)  # ... not the raw pool, which would be a tier up
    assert lm.tier_for(budget_36) is lm.tier_for(32.0)
    assert lm.tier_for(budget_36) is lm.tier_for(budget_40)  # both budgets sit in the same 24-40 band
    assert (lm.tier_for(budget_40).min_gb, lm.tier_for(40.0).min_gb) == (24, 40)

    with patch.object(gpu_mod, "detect_system_memory", return_value=sys_mem):
        recs_40 = recommend_models(gpus=[forty])
        recs_36 = recommend_models(gpus=[thirty_six])

    assert recs_40[0].model == lm.recommended(budget_40)[0].tag
    assert recs_36[0].model == lm.recommended(budget_36)[0].tag
    assert recs_40[0].tier == gpu_mod.recommendation_tier(lm.recommended(budget_40)[0]) == "full"
    assert recs_36[0].tier == gpu_mod.recommendation_tier(lm.recommended(budget_36)[0]) == "full"
    assert "~35 GB of 40 GB usable after the 5 GB OS reserve" in recs_40[0].reason
    assert "~32 GB of 36 GB usable after the 4 GB OS reserve" in recs_36[0].reason
    assert not any(r.tier.endswith("-hybrid") for r in recs_40 + recs_36)


def test_get_ollama_optimizations_unified_notes() -> None:
    with patch.object(gpu_mod, "detect_system_memory", return_value=SystemMemoryInfo(128.0, 100.0, 70.0)):
        opt = gpu_mod.get_ollama_optimizations(gpus=[GB10])
    assert opt.architecture == "Blackwell"
    assert opt.compute_capability == (12, 1)
    joined = " ".join(opt.notes)
    assert "LPDDR5x" in joined
    assert "GDDR7" not in joined
    assert "CPU-offload pool" in joined


# ---------------------------------------------------------------------------
# wizard_context + prompt block
# ---------------------------------------------------------------------------


def _stub_remote_helpers(monkeypatch) -> None:
    """Keep wizard_context() hermetic: no Ollama HTTP (127.0.0.1:11434) and no
    ``nvh.api.server`` import / provider registry walk. Neither goes through
    ``_safe_call``, so patching that alone does not cover them."""
    monkeypatch.setattr(context_module, "_ollama_models", lambda: [])
    monkeypatch.setattr(context_module, "_providers_summary", lambda: [])


def test_wizard_context_includes_platform_key(monkeypatch) -> None:
    """Hermetic: no nvidia-smi / sudo -n / curl / hostname / Ollama probes on the developer box."""
    from nvh.utils import platform_facts as pf

    _stub_remote_helpers(monkeypatch)
    monkeypatch.setattr(
        gpu_mod, "detect_gpu_status",
        lambda: {"status": "ready", "gpus": [RTX4090], "summary": "1 GPU ready: NVIDIA GeForce RTX 4090, 24 GB VRAM"},
    )
    monkeypatch.setattr(gpu_mod, "detect_system_memory", lambda: SystemMemoryInfo(64.0, 48.0, 40.0))

    def fake_facts(**kwargs):
        return pf.PlatformFacts(
            os="linux", arch="x86_64", machine="x86_64", device_class="workstation",
            device_label="Workstation (linux/x86_64; NVIDIA GeForce RTX 4090)",
            memory_total_gb=64.0, memory_available_gb=48.0,
        )

    monkeypatch.setattr(pf, "detect_platform_facts", fake_facts)

    ctx = wizard_context()
    assert "platform" in ctx
    assert ctx["platform"]["device_class"] == "workstation"
    assert ctx["platform"]["memory_total_gb"] == 64.0
    assert "memory_total_gb" not in ctx["gpu"]
    assert ctx["platform"]["device_class"] in {
        "dgx-spark", "rtx-spark", "dgx", "cloud-desktop", "workstation", "laptop", "unknown",
    }
    for key in ("device_label", "os", "arch", "unified_memory", "memory_total_gb", "has_root", "can_sudo", "in_sudo_group"):
        assert key in ctx["platform"]


def test_wizard_context_platform_degrades_when_helper_fails(monkeypatch) -> None:
    _stub_remote_helpers(monkeypatch)
    with patch.object(context_module, "_safe_call", side_effect=lambda label, fn, *a, **kw: None):
        ctx = wizard_context()
    assert ctx["platform"] == {"device_class": "unknown"}
    assert ctx["gpu"]["detected"] is False


def test_wizard_context_probes_gpu_once_and_feeds_platform(monkeypatch) -> None:
    _stub_remote_helpers(monkeypatch)
    calls: list[str] = []

    def fake_status():
        calls.append("status")
        return {"status": "ready", "gpus": [GB10], "summary": "1 GPU ready: NVIDIA GB10, 128 GB unified"}

    monkeypatch.setattr(gpu_mod, "detect_gpu_status", fake_status)

    from nvh.utils import platform_facts as pf

    captured: dict = {}

    def fake_facts(*, gpus=None, use_cache=True):
        captured["gpus"] = gpus
        return pf.PlatformFacts(device_class="dgx-spark", device_label="NVIDIA DGX Spark (GB10, 128 GB unified)", unified_memory=True)

    monkeypatch.setattr(pf, "detect_platform_facts", fake_facts)

    ctx = wizard_context()
    assert calls == ["status"]
    assert captured["gpus"] == [GB10]
    assert ctx["platform"]["device_class"] == "dgx-spark"
    assert ctx["gpu"]["primary"]["name"] == "NVIDIA GB10"


def _dgx_snapshot() -> dict:
    return {
        "gpu": {
            "detected": True,
            "summary": "1 GPU ready: NVIDIA GB10, 128 GB unified",
            "gpu_count": 1,
            "primary": {
                "name": "NVIDIA GB10", "vram_gb": 128.0, "utilization_pct": 3,
                "driver_version": "580.65", "cuda_version": "13.0", "architecture": "Blackwell",
                "unified_memory": True,
            },
            "unified_memory": True,
        },
        "platform": {
            "device_class": "dgx-spark",
            "device_label": "NVIDIA DGX Spark (GB10, 128 GB unified)",
            "os": "linux",
            "arch": "arm64",
            "distro": "Ubuntu 24.04.2 LTS (DGX OS 7)",
            "is_dgx_os": True,
            "unified_memory": True,
            "memory_total_gb": 128.0,
            "memory_available_gb": 100.0,
            "has_root": False,
            "can_sudo": False,
            "in_sudo_group": True,
            "windows_on_arm": False,
        },
        "storage": {"available": True, "home": "/home/nvidia/.nvh", "free_gb": 900.0, "total_gb": 1000.0, "ok": True, "warnings": []},
        "providers": [{"name": "openai", "healthy": True}],
        "ollama_models": [{"name": "nemotron3:33b"}],
        "recent_jobs": [],
        "receipts": {"count": 2, "unhealthy": 0},
        "vault": {"initialized": True, "memory_files": 3},
    }


def test_format_context_block_includes_platform_object() -> None:
    block = _format_context_block(_dgx_snapshot())
    parsed = json.loads(block)

    assert parsed["platform"]["device_class"] == "dgx-spark"
    assert parsed["platform"]["device_label"] == "NVIDIA DGX Spark (GB10, 128 GB unified)"
    assert parsed["platform"]["arch"] == "arm64"
    assert parsed["platform"]["is_dgx_os"] is True
    assert parsed["platform"]["unified_memory"] is True
    assert parsed["platform"]["memory_total_gb"] == 128.0
    assert parsed["platform"]["memory_available_gb"] == 100.0
    # Decision-relevant False booleans survive; noise booleans are dropped.
    assert parsed["platform"]["can_sudo"] is False
    assert parsed["platform"]["has_root"] is False
    assert parsed["platform"]["in_sudo_group"] is True
    assert "windows_on_arm" not in parsed["platform"]
    # GPU block carries only the unified flag; the pool size is the platform block's.
    assert parsed["gpu"]["unified_memory"] is True
    assert "memory_total_gb" not in parsed["gpu"]
    assert parsed["gpu"]["name"] == "NVIDIA GB10"


def test_format_context_block_omits_platform_when_absent() -> None:
    snapshot = _dgx_snapshot()
    snapshot.pop("platform")
    parsed = json.loads(_format_context_block(snapshot))
    assert "platform" not in parsed


def test_format_context_block_drops_empty_platform_fields() -> None:
    snapshot = _dgx_snapshot()
    snapshot["platform"] = {"device_class": "workstation", "distro": "", "memory_total_gb": 0.0, "is_dgx_os": False, "can_sudo": False}
    parsed = json.loads(_format_context_block(snapshot))
    assert parsed["platform"] == {"device_class": "workstation", "can_sudo": False}


def _workstation_snapshot() -> dict:
    snapshot = _dgx_snapshot()
    snapshot["gpu"] = {
        "detected": True,
        "summary": "1 GPU ready: NVIDIA GeForce RTX 4090, 24 GB VRAM",
        "gpu_count": 1,
        "primary": {
            "name": "NVIDIA GeForce RTX 4090", "vram_gb": 24.0, "utilization_pct": 3,
            "driver_version": "580.65", "cuda_version": "13.0", "architecture": "Ada Lovelace",
            "unified_memory": False,
        },
    }
    snapshot["platform"] = {
        "device_class": "workstation",
        "device_label": "Workstation (linux/x86_64; NVIDIA GeForce RTX 4090)",
        "os": "linux", "arch": "x86_64", "distro": "Ubuntu 24.04", "is_dgx_os": False,
        "unified_memory": False, "memory_total_gb": 64.0, "memory_available_gb": 40.0,
        "has_root": False, "can_sudo": True, "in_sudo_group": True, "windows_on_arm": False,
    }
    return snapshot


def test_format_context_block_discrete_gpu_never_shows_system_ram_as_gpu_memory() -> None:
    """W3: vram_gb=24 must not sit next to memory_total_gb=64 inside the gpu block."""
    parsed = json.loads(_format_context_block(_workstation_snapshot()))
    assert parsed["gpu"]["vram_gb"] == 24.0
    assert "memory_total_gb" not in parsed["gpu"]
    assert "unified_memory" not in parsed["gpu"]
    # The platform block is the single owner of the RAM facts...
    assert parsed["platform"]["memory_total_gb"] == 64.0
    assert parsed["platform"]["memory_available_gb"] == 40.0
    # ...and "not a unified pool" is kept as an explicit False.
    assert parsed["platform"]["unified_memory"] is False


def test_compact_platform_keeps_unified_memory_false() -> None:
    """'Not a unified pool' is a decision the Wizard must respect — it survives compaction."""
    out = _compact_platform({
        "device_class": "workstation", "unified_memory": False, "is_dgx_os": False,
        "windows_on_arm": False, "memory_total_gb": 0.0, "distro": "",
    })
    assert out == {"device_class": "workstation", "unified_memory": False}


def test_build_system_prompt_mentions_dgx_spark() -> None:
    prompt = build_system_prompt(_dgx_snapshot())
    assert "dgx-spark" in prompt
    assert "NVIDIA DGX Spark (GB10, 128 GB unified)" in prompt
    assert "platform-dgx-spark" not in prompt  # findings are passed separately


# ---------------------------------------------------------------------------
# findings
# ---------------------------------------------------------------------------


def test_findings_emit_platform_dgx_spark_info() -> None:
    findings = derive_findings(_dgx_snapshot())
    ids = {f.id for f in findings}
    assert "platform-dgx-spark" in ids
    f = next(f for f in findings if f.id == "platform-dgx-spark")
    assert f.severity == "info"
    assert f.category == "gpu"
    assert "128 GB" in f.detail
    assert "unified memory" in f.detail
    assert "MoE" in f.detail
    assert f.suggested_tool is None
    assert "platform-rtx-spark" not in ids


def test_findings_emit_platform_rtx_spark_provisional() -> None:
    snapshot = _dgx_snapshot()
    snapshot["platform"].update({"device_class": "rtx-spark", "os": "windows", "windows_on_arm": True, "memory_total_gb": 64.0})
    findings = derive_findings(snapshot)
    f = next(f for f in findings if f.id == "platform-rtx-spark")
    assert f.severity == "info"
    assert "provisional" in f.detail.lower() or "provisional" in f.title.lower()
    assert "64 GB" in f.detail


def test_findings_no_platform_finding_for_workstation() -> None:
    snapshot = _dgx_snapshot()
    snapshot["platform"]["device_class"] = "workstation"
    ids = {f.id for f in derive_findings(snapshot)}
    assert not any(i.startswith("platform-") for i in ids)


def _no_gpu_snapshot(device_class: str | None) -> dict:
    snapshot = _dgx_snapshot()
    snapshot["gpu"] = {"detected": False, "summary": "no NVIDIA GPU detected (nvidia-smi missing)", "gpu_count": 0, "primary": None}
    if device_class is None:
        snapshot["platform"] = {}
    else:
        snapshot["platform"]["device_class"] = device_class
    return snapshot


def test_findings_dgx_spark_with_hidden_gpu_is_one_warning() -> None:
    """W1: a container on a Spark without NVML passthrough (platform rule 4) — one finding, no contradiction."""
    findings = derive_findings(_no_gpu_snapshot("dgx-spark"))
    ids = [f.id for f in findings]
    assert "platform-dgx-spark-gpu-hidden" in ids
    assert "gpu-missing" not in ids
    assert "platform-dgx-spark" not in ids
    hidden = next(f for f in findings if f.id == "platform-dgx-spark-gpu-hidden")
    assert hidden.severity == "warn"
    assert hidden.category == "gpu"
    assert "--gpus all" in hidden.detail
    assert "NVML" in hidden.detail
    assert "rented" not in hidden.detail


@pytest.mark.parametrize("device_class", ["dgx", "rtx-spark", "workstation", "laptop"])
def test_findings_gpu_missing_on_owned_hardware_talks_about_the_driver(device_class: str) -> None:
    missing = next(f for f in derive_findings(_no_gpu_snapshot(device_class)) if f.id == "gpu-missing")
    assert missing.severity == "info"
    assert "rented" not in missing.detail
    assert "this machine's NVIDIA driver is installed" in missing.detail
    assert "nvidia-smi" in missing.detail


@pytest.mark.parametrize("device_class", ["cloud-desktop", "unknown", None])
def test_findings_gpu_missing_on_rented_or_unknown_keeps_instance_wording(device_class: str | None) -> None:
    missing = next(f for f in derive_findings(_no_gpu_snapshot(device_class)) if f.id == "gpu-missing")
    assert "rented instance was provisioned with one" in missing.detail


@pytest.mark.parametrize("memory_total_gb", [0, 0.0, None, "n/a"])
def test_findings_dgx_spark_omits_pool_size_when_not_measured(memory_total_gb) -> None:
    """W2: never fall back to a hard-coded 128 GB."""
    snapshot = _dgx_snapshot()
    snapshot["platform"]["memory_total_gb"] = memory_total_gb
    f = next(f for f in derive_findings(snapshot) if f.id == "platform-dgx-spark")
    assert "128" not in f.detail
    assert not re.search(r"\d+ GB of", f.detail)
    assert "LPDDR5x unified memory" in f.detail


def test_findings_rtx_spark_is_number_free_until_measured() -> None:
    """W2: RTX Spark memory is unknown until hardware ships — no figure, still provisional."""
    snapshot = _dgx_snapshot()
    snapshot["platform"].update({"device_class": "rtx-spark", "os": "windows", "windows_on_arm": True, "memory_total_gb": 0.0})
    f = next(f for f in derive_findings(snapshot) if f.id == "platform-rtx-spark")
    assert "provisional" in f.title.lower()
    assert "128" not in f.detail
    assert not re.search(r"\d+ GB of", f.detail)
    assert "with unified memory shared with Windows" in f.detail


# ---------------------------------------------------------------------------
# reconnect panel shows architecture
# ---------------------------------------------------------------------------


def test_reconnect_labels_and_surfaces_machine() -> None:
    assert _label_for("machine") == "Architecture"
    survived = _survived_facts({"machine": "aarch64", "gpu_name": "NVIDIA GB10"}, [])
    labels = {row["label"]: row["value"] for row in survived}
    assert labels["Architecture"] == "aarch64"
    assert labels["GPU"] == "NVIDIA GB10"
