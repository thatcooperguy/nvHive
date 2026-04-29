"""Tests for rootless GPU detection diagnostics."""

from __future__ import annotations

from nvh.utils import gpu


def test_detect_gpu_status_distinguishes_blocked_devices(monkeypatch) -> None:
    monkeypatch.setattr(gpu, "_detect_gpus_pynvml", lambda *, issues=None: [])
    monkeypatch.setattr(gpu, "_detect_gpus_smi", lambda *, issues=None: [])
    monkeypatch.setattr(gpu, "_nvidia_device_files_present", lambda: True)
    monkeypatch.setattr(gpu.shutil, "which", lambda command: "/usr/bin/nvidia-smi" if command == "nvidia-smi" else None)

    status = gpu.detect_gpu_status()

    assert status["status"] == "blocked"
    assert status["device_files_present"] is True
    assert any(issue["code"] == "devices-present-no-query" for issue in status["issues"])


def test_gpu_architecture_info_marks_name_heuristic() -> None:
    info = gpu.GPUInfo(
        name="NVIDIA RTX 4090",
        vram_mb=24576,
        vram_gb=24.0,
        driver_version="570.00",
        cuda_version="12.4",
        utilization_pct=0,
        memory_used_mb=0,
        memory_free_mb=24576,
        index=0,
    )

    arch = gpu.gpu_architecture_info(info)

    assert arch["architecture"] == "Ada Lovelace"
    assert arch["heuristic"] is True
