"""Rootless setup troubleshooting playbook for nvWizard.

The setup wizard needs one shared way to translate jobs, receipts, smoke tests,
and log tails into student-friendly next actions. This module is intentionally
deterministic so it works before a local LLM is installed.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


OLLAMA_DOC_URL = "https://docs.ollama.com/linux"
OLLAMA_DOWNLOAD_URL = "https://ollama.com/download"
COMFYUI_REPO_URL = "https://github.com/comfyanonymous/ComfyUI"
PYTORCH_INSTALL_URL = "https://pytorch.org/get-started/locally/"
NODE_DOWNLOAD_URL = "https://nodejs.org/en/download"
NVHIVE_REPO_URL = "https://github.com/thatcooperguy/nvHive"
NVHIVE_README_URL = "https://github.com/thatcooperguy/nvHive/blob/main/README.md"


@dataclass(frozen=True)
class TroubleshootFinding:
    """One classified setup failure with a rootless-safe response."""

    id: str
    title: str
    severity: str
    summary: str
    evidence: list[str]
    action_id: str | None
    button_label: str
    can_auto_repair: bool
    requires_user_approval: bool
    requires_provider_admin: bool
    safe_changes: list[str]
    report_fields: list[str]
    web_search_queries: list[str]
    official_urls: list[str]
    rootless_note: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _short(value: Any, *, limit: int = 240) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return f"{text[:limit - 3]}..."


def _event_text(event: Any) -> str:
    if isinstance(event, dict):
        for key in ("message", "error", "summary", "command", "stage", "url"):
            if event.get(key):
                return str(event[key])
    return str(event)


def _failed_job_evidence(diagnostics: dict[str, Any], failed_job: dict[str, Any] | None) -> list[str]:
    evidence: list[str] = []
    jobs_check = diagnostics.get("checks", {}).get("jobs", {})
    jobs_data = jobs_check.get("data") if jobs_check.get("ok") else {}
    for tail in (jobs_data or {}).get("failed_event_tails", [])[:3]:
        pieces = [
            tail.get("kind") or tail.get("job_id") or "setup job",
            tail.get("status") or "failed",
            tail.get("message") or "",
        ]
        events = tail.get("events") or []
        if events:
            pieces.append(_event_text(events[-1]))
        evidence.append(_short(" ".join(str(piece) for piece in pieces if piece), limit=300))
    if failed_job:
        evidence.append(
            _short(
                f"{failed_job.get('kind') or failed_job.get('title') or 'setup job'} "
                f"{failed_job.get('status', 'failed')}: {failed_job.get('message', '')}",
                limit=300,
            )
        )
    return evidence


def _log_evidence(diagnostics: dict[str, Any]) -> list[str]:
    evidence: list[str] = []
    for entry in diagnostics.get("logs", {}).get("recent", []):
        path = Path(str(entry.get("path") or "log")).name
        for line in entry.get("lines", []):
            evidence.append(_short(f"{path}: {line}", limit=300))
            if len(evidence) >= 6:
                return evidence
    return evidence


def _check_evidence(diagnostics: dict[str, Any]) -> list[str]:
    evidence: list[str] = []
    for key in ("local_ai_runtime", "runtime", "workspace_state", "smoke_tests", "compatibility"):
        check = diagnostics.get("checks", {}).get(key)
        if not isinstance(check, dict):
            continue
        data = check.get("data") if check.get("ok") else check.get("error")
        if not data:
            continue
        if isinstance(data, dict):
            summary = data.get("summary") or data.get("status") or data.get("message") or data.get("error")
            if summary:
                evidence.append(_short(f"{key}: {summary}", limit=220))
            binary_error = data.get("binary_error")
            if binary_error:
                evidence.append(_short(f"{key} binary: {binary_error}", limit=260))
        else:
            evidence.append(_short(f"{key}: {data}", limit=220))
    return evidence


def _collect_evidence(diagnostics: dict[str, Any], failed_job: dict[str, Any] | None) -> list[str]:
    seen: set[str] = set()
    evidence: list[str] = []
    for item in [*_failed_job_evidence(diagnostics, failed_job), *_log_evidence(diagnostics), *_check_evidence(diagnostics)]:
        if not item or item in seen:
            continue
        seen.add(item)
        evidence.append(item)
    return evidence[:10]


def _contains(text: str, *needles: str) -> bool:
    return any(needle in text for needle in needles)


def _finding(
    *,
    id: str,
    title: str,
    severity: str = "recommended",
    summary: str,
    evidence: list[str],
    action_id: str | None,
    button_label: str,
    can_auto_repair: bool,
    requires_user_approval: bool = False,
    requires_provider_admin: bool = False,
    safe_changes: list[str],
    report_fields: list[str],
    web_search_queries: list[str],
    official_urls: list[str],
    rootless_note: str,
) -> TroubleshootFinding:
    return TroubleshootFinding(
        id=id,
        title=title,
        severity=severity,
        summary=summary,
        evidence=evidence[:6],
        action_id=action_id,
        button_label=button_label,
        can_auto_repair=can_auto_repair,
        requires_user_approval=requires_user_approval,
        requires_provider_admin=requires_provider_admin,
        safe_changes=safe_changes,
        report_fields=report_fields,
        web_search_queries=web_search_queries,
        official_urls=official_urls,
        rootless_note=rootless_note,
    )


def analyze_setup_failure(
    diagnostics: dict[str, Any],
    failed_job: dict[str, Any] | None = None,
    *,
    home_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Classify setup state into one or more rootless-safe findings."""

    evidence = _collect_evidence(diagnostics, failed_job)
    joined = "\n".join(evidence).lower()
    home = str(home_dir or diagnostics.get("paths", {}).get("home") or "$NVH_HOME")
    findings: list[TroubleshootFinding] = []

    if "exec format" in joined and "ollama" in joined:
        findings.append(_finding(
            id="ollama-wrong-binary",
            title="Ollama binary is not runnable on this VM",
            severity="required",
            summary=(
                "The local Ollama file looks corrupt, incomplete, or built for a different CPU. "
                "nvHive should replace only the rootless binary under the persistent workspace."
            ),
            evidence=evidence,
            action_id="rootless-ollama",
            button_label="Install Runtime",
            can_auto_repair=True,
            safe_changes=[
                "Remove and replace NVH_HOME/bin/ollama",
                "Refresh NVH_HOME/lib/ollama",
                "Keep models, receipts, and project files intact",
            ],
            report_fields=["diagnostics.report_id", "logs.recent", "checks.local_ai_runtime.binary_error"],
            web_search_queries=[
                "Ollama Linux amd64 tar.zst Exec format error",
                "Ollama Linux bundle wrong architecture rootless install",
            ],
            official_urls=[OLLAMA_DOC_URL, OLLAMA_DOWNLOAD_URL],
            rootless_note="No sudo is needed; the repair is scoped to NVH_HOME/bin and NVH_HOME/lib.",
        ))

    if _contains(joined, "download ollama", "ollama linux") and _contains(joined, "404", "curl: (22)", "returned error"):
        findings.append(_finding(
            id="ollama-download-url",
            title="Ollama download URL failed",
            severity="required",
            summary=(
                "The runtime download hit an unavailable Ollama bundle URL. nvHive should retry the "
                "latest-compatible official candidates and record which URL failed."
            ),
            evidence=evidence,
            action_id="rootless-ollama",
            button_label="Install Runtime",
            can_auto_repair=True,
            safe_changes=[
                "Retry the official Ollama Linux archive candidates",
                "Use NVH_OLLAMA_URL only as an advanced override",
                "Log all failed candidate URLs for support",
            ],
            report_fields=["logs.ollama-install.log", "jobs.failed_event_tails"],
            web_search_queries=[
                "Ollama latest Linux amd64 tar zst download 404",
                "Ollama Linux rootless archive download URL",
            ],
            official_urls=[OLLAMA_DOC_URL, OLLAMA_DOWNLOAD_URL],
            rootless_note="The download and extraction stay inside the persistent nvHive workspace.",
        ))

    if _contains(joined, "node.js not found", "node not found", "'node': no such file", "npm install failed", "fnm"):
        findings.append(_finding(
            id="webui-node-runtime",
            title="WebUI Node runtime needs repair",
            summary=(
                "The browser app is up only after a rootless Node/npm bootstrap. If Node disappeared "
                "after a VM refresh, nvHive can reinstall it in the user workspace."
            ),
            evidence=evidence,
            action_id="repair-workspace",
            button_label="Fix My Setup",
            can_auto_repair=True,
            safe_changes=[
                "Repair PATH and shell shims",
                "Reinstall rootless Node into the workspace cache when needed",
                "Rebuild the WebUI without touching OS packages",
            ],
            report_fields=["logs.webui-bootstrap.log", "checks.smoke_tests"],
            web_search_queries=[
                "Node.js Linux tarball rootless install",
                "fnm rootless Node install Linux",
            ],
            official_urls=[NODE_DOWNLOAD_URL],
            rootless_note="nvHive must not use apt or sudo for Node; it uses the workspace runtime.",
        ))

    if _contains(joined, "ensurepip", "python3-venv", "venv was not created", "pip is unavailable"):
        findings.append(_finding(
            id="python-venv-runtime",
            title="Python venv support is incomplete",
            severity="required",
            summary=(
                "The base image Python cannot create a normal venv. nvHive should use an existing "
                "user Python or install the micromamba fallback under NVH_HOME."
            ),
            evidence=evidence,
            action_id="runtime-fallback",
            button_label="Install Runtime",
            can_auto_repair=True,
            safe_changes=[
                "Prefer an existing user Python such as Miniforge",
                "Install micromamba fallback under NVH_HOME when needed",
                "Avoid apt install python3-venv",
            ],
            report_fields=["checks.runtime", "logs.install.log"],
            web_search_queries=[
                "Ubuntu 24.04 python venv ensurepip unavailable no sudo",
                "micromamba rootless Python environment Linux",
            ],
            official_urls=[],
            rootless_note="Do not ask the student to install python3-venv with apt; use rootless fallback.",
        ))

    if _contains(joined, "nvh_home", "persistent storage", "read-only", "no space left", "permission denied", "not writable"):
        findings.append(_finding(
            id="workspace-storage",
            title="Persistent workspace path needs attention",
            severity="required",
            summary=(
                f"Large apps and models must live on the durable block-backed workspace. "
                f"The current workspace is {home}."
            ),
            evidence=evidence,
            action_id="storage",
            button_label="Fix My Setup",
            can_auto_repair=True,
            safe_changes=[
                "Re-detect the writable block-backed home mount",
                "Rewrite nvh-env.sh and launchers",
                "Leave existing user files alone",
            ],
            report_fields=["checks.storage", "paths.home"],
            web_search_queries=[
                "Linux cloud desktop persistent block storage home mount detect",
                "NVH_HOME rootless persistent workspace no sudo",
            ],
            official_urls=[NVHIVE_README_URL],
            rootless_note="The storage repair changes nvHive config and launchers, not the OS disk.",
        ))

    if _contains(joined, "comfyui") and _contains(joined, "torch", "cuda", "xformers", "failed", "runtimeerror"):
        findings.append(_finding(
            id="comfyui-python-gpu",
            title="ComfyUI Python or GPU package mismatch",
            summary=(
                "ComfyUI is sensitive to Python, PyTorch, CUDA wheel, and driver compatibility. "
                "nvHive should pick the compatible torch profile and reinstall only the ComfyUI env."
            ),
            evidence=evidence,
            action_id="comfyui",
            button_label="Install ComfyUI",
            can_auto_repair=True,
            safe_changes=[
                "Recreate the ComfyUI virtual environment under NVH_HOME",
                "Use the compatibility report torch profile",
                "Keep workflows, outputs, and model plans on persistent storage",
            ],
            report_fields=["checks.compatibility", "logs.comfyui.log"],
            web_search_queries=[
                "ComfyUI Linux PyTorch CUDA install NVIDIA",
                "ComfyUI Ubuntu NVIDIA CUDA torch wheel",
            ],
            official_urls=[COMFYUI_REPO_URL, PYTORCH_INSTALL_URL],
            rootless_note="The repair stays in the ComfyUI workspace; host CUDA drivers remain provider-managed.",
        ))

    if _contains(joined, "nvidia-smi", "cuda unknown", "driver") and _contains(joined, "failed", "not found", "blocked"):
        findings.append(_finding(
            id="host-gpu-driver",
            title="Host GPU driver exposure needs provider help",
            severity="required",
            summary=(
                "nvHive can use the GPU that the VM exposes, but it cannot install or repair NVIDIA "
                "kernel drivers without admin control of the host image."
            ),
            evidence=evidence,
            action_id=None,
            button_label="Copy Support Report",
            can_auto_repair=False,
            requires_provider_admin=True,
            safe_changes=[
                "Capture GPU, driver, CUDA, and device-file diagnostics",
                "Continue CPU-safe setup steps when possible",
            ],
            report_fields=["environment.platform", "checks.compatibility", "checks.workspace_state"],
            web_search_queries=[
                "NVIDIA GPU not exposed inside Ubuntu VM nvidia-smi not found",
                "GeForce NOW Linux VM NVIDIA driver CUDA container runtime",
            ],
            official_urls=[NVHIVE_README_URL],
            rootless_note="This is outside nvHive's rootless repair boundary.",
        ))

    if not findings:
        findings.append(_finding(
            id="general-rootless-check",
            title="No single failure pattern found yet",
            severity="info",
            summary=(
                "The latest report did not contain a known high-signal setup failure. Retry the action, "
                "then ask nvWizard again so it can read the fresh job and log trail."
            ),
            evidence=evidence,
            action_id="repair-workspace",
            button_label="Fix My Setup",
            can_auto_repair=True,
            safe_changes=[
                "Refresh env files and launchers",
                "Recheck storage, runtime, jobs, receipts, and smoke tests",
            ],
            report_fields=["diagnostics.report_id", "jobs.failed_event_tails", "logs.recent"],
            web_search_queries=[
                "nvHive rootless Linux setup failed logs",
                "Linux GPU desktop rootless AI workspace troubleshooting",
            ],
            official_urls=[NVHIVE_REPO_URL, NVHIVE_README_URL],
            rootless_note="Start with safe repairs; manual shell commands should remain advanced overrides.",
        ))

    primary = findings[0]
    official_urls: list[str] = []
    web_queries: list[str] = []
    for finding in findings:
        for url in finding.official_urls:
            if url not in official_urls:
                official_urls.append(url)
        for query in finding.web_search_queries:
            if query not in web_queries:
                web_queries.append(query)

    return {
        "summary": primary.summary,
        "primary_id": primary.id,
        "primary_finding": primary.as_dict(),
        "findings": [finding.as_dict() for finding in findings],
        "official_urls": official_urls,
        "web_search_queries": web_queries,
        "support_summary": {
            "what_to_try": primary.button_label,
            "action_id": primary.action_id,
            "can_auto_repair": primary.can_auto_repair,
            "requires_provider_admin": primary.requires_provider_admin,
            "report_fields": primary.report_fields,
        },
        "rootless": True,
    }
