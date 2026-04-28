"""Mission timeline for the nvWizard setup journey."""

from __future__ import annotations

from typing import Any

from nvh.integrations.auto_repair import auto_repair_plan
from nvh.integrations.boot_preflight import boot_preflight_status
from nvh.integrations.model_fit import model_fit_report
from nvh.integrations.mount_autopilot import mount_autopilot_report
from nvh.integrations.receipts import receipt_summary
from nvh.integrations.smoke_tests import smoke_test_report


def _stage(stage_id: str, title: str, status: str, summary: str, action_id: str | None = None) -> dict[str, Any]:
    return {
        "id": stage_id,
        "title": title,
        "status": status,
        "summary": summary,
        "action_id": action_id,
    }


def mission_control_report(home_dir: str | None = None) -> dict[str, Any]:
    """Return one student-friendly setup timeline."""
    boot = boot_preflight_status(home_dir=home_dir, run_if_missing=True)
    mount = mount_autopilot_report()
    repairs = auto_repair_plan(home_dir=home_dir)
    smoke = smoke_test_report(home_dir=home_dir)
    models = model_fit_report(home_dir=home_dir)
    receipts = receipt_summary()
    compatibility = boot.get("compatibility") or {}
    agent = boot.get("agent_helper") or {}

    stages = [
        _stage(
            "boot",
            "Boot Watch",
            "warn" if boot.get("changed") else "pass",
            boot.get("summary", "Boot preflight ready."),
        ),
        _stage(
            "storage",
            "Persistent Mount",
            "pass" if mount.get("confidence") in {"high", "medium"} else "warn",
            mount.get("summary", "Choose a persistent mount."),
            "storage",
        ),
        _stage(
            "repair",
            "Auto-Repair Queue",
            "warn" if repairs.get("auto_count") or repairs.get("needs_user_count") else "pass",
            repairs.get("summary", "No repairs queued."),
            "repair-workspace",
        ),
        _stage(
            "agent",
            "Local Agent Helper",
            "pass" if agent.get("local_agent_ready") else "warn",
            agent.get("summary", "Offline helper ready."),
            agent.get("recommended_action_id"),
        ),
        _stage(
            "models",
            "Model Fit Advisor",
            "pass" if models.get("recommended_ids") else "warn",
            models.get("summary", "Model fit advisor ready."),
            "starter-models",
        ),
        _stage(
            "apps",
            "App Smoke Tests",
            "pass" if smoke.get("warnings", 0) == 0 and smoke.get("failed", 0) == 0 else "warn",
            smoke.get("summary", "Smoke tests ready."),
            "smoke-tests",
        ),
        _stage(
            "receipts",
            "Install Receipts",
            "pass" if receipts.get("unhealthy", 0) == 0 else "warn",
            f"{receipts.get('count', 0)} receipt(s), {receipts.get('unhealthy', 0)} need repair.",
            "repair-receipts",
        ),
    ]
    blocked = int(compatibility.get("blocked_count", 0) or 0)
    return {
        "summary": (
            "Ready to build" if blocked == 0 and all(stage["status"] == "pass" for stage in stages[:2])
            else f"{blocked} blocked compatibility item(s); nvWizard has next steps."
        ),
        "ready": blocked == 0,
        "stages": stages,
        "boot_preflight": boot,
        "mount_autopilot": mount,
        "auto_repair": repairs,
        "smoke_tests": smoke,
        "model_fit": models,
    }
