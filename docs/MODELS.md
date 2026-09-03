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

One table decides which local model every ladder gets:
`nvh.core.local_models.LOCAL_MODEL_TIERS`. `install.sh` sources it as shell
variables (`nvh models tiers --shell`); `install.ps1` and `install-mac.sh`
ask it for this machine's pick (`nvh models tiers --pick chat`); `nvh setup`,
the setup wizard and `nvh studio --models` read the same module. The budget
is discrete VRAM as the driver reports it, or a unified pool (GB10 / DGX
Spark, Apple Silicon) minus the OS reserve. Sizes are the registry manifest
in decimal GB — the number `ollama list` prints — and "loaded" adds KV-cache
and CUDA-context headroom. `nvh models tiers` prints the tables embedded here.

<!-- BEGIN GENERATED: local-model-tiers -->
| Budget (GB) | Tier | num_ctx | parallel | quant | chat | code | vision | reasoning | embed | CPU fallback |
|---|---|---|---|---|---|---|---|---|---|---|
| 0-4 | cpu | 4096 | 1 | Q4_K_M | `gemma3:1b` | `qwen3:1.7b` | `moondream` | `qwen3:1.7b` | `nomic-embed-text` | `gemma3:1b` |
| 4-8 | mini | 4096 | 1 | Q4_K_M | `gemma3:4b` | `qwen3:4b` | `moondream` | `qwen3:4b` | `nomic-embed-text` | `gemma3:1b` |
| 8-12 | small | 4096 | 1 | Q4_K_M | `qwen3:8b` | `qwen3:8b` | `moondream` | `qwen3:8b` | `nomic-embed-text` | `gemma3:4b` |
| 12-16 | small-plus | 8192 | 1 | Q4_K_M | `qwen3:8b` | `qwen3:8b` | `qwen3-vl:8b` | `qwen3:8b` | `nomic-embed-text` | `gemma3:4b` |
| 16-24 | medium | 16384 | 1 | Q4_K_M | `qwen3:14b` | `qwen3:14b` | `llama3.2-vision` | `gpt-oss:20b` | `nomic-embed-text` | `gemma3:4b` |
| 24-40 | large | 32768 | 2 | Q4_K_M | `qwen3:30b-a3b` | `qwen3-coder:30b` | `llama3.2-vision` | `gpt-oss:20b` | `nomic-embed-text` | `gemma3:4b` |
| 40-48 | xl | 32768 | 2 | Q4_K_M | `nemotron3:33b` | `qwen3-coder:30b` | `llama3.2-vision` | `gpt-oss:20b` | `nomic-embed-text` | `gemma3:4b` |
| 48-80 | workstation | 65536 | 4 | Q4_K_M | `nemotron3:33b-q8` | `qwen3-coder:30b` | `llama3.2-vision` | `gpt-oss:20b` | `nomic-embed-text` | `gemma3:4b` |
| 80-96 | datacenter | 65536 | 4 | Q8_0 or F16 | `nemotron3:33b-q8` | `qwen3-coder:30b` | `llama3.2-vision` | `gpt-oss:120b` | `nomic-embed-text` | `gemma3:4b` |
| 96+ | max | 131072 | 4 | Q8_0 or F16 | `nemotron3:33b-q8` | `qwen3-coder:30b` | `llama3.2-vision` | `gpt-oss:120b` | `nomic-embed-text` | `gemma3:4b` |

| Tag | Catalog id | Quant | On disk (GB) | Loaded (GB) | MoE | Vision | Min CC |
|---|---|---|---|---|---|---|---|
| `gemma3:1b` | gemma3-1b | Q4_K_M | 0.8 | 1 |  |  |  |
| `gemma3:4b` | gemma3-4b | Q4_K_M | 3.3 | 4 |  | yes |  |
| `gpt-oss:120b` | gpt-oss-120b | MXFP4 | 65.4 | 71.9 | yes |  |  |
| `gpt-oss:20b` | gpt-oss-20b | MXFP4 | 13.8 | 15.2 | yes |  |  |
| `llama3.2-vision` | llama32-vision | Q4_K_M | 7.8 | 9.4 |  | yes | 8.0 |
| `moondream` | moondream | Q4_0 | 1.7 | 2 |  | yes |  |
| `nemotron3:33b` | nemotron3-33b | Q4_K_M | 27.6 | 30.4 | yes | yes |  |
| `nemotron3:33b-q8` | nemotron3-33b-q8 | Q8_0 | 36.5 | 40.2 | yes | yes |  |
| `nomic-embed-text` | nomic-embed-text | F16 | 0.3 | 0.4 |  |  |  |
| `qwen3-coder:30b` | qwen3-coder-30b | Q4_K_M | 18.6 | 20.5 | yes |  |  |
| `qwen3-vl:8b` | qwen3-vl-8b | Q4_K_M | 6.1 | 7.3 |  | yes |  |
| `qwen3:1.7b` | qwen3-1.7b | Q4_K_M | 1.4 | 1.7 |  |  |  |
| `qwen3:14b` | qwen3-14b | Q4_K_M | 9.3 | 11.2 |  |  |  |
| `qwen3:30b-a3b` | qwen3-30b-a3b | Q4_K_M | 18.6 | 20.5 | yes |  |  |
| `qwen3:4b` | qwen3-4b | Q4_K_M | 2.5 | 3 |  |  |  |
| `qwen3:8b` | qwen3-8b | Q4_K_M | 5.2 | 6.2 |  |  |  |
<!-- END GENERATED: local-model-tiers -->

A unified pool does not lose a flat 16 GB. Its OS reserve is an eighth of
the pool, never under 4 GB and never over the GB10's 16 GB
(`nvh.core.local_models.unified_os_reserve_gb`), so a 16 GB Apple Silicon
Mac keeps 4 GB for the OS and a 128 GB DGX Spark keeps 16; the budget is
what is left. `install.sh` reproduces the curve from the
`NVH_UNIFIED_OS_RESERVE_MIN_GB` / `_MAX_GB` / `_FRACTION` values the tier
snippet exports, so neither side types the numbers.

<!-- BEGIN GENERATED: unified-os-reserve -->
| Unified pool (GB) | OS reserve (GB) | Budget (GB) | Tier | First `recommended()` pick |
|---|---|---|---|---|
| 8 | 4 | 4 | 4-8 (mini) | `gemma3:4b` |
| 16 | 4 | 12 | 12-16 (small-plus) | `qwen3:8b` |
| 24 | 4 | 20 | 16-24 (medium) | `gpt-oss:20b` |
| 32 | 4 | 28 | 24-40 (large) | `qwen3:30b-a3b` |
| 48 | 6 | 42 | 40-48 (xl) | `nemotron3:33b` |
| 64 | 8 | 56 | 48-80 (workstation) | `nemotron3:33b-q8` |
| 96 | 12 | 84 | 80-96 (datacenter) | `nemotron3:33b-q8` |
| 128 | 16 | 112 | 96+ (max) | `nemotron3:33b-q8` |
| 192 | 16 | 176 | 96+ (max) | `nemotron3:33b-q8` |
<!-- END GENERATED: unified-os-reserve -->

`install.sh` pulls the tier's chat pick and, if that tag cannot be pulled,
walks the chain below one rung at a time; `nvh models pull --recommended`
and `nvh studio --install-models recommended -y` pull the `recommended()`
set. On a unified pool MoE models lead that set because dense weights are
bandwidth-bound there; that column is computed on the smallest pool whose
budget after the OS reserve is the tier's floor, and each cell names it.

<!-- BEGIN GENERATED: installer-pull-ladder -->
| Budget (GB) | Tier | `install.sh` pull chain (first success wins) | `recommended()` — discrete card | `recommended()` — unified pool (MoE first) |
|---|---|---|---|---|
| 0-4 | cpu | `gemma3:1b` | `gemma3:1b`, `qwen3:1.7b`, `moondream`, `nomic-embed-text` | 4 GB pool: `gemma3:1b`, `qwen3:1.7b`, `moondream`, `nomic-embed-text` |
| 4-8 | mini | `gemma3:4b` → `gemma3:1b` | `gemma3:4b`, `qwen3:4b`, `moondream`, `nomic-embed-text`, `gemma3:1b` | 8 GB pool: `gemma3:4b`, `qwen3:4b`, `moondream`, `nomic-embed-text`, `gemma3:1b` |
| 8-12 | small | `qwen3:8b` → `gemma3:4b` → `gemma3:1b` | `qwen3:8b`, `moondream`, `nomic-embed-text`, `gemma3:4b` | 12 GB pool: `qwen3:8b`, `moondream`, `nomic-embed-text`, `gemma3:4b` |
| 12-16 | small-plus | `qwen3:8b` → `gemma3:4b` → `gemma3:1b` | `qwen3:8b`, `qwen3-vl:8b`, `nomic-embed-text`, `gemma3:4b` | 16 GB pool: `qwen3:8b`, `qwen3-vl:8b`, `nomic-embed-text`, `gemma3:4b` |
| 16-24 | medium | `qwen3:14b` → `qwen3:8b` → `gemma3:4b` → `gemma3:1b` | `qwen3:14b`, `llama3.2-vision`, `nomic-embed-text`, `gemma3:4b` | 20 GB pool: `gpt-oss:20b`, `qwen3:14b`, `llama3.2-vision`, `nomic-embed-text`, `gemma3:4b` |
| 24-40 | large | `qwen3:30b-a3b` → `qwen3:14b` → `qwen3:8b` → `gemma3:4b` → `gemma3:1b` | `qwen3:30b-a3b`, `qwen3-coder:30b`, `llama3.2-vision`, `nomic-embed-text`, `gemma3:4b` | 28 GB pool: `qwen3:30b-a3b`, `qwen3-coder:30b`, `gpt-oss:20b`, `llama3.2-vision`, `nomic-embed-text`, `gemma3:4b` |
| 40-48 | xl | `nemotron3:33b` → `qwen3:30b-a3b` → `qwen3:14b` → `qwen3:8b` → `gemma3:4b` → `gemma3:1b` | `nemotron3:33b`, `qwen3-coder:30b`, `llama3.2-vision`, `nomic-embed-text`, `gemma3:4b` | 46 GB pool: `nemotron3:33b`, `qwen3-coder:30b`, `gpt-oss:20b`, `llama3.2-vision`, `nomic-embed-text`, `gemma3:4b` |
| 48-80 | workstation | `nemotron3:33b-q8` → `nemotron3:33b` → `qwen3:30b-a3b` → `qwen3:14b` → `qwen3:8b` → `gemma3:4b` → `gemma3:1b` | `nemotron3:33b-q8`, `qwen3-coder:30b`, `llama3.2-vision`, `nomic-embed-text`, `gemma3:4b` | 55 GB pool: `nemotron3:33b-q8`, `qwen3-coder:30b`, `gpt-oss:20b`, `llama3.2-vision`, `nomic-embed-text`, `gemma3:4b` |
| 80-96 | datacenter | `nemotron3:33b-q8` → `nemotron3:33b` → `qwen3:30b-a3b` → `qwen3:14b` → `qwen3:8b` → `gemma3:4b` → `gemma3:1b` | `nemotron3:33b-q8`, `qwen3-coder:30b`, `llama3.2-vision`, `nomic-embed-text`, `gemma3:4b` | 91 GB pool: `nemotron3:33b-q8`, `qwen3-coder:30b`, `gpt-oss:120b`, `llama3.2-vision`, `nomic-embed-text`, `gemma3:4b` |
| 96+ | max | `nemotron3:33b-q8` → `nemotron3:33b` → `qwen3:30b-a3b` → `qwen3:14b` → `qwen3:8b` → `gemma3:4b` → `gemma3:1b` | `nemotron3:33b-q8`, `qwen3-coder:30b`, `llama3.2-vision`, `nomic-embed-text`, `gemma3:4b` | 110 GB pool: `nemotron3:33b-q8`, `qwen3-coder:30b`, `gpt-oss:120b`, `llama3.2-vision`, `nomic-embed-text`, `gemma3:4b` |
<!-- END GENERATED: installer-pull-ladder -->

ComfyUI profiles follow their own ladder: `starter` on 8 GB, `edit` on 12 GB,
`control` on 16 GB, `video` on 24 GB, `video-pro` on 40 GB.

## The capability matrix

Chat plus Wizard vision is installed on every GPU by default. Image, video and
local speech generation are separate backends behind an opt-in, because they
download tens of gigabytes.

### What is "Nemotron Omni", really?

`nemotron3:33b` and `nemotron3:33b-q8` are NVIDIA's Nemotron 3 Nano Omni — a
multimodal *language* model. It gives the Wizard sight — screenshots,
uploaded images and documents alongside text — and reasons about them. It
does **not** generate images (that is ComfyUI plus a diffusion model),
synthesise speech (Edge TTS / Piper / XTTS) or recognise speech (Whisper /
WhisperX). When this codebase says "Omni", read "the Wizard's vision".

The Wizard chat and vision rows come straight from the tier table — every
tag it picks as `chat` or `vision`, with the first tier and the smallest
budget that gets it:

<!-- BEGIN GENERATED: wizard-chat-matrix -->
| Tag | Ladder role | First tier | Min budget (GB) | Sees images | Pulled by `install.sh` |
|---|---|---|---|---|---|
| `gemma3:1b` | chat | cpu | 0 |  | yes — chat pick from the cpu tier |
| `moondream` | vision | cpu | 0 | yes | no — `nvh models pull moondream` |
| `gemma3:4b` | chat | mini | 4 | yes | yes — chat pick from the mini tier |
| `qwen3:8b` | chat | small | 8 |  | yes — chat pick from the small tier |
| `qwen3-vl:8b` | vision | small-plus | 12 | yes | no — `nvh models pull qwen3-vl:8b` |
| `llama3.2-vision` | vision | medium | 16 | yes | no — `nvh models pull llama3.2-vision` |
| `qwen3:14b` | chat | medium | 16 |  | yes — chat pick from the medium tier |
| `qwen3:30b-a3b` | chat | large | 24 |  | yes — chat pick from the large tier |
| `nemotron3:33b` | chat | xl | 40 | yes | yes — chat pick from the xl tier |
| `nemotron3:33b-q8` | chat | workstation | 48 | yes | yes — chat pick from the workstation tier |
<!-- END GENERATED: wizard-chat-matrix -->

The remaining backends are hand-maintained in `studio_packs.py` and
`comfyui.py`:

| Capability | Backend | Min VRAM | Enabled by `install.sh` | Manual |
|---|---|---|---|---|
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
the installer *stages* the packs cumulatively by the tier the detected VRAM
lands in — the same budget the [generated tier table](#local-models-by-vram)
is keyed on. The `Enabled by install.sh` column above names each pack's tier:
the ComfyUI starter arrives with the `small` tier, `small-plus` adds image
editing, `medium` adds ControlNet, `large` adds the video, speech and music
packs and `xl` adds the 14B video profile. The thresholds themselves live in
`install.sh` (`nvh_capability_tiers_for_vram`) and are not repeated here.

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

Source of truth in code: `nvh/core/local_models.py` (`LOCAL_MODEL_TIERS`,
rendered by `nvh models tiers` and regenerated into this page by
`scripts/gen_models_doc.py`), `install.sh`
(`stage_full_capability_for_vram_tier`),
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
nvh bench --model qwen3:8b    # a specific model; shows VRAM use during the run
```

Measured on a DGX Spark (GB10, 120 GB unified memory) —
`nvh/utils/gpu_emulation.py` `_MEASURED_BASELINES`. Some of these tags have
since left the tier table; they stay here as measurements, not as picks:

<!-- BEGIN GENERATED: gb10-baselines -->
| Model | Size (GB) | tok/s |
|---|---|---|
| `nemotron-mini` | 2.7 | 86.6 |
| `gemma3` | 4 | 73.4 |
| `gemma3:4b` | 3.3 | 73.4 |
| `llama3.1` | 4.9 | 48.1 |
| `qwen3:8b` | 5.2 | 44 |
| `llama3.2-vision` | 7.8 | 30 |
| `nemotron-3-super` | 86 | 24.8 |
<!-- END GENERATED: gb10-baselines -->

`nvh estimate` projects throughput for other GPUs; `nvh bench` measures yours.

## Troubleshooting

- **"Ollama binary not found"** on `nvh models pull` — the rootless runtime is
  not installed yet: `nvh workstation --with-local-ai -y`, then retry.
- **Catalog empty or VRAM "unknown"** — detection found no card; `nvh status
  --deep` reports what was and was not detected.
- **A pull stalls at 0%** — check the Ollama service (`nvh services`); the
  Model Manager waits on the same runtime the Wizard uses.

Back to [README](../README.md)
