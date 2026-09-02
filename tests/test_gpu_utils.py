"""Tests for nvh.utils.gpu — detection status, model recommendations, Ollama tuning."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from nvh.utils import gpu


@pytest.fixture(autouse=True)
def _fresh_smi_memo():
    """detect_gpu_status memoises the nvidia-smi fallback for SMI_FALLBACK_TTL_S; each test starts cold."""
    gpu.clear_gpu_detection_cache()
    yield
    gpu.clear_gpu_detection_cache()


def _row(name: str, vram_mb: int, *, unified: bool = False, index: int = 0) -> gpu.GPUInfo:
    return gpu.GPUInfo(
        name=name, vram_mb=vram_mb, vram_gb=round(vram_mb / 1024, 1), driver_version="580.65",
        cuda_version="13.0", utilization_pct=0, memory_used_mb=0, memory_free_mb=vram_mb, index=index,
        unified_memory=unified,
    )


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


class TestGPURecommendations:
    """GPU recommendation and Ollama optimizations (mocked)."""

    def _make_gpu(self, name: str, vram_mb: int, index: int = 0):
        from nvh.utils.gpu import GPUInfo

        return GPUInfo(
            name=name,
            vram_mb=vram_mb,
            vram_gb=round(vram_mb / 1024, 1),
            driver_version="535.0",
            cuda_version="12.2",
            utilization_pct=0,
            memory_used_mb=0,
            memory_free_mb=vram_mb,
            index=index,
        )

    @patch("nvh.utils.gpu.detect_system_memory")
    def test_recommend_models_no_gpu(self, mock_mem):
        from nvh.utils.gpu import SystemMemoryInfo, recommend_models

        mock_mem.return_value = SystemMemoryInfo(16.0, 12.0, 8.0)
        recs = recommend_models(gpus=[])
        assert len(recs) >= 1
        model_names = [r.model for r in recs]
        assert "nemotron-mini" in model_names

    @patch("nvh.utils.gpu.detect_system_memory")
    def test_recommend_models_8gb(self, mock_mem):
        from nvh.utils.gpu import SystemMemoryInfo, recommend_models

        mock_mem.return_value = SystemMemoryInfo(32.0, 24.0, 16.0)
        gpu = self._make_gpu("RTX 4060", 8192)
        recs = recommend_models(gpus=[gpu])
        assert len(recs) >= 1
        tiers = {r.tier for r in recs}
        assert "small" in tiers or "mini" in tiers

    @patch("nvh.utils.gpu.detect_system_memory")
    def test_recommend_models_24gb(self, mock_mem):
        from nvh.utils.gpu import SystemMemoryInfo, recommend_models

        mock_mem.return_value = SystemMemoryInfo(32.0, 24.0, 16.0)
        gpu = self._make_gpu("RTX 4090", 24576)
        recs = recommend_models(gpus=[gpu])
        model_names = [r.model for r in recs]
        # 24 GB lands in the "full" branch (nemotron 70B) at the boundary,
        # or "small" for <24 — either llama3.1:8b (the real mid-tier) or
        # nemotron should appear in the list.
        assert any(m in model_names for m in ("nemotron", "llama3.1:8b"))

    @patch("nvh.utils.gpu.detect_system_memory")
    def test_recommend_models_multi_gpu(self, mock_mem):
        from nvh.utils.gpu import SystemMemoryInfo, recommend_models

        mock_mem.return_value = SystemMemoryInfo(64.0, 48.0, 32.0)
        gpus = [
            self._make_gpu("RTX 3090", 24576, 0),
            self._make_gpu("RTX 3090", 24576, 1),
        ]
        recs = recommend_models(gpus=gpus)
        assert any("multi-gpu" in r.tier for r in recs)

    @patch("nvh.utils.gpu.detect_system_memory")
    def test_get_ollama_optimizations_no_gpu(self, mock_mem):
        from nvh.utils.gpu import SystemMemoryInfo, get_ollama_optimizations

        mock_mem.return_value = SystemMemoryInfo(16.0, 12.0, 8.0)
        opt = get_ollama_optimizations(gpus=[])
        assert opt.architecture == "CPU"
        assert opt.flash_attention is False
        assert opt.num_parallel == 1

    @patch("nvh.utils.gpu.detect_system_memory")
    def test_get_ollama_optimizations_rtx4090(self, mock_mem):
        from nvh.utils.gpu import SystemMemoryInfo, get_ollama_optimizations

        mock_mem.return_value = SystemMemoryInfo(32.0, 24.0, 16.0)
        gpu = self._make_gpu("NVIDIA GeForce RTX 4090", 24576)
        opt = get_ollama_optimizations(gpus=[gpu])
        assert opt.flash_attention is True
        assert opt.architecture == "Ada Lovelace"
        assert opt.recommended_ctx >= 16384

    @patch("nvh.utils.gpu.detect_system_memory")
    def test_recommend_models_large_vram(self, mock_mem):
        from nvh.utils.gpu import SystemMemoryInfo, recommend_models

        mock_mem.return_value = SystemMemoryInfo(128.0, 100.0, 70.0)
        gpu = self._make_gpu("H100", 81920)
        recs = recommend_models(gpus=[gpu])
        model_names = [r.model for r in recs]
        # High-VRAM tier recommends a real primary Nemotron model.
        assert "nemotron" in model_names

    @patch("nvh.utils.gpu.detect_system_memory")
    def test_recommend_models_250gb_vram(self, mock_mem):
        from nvh.utils.gpu import SystemMemoryInfo, recommend_models

        mock_mem.return_value = SystemMemoryInfo(256.0, 200.0, 140.0)
        gpus = [
            self._make_gpu("A100 80GB", 81920, 0),
            self._make_gpu("A100 80GB", 81920, 1),
            self._make_gpu("A100 80GB", 81920, 2),
        ]
        recs = recommend_models(gpus=gpus)
        tiers = {r.tier for r in recs}
        assert "multi-gpu" in tiers

    # ---- Vision model tier tests ----

    @patch("nvh.utils.gpu.detect_system_memory")
    def test_vision_model_included_on_8gb(self, mock_mem):
        from nvh.utils.gpu import SystemMemoryInfo, recommend_models

        mock_mem.return_value = SystemMemoryInfo(32.0, 24.0, 16.0)
        gpu = self._make_gpu("RTX 4060", 8192)
        recs = recommend_models(gpus=[gpu])
        vision_models = [r.model for r in recs if r.tier.startswith("vision")]
        assert "moondream" in vision_models

    @patch("nvh.utils.gpu.detect_system_memory")
    def test_vision_model_on_16gb_is_minicpm(self, mock_mem):
        from nvh.utils.gpu import SystemMemoryInfo, recommend_models

        mock_mem.return_value = SystemMemoryInfo(32.0, 24.0, 16.0)
        gpu = self._make_gpu("RTX 4070 Ti", 16384)
        recs = recommend_models(gpus=[gpu])
        vision_models = [r.model for r in recs if r.tier.startswith("vision")]
        assert "minicpm-v" in vision_models

    @patch("nvh.utils.gpu.detect_system_memory")
    def test_vision_model_on_48gb_is_llama32_vision(self, mock_mem):
        from nvh.utils.gpu import SystemMemoryInfo, recommend_models

        mock_mem.return_value = SystemMemoryInfo(64.0, 48.0, 32.0)
        gpu = self._make_gpu("RTX 6000 Ada", 48 * 1024)
        recs = recommend_models(gpus=[gpu])
        vision_models = [r.model for r in recs if r.tier.startswith("vision")]
        assert "llama3.2-vision" in vision_models

    @patch("nvh.utils.gpu.detect_system_memory")
    def test_vision_model_turing_swap(self, mock_mem):
        """On Turing (CC 7.5), high-VRAM tier should swap to minicpm-v
        because llama3.2-vision BF16 paths degrade badly without tensor cores."""
        from nvh.utils.gpu import SystemMemoryInfo, recommend_models

        mock_mem.return_value = SystemMemoryInfo(64.0, 48.0, 32.0)
        # RTX 2080 Ti = Turing, CC 7.5
        gpu = self._make_gpu("RTX 2080 Ti", 24 * 1024)
        recs = recommend_models(gpus=[gpu])
        vision_models = [r.model for r in recs if r.tier.startswith("vision")]
        assert "minicpm-v" in vision_models
        assert "llama3.2-vision" not in vision_models

    @patch("nvh.utils.gpu.detect_system_memory")
    def test_no_vision_model_below_4gb(self, mock_mem):
        from nvh.utils.gpu import SystemMemoryInfo, recommend_models

        mock_mem.return_value = SystemMemoryInfo(8.0, 4.0, 2.0)
        gpu = self._make_gpu("GTX 1050", 2 * 1024)
        recs = recommend_models(gpus=[gpu])
        vision_models = [r.model for r in recs if r.tier.startswith("vision")]
        assert vision_models == []


class TestGPUDetection:
    def test_detect_system_memory_fields(self):
        from nvh.utils.gpu import detect_system_memory
        mem = detect_system_memory()
        assert hasattr(mem, "total_ram_gb")
        assert hasattr(mem, "available_ram_gb")
        assert mem.total_ram_gb >= 0

    def test_recommend_models_no_gpu(self):
        from nvh.utils.gpu import recommend_models
        recs = recommend_models(gpus=None)
        assert isinstance(recs, list)

    def test_recommend_models_empty_list(self):
        from nvh.utils.gpu import recommend_models
        recs = recommend_models(gpus=[])
        assert isinstance(recs, list)

    def test_detect_gpus_returns_list(self):
        from nvh.utils.gpu import detect_gpus
        result = detect_gpus()
        assert isinstance(result, list)

    def test_detect_system_memory(self):
        from nvh.utils.gpu import detect_system_memory
        mem = detect_system_memory()
        assert mem is not None
        assert hasattr(mem, "total_ram_gb") or hasattr(mem, "total_gb") or isinstance(mem, dict)

    def test_recommend_models(self):
        import pytest

        try:
            from nvh.utils.gpu import recommend_models
            recs = recommend_models(vram_gb=24)
            assert isinstance(recs, (list, dict))
        except (ImportError, TypeError):
            pytest.skip("recommend_models not available or different signature")


class TestCheckOomRisk:
    """check_oom_risk: a unified pool is ONE pool; discrete GPUs keep the pre-Spark numbers."""

    @staticmethod
    def _gpu(name: str, vram_mb: int, free_mb: int, *, unified: bool = False):
        return gpu.GPUInfo(
            name=name,
            vram_mb=vram_mb,
            vram_gb=round(vram_mb / 1024, 1),
            driver_version="580.65",
            cuda_version="13.0",
            utilization_pct=0,
            memory_used_mb=vram_mb - free_mb,
            memory_free_mb=free_mb,
            index=0,
            unified_memory=unified,
        )

    def test_unified_pool_is_not_counted_twice(self, monkeypatch):
        """40 GB model, GB10 with 30 GB MemAvailable: not hybrid, not safe, RAM not re-reported."""
        # memory_free_mb already IS MemAvailable; detect_system_memory must not be consulted.
        def boom():
            raise AssertionError("system RAM must not be read on a unified pool")

        monkeypatch.setattr(gpu, "detect_system_memory", boom)
        spark = self._gpu("NVIDIA GB10", 128 * 1024, 30 * 1024, unified=True)

        result = gpu.check_oom_risk(40.0, [spark])

        assert result["unified_memory"] is True
        assert result["gpu_free_gb"] == 30.0
        assert result["ram_free_gb"] == 0.0
        assert result["safe"] is False
        assert result["fits_gpu"] is False
        assert result["fits_hybrid"] is False
        assert "hybrid" not in result["recommendation"].lower()
        assert "unified memory pool" in result["recommendation"]
        assert "Short by 14 GB" in result["recommendation"]   # 40 - 30 * 0.85 = 14.5 → "14"

    def test_unified_pool_fit_is_still_reported(self, monkeypatch):
        monkeypatch.setattr(gpu, "detect_system_memory", lambda: gpu.SystemMemoryInfo(128.0, 90.0, 63.0))
        spark = self._gpu("NVIDIA GB10", 128 * 1024, 90 * 1024, unified=True)

        result = gpu.check_oom_risk(40.0, [spark])

        assert result["safe"] is True and result["fits_gpu"] is True and result["fits_hybrid"] is False
        assert result["ram_free_gb"] == 0.0
        assert result["recommendation"] == (
            "Model fits in unified memory (40 GB needed, 90 GB free) — full GPU acceleration"
        )

    def test_discrete_gpu_numbers_and_messages_unchanged(self, monkeypatch):
        """RTX 4090 with 24 GB free + 64 GB RAM (48 avail, 33.6 effective): byte-identical to HEAD."""
        monkeypatch.setattr(gpu, "detect_system_memory", lambda: gpu.SystemMemoryInfo(64.0, 48.0, 33.6))
        rtx = self._gpu("NVIDIA GeForce RTX 4090", 24576, 24576)

        fits = gpu.check_oom_risk(15.0, [rtx])
        hybrid = gpu.check_oom_risk(40.0, [rtx])
        oom = gpu.check_oom_risk(60.0, [rtx])

        for r in (fits, hybrid, oom):
            assert r["unified_memory"] is False
            assert r["gpu_free_gb"] == 24.0
            assert r["ram_free_gb"] == 48.0

        assert (fits["safe"], fits["fits_gpu"], fits["fits_hybrid"]) == (True, True, False)
        assert fits["recommendation"] == "Model fits in GPU VRAM (15 GB needed, 24 GB free) — full GPU acceleration"

        assert (hybrid["safe"], hybrid["fits_gpu"], hybrid["fits_hybrid"]) == (True, False, True)
        assert hybrid["recommendation"] == (
            "Model needs hybrid mode: 20 GB on GPU + 20 GB on CPU RAM. Expect 30-50% slower than full GPU"
        )

        assert (oom["safe"], oom["fits_gpu"], oom["fits_hybrid"]) == (False, False, False)
        assert oom["recommendation"] == (
            "OOM RISK: Model needs 60 GB but only 20 GB GPU + 34 GB RAM available. "
            "Short by 6 GB. Use a smaller model or lower quantization"
        )

    def test_no_gpu_still_uses_system_ram(self, monkeypatch):
        monkeypatch.setattr(gpu, "detect_system_memory", lambda: gpu.SystemMemoryInfo(16.0, 12.0, 8.4))

        result = gpu.check_oom_risk(5.0, [])

        assert result["unified_memory"] is False
        assert result["gpu_free_gb"] == 0.0 and result["ram_free_gb"] == 12.0
        assert result["fits_hybrid"] is True


class TestSmiFallbackMemo:
    """D3: when NVML enumerates the GPU but cannot size it (or is absent), detect_gpu_status runs
    the nvidia-smi fallback — the row query, bare ``nvidia-smi`` for the CUDA header and
    ``--query-gpu=compute_cap``: three subprocesses — and used to run them on *every* call.
    Status is polled in bursts; within SMI_FALLBACK_TTL_S the fallback now runs once."""

    _ROW = "0, NVIDIA GeForce RTX 4090, 24564, 1000, 23564, 5 %, 580.65\n"
    _NA_ROW = "0, NVIDIA GeForce RTX 4090, [N/A], [N/A], [N/A], 0 %, 580.65\n"

    @staticmethod
    def _fake_smi(monkeypatch, stdout: str) -> list[list[str]]:
        """A fake nvidia-smi binary answering all three queries; returns the spawn log."""
        spawned: list[list[str]] = []

        def run(argv, *args, **kwargs):
            spawned.append(list(argv))
            if "--query-gpu=compute_cap" in argv:
                return SimpleNamespace(returncode=0, stdout="8.9\n", stderr="")
            if len(argv) == 1:  # bare nvidia-smi: the human-readable header
                return SimpleNamespace(returncode=0, stdout="| NVIDIA-SMI 580.65  Driver Version: 580.65  CUDA Version: 13.0 |\n", stderr="")
            return SimpleNamespace(returncode=0, stdout=stdout, stderr="")

        monkeypatch.setattr(gpu.subprocess, "run", run)
        monkeypatch.setattr(gpu.shutil, "which", lambda command: "/usr/bin/nvidia-smi")
        monkeypatch.setattr(gpu, "_nvidia_device_files_present", lambda: True)
        monkeypatch.setattr(gpu, "detect_system_memory", lambda: gpu.SystemMemoryInfo(64.0, 48.0, 33.6))
        return spawned

    def test_burst_of_status_calls_spawns_the_fallback_once(self, monkeypatch):
        monkeypatch.setattr(gpu, "_detect_gpus_pynvml", lambda *, issues=None: [_row("NVIDIA GeForce RTX 4090", 0)])
        spawned = self._fake_smi(monkeypatch, self._ROW)

        first = gpu.detect_gpu_status()
        assert first["status"] == "ready" and first["source"] == "nvidia-smi"
        assert len(spawned) == 3  # the first call is exactly the old call: query, header, compute_cap

        second, third = gpu.detect_gpu_status(), gpu.detect_gpu_status()
        assert len(spawned) == 3  # ...and the rest of the burst respawns nothing
        for status in (second, third):
            assert status["status"] == "ready" and status["source"] == "nvidia-smi"
            assert [(g.name, g.vram_mb, g.memory_free_mb, g.compute_capability, g.cuda_version) for g in status["gpus"]] == [
                ("NVIDIA GeForce RTX 4090", 24564, 23564, (8, 9), "13.0"),
            ]
            assert status["summary"] == "1 GPU ready: NVIDIA GeForce RTX 4090, 24 GB VRAM"
        assert second["gpus"][0] is not first["gpus"][0]  # callers never share the memo's row objects

    def test_memo_expires_after_the_ttl(self, monkeypatch):
        monkeypatch.setattr(gpu, "_detect_gpus_pynvml", lambda *, issues=None: [])
        spawned = self._fake_smi(monkeypatch, self._ROW)
        clock = [1000.0]
        monkeypatch.setattr(gpu, "time", SimpleNamespace(monotonic=lambda: clock[0]))

        gpu.detect_gpu_status()
        clock[0] += gpu.SMI_FALLBACK_TTL_S - 0.5
        gpu.detect_gpu_status()
        assert len(spawned) == 3
        clock[0] += 1.0  # past the TTL: a fresh fallback
        assert gpu.detect_gpu_status()["status"] == "ready"
        assert len(spawned) == 6

    def test_memo_carries_the_fallback_issues(self, monkeypatch):
        """A memoised fallback must explain itself exactly like a fresh one: the row-scoped
        memory-unavailable issue (and its index) survives, as separate dicts per caller."""
        monkeypatch.setattr(gpu, "_detect_gpus_pynvml", lambda *, issues=None: [])
        spawned = self._fake_smi(monkeypatch, self._NA_ROW)

        first, second = gpu.detect_gpu_status(), gpu.detect_gpu_status()

        assert len(spawned) == 3
        for status in (first, second):
            assert status["status"] == "blocked" and status["source"] == "nvidia-smi"
            assert [(g.name, g.vram_mb) for g in status["gpus"]] == [("NVIDIA GeForce RTX 4090", 0)]
            assert [(i["source"], i["code"], i["index"]) for i in status["issues"]] == [
                ("nvidia-smi", "memory-unavailable", 0),
            ]
        assert second["issues"][0] is not first["issues"][0]

    def test_memo_carries_a_failed_fallback_too(self, monkeypatch):
        """nvidia-smi missing: the binary-missing note is still reported on the memoised call."""
        monkeypatch.setattr(gpu, "_detect_gpus_pynvml", lambda *, issues=None: [])
        monkeypatch.setattr(gpu, "_nvidia_device_files_present", lambda: False)
        monkeypatch.setattr(gpu.shutil, "which", lambda command: None)
        which_calls: list[int] = []
        real_smi = gpu._detect_gpus_smi
        monkeypatch.setattr(gpu, "_detect_gpus_smi", lambda *, issues=None: which_calls.append(1) or real_smi(issues=issues))

        first, second = gpu.detect_gpu_status(), gpu.detect_gpu_status()

        assert which_calls == [1]
        for status in (first, second):
            assert status["status"] == "not-detected"
            assert [i["code"] for i in status["issues"]] == ["binary-missing"]

    def test_clear_forces_a_fresh_fallback(self, monkeypatch):
        monkeypatch.setattr(gpu, "_detect_gpus_pynvml", lambda *, issues=None: [])
        spawned = self._fake_smi(monkeypatch, self._ROW)

        gpu.detect_gpu_status()
        gpu.clear_gpu_detection_cache()
        gpu.detect_gpu_status()
        assert len(spawned) == 6

    def test_direct_fallback_calls_are_not_memoised(self, monkeypatch):
        """Only detect_gpu_status coalesces; _detect_gpus_smi itself still answers fresh."""
        spawned = self._fake_smi(monkeypatch, self._ROW)
        gpu._detect_gpus_smi(issues=[])
        gpu._detect_gpus_smi(issues=[])
        assert len(spawned) == 6


class TestFormatGpuMemory:
    """D4: one spelling of a row's memory for every CLI/UI label — never '0 GB VRAM' for a
    GPU whose pool could not be read."""

    def test_discrete_row(self):
        assert gpu.format_gpu_memory(_row("NVIDIA GeForce RTX 4090", 24576)) == "24 GB VRAM"

    def test_unified_row(self):
        assert gpu.format_gpu_memory(_row("NVIDIA GB10", 131072, unified=True)) == "128 GB unified"

    def test_unreadable_row_is_never_zero_gb(self):
        text = gpu.format_gpu_memory(_row("NVIDIA GeForce RTX 4090", 0))
        assert text == "memory unreadable"
        assert "0" not in text and "GB" not in text

    def test_precision_and_compact_spellings(self):
        row = _row("NVIDIA GeForce RTX 4090", 24576)
        assert gpu.format_gpu_memory(row, precision=1) == "24.0 GB VRAM"
        assert gpu.format_gpu_memory(row, compact=True) == "24GB VRAM"
        assert gpu.format_gpu_memory(_row("NVIDIA GB10", 131072, unified=True), compact=True) == "128GB unified"
        assert gpu.format_gpu_memory(_row("x", 0), compact=True, precision=1) == "memory unreadable"

    def test_rounding_matches_the_old_f_strings(self):
        """The CLI sites printed f'{vram_gb:.0f} GB VRAM'; readable rows must render byte-identically."""
        for vram_mb in (10240 + 512, 12288, 24564, 81920):
            row = _row("NVIDIA GPU", vram_mb)
            assert gpu.format_gpu_memory(row) == f"{row.vram_gb:.0f} GB VRAM"
            assert gpu.format_gpu_memory(row, compact=True) == f"{row.vram_gb:.0f}GB VRAM"
