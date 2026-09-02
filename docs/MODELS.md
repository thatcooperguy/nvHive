# Models — detection, fit, and the Model Manager

nvHive detected your GPU during install, so it can say *before* a download
whether a model fits your VRAM and how much disk it needs. This page covers
how detection works, which local models each VRAM tier gets, the capability
matrix behind image/video/speech packs, the local orchestration tiers, and
`nvh bench`.

## The Model Manager

Two front doors read the same VRAM-fit report: the **Models** page of the
dashboard (`/models`) and the `nvh models` CLI, so they always agree.

The page header shows the detected GPU, VRAM and free disk. **Installed**
lists every local model with its on-disk size and a **Remove** button.
**Catalog** is the fit-ranked list for *your* GPU: each row carries a
**fits GPU** / **tight fit** badge, the estimated download size, a
**recommended** marker for the best first installs, and an **Install** button
that streams live progress over server-sent events — the bar keeps moving if
you navigate away and come back, and **Cancel** stops a pull mid-way.

```bash
nvh models list            # installed models + a hint (fast, no catalog)
nvh models list --all      # the fit-ranked catalog for the detected GPU
nvh models pull gemma3:4b  # streams `ollama pull` progress; Ctrl+C cancels
nvh models rm gemma3:4b    # remove and reclaim disk (-y skips the prompt)
```

`nvh models pull` drives the rootless Ollama binary nvHive installed under
`$NVH_HOME/bin`; models land in `$NVH_HOME/models/ollama` and survive
reconnects to the same workspace. Use the pull target shown in parentheses by
`nvh models list --all` — it is exactly what you would type after `ollama
pull`.

### How fit is decided

The catalog and badges come from `GET /v1/setup/model-fit`, the same report
the setup wizard uses. For each candidate it compares the model's recommended
VRAM with the detected GPU and its on-disk size with free space, then ranks by
a fit score so the best first install floats to the top. **Tight fit** does
not block the download — smaller quantisations and CPU offload can still make
a model usable — it is a heads-up that the model sits at or above your VRAM.

| Endpoint | Purpose |
|---|---|
| `GET /v1/setup/model-fit` | fit-ranked catalog for the detected GPU/disk |
| `GET /v1/ollama/models` | installed models with on-disk sizes |
| `POST /v1/ollama/pull` | download a model (SSE progress) |
| `DELETE /v1/ollama/models/{name}` | remove a model |

All are auth-gated when `HIVE_API_KEY` is set; the web client attaches the key
automatically, including on the streaming pull.

## GPU detection

nvHive reads the GPU through **pynvml** (`pip install nvidia-ml-py3`, included
in the `[nvidia]` and `[all]` extras) and falls back to parsing `nvidia-smi`.
pynvml gives model name, total/free VRAM, driver and CUDA version,
utilisation, temperature, power draw and limit, clocks, PCIe generation and
the processes using the GPU; the `nvidia-smi` fallback gives name, VRAM,
driver and utilisation. Rootless cloud sessions that can see the device files
but cannot query them are reported as such rather than as "no GPU".

```bash
nvh nvidia            # what nvHive sees: GPU, driver, CUDA, Ollama, NIM, Triton
nvh status --deep     # the same facts inside the full diagnostic
nvh estimate          # expected tokens/s for a model on any NVIDIA GPU
```

Multi-GPU machines work: Ollama spreads layers across every detected card.
Apple Silicon runs through Ollama's Metal backend without the pynvml detail.

## Local models by VRAM

The installer's picker, `nvh setup`, the setup wizard and `nvh studio
--models` all use the same tiering:

| VRAM | Primary local model | Vision / fallback | Typical use |
|---|---|---|---|
| none / < 4 GB | cloud and free providers | optional tiny local | CPU mode |
| 4–8 GB | `gemma3:4b` | `moondream` | lightweight local chat |
| 8–12 GB | `qwen3:8b` or `llama3.1:8b` | `moondream`, `llava:7b` | student chat and code |
| 12–24 GB | `qwen3:8b`, `qwen2.5-coder:7b` | `minicpm-v` | coding plus image help |
| 24–40 GB | `llama3.2-vision`, `nemotron-3-nano-omni` | `qwen3:8b` | multimodal desktop assistant |
| 40 GB+ | `nemotron-omni`, `nemotron` 70B | `llama3.2-vision` + a coder | strongest local AI first |

The strongest fitting primary model is pulled first, then smaller multimodal
and coding fallbacks so the Wizard keeps working when the largest model is
busy. `nvh studio --install-models recommended -y` pulls the tier's set;
`nvh studio --install-models gemma3-4b,qwen25-coder-7b -y` picks by id.

ComfyUI profiles follow the same ladder: `starter` on 8 GB, `edit` on 12 GB,
`control` on 16 GB, `video` on 24 GB, `video-pro` on 40 GB.

## The capability matrix

Chat plus Wizard vision is installed on every GPU by default. Image, video and
local speech generation are separate backends behind an opt-in, because they
download tens of gigabytes.

### What is "Nemotron Omni", really?

`nemotron-omni` and `nemotron-3-nano-omni` are NVIDIA's multimodal *language*
models. They give the Wizard sight — screenshots, uploaded images and
documents alongside text — and reason about them. They do **not** generate
images (that is ComfyUI plus a diffusion model), synthesise speech (Edge TTS /
Piper / XTTS) or recognise speech (Whisper / WhisperX). When this codebase says
"Omni", read "the Wizard's vision".

| Capability | Backend | Min VRAM | Enabled by `install.sh` | Manual |
|---|---|---|---|---|
| Chat — tiny vision | Ollama + `moondream` | 0 GB | always (CPU OK) | Wizard chat picker |
| Chat — small vision | Ollama + `minicpm-v` | 12 GB | 12+ GB tier | Wizard chat picker |
| Chat — vision | Ollama + `llama3.2-vision` | 16 GB | 16+ GB tier | Wizard chat picker |
| Chat — Omni Nano | Ollama + `nemotron-3-nano-omni` | 24 GB | 24+ GB tier (HF fallback) | Wizard chat picker |
| Chat — Omni flagship | Ollama + `nemotron-omni` | 40 GB | 40+ GB tier (HF fallback) | Wizard chat picker |
| Image generation (starter) | ComfyUI + Z-Image-Turbo | 8 GB | opt-in | `nvh studio --install comfy -y` |
| Image editing | ComfyUI + Qwen Image Edit | 12 GB | opt-in | `comfy` pack + Qwen Edit template |
| Image control | ComfyUI + FLUX.1 ControlNet | 16 GB | opt-in | `comfy` pack + FLUX template |
| Video (5B) | ComfyUI + Wan 2.2 5B | 8 GB | opt-in, 24+ GB tier | `comfy` pack + Wan 5B |
| Video (14B) | ComfyUI + Wan 2.2 14B i2v | 24 GB | opt-in, 40+ GB tier | `comfy` pack + Wan 14B |
| Speech — TTS (cloud, free) | Edge TTS (`nvh/core/voice.py`) | 0 GB | always | `nvh voice` |
| Speech — STT (cloud, free) | Groq Whisper | 0 GB | needs a Groq key | `nvh advisor add groq` |
| Speech — local STT/TTS | WhisperX + faster-whisper | 8 GB | opt-in, 24+ GB tier | `nvh studio --install music-producer-lab -y` |
| Music generation | ACE-Step | 6 GB | opt-in, 24+ GB tier | `nvh studio --install music -y` |

"Opt-in" means `NVH_INSTALL_FULL_CAPABILITY=1` at install time. With it set
the installer *stages* the qualifying packs by VRAM:

```
>=  8 GB  ComfyUI starter (Z-Image-Turbo)
>= 12 GB  + image-edit profile
>= 16 GB  + control profile
>= 24 GB  + video profile, speech pack (music-producer-lab), music pack (ACE-Step)
>= 40 GB  + video-pro profile (Wan 2.2 14B)
```

Staging writes `$NVH_HOME/state/capability/auto-enable.json` and nothing else;
the marker is for a future Wizard consumer. The companion
`NVH_INSTALL_FULL_CAPABILITY_DOWNLOAD=1` pulls the staged packs inline at
install time — slow and disk-hungry, meant for headless images that will
never have a browser.

Speech is the weakest leg: WhisperX rides in the music pack, there is no
dedicated speech pack and no VRAM-gated local TTS. The default voice path is
Edge TTS plus Groq Whisper, which works on any GPU including none. Cloud
GRID/virtual GPUs report VRAM through `nvidia-smi` like real cards; the matrix
applies as written.

Source of truth in code: `install.sh` (`DEFAULT_OLLAMA_MODEL` picker,
`stage_full_capability_for_vram_tier`),
`nvh/integrations/installs/studio_packs.py` (`STUDIO_PACKS`, `STUDIO_MODELS`
with `recommended_vram_gb`), `nvh/integrations/installs/comfyui.py`
(`TRENDING_COMFYUI_EXAMPLES`).

## Local orchestration tiers

The local model does more than answer: before a cloud call it can classify
the task, pick the advisor, rewrite the prompt for that advisor, evaluate the
answer, synthesise council responses and compress long histories — all on
your GPU at no cost. How much of that runs depends on VRAM:

| `defaults.orchestration_mode` | VRAM | What runs locally |
|---|---|---|
| `off` | any | keyword routing, template agents |
| `light` | 6 GB+ | smart routing + prompt optimisation |
| `full` | 20 GB+ | routing, agents, evaluation, synthesis, history compression |
| `auto` (default) | — | highest tier the detected VRAM supports |

```bash
nvh config get defaults.orchestration_mode
nvh config set defaults.orchestration_mode light
```

With no local model available the engine falls back to keyword routing
silently.

## How routing uses the GPU

Once Ollama is running, the router registers the local model as a provider,
scores it per task type, keeps simple queries local and escalates complex ones
to the cloud when local quality is not enough. The learning loop measures the
local model's real quality on your hardware and adjusts thresholds over time.

```bash
nvh ask "question" --local            # Ollama only; nothing leaves the machine
nvh ask "question" --prefer-nvidia    # 1.3x routing bonus for Ollama, NIM, Triton
nvh config set defaults.prefer_nvidia true
```

Before loading, nvHive checks whether a model fits VRAM: fully (GPU), partially
(GPU + CPU offload, slower), or not at all (warning plus a smaller
recommendation).

## Benchmarks

```bash
nvh bench                     # GPU tokens/s plus an AI-quality pass in one command
nvh bench --model nemotron    # a specific model; shows VRAM use during the run
```

Measured on a DGX Spark (GB10, 120 GB unified memory):

| Model | Size | tok/s |
|---|---|---|
| nemotron-mini | 2.7 GB | 86.6 |
| gemma3 | 3.3 GB | 73.4 |
| llama3.1 | 4.9 GB | 48.1 |
| nemotron-3-super | 86 GB | 24.8 |

`nvh estimate` projects throughput for other GPUs; `nvh bench` measures yours.

## Troubleshooting

- **"Ollama binary not found"** on `nvh models pull` — the rootless runtime is
  not installed yet: `nvh workstation --with-local-ai -y`, then retry.
- **Catalog empty or VRAM "unknown"** — detection found no card; `nvh status
  --deep` reports what was and was not detected.
- **A pull stalls at 0%** — check the Ollama service (`nvh services`); the
  Model Manager waits on the same runtime the Wizard uses.

Back to [README](../README.md)
