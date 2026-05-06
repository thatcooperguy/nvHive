"""ComfyUI install, status, and example-pack helpers.

The web setup wizard uses this module to install a local ComfyUI workspace
under ``NVH_HOME/comfyui`` without polluting the user's system Python.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import time
from collections.abc import AsyncIterator
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from nvh.integrations.storage import storage_layout

COMFYUI_REPO_URL = "https://github.com/comfyanonymous/ComfyUI.git"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8188


@dataclass(frozen=True)
class ComfyUIExample:
    """Curated workflow template surfaced in the nvHive setup UI."""

    id: str
    title: str
    category: str
    install_profile: str
    recommended_vram_gb: int
    why_trending: str
    workflow_hint: str
    source_url: str
    models: list[str]
    custom_nodes: list[str]
    notes: list[str]


TRENDING_COMFYUI_EXAMPLES: list[ComfyUIExample] = [
    ComfyUIExample(
        id="z-image-turbo-text-to-image",
        title="Z-Image-Turbo Text to Image",
        category="text-to-image",
        install_profile="starter",
        recommended_vram_gb=8,
        why_trending="Prominent in the official ComfyUI popular template gallery.",
        workflow_hint="Workflows > Browse Templates > Image > Z-Image-Turbo Text to Image",
        source_url="https://www.comfy.org/workflows/comfyui/",
        models=["Z-Image-Turbo"],
        custom_nodes=[],
        notes=[
            "Good first smoke test after install.",
            "Use it for fast product and concept image ideation.",
        ],
    ),
    ComfyUIExample(
        id="wan22-5b-video-generation",
        title="Wan 2.2 5B Video Generation",
        category="text-to-video",
        install_profile="video",
        recommended_vram_gb=8,
        why_trending="Official Wan 2.2 native workflow; the 5B path is documented for 8 GB VRAM.",
        workflow_hint="Workflows > Browse Templates > Video > Wan2.2 5B video generation",
        source_url="https://docs.comfy.org/tutorials/video/wan/wan2_2",
        models=[
            "wan2.2_ti2v_5B_fp16.safetensors",
            "wan2.2_vae.safetensors",
            "umt5_xxl_fp8_e4m3fn_scaled.safetensors",
        ],
        custom_nodes=[],
        notes=[
            "Best local starter for short video experiments.",
            "ComfyUI native offloading makes this practical on smaller NVIDIA cards.",
        ],
    ),
    ComfyUIExample(
        id="wan22-14b-image-to-video",
        title="Wan 2.2 14B Image to Video",
        category="image-to-video",
        install_profile="video-pro",
        recommended_vram_gb=24,
        why_trending="Listed near the top of official popular templates for image-to-video.",
        workflow_hint="Workflows > Browse Templates > Video > Wan 2.2 14B Image to Video",
        source_url="https://www.comfy.org/workflows/comfyui/",
        models=[
            "wan2.2_i2v_high_noise_14B_fp16.safetensors",
            "wan2.2_i2v_low_noise_14B_fp16.safetensors",
            "wan_2.1_vae.safetensors",
            "umt5_xxl_fp8_e4m3fn_scaled.safetensors",
        ],
        custom_nodes=[],
        notes=[
            "Use after the 5B path is working.",
            "Recommended for higher VRAM workstations.",
        ],
    ),
    ComfyUIExample(
        id="ltx23-image-to-video",
        title="LTX-2.3 Image to Video",
        category="image-to-video",
        install_profile="video",
        recommended_vram_gb=12,
        why_trending="Official popular template for fast image-to-video iteration.",
        workflow_hint="Workflows > Browse Templates > Video > LTX-2.3 Image to Video",
        source_url="https://www.comfy.org/workflows/comfyui/",
        models=["LTX-2.3"],
        custom_nodes=[],
        notes=[
            "Useful as a second video baseline against Wan.",
            "Good for comparing motion stability and prompt adherence.",
        ],
    ),
    ComfyUIExample(
        id="flux-controlnet-canny-depth",
        title="FLUX.1 ControlNet Canny and Depth",
        category="controlnet",
        install_profile="control",
        recommended_vram_gb=16,
        why_trending="Official ComfyUI guide for controlled image recreation with FLUX tools.",
        workflow_hint="Tutorials > ControlNet > FLUX.1 ControlNet examples",
        source_url="https://docs.comfy.org/tutorials/flux/flux-1-controlnet",
        models=[
            "flux1-dev.safetensors",
            "flux1-canny-dev.safetensors",
            "flux1-depth-dev-lora.safetensors",
            "clip_l.safetensors",
            "t5xxl_fp16.safetensors",
            "ae.safetensors",
        ],
        custom_nodes=[
            "ComfyUI-Advanced-ControlNet",
            "ComfyUI ControlNet Aux",
        ],
        notes=[
            "Great for product shots, pose/edge guidance, and brand-consistent variants.",
            "Some FLUX models require accepting upstream model terms before download.",
        ],
    ),
    ComfyUIExample(
        id="qwen-image-edit-2509",
        title="Qwen Image Edit 2509",
        category="image-edit",
        install_profile="edit",
        recommended_vram_gb=12,
        why_trending="Official popular template for image editing and ControlNet-style workflows.",
        workflow_hint="Workflows > Browse Templates > Image Edit > Qwen Image Edit 2509",
        source_url="https://www.comfy.org/workflows/comfyui/",
        models=["Qwen Image Edit 2509"],
        custom_nodes=[],
        notes=[
            "Good example for before/after image editing workflows.",
            "Pair with nvHive prompts for product retouching and UI asset iteration.",
        ],
    ),
]


def comfyui_root() -> Path:
    """Return the ComfyUI workspace root."""
    configured = os.environ.get("COMFYUI_HOME")
    if configured:
        return Path(configured).expanduser()
    return storage_layout().comfyui_dir


def comfyui_app_dir(root: Path | None = None) -> Path:
    return (root or comfyui_root()) / "ComfyUI"


def comfyui_venv_python(root: Path | None = None) -> Path:
    venv = (root or comfyui_root()) / "venv"
    if os.name == "nt":
        return venv / "Scripts" / "python.exe"
    return venv / "bin" / "python"


def examples_as_dicts() -> list[dict[str, Any]]:
    """Return the curated example manifest as JSON-serialisable dicts."""
    return [asdict(example) for example in TRENDING_COMFYUI_EXAMPLES]


def _model_target_folder(model_name: str) -> str:
    """Best-effort ComfyUI model folder for a named workflow requirement."""
    lowered = model_name.lower()
    if "vae" in lowered:
        return "vae"
    if "controlnet" in lowered or "canny" in lowered or "depth" in lowered:
        return "controlnet"
    if "clip" in lowered or "t5" in lowered or "text_encoder" in lowered:
        return "clip"
    if "lora" in lowered:
        return "loras"
    if "gguf" in lowered:
        return "unet"
    if "wan" in lowered or "ltx" in lowered:
        return "diffusion_models"
    return "checkpoints"


def comfyui_model_plan(example_ids: list[str] | None = None) -> dict[str, Any]:
    """Return a model download plan for selected ComfyUI workflow examples."""
    selected_ids = set(example_ids or [])
    examples = [
        example for example in TRENDING_COMFYUI_EXAMPLES
        if not selected_ids or example.id in selected_ids
    ]
    models: dict[str, dict[str, Any]] = {}
    custom_nodes: dict[str, dict[str, Any]] = {}
    for example in examples:
        for model in example.models:
            models.setdefault(model, {
                "name": model,
                "workflow_ids": [],
                "workflow_titles": [],
                "source_urls": set(),
                "target_folder": _model_target_folder(model),
                "requires_manual_download": True,
            })
            models[model]["workflow_ids"].append(example.id)
            models[model]["workflow_titles"].append(example.title)
            models[model]["source_urls"].add(example.source_url)
        for node in example.custom_nodes:
            custom_nodes.setdefault(node, {
                "name": node,
                "workflow_ids": [],
                "workflow_titles": [],
            })
            custom_nodes[node]["workflow_ids"].append(example.id)
            custom_nodes[node]["workflow_titles"].append(example.title)

    return {
        "examples": [asdict(example) for example in examples],
        "models": [
            {**model, "source_urls": sorted(model["source_urls"])}
            for model in models.values()
        ],
        "custom_nodes": list(custom_nodes.values()),
        "model_count": len(models),
        "custom_node_count": len(custom_nodes),
        "requires_manual_download": True,
        "download_helper": "download-comfy-models.sh",
        "message": (
            "ComfyUI model weights can be very large and may require upstream license "
            "acceptance. nvHive saves the plan and source links; download only models "
            "whose terms you accept."
        ),
    }


def write_model_plan(example_ids: list[str] | None = None, root: Path | None = None) -> Path:
    """Write a selected ComfyUI model plan beside the nvHive example pack."""
    target_root = root or comfyui_root()
    examples_dir = write_example_pack(target_root)
    plan = comfyui_model_plan(example_ids)
    plan_path = examples_dir / "model-download-plan.json"
    plan_path.write_text(json.dumps(plan, indent=2), encoding="utf-8")

    lines = [
        "# nvHive ComfyUI Model Download Plan",
        "",
        plan["message"],
        "",
        "Selected workflows:",
    ]
    for example in plan["examples"]:
        lines.append(f"- {example['title']} ({example['recommended_vram_gb']} GB VRAM)")
    lines.extend(["", "Models:"])
    for model in plan["models"]:
        sources = ", ".join(model["source_urls"])
        lines.append(
            f"- {model['name']} -> models/{model['target_folder']} - sources: {sources}"
        )
    if plan["custom_nodes"]:
        lines.extend(["", "Custom nodes:"])
        for node in plan["custom_nodes"]:
            lines.append(f"- {node['name']}")
    (examples_dir / "MODEL_DOWNLOAD_PLAN.md").write_text(
        "\n".join(lines).strip() + "\n",
        encoding="utf-8",
    )
    helper = examples_dir / "download-comfy-models.sh"
    helper_lines = [
        "#!/usr/bin/env bash",
        "set -euo pipefail",
        "",
        f'COMFYUI_ROOT="${{COMFYUI_ROOT:-{comfyui_app_dir(target_root)}}}"',
        'MODELS_DIR="$COMFYUI_ROOT/models"',
        'echo "ComfyUI models directory: $MODELS_DIR"',
        "",
    ]
    for model in plan["models"]:
        target = f'$MODELS_DIR/{model["target_folder"]}'
        helper_lines.extend([
            f'mkdir -p "{target}"',
            f'echo "- {model["name"]} -> {target}"',
            f'echo "  Sources: {", ".join(model["source_urls"])}"',
            'echo "  Download manually after accepting upstream terms."',
            "",
        ])
    helper.write_text("\n".join(helper_lines).strip() + "\n", encoding="utf-8")
    helper.chmod(0o755)
    return plan_path


def _status_url(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> str:
    return f"http://{host}:{port}"


def _pid_file(root: Path) -> Path:
    return root / "comfyui.pid"


def _log_file(root: Path) -> Path:
    return root / "comfyui.log"


def _read_pid(root: Path) -> int | None:
    try:
        raw = _pid_file(root).read_text(encoding="utf-8").strip()
        return int(raw) if raw else None
    except Exception:
        return None


def _is_http_reachable(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> bool:
    try:
        import httpx

        response = httpx.get(f"{_status_url(host, port)}/system_stats", timeout=2.0)
        return response.status_code < 500
    except Exception:
        return False


def detect_comfyui(
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
) -> dict[str, Any]:
    """Return local ComfyUI installation and runtime status."""
    root = comfyui_root()
    app_dir = comfyui_app_dir(root)
    venv_python = comfyui_venv_python(root)
    examples_dir = app_dir / "nvhive_examples"
    manifest_path = examples_dir / "examples.json"
    installed = (app_dir / "main.py").exists()

    return {
        "installed": installed,
        "running": _is_http_reachable(host, port),
        "url": _status_url(host, port),
        "install_root": str(root),
        "app_dir": str(app_dir),
        "venv_python": str(venv_python),
        "examples_dir": str(examples_dir),
        "examples_installed": manifest_path.exists(),
        "manager_available": (app_dir / "manager_requirements.txt").exists(),
        "log_path": str(_log_file(root)),
        "pid": _read_pid(root),
        "examples": examples_as_dicts(),
    }


def wait_for_comfyui(
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    *,
    timeout_s: float = 45.0,
    interval_s: float = 1.0,
) -> dict[str, Any]:
    """Poll ComfyUI after launch so callers know when it is actually usable."""
    started = time.monotonic()
    deadline = started + timeout_s
    while time.monotonic() < deadline:
        if _is_http_reachable(host, port):
            status = detect_comfyui(host, port)
            status.update({
                "ready": True,
                "ready_timeout": False,
                "ready_wait_seconds": round(time.monotonic() - started, 1),
            })
            return status
        time.sleep(interval_s)

    status = detect_comfyui(host, port)
    status.update({
        "ready": False,
        "ready_timeout": True,
        "ready_wait_seconds": round(time.monotonic() - started, 1),
    })
    return status


def write_example_pack(root: Path | None = None) -> Path:
    """Write nvHive's curated ComfyUI example manifest into the install."""
    app_dir = comfyui_app_dir(root)
    examples_dir = app_dir / "nvhive_examples"
    examples_dir.mkdir(parents=True, exist_ok=True)

    manifest = {
        "name": "nvHive ComfyUI starter examples",
        "description": (
            "Curated official ComfyUI templates for NVIDIA local image, edit, "
            "ControlNet, and video workflows."
        ),
        "sources": sorted({example.source_url for example in TRENDING_COMFYUI_EXAMPLES}),
        "examples": examples_as_dicts(),
    }

    manifest_path = examples_dir / "examples.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    lines = [
        "# nvHive ComfyUI Starter Examples",
        "",
        "Open ComfyUI, choose the workflow browser, then load one of these official templates.",
        "The JSON manifest beside this README is also consumed by the nvHive WebUI.",
        "",
        "## Recommended Order",
        "",
    ]
    for example in TRENDING_COMFYUI_EXAMPLES:
        lines.extend(
            [
                f"### {example.title}",
                "",
                f"- Category: {example.category}",
                f"- Profile: {example.install_profile}",
                f"- Suggested VRAM: {example.recommended_vram_gb} GB",
                f"- Open: {example.workflow_hint}",
                f"- Source: {example.source_url}",
                f"- Why: {example.why_trending}",
                "",
                "Models:",
                *[f"- {model}" for model in example.models],
                "",
            ]
        )
        if example.custom_nodes:
            lines.extend(["Custom nodes:", *[f"- {node}" for node in example.custom_nodes], ""])
        if example.notes:
            lines.extend(["Notes:", *[f"- {note}" for note in example.notes], ""])

    (examples_dir / "README.md").write_text("\n".join(lines).strip() + "\n", encoding="utf-8")
    return examples_dir


async def _run_command(
    cmd: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    label: str,
) -> AsyncIterator[dict[str, Any]]:
    """Run a subprocess and stream merged stdout/stderr lines as events."""
    yield {"event": "step", "status": "running", "message": label, "command": cmd}

    process = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=str(cwd) if cwd else None,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )

    try:
        assert process.stdout is not None
        async for raw_line in process.stdout:
            line = raw_line.decode("utf-8", errors="replace").rstrip()
            if line:
                yield {"event": "log", "status": "running", "message": line}
    except asyncio.CancelledError:
        if process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except TimeoutError:
                process.kill()
                await process.wait()
        raise

    return_code = await process.wait()
    if return_code != 0:
        yield {
            "event": "error",
            "status": "failed",
            "message": f"{label} failed with exit code {return_code}",
            "return_code": return_code,
        }
        return

    yield {"event": "step", "status": "complete", "message": f"{label} complete"}


async def _run_checked(
    cmd: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    label: str,
) -> AsyncIterator[dict[str, Any]]:
    """Run a command and stop the caller when it fails."""
    failed = False
    async for event in _run_command(cmd, cwd=cwd, env=env, label=label):
        if event.get("event") == "error":
            failed = True
        yield event
    if failed:
        raise RuntimeError(f"{label} failed")


def _torch_install_command(python_exe: Path, torch_profile: str) -> list[str] | None:
    if torch_profile == "skip":
        return None
    base = [str(python_exe), "-m", "pip", "install", "torch", "torchvision", "torchaudio"]
    if torch_profile == "nvidia-cu130":
        return base + ["--extra-index-url", "https://download.pytorch.org/whl/cu130"]
    if torch_profile == "nvidia-cu121":
        return base + ["--index-url", "https://download.pytorch.org/whl/cu121"]
    if torch_profile == "cpu":
        return base
    raise ValueError(f"Unsupported torch_profile: {torch_profile}")


async def install_comfyui(
    *,
    torch_profile: str = "nvidia-cu121",
    force_update: bool = False,
) -> AsyncIterator[dict[str, Any]]:
    """Install or update ComfyUI and write the nvHive examples pack."""
    root = comfyui_root()
    app_dir = comfyui_app_dir(root)
    venv_python = comfyui_venv_python(root)
    root.mkdir(parents=True, exist_ok=True)

    if shutil.which("git") is None:
        yield {
            "event": "error",
            "status": "failed",
            "message": "Git is required to install ComfyUI. Install Git, then try again.",
        }
        return

    env = os.environ.copy()
    env.update(storage_layout().env())
    env["PYTHONUTF8"] = "1"

    yield {
        "event": "plan",
        "status": "running",
        "message": "Preparing ComfyUI install",
        "install_root": str(root),
        "torch_profile": torch_profile,
    }

    try:
        if (app_dir / ".git").exists() and force_update:
            async for event in _run_checked(
                ["git", "-C", str(app_dir), "pull", "--ff-only"],
                label="Update ComfyUI",
            ):
                yield event
        elif not (app_dir / "main.py").exists():
            async for event in _run_checked(
                ["git", "clone", "--depth", "1", COMFYUI_REPO_URL, str(app_dir)],
                label="Clone ComfyUI",
            ):
                yield event
        else:
            yield {
                "event": "step",
                "status": "complete",
                "message": "ComfyUI source already present",
            }

        if not venv_python.exists():
            async for event in _run_checked(
                [sys.executable, "-m", "venv", str(root / "venv")],
                label="Create ComfyUI Python environment",
            ):
                yield event
        else:
            yield {
                "event": "step",
                "status": "complete",
                "message": "Python environment already present",
            }

        async for event in _run_checked(
            [
                str(venv_python),
                "-m",
                "pip",
                "install",
                "--upgrade",
                "pip",
                "wheel",
                "setuptools",
            ],
            env=env,
            label="Upgrade installer tools",
        ):
            yield event

        torch_cmd = _torch_install_command(venv_python, torch_profile)
        if torch_cmd:
            async for event in _run_checked(
                torch_cmd,
                env=env,
                label="Install PyTorch runtime",
            ):
                yield event

        async for event in _run_checked(
            [str(venv_python), "-m", "pip", "install", "-r", str(app_dir / "requirements.txt")],
            cwd=app_dir,
            env=env,
            label="Install ComfyUI requirements",
        ):
            yield event

        manager_requirements = app_dir / "manager_requirements.txt"
        if manager_requirements.exists():
            async for event in _run_checked(
                [str(venv_python), "-m", "pip", "install", "-r", str(manager_requirements)],
                cwd=app_dir,
                env=env,
                label="Install ComfyUI Manager requirements",
            ):
                yield event

        examples_dir = write_example_pack(root)
        yield {
            "event": "step",
            "status": "complete",
            "message": "Installed nvHive ComfyUI example pack",
            "examples_dir": str(examples_dir),
        }
        try:
            from nvh.integrations.receipts import write_receipt

            write_receipt(
                kind="comfyui",
                item_id="workspace",
                title="ComfyUI Workspace",
                install_path=app_dir,
                source_urls=[COMFYUI_REPO_URL],
                files=[
                    str(examples_dir / "examples.json"),
                    str(examples_dir / "README.md"),
                ],
                metadata={
                    "torch_profile": torch_profile,
                    "venv_python": str(venv_python),
                    "examples_dir": str(examples_dir),
                    "status": detect_comfyui(),
                },
            )
        except Exception as exc:
            yield {
                "event": "log",
                "status": "running",
                "message": f"Warning: could not write ComfyUI receipt: {exc}",
            }

        yield {
            "event": "complete",
            "status": "complete",
            "message": "ComfyUI install complete",
            "torch_profile": torch_profile,
            "status_snapshot": detect_comfyui(),
        }
    except Exception as exc:
        yield {"event": "error", "status": "failed", "message": str(exc)}


def start_comfyui(
    *,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
) -> dict[str, Any]:
    """Start ComfyUI in the background and return launch metadata."""
    if host not in {"127.0.0.1", "localhost"}:
        raise ValueError("ComfyUI auto-start is restricted to localhost.")

    root = comfyui_root()
    app_dir = comfyui_app_dir(root)
    python_exe = comfyui_venv_python(root)

    if _is_http_reachable(host, port):
        status = detect_comfyui(host, port)
        status["already_running"] = True
        status["ready"] = True
        status["ready_timeout"] = False
        return status

    if not (app_dir / "main.py").exists() or not python_exe.exists():
        raise FileNotFoundError("ComfyUI is not installed yet.")

    root.mkdir(parents=True, exist_ok=True)
    log_path = _log_file(root)
    log_handle = log_path.open("ab")

    cmd = [
        str(python_exe),
        str(app_dir / "main.py"),
        "--listen",
        host,
        "--port",
        str(port),
        "--enable-manager",
    ]

    env = os.environ.copy()
    env.update(storage_layout().env())
    env["PYTHONUTF8"] = "1"

    kwargs: dict[str, Any] = {
        "cwd": str(app_dir),
        "stdin": subprocess.DEVNULL,
        "stdout": log_handle,
        "stderr": log_handle,
        "env": env,
    }
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
    else:
        kwargs["start_new_session"] = True
        kwargs["close_fds"] = True

    process = subprocess.Popen(cmd, **kwargs)
    log_handle.close()
    _pid_file(root).write_text(str(process.pid), encoding="utf-8")

    status = wait_for_comfyui(host, port)
    status.update(
        {
            "started": True,
            "pid": process.pid,
            "command": cmd,
            "log_path": str(log_path),
        }
    )
    return status
