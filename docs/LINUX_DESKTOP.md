# Linux GPU Desktop Student Workstation

nvHive is designed to make a fresh NVIDIA Linux cloud desktop feel like a ready-to-use AI lab for students.

Target user journey:

1. Launch the Linux desktop instance.
2. Run one install command or `pip install nvhive`.
3. Click the NVHive AI Studio desktop icon, or run `nvh workstation --launch`.
4. Use local chat models, cloud/free advisors, ComfyUI examples, OpenClaw/NemoClaw agent packs, game-dev helpers, creative tools, and music production helpers from one WebUI.

## Quick Start

Easiest path:

```bash
curl -sSL https://raw.githubusercontent.com/thatcooperguy/nvHive/main/start-linux.sh | bash
```

That script chooses a likely persistent mount for `NVH_HOME`, installs nvHive
without root, creates the desktop launcher, and starts the WebUI setup wizard.
For the target cloud desktop shape, `NVH_HOME` should land on the writable
block-backed home/data volume that survives reconnects, ideally 200GB or
larger for local LLMs and ComfyUI assets. Avoid read-only CIFS/SMB mounts and
the ephemeral OS disk. If the block volume is mounted as the Linux home
directory, the installer selects `/home/$USER/nvhive` automatically and prints
the exact install path. In desktop sessions it also creates `~/.local/bin/nvh`
and auto-launches the WebUI so first-time users do not have to source an env
file or remember a command. Set `NVH_INSTALL_LAUNCH=0` for install-only mode.
To force the no-Python binary path:

```bash
curl -sSL https://raw.githubusercontent.com/thatcooperguy/nvHive/main/start-linux.sh | NVH_USE_BINARY=1 bash
```

Manual path:

```bash
export NVH_HOME=/mnt/persist/nvhive
curl -sSL https://raw.githubusercontent.com/thatcooperguy/nvHive/main/install.sh | bash
source "$NVH_HOME/nvh-env.sh"
nvh workstation --home-dir "$NVH_HOME" --all -y
```

Clean reset path:

```bash
bash "$NVH_HOME/uninstall.sh" --purge -y
curl -sSL https://raw.githubusercontent.com/thatcooperguy/nvHive/main/install.sh | bash
```

For a pip install path:

```bash
export NVH_HOME=/mnt/persist/nvhive
python3 -m pip install --user "nvhive[all]"
nvh workstation --home-dir "$NVH_HOME" --all -y
```

For a lighter setup that avoids large downloads:

```bash
nvh workstation
nvh webui
```

The setup wizard starts in Beginner Mode with one recommended action, a Fix My
Setup repair button, and Advanced Details for diagnostics. It is designed so a
student can pick a mission, then click through storage, models, ComfyUI, Claw
agents, creative tools, game engines, and music packs without typing manual
commands.

The desktop launcher path is intentionally conservative for managed cloud
desktops: it does not need `sudo`, does not need OS package installs, and keeps
large assets under the selected persistent `NVH_HOME`. Manual commands remain
available as overrides, but the first-run experience should be click-first.

## What `nvh workstation` Does

- Detects NVIDIA GPU availability with NVML/`nvidia-smi` and reports when a
  rootless session can see NVIDIA device files but cannot query them
- Estimates framebuffer/VRAM, architecture, and storage capacity before
  recommending local chat models
- Creates `$NVH_HOME/bin/nvhive-ai-studio`
- Creates a Linux desktop launcher named `NVHive AI Studio`
- Shows a student-friendly setup checklist
- Runs nvWizard boot checks for storage, Python, CUDA/PyTorch, ComfyUI, models, and install receipts
- Checks `/v1/ready` so the launcher and WebUI can show whether the workspace is ready, pilot-ready, or blocked
- With `--all`, ensures local AI, installs ComfyUI, installs the rootless starter pack, and launches WebUI
- Uses user-space paths only under `NVH_HOME` for durable models, ComfyUI, packs, runtime fallback tools, apps, WebUI assets, cache, logs, and config

## Rootless AI Studio Packs

`nvh studio` installs optional packs without root access. It never calls `sudo`, `apt`, `dnf`, `pacman`, or `systemctl`. NemoClaw is the exception that checks Docker because it is an OpenShell sandbox stack; the wizard blocks it unless Docker already works without sudo.

| Bundle | Command | Installs |
| --- | --- | --- |
| AI Starter | `nvh studio --install starter -y` | Rootless Ollama, top local LLMs, agent lab, ComfyUI power nodes, game-dev lab |
| Runtime fallback | `nvh studio --install python-runtime-fallback -y` | Optional micromamba binary under `$NVH_HOME` for cloud images where Python `venv` is broken |
| LLMs | `nvh studio --install llms -y` | Gemma 3, Qwen 3, Llama 3.1, Qwen coder, DeepSeek reasoning, embeddings |
| Agents | `nvh studio --install agents -y` | LangGraph, CrewAI, AutoGen, JupyterLab, search/tool packages, OpenClaw |
| Claw agents | `nvh studio --install claw -y` | OpenClaw rootless workspace, plus NVIDIA NemoClaw when Docker/OpenShell is usable |
| ComfyUI | `nvh studio --install comfy -y` | ComfyUI Manager, Impact Pack, ControlNet Aux, Video Helper Suite, GGUF, rgthree |
| Games | `nvh studio --install game -y` | Pygame/Panda3D lab, asset helpers, Linux/Wine mod workspace |
| Creative | `nvh studio --install creative -y` | Blender 4.5 LTS portable install, launcher, game/asset workspace |
| Music | `nvh studio --install music -y` | ACE-Step music generator, Demucs stems, WhisperX transcription, Audacity/LMMS AppImages, and a DAW helper workspace |

Run `nvh studio --list` to see exact pack status and disk estimates.

nvHive does not require conda, miniforge, or micromamba on the happy path. The
default installer uses Python `venv` and `pip`; the runtime fallback pack is a
student-friendly rescue option for locked-down images that are missing working
virtualenv support.

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

ComfyUI, AI Studio pack, and local model installs run as persistent background
jobs. Job metadata and logs are written to `$NVH_HOME/jobs`, so a student can
refresh the browser, reconnect to a cloud desktop, or cancel a long download
without losing the setup state.

The local setup helper endpoint, `/v1/setup/helper`, works offline. It ranks the
next storage, runtime, model, ComfyUI, OpenClaw/NemoClaw, creative-tool, and
music-tool actions before any local LLM is installed.

OpenClaw is the simple agent option. nvHive installs it into a persistent
user-owned Node workspace and writes `nvhive-openclaw`. NemoClaw is the guarded
NVIDIA/OpenShell path. It remains visible in the wizard, but it is marked
blocked until Docker is installed, running, and reachable by the current user
without sudo.

The council also includes a `product_resilience` preset with an Underdog Student
Advocate. Use it when you want a skeptical review of what could break for a
beginner on a no-root cloud GPU desktop.

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

The ComfyUI installer uses an isolated environment under `$NVH_HOME/comfyui`, installs current NVIDIA PyTorch support, enables ComfyUI Manager when available, and writes nvHive starter examples.

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
ComfyUI examples, plus `download-comfy-models.sh`, so students can see exactly
which image/video weights each workflow needs and where they belong before
accepting upstream terms.

## Student-Safe Defaults

- No root required for nvHive itself
- AI Studio packs are rootless and install to user-owned directories
- Local data path: `$NVH_HOME` on the mounted persistent file volume
- Noninteractive installs use `$NVH_HOME/venv` by default; set `NVH_USE_ACTIVE_ENV=1` only when you intentionally want an already active conda/mamba/venv
- API binds to localhost by default
- Cloud compose exposure requires `HIVE_API_KEY`
- WebUI source, npm cache, auto-installed Node, and the local API process inherit `NVH_HOME`
- WebUI starts a local API automatically unless `--no-api` is used
- ComfyUI auto-start binds to `127.0.0.1:8188`
- Cloud API keys are optional and stored locally

## Useful Commands

```bash
nvh workstation              # detect and prepare the student AI lab
nvh doctor --storage --home-dir "$NVH_HOME"
nvh workstation --launch     # open WebUI from the same flow
nvh workstation --with-comfyui
nvh workstation --with-studio-packs
nvh workstation --all -y
nvh studio --list            # show rootless LLM/agent/ComfyUI/game packs
nvh studio --models          # show recommended local model downloads
nvh studio --install-models recommended -y
nvh studio --install starter -y
nvh studio --install claw -y
nvh studio --install creative -y
nvh studio --install music -y
nvh doctor --fix             # repair local models/config where possible
nvh webui                    # launch browser dashboard
nvh safe "summarize this"    # local-only prompt path
```

Browser launch is also rootless-first. `nvh webui` honors `NVH_BROWSER`, then prefers `$NVH_HOME/apps/firefox/firefox`, then system Firefox, then Chromium/Chrome and desktop openers. On Linux x86_64 it can install Firefox under `NVH_HOME` without sudo; set `NVH_FIREFOX_AUTO_INSTALL=0` to disable that fallback.

Back to [README](../README.md)
