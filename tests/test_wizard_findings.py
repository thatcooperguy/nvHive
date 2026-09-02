"""Tests for the Wizard's explicit-diagnostic findings derivation.

The Wizard reads ``wizard_context()`` for structural snapshot ("what is true")
and then ``derive_findings()`` turns that into "what is broken." These tests
pin the contract: which context shapes produce which finding ids, and how
the rendered block reads in the system prompt.
"""

from __future__ import annotations

from nvh.integrations.wizard.findings import (
    Finding,
    derive_findings,
    render_findings_block,
)


def _ctx(**overrides):
    """Build a wizard_context-shaped dict for the happy path; override fields per test."""
    base = {
        "gpu": {"detected": True, "primary": {"name": "RTX 4090", "vram_gb": 24}},
        "storage": {"available": True, "ok": True, "warnings": [], "home": "/nvh", "free_gb": 200},
        "providers": [{"name": "openai", "healthy": True}],
        "ollama_models": [{"name": "llama3.1:8b"}],
        "recent_jobs": [],
        "receipts": {"count": 3, "unhealthy": 0},
        "vault": {"initialized": True},
    }
    base.update(overrides)
    return base


def _ids(findings: list[Finding]) -> set[str]:
    return {f.id for f in findings}


def test_happy_path_produces_no_findings() -> None:
    findings = derive_findings(_ctx())
    assert findings == []
    assert render_findings_block(findings) == ""


def test_missing_gpu_is_info_level() -> None:
    findings = derive_findings(_ctx(gpu={"detected": False, "summary": "CPU only"}))
    gpu = next(f for f in findings if f.id == "gpu-missing")
    assert gpu.severity == "info"
    assert gpu.category == "gpu"
    assert gpu.suggested_tool is None


def test_missing_gpu_wording_depends_on_ownership() -> None:
    """Renters check the instance; owners check the driver. Never tell an owner about a 'rented instance'."""
    rented = next(
        f for f in derive_findings(_ctx(gpu={"detected": False}, platform={"device_class": "cloud-desktop"}))
        if f.id == "gpu-missing"
    )
    owned = next(
        f for f in derive_findings(_ctx(gpu={"detected": False}, platform={"device_class": "workstation"}))
        if f.id == "gpu-missing"
    )
    assert "rented instance" in rented.detail
    assert "rented" not in owned.detail
    assert "this machine's NVIDIA driver is installed" in owned.detail


def test_dgx_spark_hidden_gpu_never_contradicts_itself() -> None:
    """W1: DGX Spark + GPU not visible (container without NVML) is ONE warn finding —
    not 'no GPU, check the rented instance' next to 'you have a GB10'."""
    findings = derive_findings(_ctx(
        gpu={"detected": False, "summary": "GPU device files present but could not be queried"},
        platform={"device_class": "dgx-spark", "unified_memory": True, "memory_total_gb": 0.0},
    ))
    assert [f.id for f in findings] == ["platform-dgx-spark-gpu-hidden"]
    hidden = findings[0]
    assert hidden.severity == "warn"
    assert hidden.category == "gpu"
    assert hidden.suggested_tool is None
    assert "passthrough" in hidden.detail and "--gpus all" in hidden.detail


def test_no_providers_suggests_validate_tool() -> None:
    findings = derive_findings(_ctx(providers=[]))
    np = next(f for f in findings if f.id == "no-providers")
    assert np.severity == "warn"
    assert np.suggested_tool == "validate_provider_key"


def test_unhealthy_provider_emits_specific_finding() -> None:
    findings = derive_findings(_ctx(providers=[
        {"name": "openai", "healthy": False, "error": "401 invalid key"},
        {"name": "groq", "healthy": True},
    ]))
    bad = next(f for f in findings if f.id == "provider-unhealthy-openai")
    assert bad.severity == "error"
    assert "401" in bad.detail
    assert bad.suggested_tool == "validate_provider_key"
    assert bad.suggested_tool_args == {"provider": "openai"}


def test_no_local_models_warns_with_refresh_tool() -> None:
    findings = derive_findings(_ctx(ollama_models=[]))
    nm = next(f for f in findings if f.id == "no-local-models")
    assert nm.severity == "warn"
    assert nm.suggested_tool == "refresh_models"


def test_failed_jobs_become_runtime_errors() -> None:
    findings = derive_findings(_ctx(recent_jobs=[
        {"id": "job1", "status": "failed", "kind": "install-comfyui",
         "title": "Install ComfyUI", "message": "nvidia-smi missing"},
    ]))
    assert "job-failed-job1" in _ids(findings)
    job = next(f for f in findings if f.id == "job-failed-job1")
    assert job.severity == "error"
    assert job.suggested_tool == "repair_workspace"


def test_unhealthy_receipts_surface_with_repair_tool() -> None:
    findings = derive_findings(_ctx(receipts={"count": 5, "unhealthy": 2}))
    r = next(f for f in findings if f.id == "receipts-unhealthy")
    assert r.severity == "warn"
    assert r.suggested_tool == "repair_workspace"
    assert "2" in r.title


def test_storage_warnings_emit_finding() -> None:
    findings = derive_findings(_ctx(storage={
        "available": True, "ok": True,
        "warnings": ["NVH_HOME is on an ephemeral disk"],
        "home": "/tmp/nvh", "free_gb": 5,
    }))
    sw = next(f for f in findings if f.id == "storage-warnings")
    assert sw.severity == "warn"
    assert "ephemeral" in sw.detail


def test_storage_unavailable_is_error() -> None:
    findings = derive_findings(_ctx(storage={"available": False}))
    su = next(f for f in findings if f.id == "storage-unavailable")
    assert su.severity == "error"
    assert su.suggested_tool == "repair_workspace"


def test_findings_sort_errors_first_then_warns_then_info() -> None:
    findings = derive_findings(_ctx(
        gpu={"detected": False, "summary": ""},      # info
        providers=[],                                # warn
        recent_jobs=[
            {"id": "j1", "status": "failed", "kind": "x", "title": "x"},
        ],                                           # error
    ))
    severities = [f.severity for f in findings]
    assert severities[0] == "error"
    # Last should be info; warns sandwiched
    assert severities[-1] == "info"


def test_render_block_includes_tool_hints() -> None:
    findings = derive_findings(_ctx(providers=[]))
    block = render_findings_block(findings)
    assert "Active diagnostic findings" in block
    assert "no-providers" in block
    assert "validate_provider_key" in block


def test_render_block_empty_for_no_findings() -> None:
    assert render_findings_block([]) == ""


def test_finding_to_dict_is_serializable() -> None:
    findings = derive_findings(_ctx(providers=[]))
    out = findings[0].to_dict()
    assert isinstance(out, dict)
    assert out["id"] == "no-providers"
    assert out["suggested_tool"] == "validate_provider_key"
    # Default-factory dict for empty args.
    assert out["suggested_tool_args"] == {}


def test_rtx_spark_wording_follows_the_unified_memory_flag() -> None:
    """R6: ``rtx-spark`` is provisional and ``platform.unified_memory`` follows the GPU row.
    The finding must not claim a pool shared with Windows while recommend_models and
    check_oom_risk budget for discrete VRAM — and must still claim it when the row says so."""
    def finding(**platform):
        platform.setdefault("device_class", "rtx-spark")
        return next(f for f in derive_findings(_ctx(platform=platform)) if f.id == "platform-rtx-spark")

    unified = finding(unified_memory=True, memory_total_gb=64.0)
    assert "64 GB of unified memory shared with Windows" in unified.detail
    assert "one shared pool" in unified.detail

    discrete_cases = (finding(unified_memory=False, memory_total_gb=64.0), finding())  # flag False, flag absent
    for discrete in discrete_cases:
        assert "unified" not in discrete.detail
        assert "shared" not in discrete.detail
        assert "discrete VRAM" in discrete.detail
        assert "GPU block" in discrete.detail

    for f in (unified, *discrete_cases):
        assert f.severity == "info" and f.category == "gpu"
        assert "provisional" in f.detail
        assert f.title == "Running on NVIDIA RTX Spark (provisional detection)"
