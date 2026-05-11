from __future__ import annotations

from nvh.integrations import comfyui, service_registry


def test_service_registry_reports_comfyui_url(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("NVH_HOME", str(tmp_path / "nvh"))
    monkeypatch.setattr(
        comfyui,
        "detect_comfyui",
        lambda **_: {
            "installed": True,
            "running": True,
            "ready": True,
            "url": "http://127.0.0.1:8191",
            "host": "127.0.0.1",
            "port": 8191,
            "service_status": "running",
            "app_dir": str(tmp_path / "nvh" / "comfyui" / "ComfyUI"),
            "log_path": str(tmp_path / "nvh" / "comfyui" / "comfyui.log"),
            "log_tail": ["server ready on 8191"],
        },
    )

    status = service_registry.service_status("comfyui", home_dir=tmp_path / "nvh")

    assert status["id"] == "comfyui"
    assert status["ready"] is True
    assert status["url"] == "http://127.0.0.1:8191"
    assert status["next_action_id"] == "start-comfyui"
    assert status["next_action_label"] == "Open ComfyUI"


def test_service_registry_summarizes_api_webui_and_ollama(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("NVH_HOME", str(tmp_path / "nvh"))
    monkeypatch.setattr(service_registry, "_port_open", lambda host, port, timeout=0.25: port in {8000, 3000, 11434})
    monkeypatch.setattr(service_registry, "_http_ok", lambda url, timeout=0.75: True)

    def fake_doctor(home_dir=None):
        return {
            "status": "ready",
            "ready": True,
            "summary": "Local AI is ready.",
            "binary_valid": True,
            "server_running": True,
            "local_candidate": str(tmp_path / "nvh" / "bin" / "ollama"),
            "installed_targets": ["gemma3:4b"],
            "next_action": None,
        }

    import nvh.integrations.studio_packs as studio_packs

    monkeypatch.setattr(studio_packs, "ollama_runtime_doctor", fake_doctor)
    monkeypatch.setattr(
        comfyui,
        "detect_comfyui",
        lambda **_: {"installed": False, "running": False, "ready": False, "service_status": "not-installed"},
    )
    monkeypatch.setattr(
        studio_packs,
        "catalog_with_status",
        lambda: {"packs": []},
    )

    report = service_registry.list_service_statuses(home_dir=tmp_path / "nvh")
    by_id = {service["id"]: service for service in report["services"]}

    assert by_id["nvhive-api"]["ready"] is True
    assert by_id["nvhive-webui"]["running"] is True
    assert by_id["ollama"]["ready"] is True
    assert report["service_count"] >= 4
    assert report["ready_count"] >= 3
    assert report["snapshot"]["enabled"] is True
    assert (tmp_path / "nvh" / "state" / "services" / "latest.json").exists()


def test_service_health_report_includes_ports_actions_and_friendly_copy(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("NVH_HOME", str(tmp_path / "nvh"))
    monkeypatch.setattr(service_registry, "_port_open", lambda host, port, timeout=0.25: port == 8000)
    monkeypatch.setattr(service_registry, "_http_ok", lambda url, timeout=0.75: "8000" in url)

    import nvh.integrations.studio_packs as studio_packs

    monkeypatch.setattr(
        studio_packs,
        "ollama_runtime_doctor",
        lambda home_dir=None: {
            "status": "missing",
            "ready": False,
            "summary": "Ollama runtime is missing.",
            "binary_valid": False,
            "server_running": False,
            "local_candidate": str(tmp_path / "nvh" / "bin" / "ollama"),
            "next_action": {"id": "rootless-ollama", "label": "Install Runtime"},
        },
    )
    monkeypatch.setattr(
        comfyui,
        "detect_comfyui",
        lambda **_: {"installed": False, "running": False, "ready": False, "service_status": "not-installed"},
    )
    monkeypatch.setattr(studio_packs, "catalog_with_status", lambda: {"packs": []})

    report = service_registry.service_health_report(home_dir=tmp_path / "nvh")

    assert report["status"] == "blocked"
    assert report["ports"]["occupied"][0]["port"] == 8000
    assert any(action["action_id"] == "rootless-ollama" for action in report["next_actions"])
    assert "nvHive rootless service health" in report["support_text"]


def test_run_service_action_refresh_and_rejects_unknown_action(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("NVH_HOME", str(tmp_path / "nvh"))
    monkeypatch.setattr(
        comfyui,
        "detect_comfyui",
        lambda **_: {"installed": False, "running": False, "ready": False, "service_status": "not-installed"},
    )

    result = service_registry.run_service_action("comfyui", "refresh", home_dir=tmp_path / "nvh")

    assert result["ok"] is True
    assert result["service"]["id"] == "comfyui"
    try:
        service_registry.run_service_action("comfyui", "sudo-apt-install", home_dir=tmp_path / "nvh")
    except ValueError as exc:
        assert "Unsupported rootless service action" in str(exc)
    else:
        raise AssertionError("unknown service action should be rejected")


def test_setup_assistant_answers_generic_service_question(tmp_path, monkeypatch) -> None:
    from nvh.integrations import setup_agent

    monkeypatch.setenv("NVH_HOME", str(tmp_path / "nvh"))
    monkeypatch.setattr(
        setup_agent,
        "list_service_statuses",
        lambda home_dir=None: {
            "service_count": 2,
            "ready_count": 1,
            "running_count": 1,
            "services": [
                {
                    "id": "nvhive-api",
                    "name": "nvHive API",
                    "installed": True,
                    "running": True,
                    "ready": True,
                    "status": "ready",
                    "summary": "API is healthy.",
                },
                {
                    "id": "comfyui",
                    "name": "ComfyUI",
                    "installed": True,
                    "running": False,
                    "ready": False,
                    "status": "installed-stopped",
                    "summary": "ComfyUI is installed but not running.",
                    "next_action_id": "start-comfyui",
                    "next_action_label": "Start ComfyUI",
                    "command": "",
                },
            ],
        },
    )

    reply = setup_agent.setup_assistant_reply("Which services are running?", tmp_path / "nvh")

    assert reply["focus"] == "service-status"
    assert "2 rootless service" in reply["answer"]
    assert "ComfyUI" in reply["answer"]
    assert reply["actions"][0]["id"] == "start-comfyui"
