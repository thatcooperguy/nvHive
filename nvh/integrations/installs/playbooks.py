"""Spark playbooks — NVIDIA's DGX Spark install guides as approved, audited runs.

Each :class:`Playbook` mirrors one folder of github.com/NVIDIA/dgx-spark-playbooks
(``nvidia/<id>``; the id *is* the upstream folder name, so every entry traces
back to its README): title, one-line summary, prerequisites, the numbered
steps, how to verify, how to undo, time and risk. Playbooks are **not** studio
packs — they may need ``sudo`` — so they never go through ``/v1/studio/install``.
They run only through the Wizard's privileged tier
(``WizardToolRegistry.execute()`` is the single enforcement point: red card,
approval token, kill switch) or through the CLI in the user's own terminal
(``nvh playbook install <id>``), where ``sudo`` may ask for a password nvHive
never sees.

Steps compile into :class:`nvh.integrations.wizard.system_settings.Step` /
:class:`~nvh.integrations.wizard.system_settings.Plan` and both drivers run
them through the one :func:`~nvh.integrations.wizard.system_settings.run_host_command`,
so the deny list (``denied_reason``), the sudo matrix, timeouts and output
redaction are inherited rather than re-implemented: the job path runs it as
the Wizard does (``sudo -n`` only where the platform facts found passwordless
sudo, otherwise ``needs_terminal``, ``stdin=DEVNULL``); the CLI path passes
``interactive=True`` (plain ``sudo`` with the terminal's stdin, so ``sudo``
can ask there). A test renders every step, check and verify command of every playbook and
asserts ``denied_reason()`` is ``None``; another asserts no ``bash -c`` script
word carries a placeholder (paths reach a shell only as positional
parameters, so a quote or ``$`` in ``NVH_HOME`` cannot break or inject).

Policies (design brief 2026-09-03 §9, from the upstream research):

  - **pipe-to-shell** (``curl … | sh``) is never executed as a pipe. The step
    downloads the script to ``NVH_HOME/playbooks/<id>/`` and runs that file;
    the upstream one-liner is shown verbatim on the card and the step carries
    ``unpinned=True`` (rendered as a ``pipe-to-shell: unpinned`` note). Where
    the README publishes a sha256 (comfy-ui) the download is verified first.
    A vendor artifact with no version pin and no published checksum in the
    README (vscode's ``stable`` .deb, installed as root) carries the same
    flag, rendered as an ``unpinned download`` note.
  - **Docker** playbooks start with a ``usermod -aG docker <user>`` step whose
    check is ``id -nG | grep -qw docker``; when that step actually runs the run
    halts with a MANUAL "log out and back in" note — never ``newgrp``.
  - **Browser logins, tokens, TUIs, cabling and foreground servers** are
    ``manual`` steps: rendered, never executed.
  - **Undo** is preview text only (several lines hit the deny list by design)
    and is never executed by the runner.
  - The DGX Dashboard **Update Now** path (apt upgrade + firmware + reboot) is
    never automated; ``HF_TOKEN`` / ``NGC_API_KEY`` are declared prerequisites
    nvHive never prompts for or stores.

Every run that touched the host writes an install receipt (kind ``playbook``,
honest ``no_root = not any sudo step ran``, repair plan
``nvh playbook install <id>``) and a vault ``Decisions/`` note through
:func:`nvh.integrations.wizard.tools.audit_privileged_change` — including a
job cancelled mid-step, which records the command that was in flight (the
worker thread cannot be interrupted, so it may have finished on the host).
"""

from __future__ import annotations

import asyncio
import logging
import shlex
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from nvh.integrations.services import receipts as _receipts
from nvh.integrations.wizard import system_settings as ss
from nvh.integrations.wizard.system_settings import Plan, Step, run_host_command

logger = logging.getLogger(__name__)

__all__ = [
    "DEFERRED",
    "HANDOFF_COMMAND",
    "JOB_KIND",
    "PLAYBOOKS",
    "RECEIPT_KIND",
    "DeferredPlaybook",
    "Playbook",
    "PlaybookError",
    "PlaybookStep",
    "catalogue",
    "compile_plan",
    "deferred",
    "get_playbook",
    "plan_dict",
    "playbooks_root",
    "register_wizard_tools",
    "run_in_terminal",
    "run_playbook_events",
    "start_run",
]

UPSTREAM_TREE = "https://github.com/NVIDIA/dgx-spark-playbooks/tree/main/nvidia/"
UPSTREAM_RAW = "https://raw.githubusercontent.com/NVIDIA/dgx-spark-playbooks/refs/heads/main/nvidia/"
RECEIPT_KIND = "playbook"
JOB_KIND = "playbook-run"
#: The one hand-off when sudo needs a password: the CLI runs ``sudo`` interactively.
HANDOFF_COMMAND = "nvh playbook install {id}"
PRIVILEGED_TOOL = "playbook_install"

DEFAULT_TIMEOUT_S = ss.DEFAULT_TIMEOUT_S
INSTALL_TIMEOUT_S = ss.INSTALL_TIMEOUT_S
#: Model pulls, container image pulls and CUDA builds.
LONG_TIMEOUT_S = 3600
CHECK_TIMEOUT_S = 30
#: Lines of a step's output kept in the ``log`` event (the receipt and the audit keep more).
LOG_TAIL_LINES = 20

#: Placeholders rendered at compile time (no shell, so ``~`` and ``$USER`` never expand).
PLAYBOOK_DIR_TOKEN = "@PLAYBOOK_DIR@"
USER_TOKEN = "@USER@"
HOME_TOKEN = "@HOME@"

UNPINNED_NOTE = "pipe-to-shell: unpinned"
#: An ``unpinned`` step whose upstream is not a pipe: a vendor artifact with no version pin or checksum.
UNPINNED_DOWNLOAD_NOTE = "unpinned download"
RELOGIN_NOTE = (
    "Log out and back in (and restart nvHive) so the docker group applies, then run this "
    "playbook again — finished steps are skipped."
)
DOCKER_GROUP_CHECK: tuple[str, ...] = ("bash", "-c", "id -nG | grep -qw docker")


class PlaybookError(ValueError):
    """A playbook that cannot be compiled for this host (bad login name, …)."""


# ────────────────────────────────────────────────────────────────────────────
# Data model
# ────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class PlaybookStep:
    """One upstream step.

    Either ``argv`` (executed, through the system-settings runner) or
    ``manual`` (a browser / GUI / TUI / cabling step the user does; rendered,
    never executed). ``check`` is an argv whose exit 0 means "already done" —
    the step is skipped. ``cwd`` / ``env`` compile into an ``env -C DIR K=V``
    prefix so nothing needs a shell. ``unpinned`` marks a step that installs
    an artifact the README neither pins nor checksums — a download-then-run
    of an upstream ``curl … | sh`` or a vendor ``.deb`` fetched from a
    "latest stable" URL (``upstream`` keeps the verbatim upstream command;
    a pipe in it tells the two apart). ``halt_after`` stops the run once
    this step *ran* (not skipped) with that MANUAL note — the docker-group
    re-login.
    """

    title: str
    argv: tuple[str, ...] = ()
    sudo: bool = False
    idempotent: bool = True
    check: tuple[str, ...] | None = None
    timeout_s: float = DEFAULT_TIMEOUT_S
    manual: str | None = None
    cwd: str | None = None
    env: Mapping[str, str] | None = None
    unpinned: bool = False
    upstream: str = ""
    halt_after: str = ""
    description: str = ""

    @property
    def is_manual(self) -> bool:
        return self.manual is not None


@dataclass(frozen=True)
class Playbook:
    """One upstream playbook, in the README's own skeleton."""

    id: str
    title: str
    category: str
    summary: str
    source_urls: tuple[str, ...]
    prerequisites: tuple[str, ...]
    steps: tuple[PlaybookStep, ...]
    verify: tuple[tuple[str, ...], ...]
    undo: tuple[str, ...]
    notes: tuple[str, ...] = ()
    warning: str = ""
    estimated_minutes: int = 15
    estimated_disk_gb: float = 0.0
    rootless_alternative: str | None = None
    risk: str = "Low"
    last_updated: str = ""

    @property
    def requires_sudo(self) -> bool:
        return self.sudo_steps > 0

    @property
    def sudo_steps(self) -> int:
        """How many executable steps run with sudo (the one count every surface shows)."""
        return sum(1 for step in self.steps if not step.is_manual and step.sudo)

    def executable_steps(self) -> list[PlaybookStep]:
        return [step for step in self.steps if not step.is_manual]

    def manual_steps(self) -> list[PlaybookStep]:
        return [step for step in self.steps if step.is_manual]

    @property
    def unpinned(self) -> bool:
        return any(step.unpinned for step in self.steps)


@dataclass(frozen=True)
class DeferredPlaybook:
    id: str
    title: str
    reason: str

    @property
    def source_url(self) -> str:
        return UPSTREAM_TREE + self.id

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "title": self.title, "reason": self.reason, "source_url": self.source_url}


# ────────────────────────────────────────────────────────────────────────────
# Step constructors (keep the catalogue below readable)
# ────────────────────────────────────────────────────────────────────────────


def _src(playbook_id: str) -> str:
    return UPSTREAM_TREE + playbook_id


def _cmd_exists(name: str) -> tuple[str, ...]:
    return ("bash", "-c", f"command -v {shlex.quote(name)}")


def _sha256_check(sha256: str, path: str) -> tuple[str, ...]:
    """``sha256sum -c`` of one file whose path may hold a placeholder.

    The path travels as a positional parameter (``$1``), never spliced into
    the script text, so an apostrophe, a space or ``$`` in ``NVH_HOME`` can
    neither break the command nor inject into it.
    """
    return ("bash", "-c", f"echo \"{sha256}  $1\" | sha256sum -c -", "sha256sum", path)


def _dpkg(*packages: str) -> tuple[str, ...]:
    return ("dpkg", "-s", *packages)


def _docker_running(name: str) -> tuple[str, ...]:
    return ("bash", "-c", f"docker ps -q --filter name=^{name}$ --filter status=running | grep -q .")


def _step(
    title: str,
    *argv: str,
    sudo: bool = False,
    check: tuple[str, ...] | None = None,
    timeout: float = DEFAULT_TIMEOUT_S,
    idempotent: bool = True,
    cwd: str | None = None,
    env: Mapping[str, str] | None = None,
    unpinned: bool = False,
    upstream: str = "",
    halt_after: str = "",
    description: str = "",
) -> PlaybookStep:
    return PlaybookStep(
        title=title, argv=tuple(argv), sudo=sudo, idempotent=idempotent, check=check, timeout_s=timeout,
        cwd=cwd, env=env, unpinned=unpinned, upstream=upstream, halt_after=halt_after, description=description,
    )


def _manual(title: str, text: str) -> PlaybookStep:
    return PlaybookStep(title=title, manual=text)


def _docker_group_step() -> PlaybookStep:
    """Policy (b): check group membership; add with usermod; halt for the re-login. Never ``newgrp``."""
    return _step(
        "Join the docker group",
        "usermod", "-aG", "docker", USER_TOKEN,
        sudo=True,
        check=DOCKER_GROUP_CHECK,
        halt_after=RELOGIN_NOTE,
        upstream="sudo usermod -aG docker $USER && newgrp docker",
        description="Docker commands work without sudo once you are in the docker group (its members are effectively root).",
    )


def _download_then_run(
    title: str,
    url: str,
    filename: str,
    *,
    shell: str = "bash",
    sudo: bool = False,
    check: tuple[str, ...] | None,
    timeout: float = INSTALL_TIMEOUT_S,
    description: str = "",
) -> tuple[PlaybookStep, PlaybookStep]:
    """Policy (a): an upstream ``curl URL | shell`` becomes download + run of the saved file."""
    target = f"{PLAYBOOK_DIR_TOKEN}/{filename}"
    upstream = f"curl -fsSL {url} | {shell}"
    return (
        _step(
            f"Download the installer ({title})",
            "curl", "-fsSL", url, "-o", target,
            check=check, unpinned=True, upstream=upstream,
            description=f"Saves the installer under NVH_HOME so it can be read before it runs (upstream pipes it straight into {shell}).",
        ),
        _step(
            f"Run the installer ({title})",
            shell, target,
            sudo=sudo, check=check, timeout=timeout, unpinned=True, upstream=upstream, description=description,
        ),
    )


# ────────────────────────────────────────────────────────────────────────────
# The catalogue — first tier, ids = upstream folder names
# ────────────────────────────────────────────────────────────────────────────

_OLLAMA_INSTALL = _download_then_run(
    "Ollama", "https://ollama.com/install.sh", "ollama-install.sh", shell="sh", sudo=True,
    check=_cmd_exists("ollama"),
    description="The script writes /usr/local/bin/ollama, creates the `ollama` system user and installs the systemd unit, so it runs with sudo.",
)

_OLLAMA_UNDO = (
    "sudo systemctl stop ollama",
    "sudo systemctl disable ollama",
    "sudo rm /usr/local/bin/ollama",
    "sudo rm -rf /usr/share/ollama  # removes every downloaded model",
    "sudo userdel ollama",
)

_VSCODE_DEB_URL = "https://code.visualstudio.com/sha/download?build=stable&os=linux-deb-arm64"
_VSCODE_INSTALLED: tuple[str, ...] = ("bash", "-c", "dpkg -s code | grep -q 'Status: install ok installed'")
#: Verbatim upstream (README steps 1-3): an unpinned "latest stable" .deb installed as root, no checksum.
_VSCODE_UPSTREAM = f"wget '{_VSCODE_DEB_URL}' -O vscode-arm64.deb && sudo dpkg -i vscode-arm64.deb && sudo apt-get install -f"

PLAYBOOKS: tuple[Playbook, ...] = (
    Playbook(
        id="ollama",
        title="Ollama",
        category="runtime",
        summary="Install and use Ollama",
        source_urls=(_src("ollama"),),
        prerequisites=(
            "DGX Spark device set up and connected to your network",
            "NVIDIA Sync installed and connected to your Spark",
            "Terminal access to your local machine for testing API calls",
        ),
        steps=(
            *_OLLAMA_INSTALL,
            _step(
                "Enable the ollama service at boot",
                "systemctl", "enable", "--now", "ollama",
                sudo=True, check=("systemctl", "is-enabled", "ollama"),
            ),
            _step(
                "Download and verify a language model (qwen2.5:32b, about 18 GB)",
                "ollama", "pull", "qwen2.5:32b",
                check=("ollama", "show", "qwen2.5:32b"), timeout=LONG_TIMEOUT_S,
            ),
            _manual(
                "Configure the Ollama custom app in NVIDIA Sync (laptop)",
                "In NVIDIA Sync on your laptop: tray icon → gear → Custom → Add New; Name `Ollama Server`, "
                "Port `11434`, leave Start Script empty → Add, then click `Ollama Server` under Custom to start the SSH tunnel.",
            ),
            _manual(
                "Validate API connectivity from the laptop",
                "curl http://localhost:11434/api/chat -d '{\"model\": \"qwen2.5:32b\", \"messages\": "
                "[{\"role\": \"user\", \"content\": \"Write me a haiku about GPUs and AI.\"}], \"stream\": false}'",
            ),
        ),
        verify=(
            ("ollama", "--version"),
            ("systemctl", "is-active", "ollama"),
            ("curl", "-sf", "http://localhost:11434/api/tags"),
        ),
        undo=_OLLAMA_UNDO,
        notes=(
            "The README says 'no system-level changes', but install.sh installs a system service and a user; the undo above is the README's own Step 9.",
            "Rootless instead: the `rootless-ollama` studio pack installs Ollama under NVH_HOME with no sudo.",
        ),
        estimated_minutes=15,
        estimated_disk_gb=20.0,
        rootless_alternative="rootless-ollama",
        risk="Low (upstream); nvHive: installs a system service and a system user",
        last_updated="10/12/2025",
    ),
    Playbook(
        id="cli-coding-agent",
        title="CLI Coding Agent",
        category="dev",
        summary="Build local CLI coding agents with Ollama",
        source_urls=(_src("cli-coding-agent"),),
        prerequisites=(
            "DGX Spark access with NVIDIA DGX OS 7.3.1 (Ubuntu 24.04.3 LTS base)",
            "Internet access to download model weights",
            "Ollama v0.15 or newer (required for `ollama launch`)",
            "GPU memory for the Qwen3.6 variant you choose: qwen3.6:latest ~24 GB, 35b-a3b-nvfp4 ~22 GB, q8_0 ~39 GB, bf16 ~71 GB",
        ),
        steps=(
            *_OLLAMA_INSTALL,
            _step(
                "Pull Qwen3.6 (about 24 GB)",
                "ollama", "pull", "qwen3.6",
                check=("ollama", "show", "qwen3.6"), timeout=LONG_TIMEOUT_S,
            ),
            *_download_then_run(
                "Claude Code", "https://claude.ai/install.sh", "claude-install.sh",
                check=_cmd_exists("claude"),
                description="User-space install (no sudo).",
            ),
            _manual(
                "Test local inference (optional)",
                "ollama run qwen3.6   # interactive; type /bye or press Ctrl+D to exit",
            ),
            _manual(
                "Launch the agent",
                "ollama launch claude --model qwen3.6   # OpenCode: `ollama launch opencode --model qwen3.6`; "
                "Codex: `npm install -g @openai/codex` (use a user npm prefix) then `ollama launch codex --model qwen3.6`",
            ),
        ),
        verify=(
            ("ollama", "--version"),
            ("ollama", "show", "qwen3.6"),
            _cmd_exists("claude"),
        ),
        undo=(
            "sudo systemctl stop ollama",
            "ollama rm qwen3.6   # deletes the model files",
        ),
        notes=(
            "`ollama launch` needs Ollama 0.15+; re-run the installer if it reports an unknown command.",
            "Rootless instead: the `rootless-ollama` studio pack (verify `ollama launch` is available in that build).",
        ),
        estimated_minutes=25,
        estimated_disk_gb=24.0,
        rootless_alternative="rootless-ollama",
        risk="Low",
    ),
    Playbook(
        id="open-webui",
        title="Open WebUI with Ollama",
        category="chat-ui",
        summary="Install Open WebUI and use Ollama to chat with models on your Spark",
        source_urls=(_src("open-webui"),),
        prerequisites=(
            "DGX Spark device is set up and accessible",
            "Enough disk space for the container image and models (7 GB image; 25 GB qwen3.6:latest or 15 GB gpt-oss:latest)",
            "Docker (DGX OS ships it) — the first step puts you in the docker group",
        ),
        steps=(
            _docker_group_step(),
            _step(
                "Download the Open WebUI container image (about 7 GB)",
                "docker", "pull", "ghcr.io/open-webui/open-webui:ollama",
                check=("docker", "image", "inspect", "ghcr.io/open-webui/open-webui:ollama"), timeout=LONG_TIMEOUT_S,
            ),
            _step(
                "Create the Open WebUI container",
                "docker", "run", "-d", "-p", "8080:8080", "--gpus=all",
                "-v", "open-webui:/app/backend/data", "-v", "open-webui-ollama:/root/.ollama",
                "--name", "open-webui", "ghcr.io/open-webui/open-webui:ollama",
                check=("docker", "container", "inspect", "open-webui"), idempotent=False, timeout=INSTALL_TIMEOUT_S,
                description="Fails if a container named open-webui already exists, hence the check; the next step starts an existing one.",
            ),
            _step(
                "Start the Open WebUI container",
                "docker", "start", "open-webui",
                check=_docker_running("open-webui"),
            ),
            _manual("Create the administrator account", "Open http://localhost:8080 → Get Started → fill in the admin form."),
            _manual(
                "Download and configure a model",
                "In Open WebUI: Select a model → type `gpt-oss:20b` → 'Pull gpt-oss:20b from Ollama.com'.",
            ),
            _manual("Test the model", "Prompt: 'Write me a haiku about GPUs'."),
        ),
        verify=(
            _docker_running("open-webui"),
            ("curl", "-sf", "-o", "/dev/null", "http://localhost:8080"),
        ),
        undo=(
            "docker stop open-webui",
            "docker rm open-webui",
            "docker rmi ghcr.io/open-webui/open-webui:ollama",
            "docker volume rm open-webui open-webui-ollama   # deletes every chat and model",
            f"sudo gpasswd -d {USER_TOKEN} docker",
        ),
        notes=(
            "The :ollama image bundles a second Ollama with its own model store (volume open-webui-ollama), separate from any host or nvHive Ollama.",
            "The upstream remote path publishes 12000 behind an NVIDIA Sync tunnel instead; for a local-only bind swap "
            "`-p 8080:8080` for `-p 127.0.0.1:8080:8080` when you create the container.",
        ),
        warning=(
            "Publishes port 8080 on every interface (LAN), unauthenticated until an administrator exists: the first "
            "visitor to complete the signup form becomes the admin, who can run Python inside the --gpus=all container. "
            "Create the administrator account as soon as the run finishes, or bind the port to 127.0.0.1."
        ),
        estimated_minutes=20,
        estimated_disk_gb=32.0,
        risk="Low (upstream); nvHive: LAN-reachable admin signup until you claim it",
    ),
    Playbook(
        id="comfy-ui",
        title="Comfy UI",
        category="image",
        summary="Install and use Comfy UI to generate images",
        source_urls=(_src("comfy-ui"),),
        prerequisites=(
            "NVIDIA Grace Blackwell GB10 Superchip System; at least 20 GB free storage",
            "python3, pip3 and git installed; network access to Hugging Face",
            "A browser that can reach <SPARK_IP>:8188",
        ),
        steps=(
            _step(
                "Verify system prerequisites",
                "bash", "-c", "python3 --version && pip3 --version && git --version",
            ),
            _step(
                "Download the upstream setup script",
                "curl", "-fsSL", f"{UPSTREAM_RAW}comfy-ui/assets/setup.sh", "-o", f"{PLAYBOOK_DIR_TOKEN}/setup.sh",
                check=("test", "-f", f"{PLAYBOOK_DIR_TOKEN}/ComfyUI/models/checkpoints/DreamShaper_8_pruned.safetensors"),
                upstream=f"curl -fsSL {UPSTREAM_RAW}comfy-ui/assets/setup.sh | bash",
                description="Downloaded, checksummed and then run — the README publishes the sha256, so the pipe is not needed.",
            ),
            _step(
                "Verify setup.sh against the README's sha256",
                *_sha256_check("97b03fb341b40bd8524549b234883427dda2e8bca4ceb1662a074dcc9a7cf3f8", f"{PLAYBOOK_DIR_TOKEN}/setup.sh"),
                check=("test", "-f", f"{PLAYBOOK_DIR_TOKEN}/ComfyUI/models/checkpoints/DreamShaper_8_pruned.safetensors"),
            ),
            _step(
                "Run setup.sh: venv, PyTorch cu130, ComfyUI v0.28.2, DreamShaper 8 checkpoint (about 2 GB)",
                "bash", "setup.sh",
                cwd=PLAYBOOK_DIR_TOKEN,
                check=("test", "-f", f"{PLAYBOOK_DIR_TOKEN}/ComfyUI/models/checkpoints/DreamShaper_8_pruned.safetensors"),
                timeout=LONG_TIMEOUT_S,
            ),
            _step(
                "Download the upstream launch script",
                "curl", "-fsSL", f"{UPSTREAM_RAW}comfy-ui/assets/launch.sh", "-o", f"{PLAYBOOK_DIR_TOKEN}/launch.sh",
                check=("test", "-f", f"{PLAYBOOK_DIR_TOKEN}/launch.sh"),
            ),
            _step(
                "Verify launch.sh against the README's sha256",
                *_sha256_check("7dc75b155a198a49537832c4a363d321080b130be0a6945a0bc0afe78da8badc", f"{PLAYBOOK_DIR_TOKEN}/launch.sh"),
            ),
            _manual(
                "Launch the ComfyUI server (foreground)",
                f"cd {PLAYBOOK_DIR_TOKEN} && bash launch.sh   # listens on 0.0.0.0:8188 (LAN). Local only: "
                "`source comfyui-env/bin/activate && cd ComfyUI && python main.py --listen 127.0.0.1`",
            ),
            _manual(
                "Run a template flow",
                "Open http://localhost:8188 → Templates → Getting Started → '1.1 Starter-Text to Image' → Run (about 30 s).",
            ),
        ),
        verify=(
            ("test", "-x", f"{PLAYBOOK_DIR_TOKEN}/comfyui-env/bin/python"),
            ("test", "-d", f"{PLAYBOOK_DIR_TOKEN}/ComfyUI/.git"),
            (f"{PLAYBOOK_DIR_TOKEN}/comfyui-env/bin/python", "-c", "import torch; assert torch.cuda.is_available()"),
            ("curl", "-sI", "http://localhost:8188"),
        ),
        undo=(
            f"rm -rf {PLAYBOOK_DIR_TOKEN}   # comfyui-env, ComfyUI and the checkpoint (all under NVH_HOME)",
        ),
        notes=(
            "Everything lives under NVH_HOME/playbooks/comfy-ui; no sudo anywhere.",
            "nvHive's own rootless ComfyUI (`nvh workstation --with-comfyui`) installs under NVH_HOME and listens on localhost; the `comfyui-power-nodes` pack adds node packs to it.",
        ),
        estimated_minutes=40,
        estimated_disk_gb=20.0,
        risk="Medium (upstream)",
    ),
    Playbook(
        id="dgx-dashboard",
        title="DGX Dashboard",
        category="system",
        summary="Monitor your DGX system and launch JupyterLab",
        source_urls=(_src("dgx-dashboard"),),
        prerequisites=(
            "NVIDIA Grace Blackwell GB10 Superchip System running NVIDIA DGX OS",
            "NVIDIA Sync installed (remote access) or an SSH client",
        ),
        steps=(
            _manual(
                "Access the DGX Dashboard",
                "Open http://localhost:11000 (desktop shortcut). Remotely: NVIDIA Sync, or "
                "`ssh -L 11000:localhost:11000 -L <ASSIGNED_PORT>:localhost:<ASSIGNED_PORT> <USERNAME>@<SPARK_DEVICE_IP>` "
                "with the port from /opt/nvidia/dgx-dashboard-service/jupyterlab_ports.yaml.",
            ),
            _manual("Log into the DGX Dashboard", "Use your system username and password."),
            _manual(
                "Launch a JupyterLab instance",
                "Start → Starting → Preparing → Running (creates ~/jupyterlab with its own venv; a new working directory is a new environment).",
            ),
            _manual("Monitor GPU utilization; stop JupyterLab", "Both from the dashboard."),
        ),
        verify=(
            ("curl", "-sf", "-o", "/dev/null", "http://localhost:11000"),
            ("cat", "/opt/nvidia/dgx-dashboard-service/jupyterlab_ports.yaml"),
        ),
        undo=(
            "Stop the JupyterLab instance in the dashboard",
            "rm -rf ~/jupyterlab   # your notebooks and the venv",
        ),
        notes=("Pre-installed on DGX OS: nothing to install. nvHive only reports the URL and the per-user JupyterLab port.",),
        warning=(
            "Settings → Updates → 'Update Now' upgrades packages and firmware and reboots the machine; nvHive never "
            "automates it and its only rollback is a system backup or recovery media."
        ),
        estimated_minutes=15,
        estimated_disk_gb=0.0,
        risk="Low (normal use)",
    ),
    Playbook(
        id="vscode",
        title="VS Code",
        category="dev",
        summary="Install and use VS Code locally or remotely",
        source_urls=(_src("vscode"),),
        prerequisites=(
            "A DGX Spark set up with an active internet connection",
            "sudo privileges to the Spark",
            "At least 200 MB of disk space",
            "(Remote use only) VS Code on your laptop",
        ),
        steps=(
            _step(
                "Download the VS Code ARM64 installer",
                "wget", _VSCODE_DEB_URL, "-O", f"{PLAYBOOK_DIR_TOKEN}/vscode-arm64.deb",
                check=_VSCODE_INSTALLED, timeout=INSTALL_TIMEOUT_S,
                unpinned=True, upstream=_VSCODE_UPSTREAM,
                description="The `stable` channel's current build: no version pin and no checksum in the README.",
            ),
            _step(
                "Install VS Code (apt resolves the .deb's dependencies)",
                "apt-get", "install", "-y", f"{PLAYBOOK_DIR_TOKEN}/vscode-arm64.deb",
                sudo=True, check=_VSCODE_INSTALLED, timeout=INSTALL_TIMEOUT_S,
                unpinned=True, upstream=_VSCODE_UPSTREAM,
                description=(
                    "Upstream runs `dpkg -i` and then `apt-get install -f` to repair missing dependencies; the runner "
                    "stops at the first failing step, so that repair could never run — apt-get on the .deb does both "
                    "in one command (`-y`: nothing can answer a prompt, stdin is closed)."
                ),
            ),
            _manual(
                "Open VS Code",
                "code   # needs a desktop session. A project: `mkdir ~/spark-dev-workspace && cd ~/spark-dev-workspace && code .`",
            ),
            _manual("Remote use", "On your laptop, NVIDIA Sync → VS Code opens a Remote-SSH session; check `hostnamectl` in its terminal."),
        ),
        verify=(
            _dpkg("code"),
            ("code", "--version"),
        ),
        undo=(
            "sudo apt-get remove code",
            "rm -rf ~/.config/Code ~/.vscode   # settings and extensions",
        ),
        notes=(
            "Microsoft publishes the current stable build's sha256 at https://code.visualstudio.com/sha?build=stable "
            "(platform linux-deb-arm64) if you want to check the downloaded .deb by hand before approving the install.",
        ),
        estimated_minutes=5,
        estimated_disk_gb=0.2,
        risk="Low (upstream); nvHive: installs an unpinned, unverified vendor .deb as root",
    ),
    Playbook(
        id="tailscale",
        title="Set up Tailscale on Your Spark",
        category="network",
        summary="Use Tailscale to connect to your Spark on your home network no matter where you are",
        source_urls=(_src("tailscale"),),
        prerequisites=(
            "NVIDIA DGX OS with a working package manager (`sudo apt update`) and a user with sudo privileges",
            "Internet connectivity; a valid account for Tailscale authentication (Google, GitHub, Microsoft)",
            "A client device (Mac, Windows or Linux) for remote access",
        ),
        steps=(
            _step("Update the package list", "apt", "update", sudo=True, check=_dpkg("tailscale"), timeout=INSTALL_TIMEOUT_S),
            _step(
                "Install curl and gnupg",
                "apt", "install", "-y", "curl", "gnupg",
                sudo=True, check=_dpkg("curl", "gnupg"), timeout=INSTALL_TIMEOUT_S,
            ),
            _step(
                "Download the Tailscale signing key",
                "curl", "-fsSL", "https://pkgs.tailscale.com/stable/ubuntu/noble.noarmor.gpg",
                "-o", f"{PLAYBOOK_DIR_TOKEN}/tailscale-archive-keyring.gpg",
                check=("test", "-f", "/usr/share/keyrings/tailscale-archive-keyring.gpg"),
                upstream="curl -fsSL https://pkgs.tailscale.com/stable/ubuntu/noble.noarmor.gpg | sudo tee /usr/share/keyrings/tailscale-archive-keyring.gpg",
            ),
            _step(
                "Install the signing key",
                "install", "-m", "0644", f"{PLAYBOOK_DIR_TOKEN}/tailscale-archive-keyring.gpg",
                "/usr/share/keyrings/tailscale-archive-keyring.gpg",
                sudo=True, check=("test", "-f", "/usr/share/keyrings/tailscale-archive-keyring.gpg"),
            ),
            _step(
                "Download the Tailscale repository list",
                "curl", "-fsSL", "https://pkgs.tailscale.com/stable/ubuntu/noble.tailscale-keyring.list",
                "-o", f"{PLAYBOOK_DIR_TOKEN}/tailscale.list",
                check=("test", "-f", "/etc/apt/sources.list.d/tailscale.list"),
                upstream="curl -fsSL https://pkgs.tailscale.com/stable/ubuntu/noble.tailscale-keyring.list | sudo tee /etc/apt/sources.list.d/tailscale.list",
            ),
            _step(
                "Install the repository list",
                "install", "-m", "0644", f"{PLAYBOOK_DIR_TOKEN}/tailscale.list", "/etc/apt/sources.list.d/tailscale.list",
                sudo=True, check=("test", "-f", "/etc/apt/sources.list.d/tailscale.list"),
            ),
            _step(
                "Update the package list with the new repository",
                "apt", "update", sudo=True, check=_dpkg("tailscale"), timeout=INSTALL_TIMEOUT_S,
            ),
            _step(
                "Install Tailscale",
                "apt", "install", "-y", "tailscale",
                sudo=True, check=_dpkg("tailscale"), timeout=INSTALL_TIMEOUT_S,
            ),
            _step(
                "Install the OpenSSH server (if needed)",
                "apt", "install", "-y", "openssh-server",
                sudo=True, check=("systemctl", "is-active", "ssh"), timeout=INSTALL_TIMEOUT_S,
            ),
            _step(
                "Enable and start SSH",
                "systemctl", "enable", "ssh", "--now", "--no-pager",
                sudo=True, check=("systemctl", "is-active", "ssh"),
            ),
            _manual(
                "Connect your Spark to the tailnet",
                "sudo tailscale up   # in a terminal; open the URL it prints and log in (Google, GitHub or Microsoft)",
            ),
            _manual(
                "Install Tailscale on your client devices and log in to the same tailnet",
                "Then `tailscale status` and `tailscale ping <SPARK_HOSTNAME>` from the client.",
            ),
            _manual(
                "Configure SSH authentication",
                "Client: `ssh-keygen -t ed25519 -f ~/.ssh/tailscale_spark`; Spark: append the public key to ~/.ssh/authorized_keys "
                "(`chmod 600 ~/.ssh/authorized_keys; chmod 700 ~/.ssh`); test `ssh -i ~/.ssh/tailscale_spark <USERNAME>@<SPARK_HOSTNAME>`.",
            ),
        ),
        verify=(
            ("tailscale", "version"),
            ("systemctl", "is-active", "tailscaled"),
            ("systemctl", "is-active", "ssh"),
            ("tailscale", "status"),
        ),
        undo=(
            "sudo tailscale down   # removes this device from the tailnet",
            "sudo apt remove --purge tailscale",
            "sudo rm /etc/apt/sources.list.d/tailscale.list",
            "sudo rm /usr/share/keyrings/tailscale-archive-keyring.gpg",
            "sudo apt update",
            "sudo systemctl disable ssh --now   # only if this playbook enabled SSH (its step 10 ran)",
            "sudo apt remove openssh-server   # only if this playbook installed it (its step 9 ran)",
        ),
        notes=(
            "The key and the repository list are downloaded to NVH_HOME first and installed with `install -m 0644` (upstream pipes them through `sudo tee`).",
            "`noble` is Ubuntu 24.04 — the DGX OS base.",
        ),
        warning=(
            "Adds the tailscaled service and a tailscale0 interface (100.x address). `tailscale up --accept-dns` / MagicDNS "
            "can change system DNS resolution. When sshd is not already active, steps 9-10 install openssh-server and "
            "enable it at boot on every interface (LAN, not just the tailnet) with DGX OS's default password "
            "authentication; the undo lists how to turn that back off."
        ),
        estimated_minutes=25,
        estimated_disk_gb=0.1,
        risk="Medium (upstream: potential SSH service configuration conflicts)",
    ),
    Playbook(
        id="vllm",
        title="vLLM for Inference",
        category="inference",
        summary="Install and use vLLM on DGX Spark",
        source_urls=(_src("vllm"),),
        prerequisites=(
            "DGX Spark with CUDA 13.0 toolkit (`nvcc --version`), Docker and the NVIDIA Container Toolkit",
            "Python 3.12 and git available; network access for packages and container images",
            "HF_TOKEN in your shell for gated Hugging Face models (declared only — nvHive never asks for or stores it)",
        ),
        steps=(
            _docker_group_step(),
            _step(
                "Pull the vLLM container image (NGC, pinned to the README's example tag)",
                "docker", "pull", "nvcr.io/nvidia/vllm:26.05.post1-py3",
                check=("docker", "image", "inspect", "nvcr.io/nvidia/vllm:26.05.post1-py3"), timeout=LONG_TIMEOUT_S,
            ),
            _step(
                "Create the vLLM server container (serves openai/gpt-oss-20b on port 8000)",
                "docker", "run", "-d", "--gpus", "all", "-p", "8000:8000", "--name", "vllm-server",
                "nvcr.io/nvidia/vllm:26.05.post1-py3", "vllm", "serve", "openai/gpt-oss-20b",
                check=("docker", "container", "inspect", "vllm-server"), idempotent=False, timeout=INSTALL_TIMEOUT_S,
                description="The README's detached variant with its example model; the model downloads inside the container on first start.",
            ),
            _step("Start the vLLM server container", "docker", "start", "vllm-server", check=_docker_running("vllm-server")),
            _manual(
                "Wait for the model download and server start",
                "docker logs -f vllm-server   # until 'Application startup complete'; then `curl http://localhost:8000/health`",
            ),
            _manual(
                "Serve a different model",
                "docker rm -f vllm-server, then re-run the docker run line with `vllm serve <HF_MODEL_HANDLE>`; gated models need "
                "`-e HF_TOKEN=\"$HF_TOKEN\"`. The agent-ready Qwen3.6 35B recipe (OpenClaw / NemoClaw) is the README's second tab: "
                "`docker run -it --gpus all -p 8000:8000 -e HF_TOKEN=\"$HF_TOKEN\" -v ~/.cache/huggingface:/root/.cache/huggingface "
                "vllm/vllm-openai:<tag> nvidia/Qwen3.6-35B-A3B-NVFP4 --host 0.0.0.0 --port 8000 …`.",
            ),
        ),
        verify=(
            _docker_running("vllm-server"),
            ("curl", "-sf", "http://localhost:8000/health"),
            ("curl", "-sf", "http://localhost:8000/v1/models"),
        ),
        undo=(
            "docker stop vllm-server",
            "docker rm vllm-server",
            "docker rmi nvcr.io/nvidia/vllm:26.05.post1-py3",
            f"sudo gpasswd -d {USER_TOKEN} docker",
        ),
        notes=(
            "Image pinned to 26.05.post1-py3, the README's own example (upstream leaves ${LATEST_VLLM_VERSION} for you to fill in).",
            "The upstream command mounts no Hugging Face cache: the model is downloaded again whenever the container is recreated.",
            "For a local-only bind swap `-p 8000:8000` for `-p 127.0.0.1:8000:8000` when you create the container.",
        ),
        warning=(
            "Publishes port 8000 on every interface (LAN) with no API key: anyone on the network can use the GPU "
            "through http://<spark>:8000/v1 while the container runs. Bind to 127.0.0.1 or firewall the port if the "
            "Spark shares a network."
        ),
        estimated_minutes=30,
        estimated_disk_gb=30.0,
        risk="Low (upstream); nvHive: LAN-reachable, unauthenticated API",
    ),
    Playbook(
        id="llama-cpp",
        title="Run models with llama.cpp on DGX Spark",
        category="inference",
        summary="Build llama.cpp with CUDA and serve models via an OpenAI-compatible API",
        source_urls=(_src("llama-cpp"),),
        prerequisites=(
            "NVIDIA DGX Spark with GB10 GPU; about 30 GB free unified memory for the example model",
            "At least ~40 GB free disk for the example download plus build artifacts",
            "Git, CMake 3.14+ and the CUDA Toolkit (`nvcc --version`); network access to GitHub and Hugging Face",
        ),
        steps=(
            _step(
                "Update the package list",
                "apt", "update", sudo=True,
                check=_dpkg("git", "clang", "cmake", "libcurl4-openssl-dev", "libssl-dev"), timeout=INSTALL_TIMEOUT_S,
            ),
            _step(
                "Install the build dependencies",
                "apt", "install", "-y", "git", "clang", "cmake", "libcurl4-openssl-dev", "libssl-dev",
                sudo=True, check=_dpkg("git", "clang", "cmake", "libcurl4-openssl-dev", "libssl-dev"), timeout=INSTALL_TIMEOUT_S,
            ),
            _step(
                "Clone the llama.cpp repository (under NVH_HOME)",
                "git", "clone", "https://github.com/ggml-org/llama.cpp", f"{PLAYBOOK_DIR_TOKEN}/llama.cpp",
                check=("test", "-d", f"{PLAYBOOK_DIR_TOKEN}/llama.cpp/.git"), idempotent=False, timeout=INSTALL_TIMEOUT_S,
            ),
            _step(
                "Configure the CUDA build (GB10 = CUDA arch 121a)",
                "cmake", "-B", "build", "-DGGML_NATIVE=ON", "-DGGML_CUDA=ON", "-DGGML_CURL=ON", "-DGGML_RPC=ON",
                "-DCMAKE_CUDA_ARCHITECTURES=121a-real",
                cwd=f"{PLAYBOOK_DIR_TOKEN}/llama.cpp",
                check=("test", "-x", f"{PLAYBOOK_DIR_TOKEN}/llama.cpp/build/bin/llama-server"), timeout=INSTALL_TIMEOUT_S,
            ),
            _step(
                "Build llama-server (5-10 minutes)",
                "cmake", "--build", "build", "--config", "Release", "--target", "llama-server", "-j",
                cwd=f"{PLAYBOOK_DIR_TOKEN}/llama.cpp",
                check=("test", "-x", f"{PLAYBOOK_DIR_TOKEN}/llama.cpp/build/bin/llama-server"), timeout=LONG_TIMEOUT_S,
            ),
            _manual(
                "Start llama-server with a model (foreground; about 35 GB download on first run)",
                f"cd {PLAYBOOK_DIR_TOKEN}/llama.cpp/build && ./bin/llama-server -hf unsloth/Qwen3.6-35B-A3B-MTP-GGUF:UD-Q4_K_XL "
                "--host 0.0.0.0 --port 30000   # `--host 127.0.0.1` keeps it local",
            ),
            _manual(
                "Test the API",
                "curl -X POST http://127.0.0.1:30000/v1/chat/completions -H 'Content-Type: application/json' -d "
                "'{\"model\": \"unsloth/Qwen3.6-35B-A3B-MTP-GGUF:UD-Q4_K_XL\", \"messages\": [{\"role\": \"user\", "
                "\"content\": \"New York is a great city because...\"}], \"max_tokens\": 100}'",
            ),
        ),
        verify=(
            ("test", "-x", f"{PLAYBOOK_DIR_TOKEN}/llama.cpp/build/bin/llama-server"),
            ("curl", "-sf", "http://127.0.0.1:30000/health"),
        ),
        undo=(
            f"rm -rf {PLAYBOOK_DIR_TOKEN}/llama.cpp",
            "rm -rf ~/.cache/huggingface/hub/models--unsloth--Qwen3.6-35B-A3B-MTP-GGUF",
        ),
        notes=(
            "The clone lives under NVH_HOME/playbooks/llama-cpp instead of ~/llama.cpp.",
            "If cmake cannot find CUDA: `export PATH=/usr/local/cuda/bin:$PATH`, delete build/ and run the two cmake steps again.",
        ),
        estimated_minutes=30,
        estimated_disk_gb=40.0,
        risk="Low",
    ),
    Playbook(
        id="lm-studio",
        title="LM Studio on DGX Spark",
        category="inference",
        summary="Deploy LM Studio and serve LLMs on a Spark device; use LM Link to access models remotely.",
        source_urls=(_src("lm-studio"),),
        prerequisites=(
            "DGX Spark (ARM64, Blackwell); 65 GB memory and 65 GB storage minimum for the example model (70 GB recommended)",
            "A client device on the same local network; network access to download packages and models",
        ),
        steps=(
            *_download_then_run(
                "LM Studio llmster", "https://lmstudio.ai/install.sh", "lmstudio-install.sh",
                # $1 is the home directory, passed as a positional parameter — never spliced into the script.
                check=("bash", "-c", 'command -v lms >/dev/null || test -x "$1/.lmstudio/bin/lms"', "lms-check", HOME_TOKEN),
                description="User-space install into ~/.lmstudio (no sudo).",
            ),
            _manual("Add lms to your PATH", "Follow the installer's instructions (usually `source ~/.bashrc`)."),
            _manual(
                "Start the LM Studio API server",
                "lms server start --port 1234   # upstream binds the LAN with `--bind 0.0.0.0`; add it only if you need remote access",
            ),
            _manual(
                "Download and load a model (about 65 GB)",
                "lms get nvidia/nemotron-3-nano-omni && lms load nvidia/nemotron-3-nano-omni   # `lms ls` lists what you have",
            ),
            _manual("(Optional) Connect with LM Link", "Sign in at https://lmstudio.ai/link on the Spark and on your laptop."),
        ),
        verify=(
            ("test", "-d", f"{HOME_TOKEN}/.lmstudio"),
            ("curl", "-sf", "http://localhost:1234/api/v1/models"),
        ),
        undo=(
            "rm -rf ~/.lmstudio/llmster",
            "rm -rf ~/.lmstudio/models   # downloaded models",
        ),
        notes=("Fully user-space; the only automated step is the installer.",),
        estimated_minutes=20,
        estimated_disk_gb=65.0,
        risk="Low",
    ),
    Playbook(
        id="openclaw",
        title="OpenClaw",
        category="agent",
        summary="Run OpenClaw locally on DGX Spark with a vLLM-served local model",
        source_urls=(_src("openclaw"),),
        prerequisites=(
            "DGX Spark running Linux, connected to your network; terminal (SSH or local) access",
            "Enough GPU memory for the model (the README's recipe: nvidia/Qwen3.6-35B-A3B-NVFP4 via vLLM)",
            "HF_TOKEN in your shell for the model download (declared only — nvHive never asks for or stores it)",
        ),
        steps=(
            _docker_group_step(),
            _manual(
                "Install OpenClaw (interactive)",
                "curl -fsSL https://openclaw.ai/install.sh | bash   # in a terminal: the installer shows a security warning you answer with the arrow keys → Yes",
            ),
            _manual(
                "Complete the OpenClaw onboarding (TUI)",
                "Quickstart; Model provider → Skip for now; All Providers → Keep Current; channel → Skip for Now; Skills → No; "
                "Homebrew → No; Hooks → all three; save the dashboard URL and token; Finish → Yes.",
            ),
            _manual(
                "Serve the model with vLLM (separate terminal)",
                "The vllm playbook's agent-ready Qwen3.6 recipe: `docker run -it --gpus all -p 8000:8000 -e HF_TOKEN=\"$HF_TOKEN\" "
                "-v ~/.cache/huggingface:/root/.cache/huggingface vllm/vllm-openai:<tag> nvidia/Qwen3.6-35B-A3B-NVFP4 --host 0.0.0.0 "
                "--port 8000 …`; wait for 'Application startup complete', then `curl http://localhost:8000/v1/models`.",
            ),
            _manual(
                "Point OpenClaw at the vLLM server",
                "Edit ~/.openclaw/openclaw.json: add `models.providers.vllm` with baseUrl http://localhost:8000/v1, apiKey `vllm`, "
                "api `openai-responses`, and the model id/name nvidia/Qwen3.6-35B-A3B-NVFP4 (reasoning true, contextWindow 262144, "
                "maxTokens 8192); then restart the OpenClaw gateway.",
            ),
            _manual("Verify the setup", "Open the dashboard URL with its token; new conversation; `/model nvidia/Qwen3.6-35B-A3B-NVFP4`."),
        ),
        verify=(
            _cmd_exists("openclaw"),
            ("test", "-f", f"{HOME_TOKEN}/.openclaw/openclaw.json"),
            ("curl", "-sf", "http://localhost:8000/v1/models"),
        ),
        undo=(
            "Stop the OpenClaw gateway and remove ~/.openclaw (the README gives no command)",
            "docker rm $(docker ps -aq --filter ancestor=vllm/vllm-openai:latest)",
            "docker rmi vllm/vllm-openai:latest",
            f"sudo gpasswd -d {USER_TOKEN} docker",
        ),
        notes=(
            "Guided: the installer and onboarding are interactive TUIs, so nvHive automates only the docker-group step.",
            "Rootless instead: the `openclaw-agent` studio pack installs OpenClaw under NVH_HOME via npm (config path differs from ~/.openclaw).",
        ),
        warning=(
            "Upstream rates the risk Medium to High: run OpenClaw on a dedicated, isolated system with a dedicated account, install "
            "only trusted skills, never expose the web UI or messaging channels publicly without strong authentication (SSH tunnel "
            "or VPN), limit its internet access with firewall rules and monitor its logs."
        ),
        estimated_minutes=30,
        estimated_disk_gb=30.0,
        rootless_alternative="openclaw-agent",
        risk="Medium to High",
    ),
    Playbook(
        id="nemoclaw",
        title="Run NemoClaw with a Local LLM",
        category="agent",
        summary="Build your first local AI assistant on DGX Spark using NemoClaw and vLLM in a secure sandbox, with optional Telegram.",
        source_urls=(_src("nemoclaw"),),
        prerequisites=(
            "A DGX Spark (GB10) with keyboard and monitor, or SSH access",
            "Fresh install of DGX OS with the latest updates; Docker 28.x+ (`docker info --format '{{.ServerVersion}}'`)",
            "Optional: a Telegram bot token (@BotFather) and a Brave Search API key (declared only — nvHive never asks for or stores them)",
        ),
        steps=(
            _docker_group_step(),
            _manual(
                "Install NemoClaw (interactive; sudo prompts inside)",
                "curl -fsSL https://www.nvidia.com/nemoclaw.sh | bash   # in a terminal: accept the third-party notice; "
                "`Run express install with these settings? [Y/n]` → Y (managed local vLLM, qwen3.6-35b-a3b-nvfp4, sandbox my-assistant)",
            ),
            _manual(
                "Custom onboarding (only if you declined Express Install)",
                "nemoclaw onboard --gpu --name <name>   # agent 1 (OpenClaw), inference, model, sandbox name, policy Balanced",
            ),
            _manual(
                "Open the dashboard",
                "nemoclaw my-assistant dashboard-url --quiet; from a laptop `ssh -L <port>:127.0.0.1:<port> <user>@<spark-ip>` "
                "and browse http://127.0.0.1:<port>/… (127.0.0.1, not localhost — origin check).",
            ),
            _manual("Terminal UI", "nemoclaw my-assistant connect, then `openclaw tui`; `exit` to leave."),
            _manual(
                "Optional: Brave Search and Telegram",
                "BRAVE_API_KEY=<key> nemoclaw onboard --name <sandbox> --recreate-sandbox --non-interactive; "
                "nemoclaw <sandbox> channels add telegram; nemoclaw <sandbox> policy-add telegram",
            ),
        ),
        verify=(
            _cmd_exists("nemoclaw"),
            ("docker", "info", "--format", "{{.ServerVersion}}"),
            ("curl", "-sf", "http://127.0.0.1:8000/v1/models"),
        ),
        undo=(
            "nemoclaw tunnel stop",
            "nemoclaw uninstall --yes   # add --delete-models to drop the model; removes the Docker containers and volumes",
            f"sudo gpasswd -d {USER_TOKEN} docker",
        ),
        notes=(
            "Guided: the installer is an interactive Express Install that makes its own sudo host changes (Docker settings, "
            "Node.js 22.16+) after prompting for your password in your terminal; nvHive automates only the docker-group step.",
            "'No GPU detected' during onboarding is expected on GB10 (unified memory reporting).",
            "Rootless instead: the `nemoclaw-sandbox` studio pack (needs Docker without sudo).",
        ),
        warning="Upstream wants a clean device with no personal data and calls NemoClaw a demo provided AS IS.",
        estimated_minutes=45,
        estimated_disk_gb=40.0,
        rootless_alternative="nemoclaw-sandbox",
        risk="Medium",
    ),
)

#: Upstream playbooks not shipped in this tier, with the reason (design brief §9).
DEFERRED: tuple[DeferredPlaybook, ...] = (
    DeferredPlaybook("sglang", "SGLang for Inference",
                     "unpinned lmsysorg/sglang:latest-cu130 image and `docker container prune -f` in the cleanup; pin first"),
    DeferredPlaybook("nim-llm", "NIM on Spark",
                     "needs an NGC_API_KEY `docker login nvcr.io` and `chmod -R a+w` on $HOME directories; key handling design pending"),
    DeferredPlaybook("nemotron", "Nemotron Model Family on DGX Spark",
                     "nightly cu130 vLLM image; how to obtain the Nano weights is not stated upstream"),
    DeferredPlaybook("unsloth", "Unsloth on DGX Spark",
                     "ephemeral `--rm` container with no volume (everything installed is lost on exit); thin README"),
    DeferredPlaybook("connect-two-sparks", "Connect Two Sparks",
                     "changes host networking (netplan / ip on the ConnectX-7 link) and ships a passphrase-less shared SSH private "
                     "key to every node; needs a warning and undo design"),
    DeferredPlaybook("connect-to-your-spark", "Set Up Local Network Access",
                     "laptop-side only (NVIDIA Sync / ssh); nothing runs on the Spark"),
)

_PLAYBOOKS_BY_ID: dict[str, Playbook] = {playbook.id: playbook for playbook in PLAYBOOKS}


def get_playbook(playbook_id: str) -> Playbook | None:
    if not isinstance(playbook_id, str):
        return None
    return _PLAYBOOKS_BY_ID.get(playbook_id.strip().lower())


def deferred() -> list[dict[str, Any]]:
    return [entry.to_dict() for entry in DEFERRED]


# ────────────────────────────────────────────────────────────────────────────
# Compiling a playbook into a system-settings Plan
# ────────────────────────────────────────────────────────────────────────────


def playbooks_root(home_dir: str | Path | None = None) -> Path:
    """``NVH_HOME/playbooks`` — downloads, clones and receipts' install paths live per playbook underneath."""
    from nvh.integrations.workspace.storage import nvh_home

    return nvh_home(home_dir)[0] / "playbooks"


def _user_home() -> str:
    return Path.home().as_posix()


def _needs_user(playbook: Playbook) -> bool:
    texts = [*playbook.undo]
    for step in playbook.steps:
        texts += [*step.argv, step.manual or "", step.halt_after]
        if step.check:
            texts += step.check
    return any(USER_TOKEN in text for text in texts)


def _context(playbook: Playbook, home_dir: str | Path | None = None) -> dict[str, str]:
    ctx = {
        # POSIX form: argv never sees a shell, and the Spark is Linux; on a
        # Windows dev box this keeps the rendered commands quoting-free.
        PLAYBOOK_DIR_TOKEN: (playbooks_root(home_dir) / playbook.id).as_posix(),
        HOME_TOKEN: _user_home(),
        USER_TOKEN: "",
    }
    if _needs_user(playbook):
        user = ss._current_user()
        if not ss.USERNAME_RE.match(user or ""):
            raise PlaybookError("could not determine a valid login name for the current user")
        ctx[USER_TOKEN] = user
    return ctx


def _render(text: str, ctx: Mapping[str, str]) -> str:
    for token, value in ctx.items():
        text = text.replace(token, value)
    return text


def _render_argv(argv: Sequence[str], ctx: Mapping[str, str]) -> tuple[str, ...]:
    return tuple(_render(str(word), ctx) for word in argv)


def _manual_lines(playbook: Playbook, ctx: Mapping[str, str]) -> list[str]:
    """The manual steps as ``title — text`` lines (plan notes, plan_dict, catalogue and the run events share them)."""
    return [f"{step.title} — {_render(step.manual or '', ctx)}" for step in playbook.manual_steps()]


def _unpinned_note(step: PlaybookStep, ctx: Mapping[str, str]) -> str:
    """The card's note for an ``unpinned`` step: pipe-to-shell (download-then-run) or an unverified vendor download."""
    where = ctx[PLAYBOOK_DIR_TOKEN]
    if "|" in step.upstream:
        return (
            f"{UNPINNED_NOTE} — upstream runs `{step.upstream}`; nvHive downloads the script to {where} and runs that file."
        )
    return (
        f"{UNPINNED_DOWNLOAD_NOTE} — upstream runs `{step.upstream}`; the README pins no version and publishes no "
        f"checksum, so nvHive installs whatever the vendor serves today (saved under {where} first)."
    )


def _compile_step(step: PlaybookStep, ctx: Mapping[str, str]) -> Step:
    argv = list(_render_argv(step.argv, ctx))
    if step.cwd or step.env:
        prefix = ["env"]
        if step.cwd:
            prefix += ["-C", _render(step.cwd, ctx)]
        for key, value in (step.env or {}).items():
            prefix.append(f"{key}={_render(str(value), ctx)}")
        argv = prefix + argv
    return Step(argv=tuple(argv), sudo=step.sudo, timeout=step.timeout_s)


def compile_plan(
    playbook: Playbook, *, home_dir: str | Path | None = None, ctx: Mapping[str, str] | None = None,
) -> Plan:
    """The :class:`Plan` a playbook runs — what the card shows is what the runner executes.

    Manual steps become notes prefixed ``MANUAL:``; ``unpinned`` steps add a
    ``pipe-to-shell: unpinned`` / ``unpinned download`` note quoting the
    upstream command; the docker-group step adds its re-login note. Raises
    :class:`PlaybookError` when the current login name cannot be determined
    and a step needs it. ``ctx`` (from :func:`_context`) may be passed to
    avoid rendering it twice.
    """
    ctx = ctx if ctx is not None else _context(playbook, home_dir)
    executable = playbook.executable_steps()
    steps = tuple(_compile_step(step, ctx) for step in executable)
    manual = _manual_lines(playbook, ctx)
    notes: list[str] = [_render(note, ctx) for note in playbook.notes]
    seen_upstream: set[str] = set()
    for step in executable:
        if step.unpinned and step.upstream and step.upstream not in seen_upstream:
            seen_upstream.add(step.upstream)
            notes.append(_unpinned_note(step, ctx))
        if step.halt_after:
            notes.append(f"After '{step.title}' runs, the run stops. MANUAL: {_render(step.halt_after, ctx)}")
    notes += [f"MANUAL: {line}" for line in manual]
    return Plan(
        name=playbook.id,
        title=playbook.title,
        changes=(
            f"{playbook.summary}. Runs {len(steps)} command(s), {playbook.sudo_steps} with sudo, and leaves "
            f"{len(manual)} manual step(s) to you (source: {playbook.source_urls[0] if playbook.source_urls else 'n/a'})."
        ),
        steps=steps,
        undo=tuple(_render(line, ctx) for line in playbook.undo),
        warning=playbook.warning,
        notes=tuple(notes),
    )


def _elevation() -> tuple[bool, bool]:
    """``(needs_terminal_expected_for_sudo, can_elevate)`` from the platform facts; never raises."""
    try:
        facts = ss._facts()
        passwordless = bool(facts.has_root or facts.can_sudo)
        return (not passwordless, passwordless or bool(facts.in_sudo_group))
    except Exception:
        return (True, True)


def _step_dicts(playbook: Playbook, plan: Plan, ctx: Mapping[str, str]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for index, (pstep, step) in enumerate(zip(playbook.executable_steps(), plan.steps, strict=True)):
        out.append({
            "index": index,
            "title": pstep.title,
            "command": step.render(),
            "sudo": step.sudo,
            "check": shlex.join(_render_argv(pstep.check, ctx)) if pstep.check else None,
            "idempotent": pstep.idempotent,
            "unpinned": pstep.unpinned,
            "upstream": pstep.upstream,
            "timeout_s": pstep.timeout_s,
            "halts_run": bool(pstep.halt_after),
            "description": pstep.description,
        })
    return out


Resolved = tuple[Playbook, dict[str, str], Plan]


def _refusal(error: str, **extra: Any) -> dict[str, Any]:
    """The one refusal shape every entry point returns: ``ok``/``applied`` False, the error, an empty ``commands``."""
    return {"ok": False, "applied": False, "error": error, "commands": [], **extra}


def _resolve(playbook_id: str, home_dir: str | Path | None) -> Resolved | dict[str, Any]:
    """Look the id up, render its context and compile its plan — or the refusal dict that explains why not.

    Shared by :func:`plan_dict`, :func:`start_run` and :func:`run_in_terminal`
    so an unknown id or an unplannable host is answered the same way
    everywhere and the plan is compiled once per call. The deny list is *not*
    applied here: ``plan_dict`` shows a denied plan with its reason
    (``Plan.to_dict`` carries it), while the runners refuse through
    :func:`_denied`.
    """
    playbook = get_playbook(playbook_id)
    if playbook is None:
        return _refusal(
            f"unknown playbook '{str(playbook_id)[:60]}'. Catalogue: {', '.join(_PLAYBOOKS_BY_ID)}",
            playbooks=list(_PLAYBOOKS_BY_ID),
        )
    try:
        ctx = _context(playbook, home_dir)
        plan = compile_plan(playbook, ctx=ctx)
    except PlaybookError as exc:
        return _refusal(str(exc), id=playbook.id, playbook=playbook.id)
    return playbook, ctx, plan


def _denied(playbook: Playbook, plan: Plan) -> dict[str, Any] | None:
    """The refusal for a plan the deny list rejects (the commands stay visible), else ``None``."""
    reason = plan.denied()
    if not reason:
        return None
    return _refusal(reason, denied=True, id=playbook.id, playbook=playbook.id, commands=plan.commands())


def plan_dict(playbook_id: str, *, home_dir: str | Path | None = None) -> dict[str, Any]:
    """``Plan.to_dict()`` plus the playbook's own fields — the card's ``plan`` and ``playbook_plan``'s answer."""
    resolved = _resolve(playbook_id, home_dir)
    if isinstance(resolved, dict):
        return resolved
    playbook, ctx, plan = resolved
    needs_terminal_expected, can_elevate = _elevation()
    out = plan.to_dict()
    out.update({
        "id": playbook.id,
        "category": playbook.category,
        "summary": playbook.summary,
        "prerequisites": list(playbook.prerequisites),
        "source_urls": list(playbook.source_urls),
        "steps": _step_dicts(playbook, plan, ctx),
        "steps_total": len(plan.steps),
        "sudo_steps": playbook.sudo_steps,
        "manual_steps": _manual_lines(playbook, ctx),
        "verify": [shlex.join(_render_argv(argv, ctx)) for argv in playbook.verify],
        # The one estimates shape (the catalogue rows carry the flat estimated_* fields instead).
        "estimates": {"minutes": playbook.estimated_minutes, "disk_gb": playbook.estimated_disk_gb},
        "requires_sudo": plan.needs_sudo,
        "rootless_alternative": playbook.rootless_alternative,
        "risk": playbook.risk,
        "last_updated": playbook.last_updated,
        "unpinned": playbook.unpinned,
        "needs_terminal_expected": bool(plan.needs_sudo and needs_terminal_expected),
        "can_elevate": can_elevate,
        "handoff_command": HANDOFF_COMMAND.format(id=playbook.id),
        "install_path": ctx[PLAYBOOK_DIR_TOKEN],
    })
    return out


def catalogue(home_dir: str | Path | None = None) -> list[dict[str, Any]]:
    """One row per playbook with receipt status — ``playbook_list``'s answer and the Setup page's cards."""
    by_item: dict[str, dict[str, Any]] = {}
    try:
        for receipt in _receipts.list_receipts(kind=RECEIPT_KIND, limit=500, home_dir=home_dir):
            by_item[str(receipt.get("item_id"))] = receipt
    except Exception as exc:  # a corrupt receipts dir must not hide the catalogue
        logger.warning("playbook receipts unreadable: %s", exc)
    rows: list[dict[str, Any]] = []
    for playbook in PLAYBOOKS:
        try:
            ctx = _context(playbook, home_dir)
        except PlaybookError:
            ctx = {PLAYBOOK_DIR_TOKEN: str(playbooks_root(home_dir) / playbook.id), HOME_TOKEN: _user_home(), USER_TOKEN: "<user>"}
        receipt = by_item.get(playbook.id)
        executable = playbook.executable_steps()
        manual = _manual_lines(playbook, ctx)
        rows.append({
            "id": playbook.id,
            "title": playbook.title,
            "category": playbook.category,
            "summary": playbook.summary,
            "source_urls": list(playbook.source_urls),
            "requires_sudo": playbook.requires_sudo,
            "sudo_steps": playbook.sudo_steps,
            "sudo_step_titles": [step.title for step in executable if step.sudo],
            "steps_total": len(executable),
            "manual_steps": len(manual),
            "manual": manual,
            "unpinned": playbook.unpinned,
            "prerequisites": list(playbook.prerequisites),
            "estimated_minutes": playbook.estimated_minutes,
            "estimated_disk_gb": playbook.estimated_disk_gb,
            "rootless_alternative": playbook.rootless_alternative,
            "risk": playbook.risk,
            "last_updated": playbook.last_updated,
            "installed": bool(receipt and receipt.get("status") == "installed"),
            "receipt_status": receipt.get("status") if receipt else None,
            "receipt_path": str(_receipts.receipt_path(RECEIPT_KIND, playbook.id, home_dir=home_dir)) if receipt else None,
            "handoff_command": HANDOFF_COMMAND.format(id=playbook.id),
        })
    return rows


# ────────────────────────────────────────────────────────────────────────────
# Running a playbook — one engine, two drivers (job with ``sudo -n``; terminal with interactive sudo)
# ────────────────────────────────────────────────────────────────────────────

RunCommand = Callable[[Step], Awaitable[dict[str, Any]]]
CheckCommand = Callable[[Sequence[str]], Awaitable[dict[str, Any]]]


async def _host_run(step: Step) -> dict[str, Any]:
    return await asyncio.to_thread(
        run_host_command, list(step.argv), sudo=step.sudo, sudo_user=step.sudo_user, timeout=step.timeout,
    )


async def _host_check(argv: Sequence[str]) -> dict[str, Any]:
    return await asyncio.to_thread(run_host_command, list(argv), sudo=False, timeout=CHECK_TIMEOUT_S)


def _tail(result: Mapping[str, Any], lines: int = LOG_TAIL_LINES) -> str:
    text = "\n".join(part for part in (result.get("stdout", ""), result.get("stderr", "")) if part)
    if not text:
        return ""
    return "\n".join(text.splitlines()[-lines:])


def _write_playbook_receipt(
    playbook: Playbook, ctx: Mapping[str, str], *, status: str, no_root: bool, result: Mapping[str, Any],
    home_dir: str | Path | None,
) -> str | None:
    """The ``playbook`` receipt; ``no_root`` is honest (False once any sudo step ran). Never raises.

    Sticky across runs: a second run that skipped the sudo steps (already
    done) must not turn an earlier ``no_root: False`` back into True.
    """
    try:
        try:
            previous = _receipts.load_receipt(_receipts.receipt_id(RECEIPT_KIND, playbook.id), home_dir=home_dir)
        except Exception:
            previous = {}
        no_root = no_root and bool(previous.get("no_root", True))
        _receipts.write_receipt(
            kind=RECEIPT_KIND,
            item_id=playbook.id,
            title=playbook.title,
            install_path=ctx[PLAYBOOK_DIR_TOKEN],
            status=status,
            source_urls=list(playbook.source_urls),
            no_root=no_root,
            metadata={
                "outcome": result.get("outcome"),
                # What the playbook *needs*, as opposed to ``no_root`` (what ran): a partial run
                # that stopped before its first sudo step is still not repairable without root.
                "requires_sudo": bool(result.get("requires_sudo")),
                "steps": [
                    {
                        "title": s.get("title"), "command": s.get("command"), "exit_code": s.get("exit_code"),
                        **({"canceled": True} if s.get("canceled") else {}),
                    }
                    for s in result.get("steps", [])
                ],
                "steps_total": result.get("steps_total"),
                "manual_steps": result.get("manual_steps", []),
                "verify": result.get("verify", []),
                "undo": result.get("undo", []),
                "repair_command": HANDOFF_COMMAND.format(id=playbook.id),
                "job_kind": JOB_KIND,
                "mode": result.get("mode"),
            },
            home_dir=home_dir,
        )
        return str(_receipts.receipt_path(RECEIPT_KIND, playbook.id, home_dir=home_dir))
    except Exception as exc:
        logger.warning("playbook receipt for '%s' not written: %s", playbook.id, exc)
        return None


def _record_run(
    playbook: Playbook,
    ctx: Mapping[str, str],
    plan: Plan,
    *,
    outcome: str,
    error_msg: str,
    remaining: int,
    executed: list[dict[str, Any]],
    any_sudo_ran: bool,
    verify_results: list[dict[str, Any]],
    manual: list[str],
    mode: str,
    home_dir: str | Path | None,
) -> tuple[dict[str, Any], str | None, dict[str, Any] | None]:
    """Build the apply-shaped result and write the receipt and the vault note. Synchronous, never raises.

    Called on every way a run ends — complete, halted, failed, hand-off and a
    job cancelled mid-step — so a run that touched the host always leaves a
    receipt (kind ``playbook``) and a ``Decisions/`` note behind, whatever
    path it took out of the engine.
    """
    applied = bool(executed)
    summary = f"Install the {playbook.id} playbook"
    result: dict[str, Any] = {
        "ok": outcome == "complete",
        "applied": applied,
        "partial": outcome != "complete" and remaining > 0,
        "summary": summary,
        "playbook": playbook.id,
        "outcome": outcome,
        "mode": mode,
        "steps": executed,
        "steps_total": len(plan.steps),
        "steps_run": len(executed),
        "no_root": not any_sudo_ran,
        "requires_sudo": plan.needs_sudo,
        "manual_steps": manual,
        "verify": verify_results,
        "undo": list(plan.undo),
    }
    if error_msg:
        result["error"] = error_msg
    if outcome == "needs_terminal":
        result["needs_terminal"] = True
        result["command"] = HANDOFF_COMMAND.format(id=playbook.id)
        result["hint"] = ss.NEEDS_TERMINAL_HINT
    if outcome == "canceled":
        result["canceled"] = True

    receipt_path = None
    if applied or outcome == "complete":
        status = "installed" if outcome == "complete" else ("failed" if outcome == "failed" else "partial")
        receipt_path = _write_playbook_receipt(
            playbook, ctx, status=status, no_root=not any_sudo_ran, result=result, home_dir=home_dir,
        )
    audit = None
    if applied:
        from nvh.integrations.wizard.tools import audit_privileged_change

        audit = audit_privileged_change(PRIVILEGED_TOOL, {"id": playbook.id}, result, summary=summary, home_dir=home_dir)
    return result, receipt_path, audit


async def _engine(
    playbook: Playbook,
    ctx: Mapping[str, str],
    plan: Plan,
    *,
    run_cmd: RunCommand,
    check_cmd: CheckCommand,
    home_dir: str | Path | None,
    mode: str,
) -> AsyncIterator[dict[str, Any]]:
    """Run the steps in order; skip on ``check``; stop at the first refusal, hand-off, halt or failure.

    Events (each with ``playbook``): ``plan`` → per step ``step`` (running) →
    optional ``log`` → ``step`` (complete / failed, ``skipped`` when the check
    passed) → ``needs_terminal`` when sudo needs a password → finally
    ``complete`` (``applied``, ``partial``, ``halted``) or ``error``
    (``needs_terminal`` / ``denied`` / the exit code). The receipt and the
    vault audit are written before the final event — and also when the job
    is cancelled mid-run (:func:`jobs.cancel_job` cancels the consumer task,
    so ``CancelledError`` lands on whatever this is awaiting): the step whose
    host command was in flight is recorded as such, because the worker thread
    running it cannot be interrupted and the command may finish on the host.
    The cancellation is then re-raised for the job to mark itself canceled.
    ``ctx`` and ``plan`` come from :func:`_resolve` — what the card showed.
    """
    base = {"playbook": playbook.id}
    handoff = HANDOFF_COMMAND.format(id=playbook.id)
    executable = playbook.executable_steps()
    total = len(plan.steps)
    manual = _manual_lines(playbook, ctx)
    yield {
        **base, "event": "plan", "status": "running",
        "message": f"{playbook.title}: {total} command(s), {len(manual)} manual step(s)",
        "commands": plan.commands(), "steps_total": total, "manual_steps": manual, "sudo": plan.needs_sudo,
        "undo": list(plan.undo), "warning": plan.warning, "notes": list(plan.notes),
    }
    refused = _denied(playbook, plan)
    if refused:
        denied = refused["error"]
        yield {**base, "event": "error", "status": "failed", "message": denied, "error": denied, "denied": True, "applied": False, "partial": False}
        return
    try:
        Path(ctx[PLAYBOOK_DIR_TOKEN]).mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        message = f"could not create {ctx[PLAYBOOK_DIR_TOKEN]}: {exc}"
        yield {**base, "event": "error", "status": "failed", "message": message, "error": message, "applied": False, "partial": False}
        return

    executed: list[dict[str, Any]] = []
    verify_results: list[dict[str, Any]] = []
    any_sudo_ran = False
    outcome = "complete"
    error_msg = ""
    remaining = 0
    position = 0  # the step being worked on (for the cancel record)
    title = ""
    in_flight: dict[str, Any] | None = None  # set while a host command runs; the one thing a cancel cannot stop
    try:
        for index, (pstep, step) in enumerate(zip(executable, plan.steps, strict=True)):
            position, title = index, pstep.title
            command = step.render()
            head = {**base, "step": index, "steps_total": total, "title": pstep.title, "command": command, "sudo": step.sudo}
            yield {**head, "event": "step", "status": "running", "message": f"Step {index + 1}/{total}: {pstep.title}"}
            if pstep.check:
                probe = await check_cmd(_render_argv(pstep.check, ctx))
                if probe.get("ok"):
                    yield {**head, "event": "log", "status": "running", "message": f"already done — skipped: {pstep.title}", "skipped": True}
                    yield {**head, "event": "step", "status": "complete", "skipped": True, "message": f"Step {index + 1}/{total} skipped (already done)"}
                    continue
            in_flight = {"step": index, "title": pstep.title, "command": command, "sudo": step.sudo}
            result = await run_cmd(step)
            in_flight = None
            if result.get("needs_terminal"):
                outcome = "needs_terminal"
                remaining = total - index
                error_msg = f"sudo needs a password for step {index + 1} ({pstep.title}) — run `{handoff}` in a terminal"
                yield {
                    **head, "event": "needs_terminal", "status": "running", "message": error_msg,
                    "command": handoff, "step_command": result.get("command"), "hint": ss.NEEDS_TERMINAL_HINT,
                }
                break
            if "exit_code" not in result:
                outcome = "failed"
                remaining = total - index
                error_msg = str(result.get("error") or "command did not run")
                yield {**head, "event": "step", "status": "failed", "message": error_msg, "error": error_msg, "denied": bool(result.get("denied"))}
                break
            executed.append({
                "step": index, "title": pstep.title, "command": result["command"], "exit_code": result["exit_code"],
                "stdout": result.get("stdout", ""), "stderr": result.get("stderr", ""),
            })
            if step.sudo:
                any_sudo_ran = True
            tail = _tail(result)
            if tail:
                yield {**head, "event": "log", "status": "running", "message": tail, "exit_code": result["exit_code"]}
            if not result["ok"]:
                outcome = "failed"
                remaining = total - index - 1
                error_msg = f"`{result['command']}` exited {result['exit_code']}"
                yield {**head, "event": "step", "status": "failed", "message": error_msg, "error": error_msg, "exit_code": result["exit_code"]}
                break
            yield {**head, "event": "step", "status": "complete", "message": f"Step {index + 1}/{total} done: {pstep.title}", "exit_code": 0}
            if pstep.halt_after:
                outcome = "halted"
                remaining = total - index - 1
                error_msg = _render(pstep.halt_after, ctx)
                yield {**head, "event": "log", "status": "running", "message": f"MANUAL: {error_msg}"}
                break

        if outcome == "complete":
            position, title = total, "verify"
            for argv in playbook.verify:
                rendered = _render_argv(argv, ctx)
                probe = await check_cmd(rendered)
                ok = bool(probe.get("ok"))
                verify_results.append({"command": shlex.join(rendered), "ok": ok, "exit_code": probe.get("exit_code")})
                yield {
                    **base, "event": "log", "status": "running", "verify": True,
                    "message": f"verify: {shlex.join(rendered)} → {'ok' if ok else 'not yet'}",
                    "command": shlex.join(rendered), "exit_code": probe.get("exit_code"),
                }
    except asyncio.CancelledError:
        # The job was cancelled. Nothing after this point may await: record
        # what ran (and what was running) so the receipt and the vault note
        # exist, then let the cancellation continue to the job consumer.
        outcome = "canceled"
        remaining = max(0, total - position)
        where = f"step {position + 1} ({title})" if position < total else "verification"
        error_msg = f"canceled by the user during {where}"
        if in_flight is not None:
            executed.append({**in_flight, "exit_code": None, "stdout": "", "stderr": "", "canceled": True})
            if in_flight["sudo"]:
                any_sudo_ran = True  # a sudo command was spawned; assume it ran
            error_msg += f" — `{in_flight['command']}` was already running and may have completed on the host"
        _record_run(
            playbook, ctx, plan, outcome=outcome, error_msg=error_msg, remaining=remaining, executed=executed,
            any_sudo_ran=any_sudo_ran, verify_results=verify_results, manual=manual, mode=mode, home_dir=home_dir,
        )
        raise

    result, receipt_path, audit = _record_run(
        playbook, ctx, plan, outcome=outcome, error_msg=error_msg, remaining=remaining, executed=executed,
        any_sudo_ran=any_sudo_ran, verify_results=verify_results, manual=manual, mode=mode, home_dir=home_dir,
    )
    applied = result["applied"]
    final = {
        **base, "applied": applied, "partial": result["partial"], "outcome": outcome, "no_root": not any_sudo_ran,
        "steps_run": len(executed), "steps_total": total, "verify": verify_results, "receipt_path": receipt_path,
        "audit": audit, "manual_steps": manual, "undo": list(plan.undo),
    }
    if outcome == "complete":
        message = (
            f"{playbook.title}: {len(executed)} command(s) ran, {total - len(executed)} already done"
            + (f"; {len(manual)} manual step(s) left to you" if manual else "")
        )
        yield {**final, "event": "complete", "status": "complete", "message": message}
    elif outcome == "halted":
        yield {**final, "event": "complete", "status": "complete", "halted": True, "message": error_msg}
    elif outcome == "needs_terminal":
        yield {
            **final, "event": "error", "status": "failed", "needs_terminal": True, "command": handoff,
            "hint": ss.NEEDS_TERMINAL_HINT, "message": error_msg, "error": error_msg,
        }
    else:
        yield {**final, "event": "error", "status": "failed", "message": error_msg, "error": error_msg}


async def run_playbook_events(
    playbook: Playbook, *, home_dir: str | Path | None = None, resolved: Resolved | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """The job runner's event source: host commands via :func:`run_host_command` (``sudo -n`` or ``needs_terminal``).

    ``resolved`` is the ``(playbook, ctx, plan)`` :func:`start_run` already
    compiled and showed on the card; without it the playbook is resolved here
    and a refusal becomes the one ``error`` event.
    """
    if resolved is None:
        found = _resolve(playbook.id, home_dir)
        if isinstance(found, dict):
            error = found["error"]
            yield {"playbook": playbook.id, "event": "error", "status": "failed", "message": error, "error": error, "applied": False, "partial": False}
            return
        resolved = found
    _, ctx, plan = resolved
    async for event in _engine(playbook, ctx, plan, run_cmd=_host_run, check_cmd=_host_check, home_dir=home_dir, mode="job"):
        yield event


def _jobs_module() -> Any:
    from nvh.integrations.services import jobs

    return jobs


def start_run(playbook_id: str, *, home_dir: str | Path | None = None) -> dict[str, Any]:
    """Start a ``playbook-run`` job (needs a running event loop). ``{ok, job_id, playbook, steps_total}``.

    ``applied`` is ``False`` on purpose: the registry must not audit the
    *start*; the runner writes the receipt and the vault note when the run
    finishes, having seen what actually ran.
    """
    resolved = _resolve(playbook_id, home_dir)
    if isinstance(resolved, dict):
        return resolved
    playbook, _ctx, plan = resolved
    refused = _denied(playbook, plan)
    if refused:
        return refused
    job = _jobs_module().start_job(
        kind=JOB_KIND,
        title=f"Install the {playbook.id} playbook",
        request={"playbook": playbook.id, "commands": plan.commands(), "sudo": plan.needs_sudo},
        source_factory=lambda: run_playbook_events(playbook, home_dir=home_dir, resolved=resolved),
    )
    needs_terminal_expected, _can = _elevation()
    return {
        "ok": True,
        "applied": False,
        "job_id": job["id"],
        "playbook": playbook.id,
        "title": playbook.title,
        "steps_total": len(plan.steps),
        "sudo": plan.needs_sudo,
        "needs_terminal_expected": bool(plan.needs_sudo and needs_terminal_expected),
        "handoff_command": HANDOFF_COMMAND.format(id=playbook.id),
        "summary": f"Install the {playbook.id} playbook",
        "job_kind": JOB_KIND,
    }


# ── The terminal driver (CLI): the same runner, interactive sudo in the user's own shell ──


def _default_confirm(prompt: str) -> bool:
    try:
        answer = input(f"{prompt} [y/N] ")
    except (EOFError, KeyboardInterrupt):
        return False
    return answer.strip().lower() in {"y", "yes"}


def _confirm_question(playbook: Playbook, plan: Plan) -> str:
    """The one question the CLI asks before a run (the manual-only playbooks get their own wording)."""
    if not plan.steps:
        return (
            f"The {playbook.id} playbook has no commands to run ({len(playbook.manual_steps())} manual step(s)). "
            "Record it and list the manual steps?"
        )
    return f"Run {len(plan.steps)} command(s) ({playbook.sudo_steps} with sudo) for the {playbook.id} playbook?"


def run_in_terminal(
    playbook_id: str,
    *,
    assume_yes: bool,
    echo: bool = True,
    home_dir: str | Path | None = None,
    confirm: Callable[[str], bool] | None = None,
    emit: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """The CLI path (``nvh playbook install <id>``): same engine, same runner, interactive ``sudo``.

    Steps go through :func:`run_host_command` with ``interactive=True`` —
    plain ``sudo`` (no ``-n``) and the terminal's own stdin, so ``sudo`` may
    ask for the password there; the deny list, the ``has_root`` short-circuit,
    the result shapes and the redaction are the runner's, not a copy. Checks
    and verify probes need no terminal and run exactly as on the job path
    (``stdin=DEVNULL``, captured). Synchronous (drives the engine with
    ``asyncio.run``; do not call from a running loop). ``echo=True`` streams
    each step's output to the terminal instead of capturing it; ``emit``
    receives every event as it happens for the CLI to print. With
    ``assume_yes=False`` the ``confirm`` callback (default: ``input()`` y/N)
    is asked :func:`_confirm_question` once before the first host command
    runs; a declined question returns ``{ok: False, canceled: True}`` and
    nothing runs. Writes the same receipt and vault note as the job path.
    Returns the final ``complete`` / ``error`` event plus ``events`` (all of
    them) and ``ok``.
    """
    resolved = _resolve(playbook_id, home_dir)
    if isinstance(resolved, dict):
        return resolved
    playbook, ctx, plan = resolved
    refused = _denied(playbook, plan)
    if refused:
        return refused
    if not assume_yes and not (confirm or _default_confirm)(_confirm_question(playbook, plan)):
        return _refusal("canceled", canceled=True, id=playbook.id, playbook=playbook.id, commands=plan.commands())

    # Run on the loop's thread on purpose: a Ctrl-C at the sudo prompt must
    # land on the process that spawned it, not on a worker the loop cannot stop.
    async def run_cmd(step: Step) -> dict[str, Any]:
        return run_host_command(
            list(step.argv), sudo=step.sudo, sudo_user=step.sudo_user, timeout=step.timeout,
            interactive=True, echo=echo,
        )

    async def check_cmd(argv: Sequence[str]) -> dict[str, Any]:
        return run_host_command(list(argv), sudo=False, timeout=CHECK_TIMEOUT_S)

    async def collect() -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        async for event in _engine(playbook, ctx, plan, run_cmd=run_cmd, check_cmd=check_cmd, home_dir=home_dir, mode="terminal"):
            events.append(event)
            if emit is not None:
                try:
                    emit(event)
                except Exception as exc:  # a printing slip must not abort the run
                    logger.warning("playbook emit callback failed: %s", exc)
        return events

    events = asyncio.run(collect())
    final = dict(events[-1]) if events else {"event": "error", "message": "no events", "error": "no events"}
    final["ok"] = final.get("event") == "complete"
    final["events"] = events
    return final


# ────────────────────────────────────────────────────────────────────────────
# Wizard tools
# ────────────────────────────────────────────────────────────────────────────


def _playbook_id(args: Mapping[str, Any]) -> str:
    value = args.get("id") or args.get("playbook") or args.get("name") or ""
    return str(value).strip()


def _list_payload(args: Mapping[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {
        "ok": True,
        "playbooks": catalogue(),
        "count": len(PLAYBOOKS),
        "deferred": deferred(),
        "privileged_tool": PRIVILEGED_TOOL,
        "handoff_command": HANDOFF_COMMAND.format(id="<id>"),
    }
    job_id = args.get("job_id")
    if isinstance(job_id, str) and job_id.strip():
        try:
            out["job"] = _jobs_module().load_job(job_id.strip())
        except KeyError:
            out["job_error"] = f"unknown job '{job_id.strip()[:60]}'"
    return out


# The blocking handlers run off the loop through system_settings' own wrapper,
# so a failure has the same ``{ok: False, error: "<label> failed: …"}`` shape
# (planners add ``commands: []``) as ``system_settings_plan`` on the same card.
_tool_playbook_list = ss._threaded(lambda args: _list_payload(args or {}), "playbook_list")
_tool_playbook_plan = ss._threaded(lambda args: plan_dict(_playbook_id(args or {})), "playbook_plan", plan=True)


async def _tool_playbook_install(args: dict[str, Any]) -> dict[str, Any]:
    """Privileged handler: after the registry verified the card's token, start the run as a job (on the loop)."""
    try:
        return start_run(_playbook_id(args or {}))
    except Exception as exc:
        return {"ok": False, "applied": False, "error": f"playbook_install failed: {type(exc).__name__}: {str(exc)[:200]}"}


_ID_PARAM = {
    "type": "string", "required": True,
    "description": "Playbook id (the upstream folder name): " + ", ".join(_PLAYBOOKS_BY_ID) + ".",
}


def register_wizard_tools(reg: Any) -> None:
    """Register ``playbook_list`` (auto), ``playbook_plan`` (auto) and ``playbook_install`` (privileged)."""
    from nvh.integrations.wizard.tools import WizardTool

    reg.register(WizardTool(
        name="playbook_list",
        description=(
            "List the DGX Spark playbooks nvHive can run (Ollama, Open WebUI, ComfyUI, vLLM, llama.cpp, LM Studio, "
            "Tailscale, VS Code, DGX Dashboard, CLI coding agents, OpenClaw, NemoClaw): which steps need sudo, the manual "
            "steps, time and disk estimates, a rootless studio-pack alternative where one exists, and whether the install "
            "receipt says it is installed. Pass job_id to read a running playbook_install job's progress."
        ),
        safety_class="auto",
        parameters={
            "job_id": {"type": "string", "required": False, "description": "A playbook-run job id to report progress for."},
        },
        handler=_tool_playbook_list,
        summary_template="List the Spark playbooks.",
    ))
    reg.register(WizardTool(
        name="playbook_plan",
        description=(
            "Dry run of one Spark playbook: the exact commands in order, which run with sudo, the manual steps (browser "
            "logins, TUIs, foreground servers), how to verify, how to undo, estimates and warnings. Runs nothing. Show it "
            "to the user before playbook_install."
        ),
        safety_class="auto",
        parameters={"id": _ID_PARAM},
        handler=_tool_playbook_plan,
        summary_template="Plan the {id} playbook.",
    ))
    reg.register(WizardTool(
        name="playbook_install",
        description=(
            "Install a Spark playbook on this machine (steps may use sudo). The user approves the exact commands on a "
            "red card first; the run then starts as a background job (kind playbook-run) whose events stream from "
            "/v1/jobs/<job_id>. Steps already done are skipped; if sudo needs a password the job stops and hands the "
            "user one command to run in a terminal: nvh playbook install <id>. Every run that touches the host writes "
            "an install receipt and a vault Decisions note."
        ),
        safety_class="privileged",
        parameters={"id": _ID_PARAM},
        handler=_tool_playbook_install,
        planner=_tool_playbook_plan,
        summary_template="Install the {id} playbook",
    ))
