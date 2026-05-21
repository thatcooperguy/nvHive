"""Regression tests for release, packaging, and rootless deployment hardening."""

from __future__ import annotations

import tomllib
from pathlib import Path

from nvh.storage import repository

ROOT = Path(__file__).resolve().parents[1]


def test_all_extra_contains_runtime_extras() -> None:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    extras = data["project"]["optional-dependencies"]
    all_extra = set(extras["all"])

    for group in ("serve", "nvidia", "mcp", "vision", "browser"):
        assert set(extras[group]).issubset(all_extra)


def test_release_workflow_has_tag_version_parity_gate() -> None:
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")

    assert "Verify release version matches tag" in workflow
    assert "RELEASE_TAG" in workflow
    assert "pyproject.toml" in workflow
    assert "nvh/__init__.py" in workflow


def test_repository_default_db_path_prefers_rootless_state(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("HIVE_DATA_DIR", raising=False)
    monkeypatch.delenv("NVH_STATE", raising=False)
    monkeypatch.setenv("NVH_HOME", str(tmp_path / "nvhive"))

    assert repository._default_db_path() == tmp_path / "nvhive" / "state" / "nvhive.db"

    monkeypatch.setenv("NVH_STATE", str(tmp_path / "state"))
    assert repository._default_db_path() == tmp_path / "state" / "nvhive.db"

    monkeypatch.setenv("HIVE_DATA_DIR", str(tmp_path / "data"))
    assert repository._default_db_path() == tmp_path / "data" / "state" / "nvhive.db"


def test_docker_compose_api_is_not_blocked_by_ollama_health() -> None:
    compose = (ROOT / "docker-compose.yaml").read_text(encoding="utf-8")
    hive_api_block = compose.split("hive-api:", 1)[1].split("hive-web:", 1)[0]

    assert "condition: service_healthy" not in hive_api_block


def test_cloud_compose_requires_api_key_before_public_bind() -> None:
    cloud = (ROOT / "docker-compose.cloud.yaml").read_text(encoding="utf-8")

    assert "HIVE_API_KEY: \"${HIVE_API_KEY:?" in cloud


def test_linux_installer_handles_missing_ensurepip_without_root() -> None:
    install = (ROOT / "install.sh").read_text(encoding="utf-8")

    assert "create_rootless_venv" in install
    assert "--without-pip" in install
    assert "bootstrap.pypa.io/get-pip.py" in install
    assert "create_managed_python_env" in install
    assert "$HOME/miniforge3/bin/python" in install
    assert "apt install" not in install


def test_linux_installer_autodetects_persistent_home_and_installs_reset_helper() -> None:
    install = (ROOT / "install.sh").read_text(encoding="utf-8")

    assert "home_is_persistent_candidate" in install
    assert 'roots+=("$HOME"' in install
    assert 'home="$base/nvhive"' in install
    assert "install_uninstall_script" in install
    assert "install_command_shims" in install
    assert "~/.local/bin/nvh" in install
    assert "$NVH_HOME/uninstall.sh" in install
    assert "nvh-uninstall" in install
    assert "# >>> nvhive rootless env >>>" in install


def test_linux_installer_aligns_gpu_model_config_and_auto_launch() -> None:
    install = (ROOT / "install.sh").read_text(encoding="utf-8")

    # The Wizard's user-facing default is multimodal at every VRAM tier
    # so the AI Wizard can see screenshots, images, and documents from
    # the first install. NVIDIA Nemotron Omni leads on 40+ GB rigs;
    # progressively smaller vision-capable models fall through to
    # moondream (~2 GB) which still runs on CPU-only hosts.
    assert 'DEFAULT_OLLAMA_MODEL="nemotron-omni"' in install
    assert 'DEFAULT_OLLAMA_MODEL="nemotron-3-nano-omni"' in install
    assert 'DEFAULT_OLLAMA_MODEL="llama3.2-vision"' in install
    assert 'DEFAULT_OLLAMA_MODEL="minicpm-v"' in install
    assert 'DEFAULT_OLLAMA_MODEL="moondream"' in install
    # Soft-fallback chain in pull_nvwizard_model_cli — when the
    # preferred Omni tag 404s on Ollama, walk down vision-capable
    # alternatives instead of failing the install.
    assert "_nvwizard_fallback_chain" in install
    # HuggingFace → Ollama Modelfile bootstrap — when the Omni tag 404s
    # on the Ollama library, download the official GGUF + mmproj from
    # the ggml-org HuggingFace repo and register it locally before
    # falling through to llama3.2-vision. Lands the actual NVIDIA
    # Nemotron Omni model end users were promised.
    assert "bootstrap_omni_via_hf" in install
    assert "_nvwizard_hf_gguf_source" in install
    assert "ggml-org/NVIDIA-Nemotron-3-Nano-Omni" in install
    assert "mmproj-nemotron-3-nano-omni-ga_v1.0.gguf" in install
    assert "nemotron-3-nano-omni-ga_v1.0-Q4_K_M.gguf" in install
    assert "nemotron-3-nano-omni-ga_v1.0-Q8_0.gguf" in install
    # The bootstrap must respect the user opt-out env knob.
    assert "NVH_INSTALL_MODEL_DOWNLOAD" in install
    # ollama create wires the downloaded GGUF + mmproj into a usable
    # local tag.
    assert '"$OLLAMA_BIN" create "$target_tag" -f "$modelfile"' in install
    # Original config/auto-launch contract — preserved from before the
    # multimodal refactor so refactoring this code path requires an
    # intentional update.
    assert "sync_ollama_default_model_config" in install
    assert 'default_model: "ollama/__NVH_DEFAULT_OLLAMA_MODEL__"' in install
    assert 'MODEL="$DEFAULT_OLLAMA_MODEL"' in install
    assert "launch_webui_after_install" in install
    assert "NVH_INSTALL_LAUNCH" in install
    assert "workstation --home-dir" in install
    assert "Pulling $MODEL in background" not in install
    assert "press s to skip" in install
    assert "WebUI will show AI Wizard model download" in install


def test_install_ollama_startup_has_health_wait_and_logging() -> None:
    """install.sh must NOT use the racy ``ollama serve &>/dev/null & sleep N``
    pattern. The previous code silently lost startup errors AND moved on
    before the daemon bound :11434, so the WebUI later showed "Ollama not
    running on 11434 — falling back to cloud providers" on rigs where
    Ollama just needed another 5 seconds to come up.
    """
    install = (ROOT / "install.sh").read_text(encoding="utf-8")

    # The helper that replaces the racy pattern.
    assert "start_ollama_with_health_wait" in install
    # Log redirect (not &>/dev/null) so startup errors are diagnosable.
    assert "ollama.log" in install
    # `nohup` + `disown` so the daemon survives install.sh's exit.
    assert "nohup" in install and "disown" in install
    # Real poll loop with a real timeout knob.
    assert "NVH_OLLAMA_BOOT_TIMEOUT" in install
    assert "/api/tags" in install



def test_nvh_webui_detects_and_restarts_stale_api() -> None:
    """`nvh webui` must HTTP-probe an existing API on the port instead of
    accepting any TCP listener as healthy. Verified empirically 2026-05-20:
    a stale `nvh serve` whose engine failed to initialize at startup will
    keep accepting connections, return HTTP 500 on /v1/health, and block
    the WebUI's red API-offline banner from clearing — even after the
    underlying config corruption is repaired. The TCP-only check used
    before this fix couldn't tell the difference.
    """
    cli = (ROOT / "nvh" / "cli" / "main.py").read_text(encoding="utf-8")

    # The new health probe must exist and check the right invariants.
    assert "def _api_healthy(" in cli
    assert "/v1/health" in cli
    assert "engine_initialized" in cli
    # Stale-process kill path for both Linux (fuser) and macOS (lsof).
    assert "def _kill_stale_api(" in cli
    assert 'fuser' in cli and '-iTCP:' in cli
    # The decision branch: unhealthy existing API gets killed + restarted,
    # not silently accepted.
    assert "Existing API on" in cli
    assert "is unhealthy" in cli


def test_setup_page_surfaces_startup_autopilot_status() -> None:
    setup_page = (ROOT / "web" / "app" / "setup" / "page.tsx").read_text(encoding="utf-8")

    assert "AI Wizard Launch Check" in setup_page
    assert "Download starts in" in setup_page
    assert "Cancel Download" in setup_page
    assert "Skip Model Download" in setup_page
    assert "Progress is shown in Setup Jobs" in setup_page


def test_setup_has_canonical_workspace_state_and_runtime_doctor() -> None:
    server = (ROOT / "nvh" / "api" / "server.py").read_text(encoding="utf-8")
    workspace_state = (ROOT / "nvh" / "integrations" / "diagnostics" / "workspace_state.py").read_text(encoding="utf-8")
    studio_packs = (ROOT / "nvh" / "integrations" / "installs" / "studio_packs.py").read_text(encoding="utf-8")
    setup_page = (ROOT / "web" / "app" / "setup" / "page.tsx").read_text(encoding="utf-8")
    api = (ROOT / "web" / "lib" / "api.ts").read_text(encoding="utf-8")

    assert "/v1/setup/workspace-state" in server
    assert "/v1/setup/runtime-doctor" in server
    assert "def workspace_state" in workspace_state
    assert "def ollama_runtime_doctor" in studio_packs
    assert "health_score" in workspace_state
    assert "getWorkspaceState" in api
    assert "WorkspaceStateReport" in setup_page
    assert "Copy Support Report" in setup_page


def test_linux_start_launcher_prefers_block_backed_home_over_dot_nvh() -> None:
    launch = (ROOT / "start-linux.sh").read_text(encoding="utf-8")

    assert 'printf \'%s\\n\' "$HOME/nvhive"' in launch
    assert 'home_free="$(free_gb_for_path "$HOME")"' in launch
    # Fallback to $HOME/.nvh is now gated behind NVH_ALLOW_EPHEMERAL=1 so
    # students on ephemeral cloud-desktop OS disks don't silently lose
    # models/configs on reconnect. See the preflight in start-linux.sh.
    assert 'fallback_home="$HOME/.nvh"' in launch
    assert 'NVH_ALLOW_EPHEMERAL' in launch
    assert 'exit 2' in launch


def test_workstation_local_ai_uses_hardened_studio_pack_path() -> None:
    cli = (ROOT / "nvh" / "cli" / "main.py").read_text(encoding="utf-8")
    local_ai_block = cli.split("if with_local_ai:", 1)[1].split("if with_comfyui:", 1)[0]

    assert 'install_studio_packs(["rootless-ollama"]' in local_ai_block
    assert "model_catalog_with_status" in local_ai_block
    assert "install_studio_models(model_ids" in local_ai_block
    assert "from nvh.cli.setup import _ensure_ollama" not in local_ai_block
    assert "_pull_model" not in local_ai_block


def test_linux_installer_verifies_rootless_ollama_binary() -> None:
    install = (ROOT / "install.sh").read_text(encoding="utf-8")

    assert "install_rootless_ollama_binary" in install
    assert "ollama_binary_valid" in install
    assert "ollama-linux-%s.tar.zst" in install
    assert "ollama-linux-%s.tgz" in install
    assert "NVH_OLLAMA_VERSION" in install
    assert "NVH_OLLAMA_URL" in install
    assert "github.com/ollama/ollama/releases" in install
    assert "_extract_ollama_archive" in install
    assert "tar -xzf" not in install
    assert '"$bin" --version' in install
    assert "ollama-linux-amd64 -o \"$OLLAMA_BIN\"" not in install


def test_linux_uninstaller_is_rootless_and_supports_purge_reset() -> None:
    uninstall = (ROOT / "uninstall.sh").read_text(encoding="utf-8")

    assert "--purge" in uninstall
    assert "--dry-run" in uninstall
    assert "sudo" not in uninstall
    assert "apt " not in uninstall
    assert "safe_to_remove_home" in uninstall
    assert 'remove_path "$NVH_HOME"' in uninstall
    assert "keep models/config/projects" in uninstall
