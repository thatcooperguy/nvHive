"""Tests for the /v1/wizard/diagnostics endpoint and the `diagnose` tool.

These pin the consolidated-findings contract that the setup-page System Check
and the WizardChat deep-link both depend on: same data, same shape, same
finding ids.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from nvh.api.server import app


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def _patched_context(
    *,
    gpu_detected: bool = True,
    providers: list[dict[str, Any]] | None = None,
    ollama_models: list[dict[str, Any]] | None = None,
    storage_ok: bool = True,
) -> dict[str, Any]:
    return {
        "gpu": {"detected": gpu_detected, "primary": {"name": "RTX"} if gpu_detected else None},
        "storage": {"available": True, "ok": storage_ok, "warnings": [],
                    "home": "/nvh", "free_gb": 100},
        "providers": providers if providers is not None else [{"name": "ollama", "healthy": True}],
        "ollama_models": ollama_models if ollama_models is not None else [{"name": "llama3.1"}],
        "recent_jobs": [],
        "receipts": {"count": 0, "unhealthy": 0},
        "vault": {"initialized": False},
    }


def test_diagnostics_endpoint_returns_findings_and_counts(client: TestClient) -> None:
    """Endpoint shape: findings array, context dict, and severity counts."""
    fake = _patched_context(gpu_detected=False, providers=[], ollama_models=[])
    with patch("nvh.integrations.wizard.context.wizard_context", return_value=fake):
        resp = client.get("/v1/wizard/diagnostics")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert "findings" in data
    assert "context" in data
    assert "counts" in data
    ids = {f["id"] for f in data["findings"]}
    # Three concerns from this context: missing GPU, no providers, no models.
    assert "gpu-missing" in ids
    assert "no-providers" in ids
    assert "no-local-models" in ids
    counts = data["counts"]
    assert counts["total"] == len(data["findings"])
    assert counts["warn"] + counts["error"] + counts["info"] == counts["total"]


def test_diagnostics_endpoint_happy_path_has_no_findings(client: TestClient) -> None:
    """Healthy workspace returns empty findings list with zero counts."""
    with patch("nvh.integrations.wizard.context.wizard_context",
               return_value=_patched_context()):
        resp = client.get("/v1/wizard/diagnostics")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["findings"] == []
    assert data["counts"]["total"] == 0


def test_diagnostics_findings_carry_suggested_tools(client: TestClient) -> None:
    """Findings expose ``suggested_tool`` so the setup page can label fix paths."""
    with patch("nvh.integrations.wizard.context.wizard_context",
               return_value=_patched_context(providers=[])):
        resp = client.get("/v1/wizard/diagnostics")
    data = resp.json()["data"]
    np = next(f for f in data["findings"] if f["id"] == "no-providers")
    assert np["suggested_tool"] == "validate_provider_key"


@pytest.mark.asyncio
async def test_diagnose_tool_returns_findings_and_summary() -> None:
    """The `diagnose` Wizard tool is the agent-side mirror of the endpoint."""
    from nvh.integrations.wizard.tools import default_registry

    fake = _patched_context(providers=[], ollama_models=[])
    with patch("nvh.integrations.wizard.context.wizard_context", return_value=fake):
        reg = default_registry()
        tool = reg.get("diagnose")
        assert tool is not None
        result = await tool.handler({})
    assert "findings" in result
    assert "summary" in result
    ids = {f["id"] for f in result["findings"]}
    assert {"no-providers", "no-local-models"}.issubset(ids)
    # Summary should mention the count.
    assert "active finding" in result["summary"]


@pytest.mark.asyncio
async def test_diagnose_tool_handles_clean_workspace() -> None:
    """A clean workspace returns 0-finding summary, not an error."""
    from nvh.integrations.wizard.tools import default_registry

    with patch("nvh.integrations.wizard.context.wizard_context",
               return_value=_patched_context()):
        reg = default_registry()
        tool = reg.get("diagnose")
        result = await tool.handler({})
    assert result["findings"] == []
    assert "0 active finding" in result["summary"]


def test_diagnose_tool_registered_as_auto_class() -> None:
    """``diagnose`` must be auto-class so the chat loop runs it without a confirmation card."""
    from nvh.integrations.wizard.tools import default_registry

    reg = default_registry()
    tool = reg.get("diagnose")
    assert tool is not None
    assert tool.safety_class == "auto"


def test_system_prompt_includes_findings_block_when_present() -> None:
    """The wizard system prompt must surface findings — that's the whole point."""
    from nvh.integrations.wizard.findings import derive_findings
    from nvh.integrations.wizard.personality import build_system_prompt

    snapshot = _patched_context(providers=[], ollama_models=[])
    findings = derive_findings(snapshot)
    prompt = build_system_prompt(snapshot, findings=findings)
    assert "Active diagnostic findings" in prompt
    assert "no-providers" in prompt
    assert "no-local-models" in prompt


def test_system_prompt_omits_findings_block_when_empty() -> None:
    """No findings → no findings block. Silence is the signal."""
    from nvh.integrations.wizard.personality import build_system_prompt

    prompt = build_system_prompt(_patched_context(), findings=[])
    assert "Active diagnostic findings" not in prompt
