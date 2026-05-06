from __future__ import annotations

import shutil
import uuid
from pathlib import Path

import pytest

from nvh.integrations import mission_builder
from nvh.integrations.mission_builder import (
    install_mission_profile,
    mission_profile_pack_ids,
    mission_profile_plan,
)
from nvh.integrations.storage import ensure_storage


@pytest.fixture()
def mission_workspace(monkeypatch):
    root = Path.cwd() / "pytest-workspaces"
    root.mkdir(exist_ok=True)
    path = root / f"mission-{uuid.uuid4().hex}" / "nvhive"
    monkeypatch.setenv("NVH_HOME", str(path))
    try:
        yield path
    finally:
        shutil.rmtree(path.parent, ignore_errors=True)


def test_student_mission_has_rootless_ai_basics(mission_workspace):
    plan = mission_profile_plan("student", min_free_gb=0)

    assert plan["profile"] == "student"
    assert plan["title"] == "AI Starter"
    assert plan["needs_comfyui"] is False
    assert "rootless-ollama" in plan["pack_ids"]
    assert "agent-lab" in plan["pack_ids"]
    assert plan["model_ids"]
    assert plan["stages"][0]["id"] == "studio-packs"


def test_creator_mission_runs_comfyui_before_nodes_and_models(mission_workspace):
    plan = mission_profile_plan("creator", min_free_gb=0)
    stage_ids = [stage["id"] for stage in plan["stages"]]

    assert plan["needs_comfyui"] is True
    assert "comfyui" in stage_ids
    assert stage_ids.index("comfyui") < stage_ids.index("models")
    assert "comfyui-power-nodes" in plan["comfy_node_pack_ids"]
    assert "comfyui-power-nodes" not in plan["first_pack_ids"]
    assert plan["example_ids"]


def test_unknown_mission_profile_is_rejected(mission_workspace):
    with pytest.raises(KeyError):
        mission_profile_pack_ids("classroom")


@pytest.mark.asyncio
async def test_mission_child_complete_events_do_not_finish_parent_job(mission_workspace, monkeypatch):
    ensure_storage(mission_workspace, min_free_gb=0)

    def fake_plan(profile, *, torch_profile="nvidia-cu130", min_free_gb=0, home_dir=None):
        return {
            "schema_version": 1,
            "profile": profile,
            "title": "AI Starter",
            "storage": {},
            "rootless_safe": True,
            "needs_comfyui": False,
            "torch_profile": torch_profile,
            "pack_ids": ["rootless-ollama"],
            "first_pack_ids": ["rootless-ollama"],
            "comfy_node_pack_ids": [],
            "model_ids": ["llama3.1:8b"],
            "example_ids": [],
            "estimated_disk_gb": 8,
            "stages": [],
        }

    async def fake_install_packs(*args, **kwargs):
        yield {"event": "pack", "status": "running", "message": "Pack running"}
        yield {"event": "complete", "status": "complete", "message": "Packs complete"}

    async def fake_install_models(*args, **kwargs):
        yield {"event": "model", "status": "running", "message": "Model running"}
        yield {"event": "complete", "status": "complete", "message": "Models complete"}

    monkeypatch.setattr(mission_builder, "mission_profile_plan", fake_plan)
    monkeypatch.setattr("nvh.integrations.studio_packs.install_studio_packs", fake_install_packs)
    monkeypatch.setattr("nvh.integrations.studio_packs.install_studio_models", fake_install_models)

    events = [
        event
        async for event in install_mission_profile(
            "student",
            home_dir=str(mission_workspace),
            min_free_gb=0,
        )
    ]

    early_events = events[:-1]
    assert not any(event["event"] == "complete" or event["status"] == "complete" for event in early_events)
    assert [event["stage"] for event in events if event["event"] == "stage-complete"] == ["studio-packs", "models"]
    assert events[-1]["event"] == "complete"
