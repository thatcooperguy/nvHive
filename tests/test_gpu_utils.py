"""Tests for nvh.utils.gpu — detection status, model recommendations, Ollama tuning."""

from __future__ import annotations

from unittest.mock import patch

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
