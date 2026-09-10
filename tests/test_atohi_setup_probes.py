"""Unleased preload/setup paths never run a model in shared mode."""
from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from nvh.cli import services
from nvh.config import settings
from nvh.core.atohi import AtohiAdmission
from nvh.integrations import local_chat
from nvh.integrations.wizard import setup_agent


@pytest.fixture(autouse=True)
def private_home(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    home = tmp_path / "home"
    monkeypatch.setenv("NVH_HOME", str(home))
    config_path = home / "config/config.yaml"
    config_path.parent.mkdir(parents=True)
    config_path.write_text("atohi:\n  enabled: false\n", encoding="utf-8")
    monkeypatch.setattr(settings, "DEFAULT_CONFIG_PATH", config_path)
    monkeypatch.setattr(settings, "_find_project_config", lambda: None)
    return home, config_path


@pytest.mark.parametrize("already_running", [False, True])
@pytest.mark.parametrize("policy", ["enabled", "invalid", "disabled"])
def test_service_start_preload_honors_policy_before_environment_model_override(
    private_home, monkeypatch, already_running, policy,
):
    _, config_path = private_home
    config_path.write_text({
        "enabled": "atohi:\n  enabled: true\n",
        "invalid": "atohi:\n  enabled: maybe\n",
        "disabled": "atohi:\n  enabled: false\n",
    }[policy], encoding="utf-8")
    monkeypatch.setenv("NVH_DEFAULT_OLLAMA_MODEL", "synthetic-model")
    monkeypatch.setenv("NVH_OLLAMA_PRELOAD", "1")
    monkeypatch.setattr(services, "ollama_healthy", lambda *a, **kw: (already_running, "synthetic"))
    monkeypatch.setattr(services, "port_listening", lambda *a, **kw: False)
    monkeypatch.setattr(services, "_ollama_binary", lambda: "synthetic-ollama")
    monkeypatch.setattr(services, "_wait_for", lambda *a, **kw: (True, "synthetic"))
    launches, threads, requests = [], [], []
    monkeypatch.setattr(services.subprocess, "Popen", lambda *a, **kw: launches.append(a))

    class ImmediateThread:
        def __init__(self, *, target, name, daemon):
            threads.append((name, daemon))
            self.target = target

        def start(self):
            self.target()

    def request(req, timeout):
        requests.append((req.full_url, req.get_method(), json.loads(req.data), timeout))
        return SimpleNamespace(read=lambda: b"{}")

    monkeypatch.setattr("threading.Thread", ImmediateThread)
    monkeypatch.setattr(services.urllib.request, "urlopen", request)
    healthy, message = services.start_ollama()
    assert healthy and len(launches) == (0 if already_running else 1)
    if policy == "disabled":
        assert threads == [("ollama-preload", True)]
        assert requests == [(
            "http://127.0.0.1:11434/api/generate", "POST",
            {"model": "synthetic-model", "prompt": "", "keep_alive": "10m", "stream": False}, 60,
        )]
        assert "not attempted" not in message
    else:
        assert not threads and not requests
        assert "model preload not attempted" in message


def seed_ready(home):
    state = home / "state/local-chat-smoke.json"
    state.parent.mkdir(parents=True)
    state.write_text(json.dumps({
        "ready": True, "status": "ready", "checked_at": datetime.now(UTC).isoformat(),
        "model": "synthetic-model", "cached": False,
    }), encoding="utf-8")
    return state.read_bytes()


@pytest.mark.parametrize("explicit", [False, True])
@pytest.mark.parametrize("cached", [False, True])
def test_shared_probe_does_not_post_or_reuse_cached_ready(
    private_home, monkeypatch, explicit, cached,
):
    home, config_path = private_home
    original = seed_ready(home) if cached else None
    policy = AtohiAdmission(settings.AtohiConfig(enabled=True)) if explicit else None
    if not explicit:
        config_path.write_text("atohi:\n  enabled: true\n", encoding="utf-8")
    monkeypatch.setattr(local_chat.httpx, "Client", lambda **kw: pytest.fail("No model transport is allowed"))
    result = local_chat.local_chat_smoke_status(home, admission=policy)
    assert result["ready"] is False and result["status"] == "paused"
    assert not result["cached"] and not result["attempted_models"]
    if cached:
        assert (home / "state/local-chat-smoke.json").read_bytes() == original


def test_invalid_policy_cannot_reuse_cached_ready(private_home, monkeypatch):
    home, config_path = private_home
    seed_ready(home)
    config_path.write_text("atohi:\n  enabled: yes-please\n", encoding="utf-8")
    monkeypatch.setattr(local_chat.httpx, "Client", lambda **kw: pytest.fail("Invalid policy reached HTTP"))
    result = local_chat.local_chat_smoke_status(home)
    assert not result["ready"] and not result["cached"]
    assert result["status"] == "configuration-error" and not result["attempted_models"]


@pytest.mark.parametrize("persisted_shared", [False, True])
def test_explicit_disabled_probe_preserves_success_and_cache(
    private_home, monkeypatch, persisted_shared,
):
    home, config_path = private_home
    if persisted_shared:
        config_path.write_text("atohi:\n  enabled: true\n", encoding="utf-8")
    calls = []

    class Client:
        def __init__(self, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def get(self, url):
            return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {"models": [{"name": "synthetic"}]})

        def post(self, url, json):
            calls.append((url, json))
            return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {"message": {"content": "NVHIVE_READY"}})

    monkeypatch.setattr(local_chat.httpx, "Client", Client)
    policy = AtohiAdmission(settings.AtohiConfig(enabled=False))
    first = local_chat.local_chat_smoke_status(home, admission=policy)
    second = local_chat.local_chat_smoke_status(home, admission=policy)
    assert first["ready"] and second["ready"] and second["cached"]
    assert first["output_chars"] == len("NVHIVE_READY") and len(calls) == 1
    assert calls[0][0].endswith("/api/chat") and calls[0][1]["options"]["num_predict"] == 16


def test_setup_report_and_reply_keep_explicit_policy_through_real_probe(
    private_home, monkeypatch,
):
    home, _ = private_home
    seed_ready(home)
    monkeypatch.setattr(local_chat.httpx, "Client", lambda **kw: pytest.fail("Shared setup reached model HTTP"))
    storage = SimpleNamespace(ok=True, configured_by="argument", as_dict=lambda: {
        "configured_by": "argument", "layout": {"home": str(home)},
    })
    monkeypatch.setattr(setup_agent, "storage_status", lambda **kw: storage)
    monkeypatch.setattr(setup_agent, "runtime_status", lambda: SimpleNamespace(strategy="ready", as_dict=lambda: {}))
    monkeypatch.setattr(setup_agent, "catalog_with_status", lambda: {"packs": [
        {"id": "rootless-ollama", "status": {"installed": True}},
    ]})
    monkeypatch.setattr(setup_agent, "model_catalog_with_status", lambda: {
        "models": [], "installed_targets": ["synthetic"], "ollama_running": True,
    })
    monkeypatch.setattr(setup_agent, "detect_comfyui", lambda **kw: {"installed": True, "examples_installed": True})
    for name in ("_safe_receipt_summary", "_safe_catalog_data", "_safe_catalog_status", "_safe_compatibility_report", "_safe_boot_preflight"):
        monkeypatch.setattr(setup_agent, name, lambda *a, **kw: {})
    monkeypatch.setattr(setup_agent, "_recent_failed_job", lambda **kw: None)
    policy = AtohiAdmission(settings.AtohiConfig(enabled=True))
    report = setup_agent.setup_helper_report(home, admission=policy)
    assert report["local_chat"]["status"] == "paused" and not report["local_chat"]["ready"]
    assert not any(action["id"] == "rootless-ollama" for action in report["actions"])
    reply = setup_agent.setup_assistant_reply("Is chat working?", home, admission=policy)
    assert "not run" in reply["answer"] and "shared resource admission" in reply["answer"]
    assert "chat returned text" not in reply["answer"] and not reply["commands"]
