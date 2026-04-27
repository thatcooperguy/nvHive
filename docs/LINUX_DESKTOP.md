# Linux GPU Desktop Student Workstation

nvHive is designed to make a fresh NVIDIA Linux cloud desktop feel like a ready-to-use AI lab for students.

Target user journey:

1. Launch the Linux desktop instance.
2. Run one install command or `pip install nvhive`.
3. Click the NVHive AI Studio desktop icon, or run `nvh workstation --launch`.
4. Use local chat models, cloud/free advisors, ComfyUI examples, agent packs, and game-dev helpers from one WebUI.

## Quick Start

```bash
curl -sSL https://raw.githubusercontent.com/thatcooperguy/nvHive/main/install.sh | bash
nvh workstation --all -y
```

For a pip install path:

```bash
python3 -m pip install --user "nvhive[all]"
nvh workstation --all -y
```

For a lighter setup that avoids large downloads:

```bash
nvh workstation
nvh webui
```

## What `nvh workstation` Does

- Detects NVIDIA GPU availability with `nvidia-smi`
- Estimates VRAM and recommends local chat models
- Creates `~/.local/bin/nvhive-ai-studio`
- Creates a Linux desktop launcher named `NVHive AI Studio`
- Shows a student-friendly setup checklist
- With `--all`, ensures local AI, installs ComfyUI, installs the rootless starter pack, and launches WebUI
- Uses user-space paths only: `~/.nvh`, `~/.local/bin`, and project folders under the student's home directory

## Rootless AI Studio Packs

`nvh studio` installs optional packs without root access. It never calls `sudo`, `apt`, `dnf`, `pacman`, `systemctl`, or Docker.

| Bundle | Command | Installs |
| --- | --- | --- |
| Starter lab | `nvh studio --install starter -y` | Rootless Ollama, top local LLMs, agent lab, ComfyUI power nodes, game-dev lab |
| LLMs | `nvh studio --install llms -y` | Gemma 3, Qwen 3, Llama 3.1, Qwen coder, DeepSeek reasoning, embeddings |
| Agents | `nvh studio --install agents -y` | LangGraph, CrewAI, AutoGen, JupyterLab, search/tool packages |
| ComfyUI | `nvh studio --install comfy -y` | ComfyUI Manager, Impact Pack, ControlNet Aux, Video Helper Suite, GGUF, rgthree |
| Games | `nvh studio --install game -y` | Pygame/Panda3D lab, asset helpers, Linux/Wine mod workspace |

Run `nvh studio --list` to see exact pack status and disk estimates.

## Model Picker

The WebUI setup wizard includes a dedicated Models step. It shows:

- detected GPU VRAM
- recommended local models
- exact Ollama model names
- disk estimates
- installed status
- GPU-fit warnings
- a download queue for selected models

The default recommendation set covers chat, coding, reasoning, vision, and
embeddings. Students can accept the defaults or choose models one at a time.

CLI equivalents:

```bash
nvh studio --models
nvh studio --install-models recommended -y
nvh studio --install-models gemma3-4b,qwen25-coder-7b -y
```

## Model Defaults

| GPU VRAM | Local Chat Models | ComfyUI Profiles |
| --- | --- | --- |
| CPU only | Cloud/free providers | starter |
| 4-8 GB | `nemotron-mini` | starter |
| 8-12 GB | `llama3.1:8b`, `nemotron-mini` | starter, video |
| 12-24 GB | `llama3.1:8b`, `nemotron-mini` | starter, edit, control, video |
| 24+ GB | `nemotron`, `llama3.1:8b`, `nemotron-mini` | starter, edit, control, video, video-pro |

The ComfyUI installer uses an isolated environment under `~/.nvh/comfyui`, installs current NVIDIA PyTorch support, enables ComfyUI Manager when available, and writes nvHive starter examples.

## ComfyUI Starter Examples

The setup wizard highlights current official ComfyUI template categories:

- Z-Image-Turbo text-to-image
- Wan 2.2 5B video generation
- Wan 2.2 14B image-to-video
- LTX-2.3 image-to-video
- FLUX.1 ControlNet Canny/Depth
- Qwen image editing

Large model downloads remain explicit because many image/video models require license acceptance, significant disk space, or upstream account terms.

The ComfyUI step includes a workflow model-plan selector. It saves
`MODEL_DOWNLOAD_PLAN.md` and `model-download-plan.json` beside the nvHive
ComfyUI examples so students can see exactly which image/video weights each
workflow needs before accepting upstream terms.

## Student-Safe Defaults

- No root required for nvHive itself
- AI Studio packs are rootless and install to user-owned directories
- Local data path: `~/nvh`, `~/.hive`, `~/.nvh`
- API binds to localhost by default
- WebUI starts a local API automatically unless `--no-api` is used
- ComfyUI auto-start binds to `127.0.0.1:8188`
- Cloud API keys are optional and stored locally

## Useful Commands

```bash
nvh workstation              # detect and prepare the student AI lab
nvh workstation --launch     # open WebUI from the same flow
nvh workstation --with-comfyui
nvh workstation --with-studio-packs
nvh workstation --all -y
nvh studio --list            # show rootless LLM/agent/ComfyUI/game packs
nvh studio --models          # show recommended local model downloads
nvh studio --install-models recommended -y
nvh studio --install starter -y
nvh doctor --fix             # repair local models/config where possible
nvh webui                    # launch browser dashboard
nvh safe "summarize this"    # local-only prompt path
```

Back to [README](../README.md)
