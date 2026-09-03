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
        from nvh.core import local_models
        from nvh.utils.gpu_emulation import GPU_DATABASE, estimate_performance

        sizes = local_models.size_table()
        biggest = max(sizes, key=sizes.get)
        assert sizes[biggest] > GPU_DATABASE["rtx_4060"].vram_gb
        est = estimate_performance("rtx_4060", biggest)
        assert est is not None
        assert est.fits_in_vram is False

    def test_model_memory_is_the_tier_table_plus_measured_keys(self):
        from nvh.core import local_models
        from nvh.utils.gpu_emulation import (
            _MEASURED_BASELINES,
            _MEASURED_ONLY_MEMORY_GB,
            _MODEL_MEMORY_GB,
        )

        sizes = local_models.size_table()
        for tag, gb in sizes.items():
            assert _MODEL_MEMORY_GB[tag] == gb
        # every measured model stays estimable, and nothing else survives
        for _gpu_key, model in _MEASURED_BASELINES:
            assert model in _MODEL_MEMORY_GB
        assert set(_MODEL_MEMORY_GB) == set(sizes) | set(_MEASURED_ONLY_MEMORY_GB)
        assert set(_MEASURED_ONLY_MEMORY_GB) <= {model for _g, model in _MEASURED_BASELINES}
        assert _MEASURED_ONLY_MEMORY_GB["gemma3"] == sizes["gemma3:4b"]
        for retired in ("codellama", "llama3.3:70b", "llama3.1:70b", "nemotron",
                        "qwen2.5-coder:32b", "qwen2.5-coder:7b", "minicpm-v", "llama3.1:8b"):
            assert retired not in _MODEL_MEMORY_GB

    def test_latest_suffix_resolves_through_the_table(self):
        from nvh.core import local_models
        from nvh.utils.gpu_emulation import estimate_performance

        est = estimate_performance("rtx_4090", "moondream:latest")
        assert est is not None
        assert est.fits_in_vram is True
        assert est.vram_headroom_gb == 24 - local_models.size_table()["moondream"]

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
