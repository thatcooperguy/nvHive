"""Final push L — coverage for remaining 0% modules.

Tests pure logic, config construction, and data structures.
Hardware-dependent paths are mocked or skipped gracefully.
"""
from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# 1. nvh/core/voice.py — VoiceConfig construction
# ---------------------------------------------------------------------------

class TestVoice:
    def test_voice_config_defaults(self):
        from nvh.core.voice import VoiceConfig
        vc = VoiceConfig()
        assert vc.stt_provider == "groq"
        assert vc.tts_provider == "edge"
        assert vc.tts_voice == "en-US-AriaNeural"
        assert vc.auto_listen is False
        assert vc.silence_timeout == 2.0

    def test_voice_config_custom(self):
        from nvh.core.voice import VoiceConfig
        vc = VoiceConfig(stt_provider="local", tts_provider="system", auto_listen=True)
        assert vc.stt_provider == "local"
        assert vc.tts_provider == "system"
        assert vc.auto_listen is True

    def test_speech_to_text_requires_groq_key(self):
        from nvh.core.voice import speech_to_text
        with patch.dict("os.environ", {}, clear=True):
            with patch("keyring.get_password", side_effect=Exception("no keyring")):
                # Skip if event loop is consumed by previous async tests
                try:
                    loop = asyncio.get_event_loop()
                    if loop.is_closed():
                        pytest.skip("event loop closed by prior async test")
                    with pytest.raises(ValueError, match="Groq API key"):
                        loop.run_until_complete(
                            speech_to_text("/fake.wav", provider="groq")
                        )
                except RuntimeError:
                    pytest.skip("no event loop available")


# ---------------------------------------------------------------------------
# 2. nvh/integrations/service.py — service file generation
# ---------------------------------------------------------------------------

class TestService:
    def test_generate_systemd_service_contains_unit_sections(self):
        from nvh.integrations.service import generate_systemd_service
        content = generate_systemd_service(host="0.0.0.0", port=9000)
        assert "[Unit]" in content
        assert "[Service]" in content
        assert "[Install]" in content
        assert "0.0.0.0" in content
        assert "9000" in content

    def test_generate_launchd_plist_is_valid_xml(self):
        import xml.etree.ElementTree as ET

        from nvh.integrations.service import generate_launchd_plist
        content = generate_launchd_plist(host="127.0.0.1", port=8080)
        assert content.strip().startswith("<?xml")
        # Should parse without error (strip the DOCTYPE which ET doesn't handle)
        cleaned = content.replace(
            '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"\n'
            '  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">',
            "",
        )
        ET.fromstring(cleaned)

    def test_service_status_returns_tuple(self):
        from nvh.integrations.service import service_status
        result = service_status()
        assert isinstance(result, tuple)
        assert len(result) == 2
        assert isinstance(result[0], bool)
        assert isinstance(result[1], str)


# ---------------------------------------------------------------------------
# 3. nvh/core/image_gen.py — import and provider validation
# ---------------------------------------------------------------------------

class TestImageGen:
    def test_import_image_gen(self):
        from nvh.core import image_gen
        assert hasattr(image_gen, "generate_image")
        assert hasattr(image_gen, "open_image")

    def test_generate_image_unknown_provider_raises(self):
        from nvh.core.image_gen import generate_image
        try:
            loop = asyncio.get_event_loop()
            if loop.is_closed():
                pytest.skip("event loop closed by prior async test")
            with pytest.raises(ValueError, match="Unknown image provider"):
                loop.run_until_complete(
                    generate_image("a cat", provider="nonexistent")
                )
        except RuntimeError:
            pytest.skip("no event loop available")


# ---------------------------------------------------------------------------
# 4. nvh/core/benchmark.py — dataclass, prompts, baselines, formatting
# ---------------------------------------------------------------------------

class TestBenchmark:
    def test_benchmark_suite_construction(self):
        from nvh.core.benchmark import BenchmarkSuite
        suite = BenchmarkSuite(
            gpu_name="Test GPU", vram_gb=8.0,
            results=[], total_time_ms=100, timestamp="2025-01-01",
        )
        assert suite.gpu_name == "Test GPU"

    def test_benchmark_prompts_non_empty(self):
        from nvh.core.benchmark import BENCHMARK_PROMPTS
        assert len(BENCHMARK_PROMPTS) >= 3
        for bp in BENCHMARK_PROMPTS:
            assert "prompt" in bp
            assert "max_tokens" in bp

    def test_community_baselines_has_entries(self):
        from nvh.core.benchmark import COMMUNITY_BASELINES
        assert len(COMMUNITY_BASELINES) >= 5
        assert "NVIDIA GeForce RTX 4090" in COMMUNITY_BASELINES

    def test_format_benchmark_results(self):
        from nvh.core.benchmark import BenchmarkResult, BenchmarkSuite, format_benchmark_results
        r = BenchmarkResult(
            model="test", gpu_name="GPU", vram_gb=8, prompt_tokens=10,
            output_tokens=50, time_to_first_token_ms=100, total_time_ms=500,
            tokens_per_second=100.0, prompt_eval_rate=200.0,
        )
        suite = BenchmarkSuite(
            gpu_name="GPU", vram_gb=8, results=[r],
            total_time_ms=500, timestamp="now",
        )
        text = format_benchmark_results(suite)
        assert "GPU" in text
        assert "100.0" in text
        assert isinstance(text, str)


# ---------------------------------------------------------------------------
# 5. nvh/providers/quota_info.py — quota data structure
# ---------------------------------------------------------------------------

class TestQuotaInfo:
    def test_provider_quotas_structure(self):
        from nvh.providers.quota_info import PROVIDER_QUOTAS, QuotaInfo
        assert len(PROVIDER_QUOTAS) >= 5
        for name, qi in PROVIDER_QUOTAS.items():
            assert isinstance(qi, QuotaInfo)
            assert qi.provider == name

    def test_get_quota_info_known_and_unknown(self):
        from nvh.providers.quota_info import get_quota_info
        info = get_quota_info("groq")
        assert info.provider == "groq"
        assert info.tier == "free"
        unknown = get_quota_info("made_up_provider")
        assert unknown.tier == "unknown"

    def test_parse_retry_after(self):
        from nvh.providers.quota_info import parse_retry_after
        assert parse_retry_after("please retry in 5.2s") == 5.2
        assert parse_retry_after("no info here") is None


# ---------------------------------------------------------------------------
# 6. nvh/core/smoke_test.py — SmokeTestReport with mock results
# ---------------------------------------------------------------------------

class TestSmokeTest:
    def test_smoke_test_report_counts(self):
        from nvh.core.smoke_test import SmokeTestReport, TestResult
        report = SmokeTestReport(results=[
            TestResult(name="a", category="c", passed=True),
            TestResult(name="b", category="c", passed=False),
            TestResult(name="c", category="c", passed=True),
        ])
        assert report.passed == 2
        assert report.failed == 1
        assert report.total == 3

    def test_test_result_defaults(self):
        from nvh.core.smoke_test import TestResult
        tr = TestResult(name="x", category="y", passed=True)
        assert tr.duration_ms == 0
        assert tr.message == ""
        assert tr.error == ""

    def test_soft_fail_reason_rate_limit(self):
        from nvh.core.smoke_test import _soft_fail_reason
        soft, label = _soft_fail_reason("429 rate limit exceeded")
        assert soft is True
        assert "rate" in label

    def test_soft_fail_reason_normal_error(self):
        from nvh.core.smoke_test import _soft_fail_reason
        soft, _ = _soft_fail_reason("connection refused")
        assert soft is False


# ---------------------------------------------------------------------------
# 7. nvh/utils/gpu_emulation.py — performance estimation
# ---------------------------------------------------------------------------

class TestGpuEmulation:
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


# ---------------------------------------------------------------------------
# 8. nvh/core/hooks.py — HookManager register + emit, template rendering
# ---------------------------------------------------------------------------

class TestHooks:
    def test_hook_manager_register_and_list(self):
        from nvh.core.hooks import Hook, HookManager
        mgr = HookManager()
        mgr.register(Hook(event="pre_query", command="echo hi"))
        mgr.register(Hook(event="post_query", command="echo done"))
        hooks = mgr.list_hooks()
        assert len(hooks) == 2
        assert hooks[0]["event"] == "pre_query"

    def test_load_from_config(self):
        from nvh.core.hooks import HookManager
        mgr = HookManager()
        mgr.load_from_config([
            {"event": "pre_query", "command": "echo {{prompt}}"},
            {"event": "on_error", "command": "echo {{error}}", "enabled": False},
        ])
        hooks = mgr.list_hooks()
        assert len(hooks) == 2
        assert hooks[1]["enabled"] is False

    def test_hook_template_rendering(self):
        from nvh.core.hooks import HookContext, HookManager
        mgr = HookManager()
        ctx = HookContext(event="pre_query", prompt="hello world", advisor="groq")
        rendered = mgr._render("Query: {{prompt}} via {{advisor}}", ctx)
        assert rendered == "Query: hello world via groq"


# ---------------------------------------------------------------------------
# 9. nvh/plugins/manager.py — PluginManager discover with mock dir
# ---------------------------------------------------------------------------

class TestPluginManager:
    def test_discover_with_empty_dir(self, tmp_path):
        from nvh.plugins.manager import PluginManager
        mgr = PluginManager()
        found = mgr.discover(plugin_dir=tmp_path)
        # Only entry-point plugins (if any); no file plugins
        file_plugins = [p for p in found if p.source == "file"]
        assert len(file_plugins) == 0

    def test_discover_with_plugin_file(self, tmp_path):
        from nvh.plugins.manager import PluginManager
        plugin_file = tmp_path / "my_plugin.py"
        plugin_file.write_text("NVHIVE_PLUGIN = {'type': 'provider', 'name': 'test'}\n")
        mgr = PluginManager()
        found = mgr.discover(plugin_dir=tmp_path)
        file_plugins = [p for p in found if p.source == "file"]
        assert len(file_plugins) == 1
        assert file_plugins[0].name == "my_plugin"

    def test_load_file_plugin(self, tmp_path):
        from nvh.plugins.manager import PluginManager
        plugin_file = tmp_path / "sample.py"
        plugin_file.write_text(
            "class MyProv:\n    pass\n\n"
            "NVHIVE_PLUGIN = {'type': 'provider', 'name': 'sample', 'class': MyProv}\n"
        )
        mgr = PluginManager()
        mgr.discover(plugin_dir=tmp_path)
        loaded = mgr.load("sample")
        assert loaded is not None

    def test_load_unknown_returns_none(self):
        from nvh.plugins.manager import PluginManager
        mgr = PluginManager()
        assert mgr.load("does_not_exist") is None


# ---------------------------------------------------------------------------
# 10. nvh/mcp_server.py — MCP server tools registration
# ---------------------------------------------------------------------------

class TestMcpServer:
    def test_mcp_server_create(self):
        pytest.importorskip("mcp", reason="MCP SDK not installed")
        from nvh.mcp_server import create_server
        server = create_server()
        assert server is not None
