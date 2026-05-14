from __future__ import annotations

import os
import shutil
import uuid
from pathlib import Path

import pytest

from nvh.integrations import storage as storage_module
from nvh.integrations.workspace.passport import (
    rootless_policy_report,
    support_snapshot,
    workspace_passport,
    workspace_plan,
)
from nvh.integrations.workspace.storage import nvh_home, storage_layout


@pytest.fixture()
def workspace_tmp():
    root = Path.cwd() / "pytest-workspaces"
    root.mkdir(exist_ok=True)
    path = root / f"passport-{uuid.uuid4().hex}"
    path.mkdir()
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def test_nvhive_home_alias_is_supported(workspace_tmp, monkeypatch):
    home = workspace_tmp / "persist" / "nvhive"
    monkeypatch.delenv("NVH_HOME", raising=False)
    monkeypatch.setenv("NVHIVE_HOME", str(home))

    resolved, configured_by = nvh_home()

    assert resolved == home.resolve()
    assert configured_by == "env:NVHIVE_HOME"
    assert storage_layout().env()["NVHIVE_HOME"] == str(home.resolve())


def test_workspace_passport_stays_inside_workspace(workspace_tmp, monkeypatch):
    home = workspace_tmp / "block-storage" / "nvhive"
    monkeypatch.setenv("NVH_HOME", str(home))

    passport = workspace_passport(min_free_gb=0)

    assert passport["workspace_id"].startswith("nvh-")
    assert passport["rootless"]["normal_setup_requires_admin"] is False
    assert passport["storage_home"] == str(home.resolve())
    assert passport["passport_path"] == str(home.resolve() / "config" / "workspace-passport.json")
    assert Path(passport["passport_path"]).exists()

    for key in ("models_dir", "apps_dir", "projects_dir", "outputs_dir", "support_dir"):
        assert Path(passport["paths"][key]).is_relative_to(home.resolve())


def test_workspace_passport_preview_does_not_create_workspace(workspace_tmp, monkeypatch):
    home = workspace_tmp / "block-storage" / "nvhive"
    monkeypatch.setenv("NVH_HOME", str(home))

    passport = workspace_passport(create=False, min_free_gb=0)

    assert passport["storage_home"] == str(home.resolve())
    assert passport["passport_path"] == str(home.resolve() / "config" / "workspace-passport.json")
    assert not home.exists()


def test_workspace_passport_does_not_promote_implicit_default_home(workspace_tmp, monkeypatch):
    monkeypatch.delenv("NVH_HOME", raising=False)
    monkeypatch.delenv("NVHIVE_HOME", raising=False)
    monkeypatch.setattr(storage_module.Path, "home", lambda: workspace_tmp)

    passport = workspace_passport(min_free_gb=0)

    assert passport["storage"]["configured_by"] == "default"
    assert passport["policy"]["status"] == "warn"
    assert passport["policy"]["gates"][0]["status"] == "warn"
    assert "NVH_HOME" not in os.environ


def test_rootless_policy_blocks_admin_operations(workspace_tmp, monkeypatch):
    home = workspace_tmp / "block-storage" / "nvhive"
    monkeypatch.setenv("NVH_HOME", str(home))

    policy = rootless_policy_report(min_free_gb=0)

    assert policy["no_root_required"] is True
    assert str(home.resolve()) in policy["allowed_write_roots"]
    blocked = " ".join(policy["blocked_operations"]).lower()
    assert "sudo" in blocked
    assert "/usr/local" in blocked
    assert "driver" in blocked


def test_workspace_plan_has_rootless_action_ids(workspace_tmp, monkeypatch):
    home = workspace_tmp / "block-storage" / "nvhive"
    monkeypatch.setenv("NVH_HOME", str(home))

    plan = workspace_plan(profile="creator", min_free_gb=0)

    assert plan["profile"] == "creator"
    assert plan["rootless_safe"] is True
    assert {step["action_id"] for step in plan["steps"]} >= {
        "storage",
        "studio-packs",
        "starter-models",
        "smoke-tests",
    }
    assert all(step["requires_admin"] is False for step in plan["steps"])


def test_support_snapshot_is_redacted_and_bounded(workspace_tmp, monkeypatch):
    home = workspace_tmp / "block-storage" / "nvhive"
    monkeypatch.setenv("NVH_HOME", str(home))
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-secret")

    snapshot = support_snapshot(include_logs=False, min_free_gb=0)
    snapshot_path = Path(snapshot["path"])

    assert snapshot_path.exists()
    assert snapshot_path.is_relative_to(home.resolve())
    assert "API keys and bearer tokens" in snapshot["excludes"]
    assert "sk-test-secret" not in snapshot_path.read_text(encoding="utf-8")
