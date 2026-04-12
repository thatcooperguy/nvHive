"""Batch 7 — cover small modules to push total coverage past 70%.

Targets:
  smoke_test, templates, memory, gpu, cloud_session,
  plugins/manager, gpu_emulation, sdk, hooks
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# 1. nvh/core/smoke_test.py
# ---------------------------------------------------------------------------


class TestSmokeTest:
    """Verify smoke_test dataclasses and helpers."""

    def test_test_result_dataclass(self):
        from nvh.core.smoke_test import TestResult

        r = TestResult(
            name="Import nvh",
            category="Core",
            passed=True,
            duration_ms=5,
            message="v0.5",
        )
        assert r.passed is True
        assert r.category == "Core"
        assert r.duration_ms == 5

    def test_smoke_test_report_counts(self):
        from nvh.core.smoke_test import SmokeTestReport, TestResult

        report = SmokeTestReport()
        report.results.append(
            TestResult(name="a", category="c", passed=True)
        )
        report.results.append(
            TestResult(name="b", category="c", passed=False)
        )
        report.results.append(
            TestResult(name="c", category="c", passed=True)
        )
        assert report.passed == 2
        assert report.failed == 1
        assert report.total == 3

    def test_soft_fail_reason_rate_limit(self):
        from nvh.core.smoke_test import _soft_fail_reason

        soft, label = _soft_fail_reason("HTTP 429 rate limit exceeded")
        assert soft is True
        assert "rate" in label.lower()

    def test_soft_fail_reason_unknown(self):
        from nvh.core.smoke_test import _soft_fail_reason

        soft, label = _soft_fail_reason("connection refused")
        assert soft is False
        assert label == ""

    def test_timed_success(self):
        from nvh.core.smoke_test import _timed

        result, ms = _timed(lambda: 42)
        assert result == 42
        assert ms >= 0

    def test_timed_exception(self):
        from nvh.core.smoke_test import _timed

        with pytest.raises(ValueError):
            _timed(lambda: (_ for _ in ()).throw(ValueError("boom")))


# ---------------------------------------------------------------------------
# 2. nvh/core/templates.py
# ---------------------------------------------------------------------------


class TestTemplates:
    """Template system: listing, parsing, rendering."""

    def test_builtin_templates_non_empty(self):
        from nvh.core.templates import get_builtin_templates

        templates = get_builtin_templates()
        assert len(templates) > 0
        assert "code_review" in templates

    def test_parse_template_with_frontmatter(self):
        from nvh.core.templates import _parse_template

        raw = (
            "---\nname: test\ndescription: A test\n"
            "required_vars:\n  - code\noptional_vars:\n  lang: python\n"
            "---\nReview {{code}} in {{lang}}\n"
        )
        t = _parse_template(raw)
        assert t.name == "test"
        assert "code" in t.required_vars
        assert t.optional_vars.get("lang") == "python"

    def test_render_fills_placeholders(self):
        from nvh.core.templates import _parse_template

        raw = (
            "---\nname: greet\nrequired_vars:\n  - name\n"
            "optional_vars: {}\n---\nHello {{name}}!\n"
        )
        t = _parse_template(raw)
        body, system = t.render({"name": "World"})
        assert "Hello World!" in body
        assert system is None

    def test_render_missing_required_var_raises(self):
        from nvh.core.templates import _parse_template

        raw = "---\nname: t\nrequired_vars:\n  - x\n---\n{{x}}\n"
        t = _parse_template(raw)
        with pytest.raises(ValueError, match="requires variables"):
            t.render({})

    def test_parse_template_no_frontmatter(self):
        from nvh.core.templates import _parse_template

        t = _parse_template("Just a plain body")
        assert t.name == "unknown"
        assert t.body == "Just a plain body"

    def test_optional_vars_as_list(self):
        from nvh.core.templates import _parse_template

        raw = (
            "---\nname: t\nrequired_vars: []\n"
            "optional_vars:\n  - foo\n  - bar\n---\nbody\n"
        )
        t = _parse_template(raw)
        assert "foo" in t.optional_vars
        assert t.optional_vars["foo"] == ""


# ---------------------------------------------------------------------------
# 3. nvh/core/memory.py
# ---------------------------------------------------------------------------


class TestMemory:
    """MemoryStore: add, search, remove, clear."""

    def test_add_and_get_all(self, tmp_path: Path):
        from nvh.core.memory import MemoryStore

        store = MemoryStore(memory_dir=tmp_path / "mem")
        m = store.add("Python 3.12", memory_type="fact")
        assert m.type == "fact"
        assert m.content == "Python 3.12"
        assert len(store.get_all()) == 1

    def test_search(self, tmp_path: Path):
        from nvh.core.memory import MemoryStore

        store = MemoryStore(memory_dir=tmp_path / "mem")
        store.add("Uses pytest for testing")
        store.add("Prefers short answers")
        results = store.search("pytest")
        assert len(results) == 1
        assert "pytest" in results[0].content

    def test_remove_by_id(self, tmp_path: Path):
        from nvh.core.memory import MemoryStore

        store = MemoryStore(memory_dir=tmp_path / "mem")
        m = store.add("temp memory")
        assert store.remove(m.id) is True
        assert len(store.get_all()) == 0

    def test_forget_keyword(self, tmp_path: Path):
        from nvh.core.memory import MemoryStore

        store = MemoryStore(memory_dir=tmp_path / "mem")
        store.add("Use Python 3.12")
        store.add("Prefers concise answers")
        removed = store.forget("Python")
        assert removed == 1
        assert len(store.get_all()) == 1

    def test_clear_all(self, tmp_path: Path):
        from nvh.core.memory import MemoryStore

        store = MemoryStore(memory_dir=tmp_path / "mem")
        store.add("a")
        store.add("b")
        count = store.clear_all()
        assert count == 2
        assert len(store.get_all()) == 0

    def test_get_context_prompt(self, tmp_path: Path):
        from nvh.core.memory import MemoryStore

        store = MemoryStore(memory_dir=tmp_path / "mem")
        store.add("Important fact", memory_type="fact")
        prompt = store.get_context_prompt()
        assert "<memory>" in prompt
        assert "Important fact" in prompt

    def test_get_context_prompt_empty(self, tmp_path: Path):
        from nvh.core.memory import MemoryStore

        store = MemoryStore(memory_dir=tmp_path / "mem")
        assert store.get_context_prompt() == ""

    def test_get_all_filtered_by_type(self, tmp_path: Path):
        from nvh.core.memory import MemoryStore

        store = MemoryStore(memory_dir=tmp_path / "mem")
        store.add("fact1", memory_type="fact")
        store.add("pref1", memory_type="user")
        assert len(store.get_all("fact")) == 1
        assert len(store.get_all("user")) == 1

    def test_persistence_across_loads(self, tmp_path: Path):
        from nvh.core.memory import MemoryStore

        mem_dir = tmp_path / "mem"
        store1 = MemoryStore(memory_dir=mem_dir)
        store1.add("persistent data")
        # Create a new store pointing at the same directory
        store2 = MemoryStore(memory_dir=mem_dir)
        assert len(store2.get_all()) == 1
        assert store2.get_all()[0].content == "persistent data"


# ---------------------------------------------------------------------------
# 4. nvh/utils/gpu.py — recommend_models / optimizations
# ---------------------------------------------------------------------------


class TestGPU:
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
        assert "nemotron" in model_names or "nemotron-small" in model_names

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
        assert "nemotron:120b" in model_names

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


# ---------------------------------------------------------------------------
# 5. nvh/integrations/cloud_session.py
# ---------------------------------------------------------------------------


class TestCloudSession:
    """Cloud session detection with mocked environment."""

    @patch.dict(
        "os.environ",
        {"CLOUD_SESSION_ID": "abc-123", "CLOUD_TIER": "ultimate"},
        clear=False,
    )
    @patch("subprocess.run", side_effect=FileNotFoundError)
    @patch("os.path.ismount", return_value=False)
    def test_detect_with_env_vars(self, _mnt, _sub):
        from nvh.integrations.cloud_session import detect_cloud_session

        session = detect_cloud_session()
        assert session.is_cloud_session is True
        assert session.session_id == "abc-123"
        assert session.tier == "ultimate"

    @patch("subprocess.run", side_effect=FileNotFoundError)
    @patch("os.path.ismount", return_value=False)
    def test_detect_without_cloud(self, _mnt, _sub):
        from nvh.integrations.cloud_session import detect_cloud_session

        # Remove only the cloud-specific env vars (keep HOME/USERPROFILE)
        env_patch = {
            "CLOUD_SESSION_ID": "",
            "NVIDIA_CLOUD_SESSION": "",
            "CLOUD_USER_ID": "",
            "CLOUD_TIER": "",
        }
        with patch.dict("os.environ", env_patch):
            session = detect_cloud_session()
        assert session.is_cloud_session is False

    def test_get_cloud_recommended_config_not_cloud(self):
        from nvh.integrations.cloud_session import (
            CLOUDSession,
            get_cloud_recommended_config,
        )

        session = CLOUDSession(is_cloud_session=False)
        cfg = get_cloud_recommended_config(session)
        assert cfg == {}

    def test_get_cloud_recommended_config_ultimate(self):
        from nvh.integrations.cloud_session import (
            CLOUDSession,
            get_cloud_recommended_config,
        )

        session = CLOUDSession(is_cloud_session=True, tier="ultimate")
        cfg = get_cloud_recommended_config(session)
        assert cfg["ollama_num_parallel"] == 2
        assert "nemotron-small" in cfg["recommended_models"]

    def test_format_cloud_status_not_cloud(self):
        from nvh.integrations.cloud_session import (
            CLOUDSession,
            format_cloud_status,
        )

        assert "Not running" in format_cloud_status(CLOUDSession())

    def test_format_cloud_status_cloud(self):
        from nvh.integrations.cloud_session import (
            CLOUDSession,
            format_cloud_status,
        )

        s = CLOUDSession(
            is_cloud_session=True,
            tier="performance",
            gpu_class="RTX 3080",
            session_id="xyz-456",
        )
        status = format_cloud_status(s)
        assert "Performance" in status
        assert "RTX 3080" in status


# ---------------------------------------------------------------------------
# 6. nvh/plugins/manager.py
# ---------------------------------------------------------------------------


class TestPluginManager:
    """PluginManager: discover, list, load."""

    def test_construct_and_list_empty(self):
        from nvh.plugins.manager import PluginManager

        pm = PluginManager()
        assert pm.list_plugins() == []

    def test_discover_empty_dir(self, tmp_path: Path):
        from nvh.plugins.manager import PluginManager

        pm = PluginManager()
        found = pm.discover(plugin_dir=tmp_path)
        # Only entry-point plugins (if any); no file plugins
        for p in found:
            assert p.source == "entrypoint"

    def test_discover_py_file(self, tmp_path: Path):
        from nvh.plugins.manager import PluginManager

        (tmp_path / "my_plugin.py").write_text("x = 1\n")
        pm = PluginManager()
        found = pm.discover(plugin_dir=tmp_path)
        names = [p.name for p in found]
        assert "my_plugin" in names

    def test_load_unknown_returns_none(self):
        from nvh.plugins.manager import PluginManager

        pm = PluginManager()
        assert pm.load("nonexistent") is None

    def test_load_file_plugin(self, tmp_path: Path):
        from nvh.plugins.manager import PluginManager

        (tmp_path / "simple.py").write_text("VALUE = 42\n")
        pm = PluginManager()
        pm.discover(plugin_dir=tmp_path)
        mod = pm.load("simple")
        assert mod is not None
        assert mod.VALUE == 42


# ---------------------------------------------------------------------------
# 7. nvh/utils/gpu_emulation.py
# ---------------------------------------------------------------------------


class TestGPUEmulation:
    """GPU performance estimation."""

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


# ---------------------------------------------------------------------------
# 8. nvh/sdk.py — verify convenience wrappers exist
# ---------------------------------------------------------------------------


class TestSDK:
    """SDK module: verify public API surface."""

    def test_public_async_functions_exist(self):
        import nvh.sdk as sdk

        for name in ("ask", "convene", "poll", "safe", "quick",
                     "complete", "route", "stream", "health"):
            assert callable(getattr(sdk, name)), f"sdk.{name} missing"

    def test_public_sync_functions_exist(self):
        import nvh.sdk as sdk

        for name in ("ask_sync", "convene_sync", "poll_sync",
                     "safe_sync", "quick_sync", "complete_sync",
                     "health_sync"):
            assert callable(getattr(sdk, name)), f"sdk.{name} missing"

    def test_messages_to_internal(self):
        from nvh.sdk import _messages_to_internal

        msgs = _messages_to_internal([
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "Hi"},
        ])
        assert len(msgs) == 2
        assert msgs[0].role == "system"
        assert msgs[1].content == "Hi"


# ---------------------------------------------------------------------------
# 9. nvh/core/hooks.py
# ---------------------------------------------------------------------------


class TestHooks:
    """HookManager: register, list, render, emit."""

    def test_hook_manager_construction(self):
        from nvh.core.hooks import HookManager

        hm = HookManager()
        assert hm.list_hooks() == []

    def test_register_and_list(self):
        from nvh.core.hooks import Hook, HookManager

        hm = HookManager()
        hm.register(Hook(event="pre_query", command="echo hello"))
        hooks = hm.list_hooks()
        assert len(hooks) == 1
        assert hooks[0]["event"] == "pre_query"

    def test_load_from_config(self):
        from nvh.core.hooks import HookManager

        hm = HookManager()
        hm.load_from_config([
            {"event": "post_query", "command": "echo done"},
            {"event": "on_error", "command": "echo err", "enabled": False},
        ])
        hooks = hm.list_hooks()
        assert len(hooks) == 2
        disabled = [h for h in hooks if not h["enabled"]]
        assert len(disabled) == 1

    def test_render_template(self):
        from nvh.core.hooks import HookContext, HookManager

        hm = HookManager()
        ctx = HookContext(event="pre_query", prompt="hello world", cost="0.01")
        rendered = hm._render("Query: {{prompt}}, cost: {{cost}}", ctx)
        assert rendered == "Query: hello world, cost: 0.01"

    @pytest.mark.asyncio
    async def test_emit_callback(self):
        from nvh.core.hooks import Hook, HookContext, HookManager

        hm = HookManager()
        called = []
        hm.register(Hook(
            event="test_event",
            callback=lambda ctx: called.append(ctx.prompt),
        ))
        ctx = HookContext(event="test_event", prompt="ping")
        await hm.emit("test_event", ctx)
        assert called == ["ping"]
