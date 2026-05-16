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
