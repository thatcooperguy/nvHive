"""Tests for nvh.utils.gpu_emulation — performance estimation."""

from __future__ import annotations


class TestGPUEmulation:
    def test_estimate_measured_baseline(self):
        from nvh.utils.gpu_emulation import estimate_performance

        est = estimate_performance("gb10", "nemotron-mini")
        assert est is not None
        assert est.confidence == "measured"
        assert est.estimated_toks == 86.6

    def test_estimate_scaled(self):
        from nvh.utils.gpu_emulation import estimate_performance

        est = estimate_performance("rtx_4090", "nemotron-mini")
        assert est is not None
        assert est.fits_in_vram is True
        assert est.estimated_toks > 0

    def test_estimate_model_too_large(self):
        from nvh.utils.gpu_emulation import estimate_performance

        est = estimate_performance("rtx_4060", "nemotron")
        assert est is not None
        assert est.fits_in_vram is False

    def test_estimate_unknown_gpu(self):
        from nvh.utils.gpu_emulation import estimate_performance

        assert estimate_performance("nonexistent_gpu", "nemotron-mini") is None

    def test_estimate_unknown_model(self):
        from nvh.utils.gpu_emulation import estimate_performance

        assert estimate_performance("rtx_4090", "nonexistent_model") is None

    def test_estimate_all_models(self):
        from nvh.utils.gpu_emulation import estimate_all_models

        results = estimate_all_models("rtx_4090")
        assert len(results) >= 1
        assert all(r.gpu_name == "RTX 4090" for r in results)

    def test_estimate_all_gpus(self):
        from nvh.utils.gpu_emulation import estimate_all_gpus

        results = estimate_all_gpus("nemotron-mini")
        assert len(results) >= 1

    def test_gpu_database_has_entries(self):
        from nvh.utils.gpu_emulation import GPU_DATABASE

        assert len(GPU_DATABASE) > 5
        assert "rtx_4090" in GPU_DATABASE

    def test_estimate_performance_known_gpu(self):
        from nvh.utils.gpu_emulation import estimate_performance
        est = estimate_performance("rtx_4090", "nemotron-mini")
        assert est is not None
        assert est.fits_in_vram is True
        assert est.estimated_toks > 0
        assert est.gpu_name == "RTX 4090"

    def test_estimate_performance_unknown_gpu(self):
        from nvh.utils.gpu_emulation import estimate_performance
        assert estimate_performance("nonexistent_gpu", "nemotron-mini") is None

    def test_estimate_all_models_returns_list(self):
        from nvh.utils.gpu_emulation import estimate_all_models
        results = estimate_all_models("rtx_4090")
        assert isinstance(results, list)
        assert len(results) > 0

    def test_estimate_all_gpus_returns_list(self):
        from nvh.utils.gpu_emulation import estimate_all_gpus
        results = estimate_all_gpus("nemotron-mini")
        assert isinstance(results, list)
        assert len(results) > 0
