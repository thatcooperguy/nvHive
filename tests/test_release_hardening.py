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
    # As of 2026-05-22 the install launches the service pipeline via
    # `nvh services start --open` (verified-bring-up with Rich Live
    # table), not the old `nvh workstation --launch -y`. The new
    # contract is enforced by test_install_sh_uses_services_start_for_verified_bringup.
    assert "services start --open" in install
    assert "Pulling $MODEL in background" not in install
    assert "press s to skip" in install
    # The "WebUI will show…" deferral was removed in PR #82 when model
    # pull became unconditional. The string still appears in a historical-
    # narrative comment explaining the removal, so match the actual bash
    # construct (echo statement) rather than the substring.
    # See test_install_sh_installs_ollama_and_pulls_model_unconditionally.
    assert 'echo -e "${B}WebUI will show AI Wizard model download' not in install


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

    The helpers were promoted to module level in nvh/cli/services.py so
    the new ``nvh services`` command + tests can use them; ``nvh webui``
    now imports them by name. Both files are checked here so neither side
    of the contract can silently regress.
    """
    cli = (ROOT / "nvh" / "cli" / "main.py").read_text(encoding="utf-8")
    services = (ROOT / "nvh" / "cli" / "services.py").read_text(encoding="utf-8")

    # The health probe + stale-kill helpers now live as module-level
    # functions in nvh.cli.services. The exact invariants:
    assert "def api_healthy(" in services
    assert "/v1/health" in services
    assert "engine_initialized" in services
    # Stale-process kill path for both Linux (fuser) and macOS (lsof).
    assert "def kill_stale_api(" in services
    assert "fuser" in services and "-iTCP:" in services

    # ``nvh webui`` must still use them — the import contract pins the
    # call sites so a future refactor that drops the helpers will trip
    # this test instead of silently breaking PR #65's behavior.
    assert "from nvh.cli.services import api_healthy" in cli
    assert "from nvh.cli.services import kill_stale_api" in cli
    # The decision branch: unhealthy existing API gets killed + restarted,
    # not silently accepted.
    assert "Existing API on" in cli
    assert "is unhealthy" in cli


def test_install_detects_port_conflicts_before_starting_services() -> None:
    """install.sh must probe the SET of ports the stack needs BEFORE it
    starts services — 3000/3001/3002 (WebUI cascade), 8000 (API), 11434
    (Ollama). PR #65's _api_healthy and #66's start_ollama_with_health_wait
    each fix one service in isolation; this is the top-level guard the
    owner asked for after the user-reported port-8000-stale-listener
    incident on 2026-05-20.

    Classifications: OK (empty or healthy nvHive), STALE (process name
    matches one of ours but health probe fails — kill + restart), FOREIGN
    (somebody else's process — abort with exit 2 unless override is set).
    """
    install = (ROOT / "install.sh").read_text(encoding="utf-8")

    # The bash function the owner specifically named.
    assert "detect_port_conflicts" in install
    # And it must actually be CALLED, not just defined.
    assert install.count("detect_port_conflicts") >= 2

    # The full port set — all five must appear in the bash port list so
    # nobody can silently drop one in a refactor.
    assert 'NVH_PORTS_TO_CHECK="3000 3001 3002 8000 11434"' in install

    # The override knob for power users / CI environments.
    assert "NVH_PORT_CONFLICT_KILL_FOREIGN" in install

    # Health-probe contract mirrors _api_healthy() in nvh/cli/main.py:
    # GET /v1/health for 8000, GET /api/tags for 11434.
    assert "/v1/health" in install
    assert "/api/tags" in install
    # And the macOS fallback pattern from _kill_stale_api — lsof + kill,
    # plus fuser for Linux. Both must be present.
    assert "fuser" in install
    assert "lsof -nP -iTCP" in install

    # The four status classifications all surface in the output table so
    # the user can read the state at a glance.
    for status in ("OK", "STALE", "FOREIGN"):
        assert status in install

    # Call site is wired before the rest of the install runs — find both
    # the call and a downstream service-start anchor, and assert ordering.
    call_idx = install.index("\ndetect_port_conflicts\n")
    # `start_ollama_with_health_wait` is invoked later when Ollama gets
    # spawned; the port check must precede every service-start call site.
    first_ollama_start = install.index("start_ollama_with_health_wait \"$OLLAMA_BIN\"")
    assert call_idx < first_ollama_start, (
        "detect_port_conflicts must run BEFORE start_ollama_with_health_wait"
    )


def test_nvh_services_cli_is_registered_and_documented() -> None:
    """`nvh services` codifies the startup order across all three local
    services (Ollama → API → WebUI). The command + its three subcommands
    must be wired into the typer app, and the dependency contract must be
    documented so refactoring requires an intentional update.
    """
    cli = (ROOT / "nvh" / "cli" / "main.py").read_text(encoding="utf-8")
    services = (ROOT / "nvh" / "cli" / "services.py").read_text(encoding="utf-8")
    doc = (ROOT / "docs" / "SERVICE_ORDER.md").read_text(encoding="utf-8")

    # Typer wiring — subapp + the three canonical subcommands.
    assert "services_app = typer.Typer(" in cli
    assert 'app.add_typer(services_app, name="services"' in cli
    assert '@services_app.command("status")' in cli
    assert '@services_app.command("start")' in cli
    assert '@services_app.command("restart")' in cli
    # `nvh services` (no subcommand) defaults to status via the callback.
    assert "invoke_without_command=True" in cli
    assert "ctx.invoked_subcommand is None" in cli

    # Orchestration helpers must exist and gate on real health, not TCP.
    assert "def snapshot(" in services
    assert "def start_pipeline(" in services
    assert "def restart_pipeline(" in services
    assert "def ollama_healthy(" in services
    assert "def webui_port_listening(" in services

    # The doc covers the dependency graph, the env knobs, and the
    # cross-PR pointer back to #65/#66 that this command consolidates.
    assert "Ollama" in doc and "API" in doc and "WebUI" in doc
    assert "11434" in doc and "8000" in doc and "3000" in doc
    assert "/api/tags" in doc
    assert "/v1/health" in doc
    assert "engine_initialized" in doc
    assert "NVH_OLLAMA_BOOT_TIMEOUT" in doc
    assert "NVH_API_BOOT_TIMEOUT" in doc
    assert "NVH_WEBUI_BOOT_TIMEOUT" in doc
    # Pointer to the foundation PRs (#65, #66) so future readers know
    # which fixes this command consolidates.
    assert "#65" in doc and "#66" in doc


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


def test_linux_installer_advertises_gpu_capability_matrix_and_opt_in_staging() -> None:
    """install.sh must surface the per-VRAM-tier capability matrix on every
    GPU install, and stage ComfyUI / speech / music packs behind the
    opt-in NVH_INSTALL_FULL_CAPABILITY=1 knob. See docs/GPU_TIER_MATRIX.md
    for the source of truth.
    """
    install = (ROOT / "install.sh").read_text(encoding="utf-8")

    # The helpers that map a VRAM tier to capability tokens and pack ids.
    assert "nvh_capability_tiers_for_vram" in install
    assert "nvh_capability_to_pack_ids" in install
    assert "nvh_capability_human_label" in install
    assert "stage_full_capability_for_vram_tier" in install
    assert "print_capability_summary" in install

    # Each VRAM tier in the matrix must be represented.
    assert '"$vram" -ge 8' in install
    assert '"$vram" -ge 12' in install
    assert '"$vram" -ge 16' in install
    assert '"$vram" -ge 24' in install
    assert '"$vram" -ge 40' in install

    # Capability tokens documented in docs/GPU_TIER_MATRIX.md.
    assert "vision-chat" in install
    assert "image-gen-starter" in install
    assert "image-edit" in install
    assert "image-control" in install
    assert "video-gen" in install
    assert "video-gen-pro" in install
    assert "speech-lab" in install
    assert "music-gen" in install

    # The pack ids must be ones that actually exist in studio_packs.py;
    # cross-checked by test_capability_pack_ids_exist_in_studio_packs.
    assert "comfyui-power-nodes" in install
    assert "music-producer-lab" in install
    assert "ace-step-music" in install

    # Default is opt-in; the env knob is documented and the inline-pull
    # opt-in is a SEPARATE knob so the marker-only case stays safe.
    assert "NVH_INSTALL_FULL_CAPABILITY" in install
    assert "NVH_INSTALL_FULL_CAPABILITY_DOWNLOAD" in install
    # The summary is printed on every GPU install (advisory, no download).
    assert "print_capability_summary" in install
    assert "GPU capabilities at" in install

    # Honesty banner: Nemotron Omni is a multimodal LLM, not an image
    # generator or speech synthesizer. Documented in GPU_TIER_MATRIX.md;
    # not asserted in install.sh but the doc must exist.
    matrix_doc = ROOT / "docs" / "GPU_TIER_MATRIX.md"
    assert matrix_doc.is_file(), "docs/GPU_TIER_MATRIX.md must exist"
    matrix_text = matrix_doc.read_text(encoding="utf-8")
    assert "What is \"Nemotron Omni\", really?" in matrix_text
    assert "NVH_INSTALL_FULL_CAPABILITY" in matrix_text


def test_capability_pack_ids_exist_in_studio_packs() -> None:
    """The pack ids the install.sh capability advisor references must be
    real studio packs. Catches typos in install.sh before they ship.
    """
    install = (ROOT / "install.sh").read_text(encoding="utf-8")
    studio = (ROOT / "nvh" / "integrations" / "installs" / "studio_packs.py").read_text(encoding="utf-8")

    # Pack ids referenced from nvh_capability_to_pack_ids in install.sh.
    for pack_id in ("comfyui-power-nodes", "music-producer-lab", "ace-step-music"):
        assert pack_id in install, f"install.sh must reference {pack_id}"
        assert f'id="{pack_id}"' in studio, (
            f"studio_packs.py must define a StudioPack with id={pack_id!r}"
        )


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


def test_webui_has_system_console_with_log_tail_and_restart_api_bridge() -> None:
    """The SystemConsole is the visible-in-WebUI fix for "the API is down so
    the user has to run `nvh serve` in a terminal." It must:

      1. Exist as a top-level component mounted in LayoutShell (both chat/
         setup branches AND the main shell branch — chat is the first page
         a fresh-install user sees, so it's the most important one).
      2. Tail log files via /api/logs (a Next.js route that reads
         $NVH_HOME/logs/*.log from disk — DOES NOT depend on the FastAPI
         server, so it works when the API is dead).
      3. Offer a [Restart API] action that POSTs /api/services/start-api,
         which spawns `nvh serve` rootlessly via Node child_process.
      4. Offer a [Doctor] action that hits /api/services/doctor.

    The point is "users never need a terminal." If any of these break,
    we've regressed the rootless out-of-the-box promise.
    """
    console = (ROOT / "web" / "components" / "SystemConsole.tsx").read_text(encoding="utf-8")
    layout = (ROOT / "web" / "components" / "LayoutShell.tsx").read_text(encoding="utf-8")
    logs_route = (ROOT / "web" / "app" / "api" / "logs" / "route.ts").read_text(encoding="utf-8")
    start_api_route = (
        ROOT / "web" / "app" / "api" / "services" / "start-api" / "route.ts"
    ).read_text(encoding="utf-8")
    doctor_route = (
        ROOT / "web" / "app" / "api" / "services" / "doctor" / "route.ts"
    ).read_text(encoding="utf-8")

    # 1. Mounted on every shell branch — chat/setup AND main.
    assert "import SystemConsole" in layout
    # The mount appears twice (chat/setup branch + main branch).
    assert layout.count("<SystemConsole />") >= 2

    # 2. The console reads via /api/logs and supports the 4 expected sources.
    assert "/api/logs?source=" in console
    for source in ("api", "webui", "ollama", "install"):
        assert f"'{source}'" in console or f'"{source}"' in console
    # The logs route reads $NVH_HOME/logs/*.log from disk, not FastAPI.
    assert "api-server.log" in logs_route
    assert "webui-bootstrap.log" in logs_route
    assert "ollama.log" in logs_route
    assert "install.log" in logs_route
    assert "NVH_LOGS" in logs_route or "NVH_HOME" in logs_route
    # Read-only — no spawn/exec in the logs route.
    assert "child_process" not in logs_route

    # 3. Restart API bridge — POST, child_process.spawn, rootless contract.
    assert "/api/services/start-api" in console
    assert "export async function POST" in start_api_route
    assert "spawn(" in start_api_route
    assert "'serve'" in start_api_route or '"serve"' in start_api_route
    # Rootless contract: NEVER acquire sudo / change user.
    assert "sudo" not in start_api_route
    # Detached so the API survives the request handler.
    assert "detached: true" in start_api_route
    # Binary resolution comes from @/lib/nvh-bridge — see
    # test_nvh_bridge_resolver_does_not_treat_nvh_bin_dir_as_executable
    # for the per-resolver assertions on NVH_BIN_EXE / NVH_BIN / venv paths.
    assert "@/lib/nvh-bridge" in start_api_route

    # 4. Doctor bridge — runs `nvh doctor --json`, returns parsed report.
    assert "/api/services/doctor" in console
    assert "doctor" in doctor_route
    assert "--json" in doctor_route
    assert "execFile" in doctor_route
    # Bounded execution time — doctor can hang on provider key validation.
    assert "timeout:" in doctor_route


def test_api_health_banner_points_users_at_in_webui_console_not_terminal() -> None:
    """When the API is offline, the banner must NOT tell users to "run nvh
    serve in a terminal" — that breaks the out-of-the-box rootless promise.
    It should point them at the SystemConsole's [Restart API] button.
    """
    banner = (ROOT / "web" / "components" / "ApiHealthBanner.tsx").read_text(encoding="utf-8")

    # Negative: the old "run in a terminal" hint is gone.
    assert "in a terminal" not in banner
    # Positive: the new copy points at the in-WebUI control.
    assert "Restart API" in banner
    assert "System Console" in banner
    # Banner sits below the SystemConsole's collapsed bar (32 + 24 = 56px).
    assert "top: '56px'" in banner


def test_install_sh_tees_full_run_to_install_log_for_webui_surface() -> None:
    """install.sh must write its full stdout+stderr to
    $NVH_LOGS/install.log so the SystemConsole's Install tab has something
    to show. The redirect must happen AFTER mkdir creates $NVH_LOGS but
    BEFORE the banner output — earlier and the log file doesn't exist yet,
    later and we miss the install header.
    """
    install = (ROOT / "install.sh").read_text(encoding="utf-8")

    # Process substitution + tee -a to install.log.
    assert 'tee -a "$NVH_LOGS/install.log"' in install
    # Must redirect both stdout and stderr (2>&1 right after the tee).
    assert 'exec > >(tee -a "$NVH_LOGS/install.log") 2>&1' in install
    # Marker line so users can tell runs apart.
    assert "=== nvHive install starting at" in install


def test_debug_report_button_aggregates_everything_for_phone_sharing() -> None:
    """The DebugReportButton is the "one-click show me everything" surface
    the user can photograph with a phone and share. It must:

      1. Live in the bottom-left of every page (mounted in both LayoutShell
         branches — chat/setup AND the main shell). Bottom-left is the
         chosen position so it doesn't collide with the SystemConsole at
         top or any of the right-aligned theme/sidebar controls.
      2. Pull from /api/debug/report which aggregates API health, Ollama
         health, log tails, doctor output, and env into one JSON payload.
      3. Render the payload as a phone-readable monospace report with
         section headers and pattern-matched suggestions so a screenshot
         crop is still useful.
      4. Support Copy-to-clipboard so the user has a paste option too.

    The aggregator route runs all probes in parallel (one bounded wall-time)
    and includes a `diagnose()` step that pattern-matches common failures
    (ImportError, EADDRINUSE, engine_initialized: false, etc.) into
    actionable hints — turning 80 lines of log into 3 lines of "looks like
    X — try Y."
    """
    btn = (ROOT / "web" / "components" / "DebugReportButton.tsx").read_text(encoding="utf-8")
    layout = (ROOT / "web" / "components" / "LayoutShell.tsx").read_text(encoding="utf-8")
    report_route = (
        ROOT / "web" / "app" / "api" / "debug" / "report" / "route.ts"
    ).read_text(encoding="utf-8")

    # 1. Mounted in both shell branches.
    assert "import DebugReportButton" in layout
    assert layout.count("<DebugReportButton />") >= 2
    # Bottom-left fixed position.
    assert "left-3 bottom-3" in btn
    assert 'fixed left-3 bottom-3' in btn

    # 2. Fetches the aggregator + shows the result.
    assert "/api/debug/report" in btn
    # The aggregator parallelizes its probes.
    assert "Promise.all" in report_route
    # Covers all four log sources the SystemConsole knows about.
    for filename in ("api-server.log", "webui-bootstrap.log", "ollama.log", "install.log"):
        assert filename in report_route
    # Probes API + Ollama.
    assert "localhost:8000/v1/health" in report_route
    assert "localhost:11434/api/tags" in report_route
    # Runs nvh doctor --json with a bounded timeout.
    assert "doctor" in report_route and "--json" in report_route
    assert "timeout:" in report_route
    # Same rootless binary resolution as the other bridge routes — comes
    # from the shared @/lib/nvh-bridge module (see
    # test_nvh_bridge_resolver_does_not_treat_nvh_bin_dir_as_executable).
    assert "@/lib/nvh-bridge" in report_route

    # 3. Pattern-matched diagnostics.
    assert "diagnose(" in report_route
    assert "ImportError" in report_route
    assert "engine_initialized" in report_route
    assert "EADDRINUSE" in report_route

    # 4. Copy-to-clipboard for users on devices without easy screenshot.
    assert "clipboard" in btn.lower()
    assert "Copy" in btn


def test_nvh_bridge_resolver_does_not_treat_nvh_bin_dir_as_executable() -> None:
    """Regression for the EACCES bug surfaced on a real-rig debug-report
    photo 2026-05-21:

        binary=/home/kiosk/nvhive/bin  ran=false  fmt=error
        stderr: spawn /home/kiosk/nvhive/bin EACCES

    install.sh exports NVH_BIN as the rootless bin DIRECTORY
    ($NVH_HOME/bin), not the `nvh` executable. The previous bridge
    resolver checked `fs.access(NVH_BIN, X_OK)` which succeeds for
    traversable directories (X_OK on a dir = "can list entries"), so it
    returned the directory string and every spawn/execFile failed.

    The fix consolidates resolution into web/lib/nvh-bridge.ts which:
      1. Uses fs.stat() + !isDirectory() so directories never pass the
         executable-file check.
      2. Honors NVH_BIN_EXE as the explicit FILE override.
      3. Treats NVH_BIN as a DIRECTORY — looks for `nvh` inside it.
      4. Falls through canonical rootless install paths.

    All three bridge routes (start-api, doctor, debug/report) must import
    from the shared module, not duplicate the resolver logic.
    """
    bridge = (ROOT / "web" / "lib" / "nvh-bridge.ts").read_text(encoding="utf-8")
    start_api = (
        ROOT / "web" / "app" / "api" / "services" / "start-api" / "route.ts"
    ).read_text(encoding="utf-8")
    doctor = (
        ROOT / "web" / "app" / "api" / "services" / "doctor" / "route.ts"
    ).read_text(encoding="utf-8")
    debug_report = (
        ROOT / "web" / "app" / "api" / "debug" / "report" / "route.ts"
    ).read_text(encoding="utf-8")

    # The shared module exists + exports the right surface.
    assert "export async function resolveNvhBinary" in bridge
    assert "export function nvhHome" in bridge
    assert "export function nvhLogsDir" in bridge

    # The isFile() check is the load-bearing fix. fs.access alone is what
    # caused the bug — verify we use stat + isFile() instead.
    assert "isFile()" in bridge
    # Belt-and-suspenders: the X_OK check is still here to catch
    # non-executable files (e.g. a stale config someone named `nvh`).
    assert "X_OK" in bridge

    # Resolution order documented + implemented.
    assert "NVH_BIN_EXE" in bridge  # explicit file override
    assert "NVH_BIN" in bridge  # treated as directory
    assert "venv/bin/nvh" in bridge or "'venv', 'bin', 'nvh'" in bridge

    # All three bridge routes import from the shared module — no per-route
    # copy of the resolver logic that could drift.
    assert "from '@/lib/nvh-bridge'" in start_api
    assert "from '@/lib/nvh-bridge'" in doctor
    assert "from '@/lib/nvh-bridge'" in debug_report
    # Negative: the buggy `fs.access(NVH_BIN, X_OK)` pattern is gone from
    # every route file (lives only in the shared module now, and only
    # against candidates that are already known to be files).
    for route_src in (start_api, doctor, debug_report):
        # The route bodies should no longer have their own resolveNvhBinary.
        assert "async function resolveNvhBinary" not in route_src


def test_nvh_webui_daemonizes_api_for_terminal_close_survival() -> None:
    """The API auto-started by `nvh webui` must survive `nvh webui` exit,
    install terminal close, and SIGHUP. Real-rig 2026-05-21: photo 2
    showed API UP at install completion, photo 3 ~30 seconds later showed
    API DOWN — because the previous code terminated the API subprocess
    in the `finally` block when `nvh webui` exited.

    The fix (same rootless-daemon pattern as Ollama in PR #66):
      1. `start_new_session=True` so the API is its own session leader
         (setsid()) and doesn't receive SIGHUP from the install terminal.
      2. `stdin=subprocess.DEVNULL` so it has no tty to lose.
      3. The finally-block must NOT call api_proc.terminate() — that's
         the regression the previous code shipped.
    """
    cli = (ROOT / "nvh" / "cli" / "main.py").read_text(encoding="utf-8")

    # The three load-bearing args on the API Popen call.
    assert "start_new_session=True" in cli
    assert "stdin=subprocess.DEVNULL" in cli

    # The finally-block must not terminate the API. We look for the
    # specific anti-pattern: `api_proc.terminate()` inside a finally
    # block that runs after `subprocess.run(command, cwd=web_dir`.
    assert "api_proc.terminate()" not in cli
    # The new behavior surfaces the daemon's pid + log path so the user
    # has a path to stop it later.
    assert "API left running in background" in cli
    assert "nvh services stop" in cli


def test_webui_readiness_requires_engine_initialized_not_just_tcp() -> None:
    """The post-spawn API readiness wait must require full /v1/health
    success + engine_initialized: true, not just TCP-listening. A stale
    process whose engine crashed during init holds the port and answers
    TCP but returns HTTP 500 on /v1/health — that's the exact failure
    mode PR #65 added _api_healthy to detect, and the wait loop must
    use it instead of the TCP-only _api_reachable.
    """
    cli = (ROOT / "nvh" / "cli" / "main.py").read_text(encoding="utf-8")

    # Find the wait loop and verify it calls _api_healthy.
    wait_block = cli.split(
        "Cold-import time for FastAPI + nvh providers on a fresh", 1,
    )[1].split("Surface the local LLM runtime state", 1)[0]
    assert "_api_healthy(api_port)" in wait_block
    # The success message names what we actually verified.
    assert "engine initialized" in wait_block.lower()
    # On wait timeout, dump the api-server.log tail so the SystemConsole's
    # Install tab + the install.log file both see the cause.
    assert "api-server.log tail" in wait_block


def test_system_console_silently_auto_restarts_api_when_down() -> None:
    """The user explicitly said: "we do not want the user to have to click
    it should all just work out the box." When the API is confirmed-down
    for several consecutive probes, the SystemConsole must silently POST
    /api/services/start-api on its own — no button click required.

    The auto-restart must be:
      - Triggered only after 3+ consecutive failures (~24s) so a normal
        cold-start grace window doesn't restart-storm us.
      - Rate-limited so we don't restart-loop a fundamentally broken
        install (2-min minimum gap, 3 attempts per session max).
      - Quiet — no banner, no modal, just an actionMessage line.
    """
    console = (ROOT / "web" / "components" / "SystemConsole.tsx").read_text(encoding="utf-8")

    # The three thresholds must all exist.
    assert "AUTO_RESTART_FAILURES_THRESHOLD" in console
    assert "AUTO_RESTART_MIN_GAP_MS" in console
    assert "AUTO_RESTART_MAX_ATTEMPTS" in console
    # The threshold values must match the design contract.
    assert "AUTO_RESTART_FAILURES_THRESHOLD = 3" in console
    assert "AUTO_RESTART_MIN_GAP_MS = 120_000" in console
    assert "AUTO_RESTART_MAX_ATTEMPTS = 3" in console
    # The auto-restart calls the same bridge route the manual button
    # does — not a separate code path that could drift.
    assert "method: 'POST'" in console
    assert "/api/services/start-api" in console
    # The maybe-restart gate must run on every probe.
    assert "maybeAutoRestart" in console
    # Recovery resets the counter so a later outage starts fresh.
    assert "apiFailuresRef.current = 0" in console


def test_wizard_persona_includes_proactive_repair_instructions() -> None:
    """The user said: "The Wizard agent can also help out if anything is
    broken from a change." The Wizard's system prompt must explicitly
    instruct it to scan workspace state on every turn, detect degraded
    services (Ollama down, missing models, invalid provider keys), call
    the relevant auto-tools inline, and only then answer the user's
    question.
    """
    persona = (
        ROOT / "nvh" / "integrations" / "wizard" / "personality.py"
    ).read_text(encoding="utf-8")

    # The proactive-repair section exists as a labeled block in the persona.
    assert "Proactive repair" in persona
    assert "RUN ON EVERY TURN" in persona
    # The four failure → tool-class mappings. We use natural-language
    # tool-class names (not literal function names like `refresh_models`)
    # so the persona doesn't accidentally appear to mention tools that
    # only show up in the dynamic tools block — see
    # tests/test_wizard_tool_call_parsing.py::
    # test_build_system_prompt_appends_tools_block_when_provided.
    assert "Local-model daemon unreachable" in persona
    assert "workspace-repair tool" in persona
    assert "No local models installed" in persona
    assert "model-refresh tool" in persona
    assert "provider key is missing/invalid" in persona
    assert "key-validation tool" in persona
    # The contract: fix first, then answer.
    assert "Only after you've kicked the repair, answer" in persona
    # The user-facing rationale (so future edits don't accidentally
    # delete this section thinking it's filler).
    assert "everything just works out of the box" in persona


def test_system_console_probe_timeout_matches_probe_interval() -> None:
    """The SystemConsole's /v1/health probe must use a generous-enough
    timeout for cold cloud-GPU rigs. Real-rig photo 2026-05-22 showed a
    healthy /v1/health response took 4694ms on a cold RTX 4080 rig (engine
    init warmup). The previous 2500ms timeout flagged that healthy API as
    down and triggered an unnecessary auto-restart. The fix: bump probe
    timeout to match the 8s probe interval so any response inside one
    cadence counts as "in time."
    """
    console = (ROOT / "web" / "components" / "SystemConsole.tsx").read_text(encoding="utf-8")

    # The named constant exists with the right value.
    assert "PROBE_TIMEOUT_MS = 8_000" in console
    # The probe loop uses the constant, not a hardcoded value.
    assert "setTimeout(() => ctl.abort(), PROBE_TIMEOUT_MS)" in console
    # The old 2500ms timeout is gone from the probe path.
    assert "ctl.abort(), 2500" not in console


def test_system_console_auto_restart_respects_boot_grace_window() -> None:
    """Auto-restart must NOT fire during the first 20s if we've never seen
    the API healthy. Cold cloud VMs need 15-20s for FastAPI imports + engine
    init; restarting during that window just resets the warmup clock. The
    grace window matches ApiHealthBanner.BOOT_GRACE_MS so the two surfaces
    agree on what "still booting" means.

    Once we've seen the API healthy at least once, the grace window no
    longer applies — a LATER outage triggers normal auto-restart logic
    immediately at the failure threshold.
    """
    console = (ROOT / "web" / "components" / "SystemConsole.tsx").read_text(encoding="utf-8")

    # The named constant exists and matches the banner.
    assert "BOOT_GRACE_MS = 20_000" in console
    # Sticky-once-healthy ref so the grace window only applies on first boot.
    assert "everHealthyRef" in console
    # The grace gate fires inside maybeAutoRestart.
    assert "everHealthyRef.current" in console
    assert "now - mountedAtRef.current < BOOT_GRACE_MS" in console
    # The healthy branch flips everHealthyRef so later outages skip the gate.
    assert "everHealthyRef.current = true" in console


def test_doctor_bridge_routes_parse_json_even_on_non_zero_exit() -> None:
    """Real-rig photo 2026-05-22 showed `nvh doctor --json` exit=1 on a
    cloud GPU rig because the "Environment: local/on-prem detected" check
    correctly returns "not detected" — that single failed check makes the
    overall doctor exit code 1, but the JSON output is fully valid and
    contains 4+ passing checks worth showing.

    Both doctor-running routes (/api/services/doctor AND
    /api/debug/report) must parse err.stdout as JSON before declaring
    the report unreadable. Otherwise we hide useful diagnostics behind a
    single failed check.
    """
    doctor_route = (
        ROOT / "web" / "app" / "api" / "services" / "doctor" / "route.ts"
    ).read_text(encoding="utf-8")
    debug_route = (
        ROOT / "web" / "app" / "api" / "debug" / "report" / "route.ts"
    ).read_text(encoding="utf-8")

    for source in (doctor_route, debug_route):
        # Both routes have a catch block that attempts JSON.parse on
        # err.stdout before returning a failure.
        catch_block = source.split("catch (err)", 1)[1]
        assert "e.stdout" in catch_block
        assert "JSON.parse(e.stdout)" in catch_block
        # The successful-parse path returns the report with the non-zero
        # exit code attached, not an error.
        assert "exitCode" in catch_block
    # Doctor route's success-on-non-zero-exit returns format: 'json' with
    # the report content, not the 500 error path.
    assert "format: 'json'" in doctor_route.split("catch (err)", 1)[1]


def test_nvh_webui_announce_message_reflects_daemon_lifecycle() -> None:
    """The console message printed by `nvh webui` after starting the API
    USED to say "auto-started, will stop with Ctrl+C". That was true
    before PR #80 daemonized the API; it's now wrong AND gets captured
    into install.log via the install.sh tee, misleading users who read
    the install log into thinking the API requires the install terminal
    to stay open. The corrected message must reflect the new daemon
    lifecycle.
    """
    cli = (ROOT / "nvh" / "cli" / "main.py").read_text(encoding="utf-8")

    # Negative: the old stale message is gone.
    assert "auto-started, will stop with Ctrl+C" not in cli
    # Positive: new message names the daemon contract.
    assert "daemonized; survives Ctrl+C + terminal close" in cli
    # The trailer ("Press Ctrl+C to stop") was misleading too — it implied
    # Ctrl+C kills everything. Reword to clarify only the WebUI stops.
    assert "Press Ctrl+C to stop the WebUI (the API stays running)" in cli


def test_install_sh_installs_ollama_and_pulls_model_unconditionally() -> None:
    """The single highest-leverage install bug fixed 2026-05-22: the
    "Set up Ollama (local AI)" block USED to be gated on
    `[ -n "$GPU_NAME" ]` (line 1751). On a real cloud GPU rig where
    `nvidia-smi` wasn't on PATH or the userspace NVIDIA tools weren't
    installed, GPU_NAME ended up empty and the entire block — including
    install_rootless_ollama_binary, start_ollama_with_health_wait, and
    pull_nvwizard_model_cli — was skipped. Result: setup page showed
    "Ollama Local AI: MISSING-RUNTIME" on an RTX 4080 rig.

    A second bug compounded it: even on rigs WHERE the gate passed, the
    model-pull was deferred to the WebUI via an `elif should_launch_webui;
    then echo "WebUI will show…"` branch. The WebUI launch path is
    `nvh workstation --launch -y` which doesn't imply --with-local-ai, so
    the deferred pull never happened. Result: no model, Wizard had
    nothing to route to.

    Both gates must stay gone. This test is the regression net for the
    next "Wizard isn't working after install" support ticket — if these
    asserts fail, the install path silently broke again.
    """
    install = (ROOT / "install.sh").read_text(encoding="utf-8")

    # The Ollama-install block exists.
    assert "install_rootless_ollama_binary" in install
    assert "start_ollama_with_health_wait" in install
    assert "pull_nvwizard_model_cli" in install

    # The narrative comment naming the regression survives in source so
    # future edits know why this code path is unconditional.
    assert "ALWAYS, on every Linux install" in install

    # Negative: the gates that caused the failures are gone.
    #
    # The new code path looks like:
    #     # Set up Ollama (local AI) — ALWAYS, on every Linux install
    #     <comment block>
    #     OLLAMA_BIN="$NVH_BIN/ollama"
    #     install_rootless_ollama_binary || OLLAMA_BIN=""
    #
    # Verify the lines immediately around `OLLAMA_BIN="$NVH_BIN/ollama"`
    # at the install-site (NOT the function definition far above) sit at
    # column 0 — no indentation, no `if` wrapping.
    install_block = install.split(
        "# Set up Ollama (local AI) — ALWAYS, on every Linux install", 1,
    )[1].split("# Show the full capability matrix", 1)[0]
    # The OLLAMA_BIN assignment + install call should be UNINDENTED
    # (column 0), proving they are not inside any `if` block.
    assert '\nOLLAMA_BIN="$NVH_BIN/ollama"' in install_block
    assert "\ninstall_rootless_ollama_binary || OLLAMA_BIN=" in install_block
    # The model pull should be unconditional (when Ollama is up), not
    # deferred to should_launch_webui.
    assert "should_launch_webui" not in install_block, (
        "regression: install.sh defers model pull to the WebUI but the WebUI "
        "launch path (`nvh workstation --launch`) doesn't pull. The deferred "
        "branch leaves users with no model."
    )
    # And the pull call itself should be present in the block.
    assert "pull_nvwizard_model_cli" in install_block

    # The capability-matrix display is still GPU-gated (correct — no GPU
    # = no per-tier capability bundle).
    assert 'if [ -n "$GPU_NAME" ]; then\n    print_capability_summary' in install


def test_nvh_workstation_launch_implies_with_local_ai() -> None:
    """`nvh workstation --launch -y` (the command install.sh invokes
    after package install) must imply --with-local-ai. Without that, the
    WebUI launches over a /setup page where Ollama still shows
    MISSING-RUNTIME — which is exactly the regression we shipped twice
    before catching it on a real rig.

    This is belt-and-suspenders for the install.sh fix: install.sh now
    installs Ollama + pulls the model BEFORE invoking workstation, but
    users who run `nvh workstation --launch` directly (without curl|bash)
    should ALSO get a working stack.
    """
    cli = (ROOT / "nvh" / "cli" / "main.py").read_text(encoding="utf-8")

    # Find the workstation command body and verify the implication.
    workstation_block = cli.split("def workstation(", 1)[1].split("\ndef ", 1)[0]
    # The implication must fire when launch=True even without all_in_one.
    assert "if launch and not with_local_ai:" in workstation_block
    assert "with_local_ai = True" in workstation_block
    # Comment explains the why so future edits know. We use the
    # hyphenated form to keep the phrase on a single source line — the
    # multi-word non-hyphenated form is hard to keep intact across line
    # wraps in the comment block.
    assert "everything-just-works-out-of-the-box" in workstation_block


def test_debug_report_probe_timeout_matches_system_console() -> None:
    """Real-rig photo 2026-05-22 (4th install retest) showed the Debug
    Report SUMMARY claiming "API process is not listening" with
    `(3002ms) error: This operation was aborted` while the SAME page's
    server-rendered service-control panel said "nvHive API READY". The
    contradiction was: the Debug Report's probeService default timeout
    was 3000ms (set when this code was written, never updated when
    SystemConsole's PROBE_TIMEOUT_MS was bumped to 8000ms in PR #81).

    Both probe surfaces must share the same tolerance, otherwise users
    will keep seeing this exact false-positive during the heavy-I/O
    window of a fresh-install model pull.
    """
    debug_route = (
        ROOT / "web" / "app" / "api" / "debug" / "report" / "route.ts"
    ).read_text(encoding="utf-8")
    console = (
        ROOT / "web" / "components" / "SystemConsole.tsx"
    ).read_text(encoding="utf-8")

    # SystemConsole already has the 8s timeout (locked in by
    # test_system_console_probe_timeout_matches_probe_interval).
    assert "PROBE_TIMEOUT_MS = 8_000" in console
    # Debug Report's probeService default must also be 8000.
    assert "async function probeService(url: string, timeoutMs = 8000)" in debug_route
    # Negative: the old 3000 default is gone.
    assert "timeoutMs = 3000" not in debug_route
    # Comment cross-references the SystemConsole + names the regression.
    assert "matches SystemConsole's PROBE_TIMEOUT_MS" in debug_route


def test_install_sh_uses_services_start_for_verified_bringup() -> None:
    """The owner-requested contract: "all services installed and verified
    up and running via CLI view. Once everything is up and running then
    launch the browser so it starts in a good and working state."

    install.sh must call `nvh services start --open` at the end (not
    `nvh workstation --launch -y`). `nvh services start` is the
    Rich-Live-progress-table command that:
      1. Brings up Ollama → API → WebUI in order
      2. Health-gates each step before the next starts
      3. Shows live per-service status (waiting → starting → healthy/failed)
      4. Opens the browser ONLY when all three services are healthy

    `workstation --launch` had no such gate — it opened the browser as
    part of `nvh webui` regardless of API engine state, which led to
    every "red API offline banner on first page load" failure mode
    photographed in this cycle.
    """
    install = (ROOT / "install.sh").read_text(encoding="utf-8")

    # Find the launcher function body.
    fn_block = install.split("launch_webui_after_install() {", 1)[1].split("\n}\n", 1)[0]

    # Positive: the new bring-up-verified call is present.
    assert '"$nvh_cmd" services start --open' in fn_block
    # The user-facing copy reflects the new contract.
    assert "Verifying all services up before opening the browser" in fn_block
    assert "browser opens ONLY" in fn_block or "browser opens only" in fn_block.lower()

    # Negative: the old foreground workstation --launch is no longer
    # the install entry. `nvh workstation` is still a valid command for
    # explicit invocation but the install no longer runs it.
    assert '"$nvh_cmd" workstation --home-dir "$NVH_HOME" --launch -y' not in fn_block
    # Failure path tells the user the retry command and doesn't deadend.
    assert "services start" in fn_block.split("Service bring-up failed", 1)[1]


def test_services_start_uses_rich_live_progress_table() -> None:
    """The `nvh services start` command must show a Rich Live table with
    per-service rows that update in place (waiting → starting → healthy
    /failed). The owner asked for a "CLI view" — the table is the view.

    Each row must support all four states so an install never leaves a
    user staring at an ambiguous "starting…" forever (skipped states
    must propagate when an earlier step fails).
    """
    cli = (ROOT / "nvh" / "cli" / "main.py").read_text(encoding="utf-8")
    # Find the services_start command body.
    body = cli.split("def services_start(", 1)[1].split("\n@services_app", 1)[0]

    # Rich Live + Table imports / usage.
    assert "from rich.live import Live" in body
    assert "from rich.table import Table" in body
    assert "with Live(" in body
    assert "live.update(" in body

    # The four canonical row states.
    for status in ("waiting", "starting", "healthy", "failed", "skipped"):
        assert status in body, f"missing status: {status}"

    # Begin + complete callbacks so the row can show 'starting' mid-step.
    assert "on_step_begin" in body
    assert "_on_begin" in body

    # Browser only opens on success — the failure path raises before
    # the success-banner is printed.
    assert "All services healthy" in body
    assert "raise typer.Exit(1)" in body
    # The success message references the actual webui port.
    assert "Browser → http://localhost:" in body


def test_start_pipeline_threads_on_step_begin_callback() -> None:
    """`start_pipeline` must accept + invoke an `on_step_begin` callback
    so the Rich Live table can render 'starting' state for the step
    currently being attempted. Without this, the table would jump from
    'waiting' straight to 'healthy/failed' with no visible in-progress
    state — feels like a freeze on slow rigs where each step's health
    wait can run 15-20s.
    """
    services_py = (ROOT / "nvh" / "cli" / "services.py").read_text(encoding="utf-8")

    # The signature + propagation through restart_pipeline.
    assert "on_step_begin: Any = None" in services_py
    assert "on_step_begin(\"Ollama\")" in services_py
    assert "on_step_begin(\"API\")" in services_py
    assert "on_step_begin(\"WebUI\")" in services_py
    # restart_pipeline must thread the same callback so the Live table
    # works on restart too.
    assert services_py.count("on_step_begin=on_step_begin") >= 1


def test_services_start_runs_wizard_smoke_test_before_browser() -> None:
    """The final piece of the "open the browser only on a working state"
    contract: `nvh services start` must run a real end-to-end Wizard
    smoke test as the 4th pipeline step, AFTER WebUI port is listening
    but BEFORE the browser opens.

    Why: three ports listening doesn't prove the Wizard can actually
    answer. The model might not be loaded, the engine might be in a
    broken state, providers might be misconfigured. The smoke test
    POSTs to /v1/wizard/chat and waits for a non-empty answer — the
    minimum signal that the user can chat.

    Permissive: fallback-mode answers (no local LLM available) still
    count as ok for the browser-open decision. Only timeouts / endpoint
    errors / empty answers gate the browser.
    """
    services = (ROOT / "nvh" / "cli" / "services.py").read_text(encoding="utf-8")
    cli = (ROOT / "nvh" / "cli" / "main.py").read_text(encoding="utf-8")

    # The helper exists and POSTs to the canonical Wizard endpoint.
    assert "def wizard_smoke_test(" in services
    assert "/v1/wizard/chat" in services
    # Generous default timeout — bumped 2026-05-22 from 45s to 90s
    # after audit found cold-cache first-run could exceed 45s on slow
    # ephemeral disks. The per-call timeout shape is asserted by
    # test_wizard_smoke_test_distinguishes_llm_from_deterministic_fallback.
    assert "timeout: float = 90.0" in services
    # Permissive on response shape — handles wrapped and flat envelopes.
    assert 'body.get("data") or body' in services

    # start_pipeline runs the smoke test as the 4th step, after WebUI.
    pipeline_block = services.split("def start_pipeline(", 1)[1].split(
        "\ndef restart_pipeline(", 1,
    )[0]
    assert "wizard_smoke_test(" in pipeline_block
    assert 'on_step_begin("Smoke test")' in pipeline_block
    assert 'on_step("Smoke test"' in pipeline_block
    # The smoke step must come AFTER the WebUI step in source order.
    webui_idx = pipeline_block.index('on_step_begin("WebUI")')
    smoke_idx = pipeline_block.index('on_step_begin("Smoke test")')
    assert smoke_idx > webui_idx, "smoke test must run after WebUI"

    # The Rich Live table has a 4th row for the smoke test.
    services_start_block = cli.split("def services_start(", 1)[1].split(
        "\n@services_app", 1,
    )[0]
    assert '"Smoke test"' in services_start_block
    # Skip-propagation order includes the new row.
    assert '["Ollama", "API", "WebUI", "Smoke test"]' in services_start_block


def test_services_smoke_test_subcommand_exists() -> None:
    """`nvh services smoke-test` is the standalone command users run
    when they want to verify the stack works without redoing the full
    start pipeline. Exits 0 on success, 1 on hard failure with a
    diagnostic hint pointing at `nvh services status` + `nvh doctor`
    + `nvh services restart`.
    """
    cli = (ROOT / "nvh" / "cli" / "main.py").read_text(encoding="utf-8")

    # Subcommand wiring.
    assert '@services_app.command("smoke-test")' in cli
    assert "def services_smoke_test(" in cli
    # Body imports + calls the shared helper (no duplicate logic).
    body = cli.split("def services_smoke_test(", 1)[1].split("\n@", 1)[0]
    assert "from nvh.cli.services import wizard_smoke_test" in body
    # The diagnostic hints on failure.
    assert "nvh services status" in body
    assert "nvh doctor" in body
    assert "nvh services restart" in body
    # Non-zero exit on hard failure.
    assert "raise typer.Exit(1)" in body


def test_wizard_smoke_test_distinguishes_llm_from_deterministic_fallback() -> None:
    """2026-05-22 audit (Agent B): the previous wizard_smoke_test treated
    any non-empty answer as "ok: <mode>". But the deterministic-fallback
    path in `nvh/integrations/wizard/chat.py` ALWAYS returns a non-empty
    answer (canned "open Setup" replies) — so the smoke test greened the
    gate even when no LLM was actually loaded. The fix:

      - mode == "llm"                              → (True, "ok: llm")
      - mode in {"deterministic", "deterministic-stream-fallback"}
        → (True, "degraded: <mode> (no LLM); <fallback_reason>")

    Both still return ok=True so the browser opens (the user has SOME
    Wizard, even if degraded), but the caller can detect "degraded:"
    prefix and render a yellow row instead of green.
    """
    services_py = (ROOT / "nvh" / "cli" / "services.py").read_text(encoding="utf-8")

    # Mode-based classification.
    assert 'mode == "llm"' in services_py
    assert "degraded:" in services_py
    # Surfaces fallback_reason from the response body so the user sees
    # WHY it fell back.
    assert "fallback_reason" in services_py
    # Default timeout bumped to 90s (Agent A: cold-cache first-run can
    # exceed 45s on slow ephemeral disks).
    assert "timeout: float = 90.0" in services_py


def test_start_ollama_kills_wedged_process_before_spawn() -> None:
    """2026-05-22 audit (Agent A): start_api had kill_stale_api for the
    wedged-but-listening case. start_ollama did not. Common after OOM
    on small VRAM nodes: Ollama is still bound to :11434 but /api/tags
    doesn't respond. The new spawn fails to bind, error message is the
    opaque "did not bind 11434 in 15s". Fix: mirror the kill_stale
    branch from start_api.
    """
    services_py = (ROOT / "nvh" / "cli" / "services.py").read_text(encoding="utf-8")
    # Locate the start_ollama body.
    body = services_py.split("def start_ollama(", 1)[1].split("\ndef ", 1)[0]
    # The wedged-recovery branch.
    assert "if port_listening(OLLAMA_PORT):" in body
    assert "kill_stale_port(OLLAMA_PORT)" in body


def test_start_pipeline_attaches_log_tail_to_failure_result() -> None:
    """2026-05-22 audit (Agent B): every previously-failed retest cycle
    was a user squinting at "did not become healthy in 20s" with no
    log path or content. start_pipeline now populates StartResult.log_path
    + StartResult.log_tail on every failure so the CLI can show the
    actual error inline.
    """
    services_py = (ROOT / "nvh" / "cli" / "services.py").read_text(encoding="utf-8")
    cli_py = (ROOT / "nvh" / "cli" / "main.py").read_text(encoding="utf-8")

    # Dataclass fields.
    assert "log_path: str | None = None" in services_py
    assert "log_tail: list[str] = field(default_factory=list)" in services_py
    # Helper that reads the tail.
    assert "def read_log_tail(" in services_py
    # Populated on each failure path in start_pipeline.
    pipeline_block = services_py.split("def start_pipeline(", 1)[1].split(
        "\ndef restart_pipeline(", 1,
    )[0]
    assert pipeline_block.count("result.log_path =") >= 3, \
        "expected log_path populated on Ollama/API/WebUI/Smoke-test failure"
    assert pipeline_block.count("result.log_tail = read_log_tail(") >= 3

    # CLI services_start prints the tail on failure.
    services_start_block = cli_py.split("def services_start(", 1)[1].split(
        "\n@services_app", 1,
    )[0]
    assert "result.log_path" in services_start_block
    assert "result.log_tail" in services_start_block
    assert "end tail" in services_start_block


def test_start_api_bridge_route_uses_opensync_not_filehandle() -> None:
    """2026-05-22 audit (Agent D): /api/services/start-api/route.ts used
    `await fs.open(logPath, 'a')` which returns a FileHandle with a GC
    finalizer that calls .close() on the underlying fd. Once `fh`
    becomes unreachable (immediately on function return), GC can close
    the fd BEFORE spawn() has finished duplicating it into the child,
    producing an `nvh serve` whose stdout/stderr writes to a closed fd
    → silent crash on first log line. Fix: use fs.openSync (raw int,
    no finalizer) and close the parent's reference explicitly after
    spawn dups it.
    """
    route_ts = (
        ROOT / "web" / "app" / "api" / "services" / "start-api" / "route.ts"
    ).read_text(encoding="utf-8")

    # Positive: openSync is used.
    assert "openSync(logPath, 'a')" in route_ts
    # Negative: the FileHandle-returning fs.open is gone.
    assert "await fs.open(logPath" not in route_ts
    # Parent closes its reference after spawn so the fd doesn't leak.
    assert "fsSync.closeSync(logFd)" in route_ts


def test_install_sh_bashrc_hook_uses_pgrep_guard_before_spawning_ollama() -> None:
    """2026-05-22 audit (Agent D): the .bashrc hook used only a curl
    probe to decide whether to fork `ollama serve &`. On a slow daemon
    the curl false-negatives and every new terminal during boot forks
    an additional `ollama serve` racing for :11434. With multi-tab
    terminals this stormed the OS. Fix: add a `pgrep -f "ollama serve"`
    guard so the hook is idempotent.
    """
    install = (ROOT / "install.sh").read_text(encoding="utf-8")
    # The marker strings appear twice in install.sh — once in an awk
    # skip-block on lines 241-242 (used to dedupe re-installs), and
    # once in the actual heredoc that's appended to the user's rc.
    # We want the rc-heredoc body — split on the heredoc start marker
    # ("RCEOF") to skip the awk reference.
    rc_heredoc = install.split("cat >> \"$tmp\" << RCEOF", 1)[1].split(
        "RCEOF\n", 1,
    )[0]
    # The pgrep guard.
    assert "pgrep -f" in rc_heredoc
    assert "ollama serve" in rc_heredoc


def test_nvh_services_stop_subcommand_exists_and_preserves_ollama_by_default() -> None:
    """2026-05-22 audit (Agent B, finding F4): help text + log copy
    referenced `nvh services stop` but the command was never registered.
    Users running it got typer's "No such command 'stop'" error. Fix:
    register the command. Default behavior preserves Ollama (warm model
    cache is expensive to rebuild); --ollama flag opts in to stopping
    it too.
    """
    cli_py = (ROOT / "nvh" / "cli" / "main.py").read_text(encoding="utf-8")

    # Subcommand wiring.
    assert '@services_app.command("stop")' in cli_py
    assert "def services_stop(" in cli_py
    # Body asserts the right shape.
    body = cli_py.split("def services_stop(", 1)[1].split("\n@", 1)[0]
    # Reverse-dependency order: WebUI before API.
    webui_idx = body.index("WebUI on :")
    api_idx = body.index("API on :")
    assert webui_idx < api_idx, "WebUI must stop before API (reverse dependency order)"
    # Ollama is preserved by default (only stops on --ollama flag).
    assert "preserves warmed model cache" in body
    assert '"--ollama/--no-ollama"' in body


def test_services_pipeline_preloads_ollama_default_model() -> None:
    """2026-05-22 audit (Agent B finding F2 / Agent C finding C4):
    `ollama_healthy()` returns True the instant /api/tags responds —
    well before any model weights are mmap'd into RAM. The user's
    FIRST chat turn then cold-loads the model inline (30-60s on CPU),
    feeling like a hang. Fix: fire-and-forget a `/api/generate
    {prompt:"", keep_alive:"10m"}` after start_ollama returns healthy,
    so the model is warm by the time the smoke test runs.

    Opt-out: NVH_OLLAMA_PRELOAD=0 for unit-test / scripted contexts
    that don't want a background warmup thread.
    """
    services_py = (ROOT / "nvh" / "cli" / "services.py").read_text(encoding="utf-8")

    assert "def _preload_default_model(" in services_py
    assert "_preload_default_model()" in services_py
    # The Ollama keep-alive warmup pattern.
    assert "keep_alive" in services_py
    assert "/api/generate" in services_py
    # Opt-out env knob.
    assert "NVH_OLLAMA_PRELOAD" in services_py
    # Best-effort: runs in a daemon thread so start_ollama doesn't block.
    assert "daemon=True" in services_py
