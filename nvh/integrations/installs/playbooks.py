"""Spark playbooks — NVIDIA's DGX Spark install guides as approved, audited runs.

Each :class:`Playbook` mirrors one folder of github.com/NVIDIA/dgx-spark-playbooks:
title, one-line summary, prerequisites, the numbered steps, how to verify, how
to undo, time and risk. Ids are nvHive's stable short names (``ollama``,
``vscode`` …); ``upstream_folder`` names the folder under ``nvidia/`` the
steps were extracted from. Upstream moved every guide to
``nvidia/playbook-<name>/`` in 2026 and left the old folders behind unlisted,
so ``ollama`` maps to ``playbook-llms`` (the generic "Run Local LLMs" guide,
of which the DGX Spark tier is what ships here) and ``comfy-ui`` to
``playbook-comfyui``. ``scripts/check_playbook_upstream.py --check`` compares
today's READMEs and assets with the committed snapshot so a rewrite upstream
is noticed rather than silently shipped. Playbooks are **not** studio packs —
they may need ``sudo`` — so they never go through ``/v1/studio/install``.
They run only through the Wizard's privileged tier
(``WizardToolRegistry.execute()`` is the single enforcement point: red card,
approval token, kill switch) or through the CLI in the user's own terminal
(``nvh playbook install <id>``), where ``sudo`` may ask for a password nvHive
never sees. Both refuse off-Linux in plain words: the commands are apt,
systemd and docker, and the Spark runs DGX OS.

Steps compile into :class:`nvh.integrations.wizard.system_settings.Step` /
:class:`~nvh.integrations.wizard.system_settings.Plan` and both drivers run
them through that module's runners -- the job path's steps through
:func:`~nvh.integrations.wizard.system_settings.run_host_command_killable`,
its check probes and the CLI path through
:func:`~nvh.integrations.wizard.system_settings.run_host_command`, which
share one deny list and one sudo rule (``_resolve_host_command``) --
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
    ``unpinned=True`` (rendered as a ``pipe-to-shell: unpinned`` note).
  - **Checksums.** A script nvHive runs from the upstream repository
    (comfy-ui's ``setup.sh`` and ``launch.sh``, the vllm Sync launch script)
    is verified against :data:`UPSTREAM_ASSETS` before it runs: upstream
    stopped publishing checksums in its 2026 rewrite, so the pins are the
    sha256 of each file as fetched on :data:`ASSET_PIN_DATE`, and the drift
    check reports a changed file. The VS Code ``.deb`` is verified against
    Microsoft's own manifest (``https://code.visualstudio.com/sha?build=stable``,
    the ``linux-deb-arm64`` product's ``sha256hash``) and its
    ``productVersion`` is recorded, so no playbook step installs an
    unverified vendor artifact as root; the ``unpinned download`` note stays
    for any future step that has to.
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
  - **Model tags are never hand-typed.** A step that pulls an Ollama model
    carries ``@MODEL_CHAT@`` or ``@MODEL_CODE@`` (title, pull, check, the
    manual prose), rendered at compile time from the tier table
    (:mod:`nvh.core.local_models`) for this host's platform facts -- a 128 GB
    unified Spark gets the top pick that fits the pool after its OS reserve, a
    host the facts cannot size gets the table's default -- so a tag the
    registry retires cannot outlive the table
    (``tests/test_no_retired_model_tags.py``). Upstream's own recommendation
    is recorded in the notes as prose. LM Studio's catalogue ids are not
    Ollama tags: its model step quotes the README's id in manual prose.

Every run that touched the host writes an install receipt (kind ``playbook``,
honest ``no_root = not any sudo step ran``, repair plan
``nvh playbook install <id>``) and a vault ``Decisions/`` note through
:func:`nvh.integrations.wizard.tools.audit_privileged_change` — including a
cancelled job. Cancel semantics (W1-lite, and the 0.46 kill): the engine
re-reads the job's ``cancel_requested`` flag before every check probe and
host command and after every step, so a cancel that arrives between steps
stops the run before the next command starts and the receipt says ``stopped
after step N``. A job's host command runs in a process group of its own
(:func:`~nvh.integrations.wizard.system_settings.run_host_command_killable`):
its worker thread re-reads the same flag every ``CANCEL_POLL_S`` seconds
while the command runs and, when it is up, SIGTERMs the group, waits
``KILL_GRACE_S`` and SIGKILLs it -- the receipt says ``stopped during step N
(command killed)`` with the exit code the signal left, pipes drained and
closed, no zombie -- and the engine yields its own ``canceled`` event, so the
job log and the vault note carry that account rather than a generic line.
A command that exited on its own before the signal is recorded as the plain
exit it was (``stopped after step N``), never as a kill; one the signals did
not stop for sure (a root-owned command behind ``sudo`` that outlived
SIGKILL and the ``sudo -n kill`` escalation) is recorded with ``kill_failed``
as ``may still be running on the host``, not as killed; a step whose own
deadline fired as the cancel landed keeps its ``timed out`` record. A repeat
cancel while the handler waits for the runner's report does not cut the
wait short. A
task cancelled without the job's flag (a server shutdown) leaves the command
alone and records it as in flight (it may finish on the host); a job consumer
that closes the stream early still gets the receipt written from the engine's
close handler.
"""

from __future__ import annotations

import asyncio
import logging
import shlex
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import nvh.core.local_models as lm
from nvh.integrations.services import receipts as _receipts
from nvh.integrations.wizard import system_settings as ss
from nvh.integrations.wizard.system_settings import (
    Plan,
    Step,
    run_host_command,
    run_host_command_killable,
)

logger = logging.getLogger(__name__)

__all__ = [
    "ASSET_PIN_DATE",
    "DEFERRED",
    "HANDOFF_COMMAND",
    "JOB_KIND",
    "PLAYBOOKS",
    "RECEIPT_KIND",
    "UPSTREAM_ASSETS",
    "UPSTREAM_RAW",
    "UPSTREAM_TREE",
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
    "upstream_folders",
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
#: The killable runner's poll cadence and SIGTERM grace (:mod:`system_settings` owns the values; read at call time).
CANCEL_POLL_S = ss.CANCEL_POLL_S
KILL_GRACE_S = ss.KILL_GRACE_S
#: Lines of a step's output kept in the ``log`` event (the receipt and the audit keep more).
LOG_TAIL_LINES = 20

#: Placeholders rendered at compile time (no shell, so ``~`` and ``$USER`` never expand).
PLAYBOOK_DIR_TOKEN = "@PLAYBOOK_DIR@"
USER_TOKEN = "@USER@"
HOME_TOKEN = "@HOME@"
#: This host's ``chat`` / ``code`` pick from the tier table (:mod:`nvh.core.local_models`) and
#: its size on disk in GB. The catalogue is static data and the machine is only known at compile
#: time, and a hand-typed tag is exactly what the registry retires -- see :func:`_model_pick`.
MODEL_CHAT_TOKEN = "@MODEL_CHAT@"
MODEL_CHAT_GB_TOKEN = "@MODEL_CHAT_GB@"
MODEL_CODE_TOKEN = "@MODEL_CODE@"
MODEL_CODE_GB_TOKEN = "@MODEL_CODE_GB@"
_MODEL_TOKENS: dict[str, tuple[str, str]] = {
    "chat": (MODEL_CHAT_TOKEN, MODEL_CHAT_GB_TOKEN),
    "code": (MODEL_CODE_TOKEN, MODEL_CODE_GB_TOKEN),
}

UNPINNED_NOTE = "pipe-to-shell: unpinned"
#: An ``unpinned`` step whose upstream is not a pipe: a vendor artifact with no version pin or checksum.
UNPINNED_DOWNLOAD_NOTE = "unpinned download"
RELOGIN_NOTE = (
    "Log out and back in (and restart nvHive) so the docker group applies, then run this "
    "playbook again — finished steps are skipped."
)
DOCKER_GROUP_CHECK: tuple[str, ...] = ("bash", "-c", "id -nG | grep -qw docker")

#: Upstream scripts a step downloads and runs (or hands to the user to run), pinned by sha256.
#: Upstream publishes no checksums since its 2026 rewrite; these are the digests of the files
#: as fetched on :data:`ASSET_PIN_DATE`. The verify step fails -- and the run stops before the
#: script runs -- when the file changes; ``scripts/check_playbook_upstream.py --check`` reports
#: the same drift from CI. Re-pin by hand after reading the new file.
ASSET_PIN_DATE = "2026-11-01"
COMFYUI_SETUP_SH = f"{UPSTREAM_RAW}playbook-comfyui/assets/setup.sh"
COMFYUI_LAUNCH_SH = f"{UPSTREAM_RAW}playbook-comfyui/assets/launch.sh"
VLLM_SYNC_SCRIPT_NAME = "sync-vllm-spark-128gb-qwen38-27b.sh"
VLLM_SYNC_SCRIPT = f"{UPSTREAM_RAW}playbook-vllm/assets/{VLLM_SYNC_SCRIPT_NAME}"
UPSTREAM_ASSETS: dict[str, str] = {
    COMFYUI_SETUP_SH: "d4d456d9d09901a53fdc33e12acab7387fdfb1b5050f00a93ed9c91a09cfd728",
    COMFYUI_LAUNCH_SH: "7dc75b155a198a49537832c4a363d321080b130be0a6945a0bc0afe78da8badc",
    VLLM_SYNC_SCRIPT: "802f0ed4c60f5569158528021163556e9616963a6cb8875d46a82341fc976b47",
}

#: Microsoft's manifest of the current stable VS Code builds (one product per platform, each
#: with ``sha256hash`` and ``productVersion``); the ``.deb`` URL below redirects to that build.
VSCODE_SHA_MANIFEST_URL = "https://code.visualstudio.com/sha?build=stable"
VSCODE_PLATFORM = "linux-deb-arm64"
_VSCODE_DEB_URL = "https://code.visualstudio.com/sha/download?build=stable&os=linux-deb-arm64"
_VSCODE_INSTALLED: tuple[str, ...] = ("bash", "-c", "dpkg -s code | grep -q 'Status: install ok installed'")
#: Verbatim upstream (README Desktop Use steps 2-3): the current stable .deb installed as root, unverified.
_VSCODE_UPSTREAM = f"wget '{_VSCODE_DEB_URL}' -O ~/vscode-arm64.deb && sudo apt install ~/vscode-arm64.deb"
#: ``python3 -I -c`` body: ``argv[1]`` the manifest, ``argv[2]`` the .deb, ``argv[3]`` where to record
#: ``productVersion``. Paths travel as arguments (never spliced into the code), the manifest's
#: ``linux-deb-arm64`` product must be unique and carry a 64-hex sha256, and a mismatch exits 1
#: before ``apt-get`` ever sees the file. Every gate is an explicit ``sys.exit`` (never ``assert``,
#: which ``-O`` / ``PYTHONOPTIMIZE`` strips), and ``-I`` ignores the user's ``PYTHON*`` environment
#: and site directories so the inherited shell cannot change what the check does. Runs as the user
#: under PLAYBOOK_DIR, no sudo.
_VSCODE_VERIFY_ARGV: tuple[str, ...] = ("python3", "-I", "-c")
_VSCODE_VERIFY_PY = (
    "import hashlib,json,sys;m,d,r=sys.argv[1:4];"
    "p=[x for x in json.load(open(m)).get('products',[]) if (x.get('platform') or {}).get('os')=='linux-deb-arm64'];"
    "len(p)==1 or sys.exit('manifest lists %d linux-deb-arm64 products'%len(p));"
    "w=str(p[0].get('sha256hash') or '').lower();len(w)==64 or sys.exit('manifest carries no sha256 for linux-deb-arm64');"
    "h=hashlib.sha256(open(d,'rb').read()).hexdigest();"
    "h==w or sys.exit('sha256 mismatch: manifest %s, download %s'%(w,h));"
    "v=str(p[0].get('productVersion') or 'unknown');open(r,'w').write(v+'\\n');"
    "print('VS Code %s: sha256 %s verified against the stable manifest'%(v,h))"
)

#: Plain-words refusal for a host that is not Linux (the Spark runs DGX OS; the steps are apt,
#: systemd and docker). ``{what}`` is the OS as the platform facts report it.
UNSUPPORTED_HOST_ERROR = (
    "Spark playbooks run on Linux (DGX OS). This machine reports {what}, so nvHive will not plan "
    "or run the {id} playbook here and nothing was changed. Run it on the Spark itself."
)


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
    an artifact nvHive could not verify — a download-then-run of an upstream
    ``curl … | sh`` or a vendor package with no checksum anywhere (``upstream``
    keeps the verbatim upstream command; a pipe in it tells the two apart).
    ``halt_after`` stops the run once this step *ran* (not skipped) with that
    MANUAL note — the docker-group re-login.
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
    """One upstream playbook, in the README's own skeleton.

    ``id`` is nvHive's stable name; ``upstream_folder`` the folder under
    ``nvidia/`` in github.com/NVIDIA/dgx-spark-playbooks the steps come from.
    """

    id: str
    upstream_folder: str
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
    upstream_folder: str
    title: str
    reason: str
    last_updated: str = ""

    @property
    def source_url(self) -> str:
        return UPSTREAM_TREE + self.upstream_folder

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "upstream_folder": self.upstream_folder, "title": self.title, "reason": self.reason,
            "source_url": self.source_url, "last_updated": self.last_updated,
        }


# ────────────────────────────────────────────────────────────────────────────
# Step constructors (keep the catalogue below readable)
# ────────────────────────────────────────────────────────────────────────────


def _src(upstream_folder: str) -> str:
    return UPSTREAM_TREE + upstream_folder


def _cmd_exists(name: str) -> tuple[str, ...]:
    return ("bash", "-c", f"command -v {shlex.quote(name)}")


def _sha256_check(sha256: str, path: str, into: str | None = None) -> tuple[str, ...]:
    """``sha256sum -c`` of one file whose path may hold a placeholder.

    The path travels as a positional parameter (``$1``), never spliced into
    the script text, so an apostrophe, a space or ``$`` in ``NVH_HOME`` can
    neither break the command nor inject into it. With ``into`` the verified
    file is then moved to that path (``$2``) — the shape
    :func:`_pinned_download` uses so a file only ever reaches the name the
    later steps run once its digest matched.
    """
    if into is None:
        return ("bash", "-c", f"echo \"{sha256}  $1\" | sha256sum -c -", "sha256sum", path)
    return ("bash", "-c", f"echo \"{sha256}  $1\" | sha256sum -c - && mv -f \"$1\" \"$2\"", "sha256sum", path, into)


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


#: Suffix of the name a pinned download is saved under until its sha256 matched (``_pinned_download``).
UNVERIFIED_SUFFIX = ".download"


def _pinned_download(
    title: str, url: str, filename: str, *, check: tuple[str, ...] | None, upstream: str = "",
) -> tuple[PlaybookStep, PlaybookStep]:
    """An upstream script pinned in :data:`UPSTREAM_ASSETS`: download it, then verify the sha256 before anything runs it.

    The download lands under ``<filename>.download`` and the verify step
    moves it to ``<filename>`` only after ``sha256sum -c`` passed, so the
    name the later steps (and the user) run never holds an unverified copy.
    Both steps share ``check``: when it is ``test -f <filename>`` the file
    only exists once verified, so neither step is skipped before the digest
    matched (the download alone cannot satisfy it); when it is a
    post-install marker both are skipped on a finished install.
    """
    target = f"{PLAYBOOK_DIR_TOKEN}/{filename}"
    unverified = f"{target}{UNVERIFIED_SUFFIX}"
    return (
        _step(
            f"Download {title}",
            "curl", "-fsSL", url, "-o", unverified,
            check=check, upstream=upstream,
            description=(
                f"Saved under NVH_HOME as {filename}{UNVERIFIED_SUFFIX} and checked against the sha256 nvHive pinned on "
                f"{ASSET_PIN_DATE} (upstream publishes no checksum); only a matching file is moved to {filename}, so a "
                "changed upstream file stops the run at the next step."
            ),
        ),
        _step(
            f"Verify {title} against the pinned sha256",
            *_sha256_check(UPSTREAM_ASSETS[url], unverified, into=target),
            check=check,
        ),
    )


# ────────────────────────────────────────────────────────────────────────────
# The catalogue — first tier; folders are today's nvidia/playbook-<name>/
# ────────────────────────────────────────────────────────────────────────────

_OLLAMA_INSTALL = _download_then_run(
    "Ollama", "https://ollama.com/install.sh", "ollama-install.sh", shell="sh", sudo=True,
    check=_cmd_exists("ollama"),
    description="The script writes /usr/local/bin/ollama, creates the `ollama` system user and installs the systemd unit, so it runs with sudo.",
)

#: Ollama's own Linux uninstall sequence (docs.ollama.com/linux); the upstream guide gives none.
_OLLAMA_UNDO = (
    "sudo systemctl stop ollama",
    "sudo systemctl disable ollama",
    "sudo rm /usr/local/bin/ollama",
    "sudo rm -rf /usr/share/ollama  # removes every downloaded model",
    "sudo userdel ollama",
)

_COMFYUI_DONE: tuple[str, ...] = ("test", "-f", f"{PLAYBOOK_DIR_TOKEN}/ComfyUI/models/vae/ae.safetensors")
_VLLM_CONTAINER = "vllm-spark-128gb-qwen38-27b"
_VLLM_IMAGE = "nvcr.io/nvidia/vllm:26.10.pre1-py3"
_VLLM_MODEL = "nvidia/Qwen3.8-27B-NVFP4"
_VLLM_DRAFT = "z-lab/Qwen3.8-27B-DFlash2"
_HF_HUB = f"{HOME_TOKEN}/.cache/huggingface/hub"

PLAYBOOKS: tuple[Playbook, ...] = (
    Playbook(
        id="ollama",
        upstream_folder="playbook-llms",
        title="Run Local LLMs (Ollama)",
        category="runtime",
        summary="Chat, agents and coding with a local model served by Ollama on your Spark",
        source_urls=(_src("playbook-llms"),),
        prerequisites=(
            "A DGX Spark (128 GB unified memory) — the upstream guide's 'DGX Spark' tier; smaller RTX tiers are in the notes",
            "Internet access to download Ollama and the model weights",
            f"Room for this host's chat pick, {MODEL_CHAT_TOKEN} (about {MODEL_CHAT_GB_TOKEN} GB)",
        ),
        steps=(
            *_OLLAMA_INSTALL,
            _step(
                "Enable the ollama service at boot",
                "systemctl", "enable", "--now", "ollama",
                sudo=True, check=("systemctl", "is-enabled", "ollama"),
            ),
            _step(
                f"Download a model that fits this Spark ({MODEL_CHAT_TOKEN}, about {MODEL_CHAT_GB_TOKEN} GB)",
                "ollama", "pull", MODEL_CHAT_TOKEN,
                check=("ollama", "show", MODEL_CHAT_TOKEN), timeout=LONG_TIMEOUT_S,
                description=(
                    "The tag is this host's chat pick from nvHive's tier table (nvh.core.local_models), sized from "
                    "the memory the platform facts report; the guide's own picks per GPU are in the notes."
                ),
            ),
            _manual(
                "Chat, with a large context window",
                f"ollama run {MODEL_CHAT_TOKEN}   # then `/set parameter num_ctx 32768`: the guide asks for 32k or more for "
                "agents and coding; /bye or Ctrl+D exits",
            ),
            _manual(
                "Note the server's URL and port, then test it",
                "Ollama listens on http://localhost:11434 (OpenAI-compatible under /v1). "
                "curl http://localhost:11434/v1/chat/completions -H 'Content-Type: application/json' -d "
                f"'{{\"model\": \"{MODEL_CHAT_TOKEN}\", \"messages\": "
                "[{\"role\": \"user\", \"content\": \"Write me a haiku about GPUs and AI.\"}]}'",
            ),
            _manual(
                "Point an agent or coding tool at it",
                "OpenClaw, Hermes Agent or OpenCode: enter http://localhost:11434/v1 as the model provider during their "
                "onboarding, or use `ollama launch <agent>` (the cli-coding-agent playbook).",
            ),
        ),
        verify=(
            ("ollama", "--version"),
            ("systemctl", "is-active", "ollama"),
            ("curl", "-sf", "http://localhost:11434/api/tags"),
        ),
        undo=_OLLAMA_UNDO,
        notes=(
            "Upstream's starting picks by GPU: 6-8 GB RTX — Qwen 3.5 4B; 12-16 GB RTX — Qwen 3.5 9B or Gemma 4 12B; "
            "24 GB+ RTX — Qwen 3.6 27B; DGX Spark — Qwen 3.6 35B (NVIDIA's Qwen3.6-35B-A3B-NVFP4 checkpoint under vLLM "
            "for the best performance). Between ranges, take the next lower pick or a lower-bit quantisation.",
            f"The model step pulls {MODEL_CHAT_TOKEN} — this host's chat pick from nvHive's tier table — rather than a "
            "hand-typed tag; pull an upstream pick yourself with `ollama pull <tag>` from https://ollama.com/library if you prefer.",
            "The guide links ollama.com for the install; Ollama's Linux installer is the `install.sh` this run downloads "
            "first, and the undo above is Ollama's own uninstall sequence (the guide gives none).",
            "Rootless instead: the `rootless-ollama` studio pack installs Ollama under NVH_HOME with no sudo.",
        ),
        estimated_minutes=20,
        estimated_disk_gb=40.0,
        rootless_alternative="rootless-ollama",
        risk="Low (upstream gives none); nvHive: installs a system service and a system user",
    ),
    Playbook(
        id="cli-coding-agent",
        upstream_folder="playbook-cli-coding-agent",
        title="Set Up CLI Coding Agents with Local Inference",
        category="dev",
        summary="Your chosen agent (Claude Code, OpenCode or Codex CLI) connected to a local model in one command",
        source_urls=(_src("playbook-cli-coding-agent"),),
        prerequisites=(
            "DGX Spark (64 GB or 128 GB unified memory), powered on and reachable for a local or SSH terminal",
            "A current Ollama release (`ollama launch` needs it): `ollama --version`",
            f"Memory and disk for the coding model: this host's code pick {MODEL_CODE_TOKEN} (about {MODEL_CODE_GB_TOKEN} GB); "
            "upstream's default Qwen3.6 MTP Q4_K_M needs about 23 GB (q8_0 about 39 GB, bf16 about 71 GB)",
            "Codex CLI only: Node.js and npm; the optional coding task needs Python 3 with venv",
        ),
        steps=(
            _step(
                "Confirm the environment (OS release and a visible GPU)",
                "bash", "-c", "head -n 2 /etc/os-release && nvidia-smi",
            ),
            *_OLLAMA_INSTALL,
            _step(
                f"Pull the coding model ({MODEL_CODE_TOKEN}, about {MODEL_CODE_GB_TOKEN} GB)",
                "ollama", "pull", MODEL_CODE_TOKEN,
                check=("ollama", "show", MODEL_CODE_TOKEN), timeout=LONG_TIMEOUT_S,
                description=(
                    "This host's code pick from nvHive's tier table (nvh.core.local_models); upstream's Qwen3.6 "
                    "recommendation is in the notes."
                ),
            ),
            *_download_then_run(
                "Claude Code", "https://claude.ai/install.sh", "claude-install.sh",
                check=_cmd_exists("claude"),
                description="User-space install (no sudo).",
            ),
            _manual(
                "Test local inference (optional)",
                f"ollama run {MODEL_CODE_TOKEN}   # interactive; type /bye or press Ctrl+D to exit",
            ),
            _manual(
                "Launch your agent against the local model",
                f"ollama launch claude --model {MODEL_CODE_TOKEN}   # OpenCode: `curl -fsSL https://opencode.ai/install | bash`, "
                f"`export PATH=\"$HOME/.opencode/bin:$PATH\"`, then `ollama launch opencode --model {MODEL_CODE_TOKEN}`; "
                f"Codex: `npm install -g @openai/codex` (use a user npm prefix) then `ollama launch codex --model {MODEL_CODE_TOKEN}`",
            ),
            _manual(
                "Complete a small coding task",
                "mkdir -p ~/cli-agent-demo && cd ~/cli-agent-demo; write math_utils.py (an `add(a, b)` stub) and "
                "test_math_utils.py (`assert math_utils.add(1, 2) == 3`); ask the agent to implement add() and make the "
                "test pass; `python3 -m venv .venv && source .venv/bin/activate && python3 -m pip install -U pytest && "
                "python3 -m pytest -q`.",
            ),
        ),
        verify=(
            ("ollama", "--version"),
            ("ollama", "show", MODEL_CODE_TOKEN),
            _cmd_exists("claude"),
        ),
        undo=(
            "sudo systemctl stop ollama",
            f"ollama rm {MODEL_CODE_TOKEN}   # deletes the model files",
        ),
        notes=(
            "Upstream pulls Qwen3.6 35B-A3B (the MTP Q4_K_M build, about 23 GB; q8_0 and bf16 variants for more memory) "
            "from https://ollama.com/library/qwen3.6 and launches every agent against it. nvHive pulls "
            f"{MODEL_CODE_TOKEN} — this host's code pick from its tier table — because a playbook never hand-types a tag; "
            "`ollama launch <agent> --model <tag>` works the same with any tag you pull yourself.",
            "`ollama launch` needs a current Ollama; re-run the installer if it reports an unknown command "
            "(the install step is skipped while `ollama` is on PATH).",
            "Rootless instead: the `rootless-ollama` studio pack (verify `ollama launch` is available in that build).",
        ),
        estimated_minutes=20,
        estimated_disk_gb=25.0,
        rootless_alternative="rootless-ollama",
        risk="Low",
        last_updated="08/03/2026",
    ),
    Playbook(
        id="open-webui",
        upstream_folder="playbook-open-webui",
        title="Chat with LLMs Using Open WebUI and Ollama",
        category="chat-ui",
        summary="A self-hosted browser interface with models running locally on your GPU",
        source_urls=(_src("playbook-open-webui"),),
        prerequisites=(
            "DGX Spark (64 GB or 128 GB unified memory), powered on and networked; Docker available (`docker ps`)",
            "Disk for the container image (about 7 GB) and a model (about 15 GB for gpt-oss:20b; about 25 GB for the "
            "agent-ready Qwen3.6 pick)",
            "A browser that can reach port 8080 on the Spark (the remote path publishes 12000 through NVIDIA Sync instead)",
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
                "In Open WebUI: Select a model → type `gpt-oss:20b` → click Download \"gpt-oss:20b\" (Ollama tag) → wait, "
                "then select it. Agent-ready alternative for the Spark: the README's Qwen3.6-35B-A3B from "
                "https://ollama.com/library/qwen3.6 with a context window of at least 32K.",
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
            "Upstream's remote path runs the same container from an NVIDIA Sync custom app on port 12000 (its launch "
            "script starts or creates the container); for a local-only bind swap `-p 8080:8080` for "
            "`-p 127.0.0.1:8080:8080` when you create the container.",
            "Agent-ready models (upstream tab): prefer a context window of at least 32K tokens, 64K or more with headroom.",
        ),
        warning=(
            "Publishes port 8080 on every interface (LAN), unauthenticated until an administrator exists: the first "
            "visitor to complete the signup form becomes the admin, who can run Python inside the --gpus=all container. "
            "Create the administrator account as soon as the run finishes, or bind the port to 127.0.0.1."
        ),
        estimated_minutes=20,
        estimated_disk_gb=32.0,
        risk="Low (upstream); nvHive: LAN-reachable admin signup until you claim it",
        last_updated="10/14/2026",
    ),
    Playbook(
        id="comfy-ui",
        upstream_folder="playbook-comfyui",
        title="Generate Images and Videos with ComfyUI",
        category="image",
        summary="Node-based diffusion workflows; the Image Gen Quick Start (Z-Image-Turbo) on the host, no container",
        source_urls=(_src("playbook-comfyui"),),
        prerequisites=(
            "DGX Spark (64 GB or 128 GB unified memory) on DGX OS; about 30 GB free disk and about 24 GB GPU memory for Z-Image-Turbo",
            "python3, pip3 and git; a CUDA toolkit; network access to Hugging Face (public weights, no token)",
            "A browser that can reach <SPARK_IP>:8188",
        ),
        steps=(
            _step(
                "Verify system prerequisites",
                "bash", "-c", "python3 --version && pip3 --version && git --version",
            ),
            *_pinned_download(
                "the upstream setup script (setup.sh)", COMFYUI_SETUP_SH, "setup.sh",
                check=_COMFYUI_DONE, upstream=f'curl -fsSL "{COMFYUI_SETUP_SH}" | bash',
            ),
            _step(
                "Run setup.sh: venv, PyTorch cu130, ComfyUI v0.33.2, Z-Image-Turbo weights (about 20 GB)",
                "bash", "setup.sh",
                cwd=PLAYBOOK_DIR_TOKEN,
                check=_COMFYUI_DONE,
                timeout=LONG_TIMEOUT_S,
                description="README steps 1-6 in one script: prerequisite check, comfyui-env, torch/torchvision, the ComfyUI clone, its requirements and the three model files.",
            ),
            *_pinned_download(
                "the upstream launch script (launch.sh)", COMFYUI_LAUNCH_SH, "launch.sh",
                check=("test", "-f", f"{PLAYBOOK_DIR_TOKEN}/launch.sh"), upstream=f'curl -fsSL "{COMFYUI_LAUNCH_SH}" | bash',
            ),
            _manual(
                "Launch the ComfyUI server (foreground)",
                f"cd {PLAYBOOK_DIR_TOKEN} && bash launch.sh   # listens on 0.0.0.0:8188 (LAN). Local only: "
                "`source comfyui-env/bin/activate && cd ComfyUI && python main.py --listen 127.0.0.1`",
            ),
            _manual("Validate the installation", "In a second terminal: `curl -I http://localhost:8188` → HTTP 200; then open http://<SPARK_IP>:8188."),
            _manual(
                "Run a template flow",
                "Templates → Image → 'Z-Image-Turbo: Text to Image' → Run (about 30 s); watch `nvidia-smi` in another terminal.",
            ),
        ),
        verify=(
            ("test", "-x", f"{PLAYBOOK_DIR_TOKEN}/comfyui-env/bin/python"),
            ("test", "-d", f"{PLAYBOOK_DIR_TOKEN}/ComfyUI/.git"),
            (f"{PLAYBOOK_DIR_TOKEN}/comfyui-env/bin/python", "-c", "import torch; assert torch.cuda.is_available()"),
            ("curl", "-sI", "http://localhost:8188"),
        ),
        undo=(
            f"rm -rf {PLAYBOOK_DIR_TOKEN}   # comfyui-env, ComfyUI and the downloaded weights (all under NVH_HOME)",
        ),
        notes=(
            "Everything lives under NVH_HOME/playbooks/comfy-ui; no sudo anywhere.",
            f"Upstream publishes no checksum for setup.sh and launch.sh; nvHive verifies each against the sha256 it pinned on "
            f"{ASSET_PIN_DATE} before running it (scripts/check_playbook_upstream.py --check reports a changed file).",
            "The README's Video Gen Workflow tab (a Docker image, FLUX / Wan / HunyuanVideo tiers of 70-230 GB, an HF token and "
            "accepted model licences) is not shipped here; follow it by hand from the source link.",
            "nvHive's own rootless ComfyUI (`nvh workstation --with-comfyui`) installs under NVH_HOME and listens on localhost; the `comfyui-power-nodes` pack adds node packs to it.",
        ),
        warning=(
            "launch.sh binds 0.0.0.0:8188 — every interface (LAN), unauthenticated — while it runs; the manual step shows "
            "the 127.0.0.1 variant."
        ),
        estimated_minutes=60,
        estimated_disk_gb=30.0,
        risk="Medium (upstream)",
        last_updated="10/13/2026",
    ),
    Playbook(
        id="dgx-dashboard",
        upstream_folder="playbook-dgx-dashboard",
        title="Monitor Your AI Compute with DGX Dashboard",
        category="system",
        summary="Notebook access, GPU status and update controls in one place",
        source_urls=(_src("playbook-dgx-dashboard"),),
        prerequisites=(
            "DGX Spark (64 GB or 128 GB unified memory) running NVIDIA DGX OS, powered on and reachable",
            "A browser that can reach port 11000 (local or tunnelled); NVIDIA Sync (recommended) or an SSH client",
        ),
        steps=(
            _manual(
                "Access the DGX Dashboard",
                "Open http://localhost:11000 (desktop shortcut) or NVIDIA Sync → DGX Dashboard. Manual tunnel: "
                "`ssh -L 11000:localhost:11000 -L <ASSIGNED_PORT>:localhost:<ASSIGNED_PORT> <USERNAME>@<SPARK_IP>` "
                "with your port from /opt/nvidia/dgx-dashboard-service/jupyterlab_ports.yaml.",
            ),
            _manual("Log into the DGX Dashboard", "Use your system username and password."),
            _manual(
                "Launch a JupyterLab instance",
                "Start → Starting → Preparing → Running (creates ~/jupyterlab with its own venv and requirements.txt; "
                "a new working directory is a new environment).",
            ),
            _manual(
                "Test with a sample AI workload",
                "New notebook → Python 3 → paste the README's Stable Diffusion XL cell (assets/jupyter-cell.py) → run; "
                "the first run downloads the model.",
            ),
            _manual("Monitor GPU utilization; stop JupyterLab", "Both from the dashboard (Stop in the JupyterLab panel)."),
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
        estimated_minutes=30,
        estimated_disk_gb=0.0,
        risk="Low (normal use)",
        last_updated="07/31/2026",
    ),
    Playbook(
        id="vscode",
        upstream_folder="playbook-vscode",
        title="Set Up VS Code for Local and Remote Development",
        category="dev",
        summary="Editing, debugging and terminal access in one development workspace",
        source_urls=(_src("playbook-vscode"),),
        prerequisites=(
            "A DGX Spark (DGX OS, ARM64) with an internet connection; keyboard, monitor and mouse for local desktop use",
            "sudo privileges for the local install; at least 200 MB of disk",
            "(Remote use only) VS Code on your computer and NVIDIA Sync connected to the Spark",
        ),
        steps=(
            _step(
                "Verify the system architecture is arm64",
                "bash", "-c", 'test "$(dpkg --print-architecture)" = arm64',
                check=_VSCODE_INSTALLED,
            ),
            _step(
                "Fetch Microsoft's stable-build manifest",
                "curl", "-fsSL", VSCODE_SHA_MANIFEST_URL, "-o", f"{PLAYBOOK_DIR_TOKEN}/vscode-stable.json",
                check=_VSCODE_INSTALLED,
                description="The manifest lists every current stable build with its sha256 and productVersion; the download below redirects to the same build.",
            ),
            _step(
                "Download the VS Code ARM64 .deb",
                "wget", _VSCODE_DEB_URL, "-O", f"{PLAYBOOK_DIR_TOKEN}/vscode-arm64.deb",
                check=_VSCODE_INSTALLED, timeout=INSTALL_TIMEOUT_S, upstream=_VSCODE_UPSTREAM,
            ),
            _step(
                "Verify the .deb against the manifest's sha256 and record its version",
                *_VSCODE_VERIFY_ARGV, _VSCODE_VERIFY_PY,
                f"{PLAYBOOK_DIR_TOKEN}/vscode-stable.json", f"{PLAYBOOK_DIR_TOKEN}/vscode-arm64.deb",
                f"{PLAYBOOK_DIR_TOKEN}/vscode-version.txt",
                check=_VSCODE_INSTALLED,
                description=(
                    "Compares the download's sha256 with the linux-deb-arm64 product in the manifest and writes its "
                    "productVersion to vscode-version.txt; a mismatch (or a manifest without a sha256) stops the run before apt."
                ),
            ),
            _step(
                "Install VS Code (apt resolves the .deb's dependencies)",
                "apt-get", "install", "-y", f"{PLAYBOOK_DIR_TOKEN}/vscode-arm64.deb",
                sudo=True, check=_VSCODE_INSTALLED, timeout=INSTALL_TIMEOUT_S, upstream=_VSCODE_UPSTREAM,
                description="Upstream runs `sudo apt install ~/vscode-arm64.deb`; `-y` because nothing can answer a prompt (stdin is closed).",
            ),
            _manual(
                "Open a workspace",
                "mkdir -p ~/vscode-workspace && cd ~/vscode-workspace && code .   # needs a desktop session; Terminal > New Terminal",
            ),
            _manual(
                "Validate the setup",
                "In the integrated terminal: `printf 'print(\"Hello from VS Code!\")\\n' > test.py && python3 test.py`; "
                "install extensions that support Linux ARM64.",
            ),
            _manual(
                "Remote use",
                "On your computer, NVIDIA Sync → VS Code opens a Remote-SSH session; check `hostnamectl` and `whoami` in its "
                "terminal. Without Sync: the Remote - SSH extension and `ssh <USER>@<SPARK_HOST>`.",
            ),
        ),
        verify=(
            _dpkg("code"),
            ("code", "--version"),
        ),
        undo=(
            "sudo apt remove code",
            f"rm -f {PLAYBOOK_DIR_TOKEN}/vscode-arm64.deb {PLAYBOOK_DIR_TOKEN}/vscode-stable.json",
            "rm -rf ~/.config/Code ~/.vscode   # settings, cached data and extensions",
        ),
        notes=(
            "Upstream installs the current stable .deb as downloaded. nvHive fetches Microsoft's manifest "
            f"({VSCODE_SHA_MANIFEST_URL}), verifies the download's sha256 against its {VSCODE_PLATFORM} entry and records "
            f"the productVersion under {PLAYBOOK_DIR_TOKEN}/vscode-version.txt before apt installs it.",
            "Unmet dependencies at install time: `sudo apt update`, then run the playbook again (finished steps are skipped).",
        ),
        estimated_minutes=10,
        estimated_disk_gb=0.2,
        risk="Low (upstream); nvHive: installs the vendor .deb as root after verifying Microsoft's published sha256",
        last_updated="07/31/2026",
    ),
    Playbook(
        id="tailscale",
        upstream_folder="playbook-tailscale",
        title="Set Up Tailscale for Remote Access",
        category="network",
        summary="Encrypted SSH from any network without port forwarding or firewall changes",
        source_urls=(_src("playbook-tailscale"),),
        prerequisites=(
            "DGX Spark on DGX OS (Ubuntu noble) with a working package manager (`sudo apt update`) and a user with sudo privileges",
            "Internet connectivity; a Tailscale login (Google, GitHub, Microsoft or another supported identity provider)",
            "A client device (macOS, Windows or Linux), ideally on a different network for the remote test",
        ),
        steps=(
            _step("Update the package list", "apt", "update", sudo=True, check=("systemctl", "is-active", "ssh"), timeout=INSTALL_TIMEOUT_S),
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
            _manual(
                "Connect your Spark to the tailnet",
                "sudo tailscale up   # in a terminal; open the URL it prints and log in (Google, GitHub, Microsoft or another provider)",
            ),
            _manual(
                "Install Tailscale on your client devices and log in to the same tailnet",
                "macOS: App Store or the .pkg; Windows: the .msi; Linux: the same apt steps as above. Log in with the same account.",
            ),
            _manual(
                "Verify network connectivity",
                "On any device: `tailscale status`, then `tailscale ping <SPARK_HOSTNAME>`.",
            ),
            _manual(
                "Configure SSH authentication",
                "Client: `ssh-keygen -t ed25519 -f ~/.ssh/tailscale_remote`; Spark: append the public key to ~/.ssh/authorized_keys "
                "(`chmod 600 ~/.ssh/authorized_keys; chmod 700 ~/.ssh`).",
            ),
            _manual(
                "Test the connection",
                "ssh -i ~/.ssh/tailscale_remote <USERNAME>@<SPARK_HOSTNAME>; then `scp -i ~/.ssh/tailscale_remote test.txt "
                "<USERNAME>@<SPARK_HOSTNAME>:~/` and `ssh -i ~/.ssh/tailscale_remote <USERNAME>@<SPARK_HOSTNAME> 'nvidia-smi'`.",
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
            "sudo systemctl disable ssh --now   # only if this playbook enabled SSH (its step 3 ran)",
            "sudo apt remove openssh-server   # only if this playbook installed it (its step 2 ran)",
        ),
        notes=(
            "The key and the repository list are downloaded to NVH_HOME first and installed with `install -m 0644` (upstream pipes them through `sudo tee`).",
            "`noble` is Ubuntu 24.04 — the DGX OS base. Steps 1-3 are the README's 'Install SSH server (if needed)'; they are skipped while sshd is active.",
        ),
        warning=(
            "Adds the tailscaled service and a tailscale0 interface (100.x address). `tailscale up --accept-dns` / MagicDNS "
            "can change system DNS resolution. When sshd is not already active, steps 1-3 install openssh-server and "
            "enable it at boot on every interface (LAN, not just the tailnet) with DGX OS's default password "
            "authentication; the undo lists how to turn that back off."
        ),
        estimated_minutes=30,
        estimated_disk_gb=0.1,
        risk="Medium (upstream: potential SSH service configuration conflicts)",
        last_updated="07/31/2026",
    ),
    Playbook(
        id="vllm",
        upstream_folder="playbook-vllm",
        title="Serve LLMs with vLLM on One Device",
        category="inference",
        summary="A locally accessible model API from one DGX Spark (the README's 128 GB Qwen3.8 27B NVFP4 row)",
        source_urls=(_src("playbook-vllm"),),
        prerequisites=(
            "One DGX Spark with 128 GB unified memory (the 64 GB rows and the DGX Station rows are in the notes)",
            "Docker available to your user (the first step joins the docker group); the Hugging Face CLI installed "
            "(`hf --help`; https://huggingface.co/docs/huggingface_hub/main/en/guides/cli)",
            "Disk for the container image and the model plus its draft model; HF_TOKEN only for gated models (declared only — nvHive never asks for or stores it)",
            "(Intermediate path) NVIDIA Sync on your laptop, connected to the Spark",
        ),
        steps=(
            _docker_group_step(),
            _step(
                "Check the Hugging Face CLI is installed",
                *_cmd_exists("hf"),
                description="The README's prerequisite; the run stops here when `hf` is missing (install it from the standalone installer first).",
            ),
            _step(
                f"Pull the vLLM container image ({_VLLM_IMAGE})",
                "docker", "pull", _VLLM_IMAGE,
                check=("docker", "image", "inspect", _VLLM_IMAGE), timeout=LONG_TIMEOUT_S,
                description="The README's pinned NGC image for the 128 GB Qwen3.8 27B row.",
            ),
            _step(
                f"Download the model ({_VLLM_MODEL})",
                "hf", "download", _VLLM_MODEL,
                check=("test", "-d", f"{_HF_HUB}/models--nvidia--Qwen3.8-27B-NVFP4"), timeout=LONG_TIMEOUT_S,
                description="Into ~/.cache/huggingface; `HF_HUB_DISABLE_XET=1 hf download …` is faster on some links.",
            ),
            _step(
                f"Download the draft model for speculative decoding ({_VLLM_DRAFT})",
                "hf", "download", _VLLM_DRAFT,
                check=("test", "-d", f"{_HF_HUB}/models--z-lab--Qwen3.8-27B-DFlash2"), timeout=LONG_TIMEOUT_S,
            ),
            *_pinned_download(
                f"the README's launch script ({VLLM_SYNC_SCRIPT_NAME})", VLLM_SYNC_SCRIPT, VLLM_SYNC_SCRIPT_NAME,
                check=("test", "-f", f"{PLAYBOOK_DIR_TOKEN}/{VLLM_SYNC_SCRIPT_NAME}"),
            ),
            _manual(
                "Start the server (foreground script, or an NVIDIA Sync custom app)",
                f"bash {PLAYBOOK_DIR_TOKEN}/{VLLM_SYNC_SCRIPT_NAME}   # runs `docker run --network host … {_VLLM_IMAGE} vllm serve "
                f"{_VLLM_MODEL} --host 127.0.0.1 --port 8000 …` and stops the container when you Ctrl+C. Remote: paste the "
                "script into NVIDIA Sync → Custom → Add New (Name vLLM-Qwen3.8-27B-128GB, Port 8000) and click it.",
            ),
            _manual(
                "Wait for the model to load, then test the API",
                "docker logs --tail 50 --follow vllm-spark-128gb-qwen38-27b; then `curl --fail-with-body -i http://localhost:8000/health` "
                "and `curl --fail-with-body -sS http://localhost:8000/v1/models` (copy the model id for chat requests).",
            ),
            _manual(
                "Serve a different model",
                "Pick another row of the README's table (64 GB: Qwen3.8 27B NVFP4 on vllm/vllm-openai:v0.28.0, or Nemotron 3.5 "
                "Bolt 74B A9B) or a recipe from https://nvidia.github.io/local-ai-inference-recipes/ (Advanced tab).",
            ),
        ),
        verify=(
            ("docker", "image", "inspect", _VLLM_IMAGE),
            ("test", "-d", f"{_HF_HUB}/models--nvidia--Qwen3.8-27B-NVFP4"),
            ("curl", "-sf", "http://localhost:8000/health"),
        ),
        undo=(
            f"docker rm -f {_VLLM_CONTAINER}   # the script removes it on exit; this covers a crash",
            f"docker rmi {_VLLM_IMAGE}",
            "rm -rf ~/.cache/huggingface/hub/models--nvidia--Qwen3.8-27B-NVFP4 ~/.cache/huggingface/hub/models--z-lab--Qwen3.8-27B-DFlash2",
            f"rm -rf {PLAYBOOK_DIR_TOKEN}",
            f"sudo gpasswd -d {USER_TOKEN} docker",
        ),
        notes=(
            f"The launch script is the README's own ({VLLM_SYNC_SCRIPT}), verified against the sha256 nvHive pinned on "
            f"{ASSET_PIN_DATE}; it binds 127.0.0.1:8000 with Docker host networking (NVIDIA Sync forwards the port to your laptop).",
            "The README's other rows: 64 GB Spark — Qwen3.8 27B NVFP4 (vllm/vllm-openai:v0.28.0) or Nemotron 3.5 Bolt 74B A9B "
            "(eugr/spark-vllm:nightly-20261016); 128 GB — Qwen3.8 Flash-Next (eugr/spark-vllm-b12x:nightly-20261015); nvHive "
            "ships the 128 GB row with the pinned NGC image.",
            "The model is cached under ~/.cache/huggingface, so recreating the container does not download it again.",
        ),
        warning=(
            "The Advanced recipes' `--host 0.0.0.0` (or a `-p` mapping) publishes an unauthenticated API on every interface "
            "(LAN): anyone on the network can use the GPU through http://<spark>:8000/v1 while the container runs. The shipped "
            "script binds 127.0.0.1; keep it that way or firewall the port if the Spark shares a network."
        ),
        estimated_minutes=30,
        estimated_disk_gb=40.0,
        risk="Low (upstream); nvHive: a recipe that binds 0.0.0.0 is a LAN-reachable, unauthenticated API",
        last_updated="10/19/2026",
    ),
    Playbook(
        id="llama-cpp",
        upstream_folder="playbook-llama-cpp",
        title="Serve Models with llama.cpp",
        category="inference",
        summary="Efficient inference for any GGUF model with minimal dependencies, behind an OpenAI-compatible API",
        source_urls=(_src("playbook-llama-cpp"),),
        prerequisites=(
            "DGX Spark (64 GB or 128 GB unified memory); about 30 GB free memory for the example model and its KV cache",
            "At least ~40 GB free disk for the example download (about 21 GB) plus build artifacts",
            "Git, CMake 3.18+ and the CUDA Toolkit (`nvcc --version`); network access to GitHub and Hugging Face",
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
                "Start llama-server with a model (foreground; about 21 GB download on first run)",
                f"cd {PLAYBOOK_DIR_TOKEN}/llama.cpp && ./build/bin/llama-server -hf unsloth/Qwen3.6-35B-A3B-MTP-GGUF:UD-Q4_K_XL "
                "--host 0.0.0.0 --port 30000   # `--host 127.0.0.1` keeps it local; agent-ready: add "
                "`--chat-template-kwargs '{\"preserve_thinking\": true}' --spec-type draft-mtp --spec-draft-n-max 3`",
            ),
            _manual(
                "Test the API",
                "timeout 900 bash -c 'until curl -sf http://127.0.0.1:30000/health > /dev/null 2>&1; do sleep 5; done'; then "
                "curl -X POST http://127.0.0.1:30000/v1/chat/completions -H 'Content-Type: application/json' -d "
                "'{\"model\": \"unsloth/Qwen3.6-35B-A3B-MTP-GGUF:UD-Q4_K_XL\", \"messages\": [{\"role\": \"user\", "
                "\"content\": \"New York is a great city because...\"}], \"max_tokens\": 2048}'   # a small max_tokens is spent on reasoning",
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
            "Context: llama.cpp allocates the model's maximum by default; for agentic or coding work the README wants at least "
            "32768 tokens (preferably 100000+), set with `--ctx-size`.",
        ),
        estimated_minutes=30,
        estimated_disk_gb=40.0,
        risk="Low",
        last_updated="10/09/2026",
    ),
    Playbook(
        id="lm-studio",
        upstream_folder="playbook-lm-studio",
        title="Serve LLMs with LM Studio",
        category="inference",
        summary="Local models reachable from your own machine's tools over an encrypted link (llmster, the headless LM Studio)",
        source_urls=(_src("playbook-lm-studio"),),
        prerequisites=(
            "DGX Spark (128 GB unified memory); at least 65 GB memory and 65 GB storage for the example model (70 GB or more recommended)",
            "A client device (Mac, Windows or Linux) to call the API or join LM Link; network access to download packages and models",
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
                "lms server start --port 1234   # upstream adds `--bind 0.0.0.0` for LAN clients; with LM Link you can leave it off",
            ),
            _manual(
                "(Optional) Connect with LM Link",
                "Create a Link at https://lmstudio.ai/link, sign in on the Spark (llmster) and on your laptop; remote models then "
                "appear at localhost:1234 on the laptop (end-to-end encrypted, preview: 2 users, 5 devices each).",
            ),
            _manual(
                "Download and load a model (about 65 GB)",
                "lms get nvidia/nemotron-3.5-super-vl && lms ls && lms load nvidia/nemotron-3.5-super-vl   # the README's "
                "recommended Nemotron 3.5 Super VL; an LM Studio catalogue path, not an Ollama tag (GPT-OSS-120B is its other example)",
            ),
            _manual(
                "Use the LM Studio SDK from your laptop",
                "Download run.js / run.py / run.sh from the README's Ancillary files, replace <SPARK_LOCAL_IP> (or use localhost with "
                "LM Link) and the model name, then `npm install @lmstudio/sdk && node run.js`, `uv run --script run.py` or `bash run.sh`.",
            ),
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
        estimated_minutes=30,
        estimated_disk_gb=65.0,
        risk="Low",
        last_updated="10/21/2026",
    ),
    Playbook(
        id="openclaw",
        upstream_folder="playbook-openclaw",
        title="Run OpenClaw with a Local LLM",
        category="agent",
        summary="Install a local-first AI agent and connect it to a private OpenAI-compatible model endpoint",
        source_urls=(_src("playbook-openclaw"),),
        prerequisites=(
            "DGX Spark (64 GB or 128 GB unified memory) running Linux; terminal (SSH or local) access",
            "A local OpenAI-compatible inference backend — the vllm playbook (Option A, recommended) or Ollama (Option B) — or the "
            "willingness to install one; memory for the model it serves",
            "Network access to download OpenClaw and the model checkpoint",
        ),
        steps=(
            _docker_group_step(),
            _manual(
                "Install OpenClaw (interactive)",
                "curl -fsSL https://openclaw.ai/install.sh | bash   # in a terminal: read the security disclaimer and accept it to proceed",
            ),
            _manual(
                "Complete the OpenClaw onboarding (TUI)",
                "Quick start (recommended) → One agent → Model/auth provider → Skip for now (configure the model after the server is up); "
                "channels, skills and hooks can be added later.",
            ),
            _manual(
                "Serve a local model (separate terminal)",
                f"Option A — vLLM: the vllm playbook (its server answers http://localhost:8000/v1/models). Option B — Ollama: "
                f"`ollama pull {MODEL_CHAT_TOKEN}` and `ollama run {MODEL_CHAT_TOKEN}`, then `/set parameter num_ctx 65536` "
                "(at least 32K; 64K+ with headroom). Upstream's Option B pulls Qwen3.6 27B.",
            ),
            _manual(
                "Point OpenClaw at the local server",
                "Edit ~/.openclaw/openclaw.json: `models.providers.vllm` with baseUrl http://localhost:8000/v1, apiKey `vllm`, "
                "api `openai-responses` (or the chat-completions variant), and the served model id/name — the README's example is "
                "nvidia/Qwen3.6-35B-A3B-NVFP4 (reasoning true, contextWindow 262144, maxTokens 8192); for Ollama use "
                f"http://localhost:11434/v1 and the exact tag ({MODEL_CHAT_TOKEN}). Restart the gateway so it reloads the file.",
            ),
            _manual("Verify the setup", "Open the dashboard URL (with its access token); new conversation; `/model MODEL_NAME` switches models."),
            _manual("Optional: skills", "Enable only skills you trust (sidebar, or Clawhub at https://docs.openclaw.ai/clawhub)."),
        ),
        verify=(
            _cmd_exists("openclaw"),
            ("test", "-f", f"{HOME_TOKEN}/.openclaw/openclaw.json"),
            ("curl", "-sf", "http://localhost:8000/v1/models"),
        ),
        undo=(
            "Stop the OpenClaw gateway; uninstall via the same install script or remove ~/.openclaw (the README's rollback)",
            "Stop the inference server separately (the vllm playbook's undo, or `ollama rm <tag>`)",
            f"sudo gpasswd -d {USER_TOKEN} docker",
        ),
        notes=(
            "Guided: the installer and onboarding are interactive TUIs, so nvHive automates only the docker-group step (needed by "
            "the vLLM backend).",
            "Rootless instead: the `openclaw-agent` studio pack installs OpenClaw under NVH_HOME via npm (config path differs from ~/.openclaw).",
        ),
        warning=(
            "Upstream rates the risk Medium to High: run OpenClaw on a dedicated or isolated system with dedicated accounts, enable "
            "only skills you trust, never expose the web UI or messaging channels publicly without strong authentication (SSH "
            "tunnel or VPN), limit its internet access with firewall rules and monitor its logs."
        ),
        estimated_minutes=30,
        estimated_disk_gb=30.0,
        rootless_alternative="openclaw-agent",
        risk="Medium to High",
        last_updated="10/22/2026",
    ),
    Playbook(
        id="nemoclaw",
        upstream_folder="playbook-nemoclaw",
        title="Run NemoClaw with a Local LLM",
        category="agent",
        summary="Build a local AI assistant in an OpenShell sandbox with vLLM inference and optional Telegram",
        source_urls=(_src("playbook-nemoclaw"),),
        prerequisites=(
            "A DGX Spark (64 GB or 128 GB unified memory) with keyboard and monitor, or SSH access; sudo access (passwordless or interactive)",
            "Fresh install of DGX OS with the latest updates; Docker installed and running, 28.x+ (`docker info --format '{{.ServerVersion}}'`)",
            "Storage for the model download, the vLLM container and temporary download space (large Express models need hundreds of GB)",
            "Optional: a Telegram bot token (@BotFather), a Brave Search API key, an HF read token for the Express download "
            "(declared only — nvHive never asks for or stores them)",
        ),
        steps=(
            _docker_group_step(),
            _manual(
                "Install NemoClaw (interactive; sudo prompts inside)",
                "curl -fsSL https://www.nvidia.com/nemoclaw.sh | bash   # accept the third-party notice; on the Spark "
                "`Choose 1 or 2 [1]:` → 1 (managed vLLM, automatic serving profile) or 2 (Qwen3.6 35B-A3B NVFP4 fixed profile); "
                "`Run express install with these settings? [Y/n]` → Y (managed local vLLM, the maintained Express model, sandbox "
                "my-assistant, Balanced policy)",
            ),
            _manual(
                "Custom onboarding (only if you declined Express Install)",
                "nemoclaw onboard --gpu --name <name>   # agent 1 (OpenClaw), inference, model, sandbox name, Brave (optional), "
                "messaging (prefer No at first), resource profile, policy Balanced",
            ),
            _manual(
                "Open the Web UI",
                "nemoclaw my-assistant dashboard-url --quiet; remote: `ssh -L <port>:127.0.0.1:<port> <user>@<spark-ip>` and open "
                "the URL exactly as printed (127.0.0.1 with its token). Stale forward: `nemoclaw my-assistant recover`.",
            ),
            _manual("Terminal UI", "nemoclaw my-assistant connect, then `openclaw tui` (Ctrl+C to leave; `exit` to leave the sandbox)."),
            _manual(
                "Optional: Brave Search and Telegram",
                "BRAVE_API_KEY=<key> nemoclaw onboard --name <sandbox> --recreate-sandbox --non-interactive; "
                "nemoclaw <sandbox> channels add telegram (the README's Optional — Messaging section).",
            ),
            _manual(
                "Keep it current",
                "nemoclaw update --check, then `nemoclaw update --yes` (host CLI only) and `nemoclaw upgrade-sandboxes --check`.",
            ),
        ),
        verify=(
            _cmd_exists("nemoclaw"),
            ("docker", "info", "--format", "{{.ServerVersion}}"),
            ("curl", "-sf", "http://127.0.0.1:8000/v1/models"),
        ),
        undo=(
            "nemoclaw tunnel stop",
            "nemoclaw my-assistant stop   # keeps the sandbox state; `nemoclaw my-assistant recover` brings it back",
            "nemoclaw uninstall --yes   # add --delete-models to drop the weights; preserves Docker, Node.js and the vLLM image",
            f"sudo gpasswd -d {USER_TOKEN} docker",
        ),
        notes=(
            "Guided: the installer is an interactive Express Install that makes its own sudo host changes (Docker settings, "
            "Node.js 22.16+, OpenShell) after prompting for your password in your terminal; nvHive automates only the docker-group step.",
            "'No GPU detected' during onboarding is expected on some unified-memory platforms; the wizard still uses vLLM.",
            "In QA the managed profile served nvidia/Qwen3.6-35B-A3B-NVFP4 on a 128 GB Spark; `nemoclaw <sandbox> status` shows yours.",
            "Rootless instead: the `nemoclaw-sandbox` studio pack (needs Docker without sudo).",
        ),
        warning=(
            "Upstream wants a clean device with no personal data and calls NemoClaw a demo provided AS IS: the OpenShell "
            "sandbox reduces the agent's risks (data leakage, code execution, unintended actions, prompt injection) but does "
            "not eliminate them."
        ),
        estimated_minutes=60,
        estimated_disk_gb=60.0,
        rootless_alternative="nemoclaw-sandbox",
        risk="Medium",
        last_updated="10/22/2026",
    ),
)

#: Upstream playbooks not shipped in this tier, with the reason (design brief §9), re-read 2026-11-01.
DEFERRED: tuple[DeferredPlaybook, ...] = (
    DeferredPlaybook(
        "sglang", "playbook-sglang", "Serve LLMs with SGLang",
        "the only image the README names is the floating `lmsysorg/sglang:latest-cu130`; pin a tag first",
        last_updated="07/31/2026",
    ),
    DeferredPlaybook(
        "nim-llm", "playbook-nim-llm", "Deploy NVIDIA NIM for LLM Inference",
        "needs an NGC_API_KEY `docker login nvcr.io`, pulls a floating `:latest` NIM image and runs `chmod -R a+w` on "
        "~/.cache/nim and ~/.local/share/nim/workspace; key handling design pending",
        last_updated="09/30/2026",
    ),
    DeferredPlaybook(
        "nemotron", "playbook-nemotron", "Serve Nemotron Nano and Super Models",
        "Nano points `WEIGHTS` at local Nano Omni weights the README never says how to obtain; Super needs the floating "
        "`vllm/vllm-openai:cu130-nightly` image and a gated Hugging Face checkpoint",
        last_updated="07/31/2026",
    ),
    DeferredPlaybook(
        "unsloth", "playbook-unsloth", "Fine-Tune Faster with Unsloth",
        "runs inside an ephemeral `--rm` nvcr.io/nvidia/pytorch:25.11-py3 container and pip-installs Unsloth into it, so "
        "everything is lost on exit; nothing to receipt",
        last_updated="07/31/2026",
    ),
    DeferredPlaybook(
        "connect-two-sparks", "playbook-connect-two-sparks", "Connect Two DGX Sparks for Distributed Workloads",
        "changes host networking (netplan or `ip addr` on the ConnectX-7 link), may create a shared `nvidia` login with "
        "`useradd`/`passwd` (deny-listed) and its discover-sparks script installs a shared SSH key (id_ed25519_shared) on every "
        "node; needs a warning and undo design",
        last_updated="08/12/2026",
    ),
    DeferredPlaybook(
        "connect-to-your-spark", "playbook-connect-to-your-spark", "Use NVIDIA Sync to Connect Remotely to Your AI Compute",
        "laptop-side only (NVIDIA Sync install, its Tailscale toggle, manual ssh); nothing runs on the Spark",
        last_updated="09/24/2026",
    ),
)

_PLAYBOOKS_BY_ID: dict[str, Playbook] = {playbook.id: playbook for playbook in PLAYBOOKS}


def get_playbook(playbook_id: str) -> Playbook | None:
    if not isinstance(playbook_id, str):
        return None
    return _PLAYBOOKS_BY_ID.get(playbook_id.strip().lower())


def deferred() -> list[dict[str, Any]]:
    return [entry.to_dict() for entry in DEFERRED]


def upstream_folders() -> dict[str, str]:
    """``{id: upstream folder}`` for every shipped and deferred playbook (what the drift check snapshots)."""
    out = {playbook.id: playbook.upstream_folder for playbook in PLAYBOOKS}
    out.update({entry.id: entry.upstream_folder for entry in DEFERRED})
    return out


# ────────────────────────────────────────────────────────────────────────────
# Compiling a playbook into a system-settings Plan
# ────────────────────────────────────────────────────────────────────────────


def playbooks_root(home_dir: str | Path | None = None) -> Path:
    """``NVH_HOME/playbooks`` — downloads, clones and receipts' install paths live per playbook underneath."""
    from nvh.integrations.workspace.storage import nvh_home

    return nvh_home(home_dir)[0] / "playbooks"


def _user_home() -> str:
    return Path.home().as_posix()


def _host_os() -> str:
    """The platform facts' ``os`` (``linux`` / ``windows`` / ``macos`` / ``unknown``); never raises."""
    try:
        return str(getattr(ss._facts(), "os", "") or "unknown").strip().lower() or "unknown"
    except Exception:
        return "unknown"


def _texts(playbook: Playbook) -> list[str]:
    """Every string a playbook renders: prerequisites, titles, argv, checks, manual text, halt notes, descriptions, verify, undo, notes."""
    texts: list[str] = [*playbook.prerequisites, *playbook.undo, *playbook.notes]
    for step in playbook.steps:
        texts += [step.title, *step.argv, step.manual or "", step.halt_after, step.description]
        if step.check:
            texts += step.check
    for argv in playbook.verify:
        texts += argv
    return texts


def _uses(playbook: Playbook, *tokens: str) -> bool:
    return any(token in text for text in _texts(playbook) for token in tokens)


def _default_pick(use_case: str) -> lm.LocalModelPick:
    """The table's default pick for ``use_case`` -- the budget ``hive config init`` names before any hardware is known."""
    from nvh.config.settings import _DEFAULT_LOCAL_BUDGET_GB

    return lm.pick(_DEFAULT_LOCAL_BUDGET_GB, use_case) or lm.LOCAL_MODEL_TIERS[0].picks[use_case]


def _model_pick(use_case: str) -> lm.LocalModelPick:
    """This host's ``use_case`` pick from the tier table, sized from the platform facts.

    A unified pool (GB10 / DGX Spark, Apple Silicon) is planned through
    :func:`nvh.core.local_models.tier_budget` -- the pool minus its OS reserve,
    the same maths ``nvh.utils.gpu`` uses -- so a 128 GB Spark gets the top
    pick that fits. Facts that size no pool (a discrete card the facts do not
    measure, no GPU, a pool of 0 GB, a probe that failed) fall back to
    :func:`_default_pick`. Never raises: the catalogue must render on any box.
    """
    try:
        facts = ss._facts()
        total_gb = float(getattr(facts, "memory_total_gb", 0.0) or 0.0)
        if getattr(facts, "unified_memory", False) and total_gb > 0:
            pool = SimpleNamespace(vram_mb=total_gb * 1024, unified_memory=True)
            chosen = lm.pick(lm.tier_budget([pool]), use_case)
            if chosen is not None:
                return chosen
    except Exception:  # the facts are a sizing hint here, not a requirement
        logger.debug("platform facts unavailable; rendering the default %s pick", use_case, exc_info=True)
    return _default_pick(use_case)


def _base_context(playbook: Playbook, home_dir: str | Path | None = None) -> dict[str, str]:
    """The placeholders that always render: paths, home and -- where the playbook names one -- the model picks."""
    ctx = {
        # POSIX form: argv never sees a shell, and the Spark is Linux; on a
        # Windows dev box this keeps the rendered commands quoting-free.
        PLAYBOOK_DIR_TOKEN: (playbooks_root(home_dir) / playbook.id).as_posix(),
        HOME_TOKEN: _user_home(),
        USER_TOKEN: "",
    }
    for use_case, (tag_token, gb_token) in _MODEL_TOKENS.items():
        if _uses(playbook, tag_token, gb_token):
            chosen = _model_pick(use_case)
            ctx[tag_token] = chosen.tag
            ctx[gb_token] = f"{chosen.weights_gb:g}"
    return ctx


def _context(playbook: Playbook, home_dir: str | Path | None = None) -> dict[str, str]:
    ctx = _base_context(playbook, home_dir)
    if _uses(playbook, USER_TOKEN):
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
    return [f"{_render(step.title, ctx)} — {_render(step.manual or '', ctx)}" for step in playbook.manual_steps()]


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
            notes.append(f"After '{_render(step.title, ctx)}' runs, the run stops. MANUAL: {_render(step.halt_after, ctx)}")
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
            "title": _render(pstep.title, ctx),
            "command": step.render(),
            "sudo": step.sudo,
            "check": shlex.join(_render_argv(pstep.check, ctx)) if pstep.check else None,
            "idempotent": pstep.idempotent,
            "unpinned": pstep.unpinned,
            "upstream": pstep.upstream,
            "timeout_s": pstep.timeout_s,
            "halts_run": bool(pstep.halt_after),
            "description": _render(pstep.description, ctx),
        })
    return out


Resolved = tuple[Playbook, dict[str, str], Plan]


def _refusal(error: str, **extra: Any) -> dict[str, Any]:
    """The one refusal shape every entry point returns: ``ok``/``applied`` False, the error, an empty ``commands``."""
    return {"ok": False, "applied": False, "error": error, "commands": [], **extra}


def _platform_refusal(playbook: Playbook) -> dict[str, Any] | None:
    """The plain-words refusal for a host that is not Linux, else ``None``.

    The steps are apt, systemd and docker and the Spark runs DGX OS; planning
    them on Windows or macOS would show commands that cannot run there, and
    running them would fail one spawn at a time. The catalogue (``list``)
    still renders everywhere -- it is read-only.
    """
    os_name = _host_os()
    if ss.system_changes_unavailable(os_name) is None:
        return None
    # The same labels the privileged tier's sentence uses, so one card never names the host two ways.
    what = ss._OS_LABELS.get(os_name) or ("an OS it could not determine" if os_name == "unknown" else f"'{os_name}'")
    return _refusal(
        UNSUPPORTED_HOST_ERROR.format(what=what, id=playbook.id),
        id=playbook.id, playbook=playbook.id, unsupported_host=True, host_os=os_name,
    )


def _resolve(playbook_id: str, home_dir: str | Path | None) -> Resolved | dict[str, Any]:
    """Look the id up, refuse off-Linux, render the context and compile the plan — or the refusal dict that explains why not.

    Shared by :func:`plan_dict`, :func:`start_run` and :func:`run_in_terminal`
    so an unknown id, a non-Linux host or an unplannable login name is
    answered the same way everywhere and the plan is compiled once per call.
    The deny list is *not* applied here: ``plan_dict`` shows a denied plan
    with its reason (``Plan.to_dict`` carries it), while the runners refuse
    through :func:`_denied`.
    """
    playbook = get_playbook(playbook_id)
    if playbook is None:
        return _refusal(
            f"unknown playbook '{str(playbook_id)[:60]}'. Catalogue: {', '.join(_PLAYBOOKS_BY_ID)}",
            playbooks=list(_PLAYBOOKS_BY_ID),
        )
    refused = _platform_refusal(playbook)
    if refused is not None:
        return refused
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
        "upstream_folder": playbook.upstream_folder,
        "category": playbook.category,
        "summary": playbook.summary,
        "prerequisites": [_render(item, ctx) for item in playbook.prerequisites],
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
            ctx = {**_base_context(playbook, home_dir), USER_TOKEN: "<user>"}
        receipt = by_item.get(playbook.id)
        executable = playbook.executable_steps()
        manual = _manual_lines(playbook, ctx)
        rows.append({
            "id": playbook.id,
            "upstream_folder": playbook.upstream_folder,
            "title": playbook.title,
            "category": playbook.category,
            "summary": playbook.summary,
            "source_urls": list(playbook.source_urls),
            "requires_sudo": playbook.requires_sudo,
            "sudo_steps": playbook.sudo_steps,
            "sudo_step_titles": [_render(step.title, ctx) for step in executable if step.sudo],
            "steps_total": len(executable),
            "manual_steps": len(manual),
            "manual": manual,
            "unpinned": playbook.unpinned,
            "prerequisites": [_render(item, ctx) for item in playbook.prerequisites],
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
CancelRequested = Callable[[], bool]


async def _host_run(step: Step, *, should_stop: CancelRequested | None = None) -> dict[str, Any]:
    """One step on the host, in a worker thread, through the killable runner (the job path).

    ``should_stop`` is the job's cancel flag: the thread polls it every
    ``CANCEL_POLL_S`` seconds while the command runs and kills the command's
    process group (SIGTERM, ``KILL_GRACE_S``, SIGKILL) when it goes up.
    """
    return await asyncio.to_thread(
        run_host_command_killable, list(step.argv), sudo=step.sudo, sudo_user=step.sudo_user, timeout=step.timeout,
        should_stop=should_stop, poll_s=CANCEL_POLL_S, grace_s=KILL_GRACE_S,
    )


def _flag_up(cancel_requested: CancelRequested | None, playbook_id: str) -> bool:
    """Read the job's cancel flag; ``False`` without one, and when it cannot be read (that is not a cancel)."""
    if cancel_requested is None:
        return False
    try:
        return bool(cancel_requested())
    except Exception:
        logger.debug("cancel flag unreadable for playbook '%s'", playbook_id, exc_info=True)
        return False


def _settle_bound() -> float:
    """How long the cancel handler waits for the runner thread's report: the runner's own worst case, plus slack.

    The runner's side (:func:`system_settings.stop_worst_case_s`): one
    poll, the SIGTERM grace, the SIGKILL grace, the root escalation and
    its grace, the pipe wait. The slack covers the thread hand-off, the
    output clean-up and event-loop latency, so a report that arrives at
    the runner's worst case is still taken rather than dropped.
    """
    return ss.stop_worst_case_s(poll_s=CANCEL_POLL_S, grace_s=KILL_GRACE_S) + 2 * CANCEL_POLL_S + 2.0


async def _settle_in_flight(
    pending: asyncio.Future[dict[str, Any]] | None, timeout: float,
) -> tuple[dict[str, Any] | None, int]:
    """The runner's result once the cancelled step's thread reports back, and how many repeat cancels were absorbed.

    Never cancels ``pending`` -- the thread is killing the command and must
    finish its account. A repeat cancel of the task while waiting (the
    Cancel button pressed again, the endpoint called twice) is absorbed and
    counted so the wait still ends with the runner's report or at
    ``timeout``; the caller uncancels the task once per absorbed cancel.
    The result is ``None`` when the runner did not report in time.
    """
    if pending is None:
        return None, 0
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    swallowed = 0
    while not pending.done():
        remaining = deadline - loop.time()
        if remaining <= 0:
            break
        try:
            await asyncio.wait({pending}, timeout=remaining)
        except asyncio.CancelledError:
            swallowed += 1
    if not pending.done() or pending.cancelled() or pending.exception() is not None:
        return None, swallowed
    result = pending.result()
    return (result if isinstance(result, dict) else None), swallowed


def _forget(pending: asyncio.Future[Any] | None) -> None:
    """Let a shielded step future finish on its own, without an "exception was never retrieved" warning."""
    if pending is not None and not pending.done():
        pending.add_done_callback(lambda fut: fut.cancelled() or fut.exception())


def _killed_message(step_number: int, command: str, how: str) -> str:
    """The mid-step cancel wording: the command was running and the runner killed its process group."""
    if how == "SIGKILL":
        ending = f"ignored SIGTERM for {KILL_GRACE_S:g}s and was killed with SIGKILL"
    else:
        ending = "was stopped with SIGTERM"
    return f"canceled by the user — stopped during step {step_number} (command killed); `{command}` {ending}"


def _kill_failed_message(step_number: int, title: str, command: str, reason: str) -> str:
    """The wording when the runner signalled the command but cannot vouch that it stopped."""
    return (
        f"canceled by the user during step {step_number} ({title}) — `{command}` was signalled (SIGTERM, then SIGKILL) "
        f"but {reason}; it may still be running on the host"
    )


def _timed_out_message(step_number: int, title: str, command: str, error: str) -> str:
    """The wording when the step's own deadline killed the command as the cancel landed."""
    return f"canceled by the user during step {step_number} ({title}) — `{command}` {error} and was killed by its deadline"


def _unsettled_row(in_flight: Mapping[str, Any], result: Mapping[str, Any], **marks: Any) -> dict[str, Any]:
    """The executed-steps row of a command the cancel did not stop for sure (``kill_failed`` / ``timed_out`` marks)."""
    return {
        **in_flight, "command": result.get("command") or in_flight["command"], "exit_code": result.get("exit_code"),
        "stdout": result.get("stdout", ""), "stderr": result.get("stderr", ""), "canceled": True, **marks,
    }


def _killed_row(in_flight: Mapping[str, Any], result: Mapping[str, Any]) -> dict[str, Any]:
    """The executed-steps row of a killed command: what ran, the exit code the signal left, the output that arrived."""
    return {
        **in_flight, "command": result.get("command") or in_flight["command"], "exit_code": result.get("exit_code"),
        "stdout": result.get("stdout", ""), "stderr": result.get("stderr", ""), "canceled": True, "killed": result["killed"],
    }


def _killed_in_flight(in_flight: Mapping[str, Any], result: Mapping[str, Any]) -> dict[str, Any]:
    """The receipt's ``in_flight`` record of a killed command: the step as the card showed it, plus how it ended."""
    return {**in_flight, "killed": result["killed"], "exit_code": result.get("exit_code")}


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
    done) must not turn an earlier ``no_root: False`` back into True. A
    cancelled run records ``stopped_after_step`` (how many steps had
    finished) and ``in_flight`` (the command that was running -- with
    ``killed`` and the signal's exit code when the runner killed it -- or
    ``None`` when the cancel landed between steps); a killed step's row
    carries ``canceled`` and ``killed`` too, one the signals did not stop
    for sure ``kill_failed``, one its deadline killed ``timed_out``.
    """
    try:
        try:
            previous = _receipts.load_receipt(_receipts.receipt_id(RECEIPT_KIND, playbook.id), home_dir=home_dir)
        except Exception:
            previous = {}
        no_root = no_root and bool(previous.get("no_root", True))
        metadata: dict[str, Any] = {
            "outcome": result.get("outcome"),
            # What the playbook *needs*, as opposed to ``no_root`` (what ran): a partial run
            # that stopped before its first sudo step is still not repairable without root.
            "requires_sudo": bool(result.get("requires_sudo")),
            "steps": [
                {
                    "title": s.get("title"), "command": s.get("command"), "exit_code": s.get("exit_code"),
                    **({"canceled": True} if s.get("canceled") else {}),
                    **({"killed": s["killed"]} if s.get("killed") else {}),
                    **({"kill_failed": s["kill_failed"]} if s.get("kill_failed") else {}),
                    **({"timed_out": True} if s.get("timed_out") else {}),
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
            "upstream_folder": playbook.upstream_folder,
        }
        if result.get("outcome") == "canceled":
            metadata["stopped_after_step"] = result.get("stopped_after_step")
            metadata["in_flight"] = result.get("in_flight")
        _receipts.write_receipt(
            kind=RECEIPT_KIND,
            item_id=playbook.id,
            title=playbook.title,
            install_path=ctx[PLAYBOOK_DIR_TOKEN],
            status=status,
            source_urls=list(playbook.source_urls),
            no_root=no_root,
            metadata=metadata,
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
    stopped_after: int | None = None,
    in_flight: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], str | None, dict[str, Any] | None]:
    """Build the apply-shaped result and write the receipt and the vault note. Synchronous, never raises.

    Called on every way a run ends — complete, halted, failed, hand-off, a
    job cancelled between steps and a job cancelled mid-step — so a run that
    touched the host always leaves a receipt (kind ``playbook``) and a
    ``Decisions/`` note behind, whatever path it took out of the engine.
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
        result["stopped_after_step"] = stopped_after
        result["in_flight"] = dict(in_flight) if in_flight is not None else None

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


def _stopped_message(stopped_after: int, total: int) -> str:
    """The between-steps cancel wording: what finished, and that nothing was running."""
    if stopped_after <= 0:
        return f"canceled by the user — stopped before the first of {total} step(s); no command was in flight"
    return f"canceled by the user — stopped after step {stopped_after} of {total}; no command was in flight"


async def _engine(
    playbook: Playbook,
    ctx: Mapping[str, str],
    plan: Plan,
    *,
    run_cmd: RunCommand,
    check_cmd: CheckCommand,
    home_dir: str | Path | None,
    mode: str,
    cancel_requested: CancelRequested | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """Run the steps in order; skip on ``check``; stop at the first refusal, hand-off, halt, failure or cancel.

    Events (each with ``playbook``): ``plan`` → per step ``step`` (running) →
    optional ``log`` → ``step`` (complete / failed, ``skipped`` when the check
    passed) → ``needs_terminal`` when sudo needs a password → finally
    ``complete`` (``applied``, ``partial``, ``halted``), ``error``
    (``needs_terminal`` / ``denied`` / the exit code) or ``canceled``. The
    receipt and the vault audit are written before the final event.

    Cancel (W1-lite, decision 16; the 0.46 kill): ``cancel_requested`` --
    the job's flag, re-read from disk -- is consulted before each check
    probe, after it (so a flag raised while the probe ran never starts the
    command) and after each step, whether it passed or failed; when it is up
    the run stops there and the receipt says ``stopped after step N`` with
    ``in_flight: None``. While a host command runs, the job path's runner
    (:func:`~nvh.integrations.wizard.system_settings.run_host_command_killable`)
    polls the same flag from its worker thread and kills the command's
    process group when it goes up; its result then says ``killed`` and the
    run is a cancel ``stopped during step N (command killed)``, recorded with
    the exit code the signal left. The cancel itself lands as
    ``CancelledError`` at the shielded ``await`` of the step
    (:func:`jobs.cancel_job` sets the flag, then cancels the consumer task):
    the handler waits -- bounded by :func:`_settle_bound` -- for the runner's
    report, records it (a command that finished before the kill landed is
    recorded like a flag read after a step) and yields the ``canceled``
    event itself, so the job log keeps this account rather than the job's
    generic line. A task cancelled without the flag (a server shutdown) does
    not wait: the command is left alone, recorded as in flight -- it may
    finish on the host -- and the cancellation is re-raised, as before. A
    consumer that closes the stream at a yield instead (the job
    consumer saw the flag before this engine's next read of it) lands here
    as ``GeneratorExit``: the receipt and the note are written from that
    handler too, so a run that touched the host always leaves them, whatever
    path it took out. ``ctx`` and ``plan`` come from :func:`_resolve` — what
    the card showed.
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

    def stop_requested() -> bool:
        return _flag_up(cancel_requested, playbook.id)

    executed: list[dict[str, Any]] = []
    verify_results: list[dict[str, Any]] = []
    any_sudo_ran = False
    outcome = "complete"
    error_msg = ""
    remaining = 0
    position = 0  # the step being worked on (for the cancel record)
    title = ""
    settled = 0  # steps finished or skipped so far: where a stream closed at a yield stopped the run
    stopped_after: int | None = None  # steps finished when a cancel stopped the run between steps
    in_flight: dict[str, Any] | None = None  # set while a host command runs (the record a cancel lands on)
    pending: asyncio.Future[dict[str, Any]] | None = None  # the running step, shielded so a cancel cannot lose its result
    killed_record: dict[str, Any] | None = None  # the in-flight record of a command a cancel killed (or left running)
    cancel_record: tuple[dict[str, Any], str | None, dict[str, Any] | None] | None = None  # written by the cancel handler
    try:
        for index, (pstep, step) in enumerate(zip(executable, plan.steps, strict=True)):
            position, title = index, _render(pstep.title, ctx)
            settled = index
            if stop_requested():
                outcome, stopped_after, remaining = "canceled", index, total - index
                error_msg = _stopped_message(index, total)
                break
            command = step.render()
            head = {**base, "step": index, "steps_total": total, "title": title, "command": command, "sudo": step.sudo}
            yield {**head, "event": "step", "status": "running", "message": f"Step {index + 1}/{total}: {title}"}
            if pstep.check:
                probe = await check_cmd(_render_argv(pstep.check, ctx))
                if probe.get("ok"):
                    settled = index + 1
                    yield {**head, "event": "log", "status": "running", "message": f"already done — skipped: {title}", "skipped": True}
                    yield {**head, "event": "step", "status": "complete", "skipped": True, "message": f"Step {index + 1}/{total} skipped (already done)"}
                    continue
                if stop_requested():  # raised while the probe ran: never start the command
                    outcome, stopped_after, remaining = "canceled", index, total - index
                    error_msg = _stopped_message(index, total)
                    break
            in_flight = {"step": index, "title": title, "command": command, "sudo": step.sudo}
            # Shielded: a cancel that lands here must not take the runner's result with it -- the
            # thread goes on to kill the command, and its report is what the cancel handler waits for.
            pending = asyncio.ensure_future(run_cmd(step))
            result = await asyncio.shield(pending)
            running, in_flight, pending = in_flight, None, None
            if result.get("killed"):
                # The runner polls the same flag and killed the command itself (the flag went up on disk
                # without the task being cancelled): a cancel stopped during this step, recorded with the
                # exit code the signal left.
                outcome, stopped_after, remaining = "canceled", index, total - index
                error_msg = _killed_message(index + 1, result["command"], result["killed"])
                executed.append(_killed_row(running, result))
                killed_record = _killed_in_flight(running, result)
                if step.sudo:
                    any_sudo_ran = True
                break
            if result.get("kill_failed"):
                # The runner saw the flag and signalled the command but cannot vouch that it stopped (a
                # root-owned command behind sudo, say): a cancel, recorded as possibly still running.
                outcome, stopped_after, remaining = "canceled", index, total - index
                error_msg = _kill_failed_message(index + 1, title, result.get("command") or command, result["kill_failed"])
                executed.append(_unsettled_row(running, result, kill_failed=result["kill_failed"]))
                killed_record = {**running, "kill_failed": result["kill_failed"], "exit_code": result.get("exit_code")}
                if step.sudo:
                    any_sudo_ran = True
                break
            if result.get("needs_terminal"):
                outcome = "needs_terminal"
                remaining = total - index
                error_msg = f"sudo needs a password for step {index + 1} ({title}) — run `{handoff}` in a terminal"
                yield {
                    **head, "event": "needs_terminal", "status": "running", "message": error_msg,
                    "command": handoff, "step_command": result.get("command"), "hint": ss.NEEDS_TERMINAL_HINT,
                }
                break
            if "exit_code" not in result:
                outcome = "failed"
                remaining = total - index
                error_msg = str(result.get("error") or "command did not run")
                if result.get("timed_out") and step.sudo:
                    any_sudo_ran = True  # it ran until its deadline: no_root stays honest
                yield {**head, "event": "step", "status": "failed", "message": error_msg, "error": error_msg, "denied": bool(result.get("denied"))}
                break
            executed.append({
                "step": index, "title": title, "command": result["command"], "exit_code": result["exit_code"],
                "stdout": result.get("stdout", ""), "stderr": result.get("stderr", ""),
            })
            settled = index + 1
            if step.sudo:
                any_sudo_ran = True
            if stop_requested():
                # The step finished (or failed) while the flag went up: record it (output included)
                # before the next yield, because the job consumer ends the stream at the first event
                # it sees after a cancel; nothing else starts. A failed step is still a cancel — the
                # user asked for one — and its exit code is in the record and the message.
                outcome, stopped_after, remaining = "canceled", index + 1, total - index - 1
                error_msg = _stopped_message(index + 1, total)
                if not result["ok"]:
                    error_msg += f"; step {index + 1} ({title}) had exited {result['exit_code']}"
                break
            tail = _tail(result)
            if tail:
                yield {**head, "event": "log", "status": "running", "message": tail, "exit_code": result["exit_code"]}
            if not result["ok"]:
                outcome = "failed"
                remaining = total - index - 1
                error_msg = f"`{result['command']}` exited {result['exit_code']}"
                yield {**head, "event": "step", "status": "failed", "message": error_msg, "error": error_msg, "exit_code": result["exit_code"]}
                break
            yield {**head, "event": "step", "status": "complete", "message": f"Step {index + 1}/{total} done: {title}", "exit_code": 0}
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
        # The job was cancelled while this was awaiting a host command or a
        # probe. The one await allowed here is the bounded wait for the
        # runner's report, and only when the job's own flag is up:
        # ``cancel_job`` sets it before cancelling the task, so the runner
        # thread -- polling that flag -- is killing the command's process
        # group right now and will say how it went. Without the flag (the
        # task was cancelled from outside the job: a server shutdown) the
        # command is left alone and recorded as in flight, as before.
        outcome = "canceled"
        remaining = max(0, total - position)
        stopped_after = position
        user_cancel = stop_requested()
        swallowed = 0  # repeat cancels absorbed while waiting for the runner's report
        if position >= total:
            error_msg = f"canceled by the user during verification; all {total} step(s) had finished"
        elif in_flight is not None:
            settled, swallowed = (await _settle_in_flight(pending, _settle_bound())) if user_cancel else (None, 0)
            spawned = True
            if settled is not None and settled.get("killed"):
                error_msg = _killed_message(position + 1, settled["command"], settled["killed"])
                executed.append(_killed_row(in_flight, settled))
                killed_record = _killed_in_flight(in_flight, settled)
            elif settled is not None and settled.get("kill_failed"):
                # Signalled, but not shown to have stopped: the honest record is "may still be running".
                error_msg = _kill_failed_message(
                    position + 1, title, settled.get("command") or in_flight["command"], settled["kill_failed"],
                )
                executed.append(_unsettled_row(in_flight, settled, kill_failed=settled["kill_failed"]))
                killed_record = {**in_flight, "kill_failed": settled["kill_failed"], "exit_code": settled.get("exit_code")}
            elif settled is not None and settled.get("timed_out"):
                # The step's own deadline fired as the cancel landed: the command ran until then and the
                # deadline killed it. It did run (a sudo step counts), so the row and the receipt say so.
                error_msg = _timed_out_message(
                    position + 1, title, settled.get("command") or in_flight["command"], str(settled.get("error") or "timed out"),
                )
                executed.append(_unsettled_row(in_flight, settled, timed_out=True))
                killed_record = {**in_flight, "timed_out": True, "exit_code": None}
            elif settled is not None and "exit_code" in settled:
                # The command finished before the kill landed: the record a flag read after the step leaves.
                executed.append({
                    "step": position, "title": title, "command": settled["command"], "exit_code": settled["exit_code"],
                    "stdout": settled.get("stdout", ""), "stderr": settled.get("stderr", ""),
                })
                stopped_after, remaining = position + 1, total - position - 1
                error_msg = _stopped_message(position + 1, total)
                if not settled.get("ok"):
                    error_msg += f"; step {position + 1} ({title}) had exited {settled['exit_code']}"
            elif settled is not None:
                spawned = False  # refused (denied, needs a terminal, not found) as the cancel landed: nothing ran
                error_msg = _stopped_message(position, total)
            else:
                _forget(pending)
                error_msg = (
                    f"canceled by the user during step {position + 1} ({title}) — `{in_flight['command']}` was already "
                    "running and may have completed on the host"
                )
                executed.append({**in_flight, "exit_code": None, "stdout": "", "stderr": "", "canceled": True})
                killed_record = dict(in_flight)
            if spawned and in_flight["sudo"]:
                any_sudo_ran = True  # a sudo command was spawned; assume it ran
        else:
            error_msg = (
                f"canceled by the user during step {position + 1} ({title}) while its check probe ran; "
                "no host command was in flight"
            )
        cancel_record = _record_run(
            playbook, ctx, plan, outcome=outcome, error_msg=error_msg, remaining=remaining, executed=executed,
            any_sudo_ran=any_sudo_ran, verify_results=verify_results, manual=manual, mode=mode, home_dir=home_dir,
            stopped_after=stopped_after, in_flight=killed_record,
        )
        if not user_cancel:
            raise
        # The job's own cancel is honoured: the receipt is written and the command stopped (or recorded as
        # left running). The engine's account goes out as the ``canceled`` event below -- the job consumer
        # keeps a source's own ``canceled`` payload -- instead of the generic line a bare CancelledError
        # would make it write; the task is told its cancellation was handled.
        task = asyncio.current_task()
        if task is not None:
            for _ in range(1 + swallowed):  # the cancel that landed here, plus any absorbed while waiting
                task.uncancel()
    except GeneratorExit:
        # The consumer closed the stream at one of the yields above (the job
        # consumer saw ``cancel_requested`` before this engine's next read of
        # it, or stopped reading). No command is in flight at a yield and
        # nothing may await here: record what ran -- a failed, halted or
        # hand-off step keeps its own outcome, an unfinished run is a cancel
        # stopped after the steps settled so far -- so the receipt and the
        # vault note exist, then let the close continue.
        if outcome == "complete":
            outcome = "canceled"
            if position >= total:
                stopped_after, remaining = total, 0
                error_msg = f"canceled by the user during verification; all {total} step(s) had finished"
            else:
                stopped_after, remaining = settled, total - settled
                error_msg = _stopped_message(settled, total)
        _record_run(
            playbook, ctx, plan, outcome=outcome, error_msg=error_msg, remaining=remaining, executed=executed,
            any_sudo_ran=any_sudo_ran, verify_results=verify_results, manual=manual, mode=mode, home_dir=home_dir,
            stopped_after=stopped_after, in_flight=None,
        )
        raise

    if cancel_record is not None:
        result, receipt_path, audit = cancel_record
    else:
        result, receipt_path, audit = _record_run(
            playbook, ctx, plan, outcome=outcome, error_msg=error_msg, remaining=remaining, executed=executed,
            any_sudo_ran=any_sudo_ran, verify_results=verify_results, manual=manual, mode=mode, home_dir=home_dir,
            stopped_after=stopped_after, in_flight=killed_record,
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
    elif outcome == "canceled":
        yield {
            **final, "event": "canceled", "status": "canceled", "canceled": True, "stopped_after_step": stopped_after,
            "in_flight": killed_record, "message": error_msg, "error": error_msg,
        }
    else:
        yield {**final, "event": "error", "status": "failed", "message": error_msg, "error": error_msg}


async def run_playbook_events(
    playbook: Playbook, *, home_dir: str | Path | None = None, resolved: Resolved | None = None,
    cancel_requested: CancelRequested | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """The job runner's event source: host commands via :func:`run_host_command` (``sudo -n`` or ``needs_terminal``).

    ``resolved`` is the ``(playbook, ctx, plan)`` :func:`start_run` already
    compiled and showed on the card; without it the playbook is resolved here
    and a refusal becomes the one ``error`` event. ``cancel_requested`` reads
    the job's flag: the engine stops between steps on it, and the runner
    thread polls it while a command runs so it can kill the command's process
    group (:func:`_host_run`).
    """
    if resolved is None:
        found = _resolve(playbook.id, home_dir)
        if isinstance(found, dict):
            error = found["error"]
            yield {"playbook": playbook.id, "event": "error", "status": "failed", "message": error, "error": error, "applied": False, "partial": False}
            return
        resolved = found
    _, ctx, plan = resolved

    async def run_cmd(step: Step) -> dict[str, Any]:
        return await _host_run(step, should_stop=lambda: _flag_up(cancel_requested, playbook.id))

    async for event in _engine(
        playbook, ctx, plan, run_cmd=run_cmd, check_cmd=_host_check, home_dir=home_dir, mode="job",
        cancel_requested=cancel_requested,
    ):
        yield event


def _jobs_module() -> Any:
    from nvh.integrations.services import jobs

    return jobs


def _job_cancel_requested(job_id: str | None) -> bool:
    """The job record's ``cancel_requested`` flag, re-read from disk; ``False`` when unknown or unreadable."""
    if not job_id:
        return False
    try:
        job = _jobs_module().load_job(job_id, reconcile=False, include_recent_events=False)
    except Exception:
        return False
    return bool(job.get("cancel_requested"))


def start_run(playbook_id: str, *, home_dir: str | Path | None = None) -> dict[str, Any]:
    """Start a ``playbook-run`` job (needs a running event loop). ``{ok, job_id, playbook, steps_total}``.

    ``applied`` is ``False`` on purpose: the registry must not audit the
    *start*; the runner writes the receipt and the vault note when the run
    finishes, having seen what actually ran. The engine reads the job's own
    ``cancel_requested`` flag between steps, and the runner thread polls it
    while a command runs (the id is known once the job exists; the task only
    starts on the next loop iteration).
    """
    resolved = _resolve(playbook_id, home_dir)
    if isinstance(resolved, dict):
        return resolved
    playbook, _ctx, plan = resolved
    refused = _denied(playbook, plan)
    if refused:
        return refused
    pending: dict[str, str] = {}
    job = _jobs_module().start_job(
        kind=JOB_KIND,
        title=f"Install the {playbook.id} playbook",
        request={"playbook": playbook.id, "commands": plan.commands(), "sudo": plan.needs_sudo},
        source_factory=lambda: run_playbook_events(
            playbook, home_dir=home_dir, resolved=resolved,
            cancel_requested=lambda: _job_cancel_requested(pending.get("id")),
        ),
    )
    pending["id"] = job["id"]
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
    nothing runs. Refuses off-Linux like every entry point. Writes the same
    receipt and vault note as the job path. Returns the final ``complete`` /
    ``error`` event plus ``events`` (all of them) and ``ok``.
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
    host_os = _host_os()
    out: dict[str, Any] = {
        "ok": True,
        "playbooks": catalogue(),
        "count": len(PLAYBOOKS),
        "deferred": deferred(),
        "privileged_tool": PRIVILEGED_TOOL,
        "handoff_command": HANDOFF_COMMAND.format(id="<id>"),
        "upstream_tree": UPSTREAM_TREE,
        # Off-Linux the catalogue still lists, but plan and install refuse (see ``_platform_refusal``);
        # the predicate is the privileged tier's own, not a second spelling of it.
        "host_os": host_os,
        "supported_host": ss.system_changes_unavailable(host_os) is None,
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
    "description": "Playbook id: " + ", ".join(_PLAYBOOKS_BY_ID) + ".",
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
            "logins, TUIs, foreground servers), how to verify, how to undo, estimates and warnings. Runs nothing; refuses "
            "in plain words on a host that is not Linux. Show it to the user before playbook_install."
        ),
        safety_class="auto",
        parameters={"id": _ID_PARAM},
        handler=_tool_playbook_plan,
        summary_template="Plan the {id} playbook.",
    ))
    reg.register(WizardTool(
        name="playbook_install",
        description=(
            "Install a Spark playbook on this machine (steps may use sudo; Linux only). The user approves the exact commands "
            "on a red card first; the run then starts as a background job (kind playbook-run) whose events stream from "
            "/v1/jobs/<job_id>. Steps already done are skipped; if sudo needs a password the job stops and hands the "
            "user one command to run in a terminal: nvh playbook install <id>. A cancel stops the run after the current "
            "step. Every run that touches the host writes an install receipt and a vault Decisions note."
        ),
        safety_class="privileged",
        parameters={"id": _ID_PARAM},
        handler=_tool_playbook_install,
        planner=_tool_playbook_plan,
        summary_template="Install the {id} playbook",
    ))
