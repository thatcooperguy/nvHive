# GPU Capability Matrix

This is the canonical reference for which nvHive capabilities are
automatically gated on VRAM, what the recommended backend for each
capability is, and which of those capabilities `install.sh` will
auto-stage on first install vs. wait for the user to opt in from the
Wizard or `nvh studio` CLI.

The owner-facing version of the question is: **"if the GPU is large
enough we can have the NVIDIA Omni multi-model enabled for image
generation, speech, etc."** The honest answer is that "Omni" is a
multimodal *language* model — it adds **vision** (image
understanding) to the Wizard, not image generation or speech
synthesis. Those are separate backends. nvHive can still auto-enable
all of them on a big enough GPU; this doc spells out the rules.

## What is "Nemotron Omni", really?

`nemotron-omni` and `nemotron-3-nano-omni` are NVIDIA's flagship
**multimodal LLMs**. They give the AI Wizard *sight* — the ability
to look at screenshots, uploaded images, and documents alongside
text — and reason about them. They are NOT:

- image generators (you don't ask Omni to "draw a cat"; that's
  ComfyUI + a diffusion model),
- speech synthesizers (no built-in TTS; that's Edge TTS / Piper /
  XTTS in the music producer lab pack),
- speech recognizers (no built-in STT; that's Whisper / WhisperX,
  which lives in the music producer lab pack).

When you see "Omni" in this codebase, think "the Wizard's vision".
The other capabilities live in their own packs and have their own
VRAM gates.

## The matrix

| Capability                  | Backend                                | Min VRAM | Auto-enabled by `install.sh`         | Where to enable manually                                  |
| --------------------------- | -------------------------------------- | -------- | ------------------------------------ | --------------------------------------------------------- |
| Chat — tiny vision          | Ollama + `moondream`                   | 0 GB     | yes (every tier; CPU OK)             | Wizard chat picker                                        |
| Chat — small vision         | Ollama + `minicpm-v`                   | 12 GB    | yes (12+ GB tier)                    | Wizard chat picker                                        |
| Chat — vision               | Ollama + `llama3.2-vision`             | 16 GB    | yes (16+ GB tier)                    | Wizard chat picker                                        |
| Chat — Omni Nano (24+ GB)   | Ollama + `nemotron-3-nano-omni`        | 24 GB    | yes (24+ GB tier; HF fallback)       | Wizard chat picker                                        |
| Chat — Omni flagship        | Ollama + `nemotron-omni`               | 40 GB    | yes (40+ GB tier; HF fallback)       | Wizard chat picker                                        |
| Image generation (starter)  | ComfyUI + Z-Image-Turbo                | 8 GB     | only with `NVH_INSTALL_FULL_CAPABILITY=1` | `nvh studio --install comfy -y`                      |
| Image generation (edit)     | ComfyUI + Qwen Image Edit 2509         | 12 GB    | only with `NVH_INSTALL_FULL_CAPABILITY=1` | `nvh studio --install comfy -y` (+ Qwen Edit template) |
| Image generation (control)  | ComfyUI + FLUX.1 ControlNet            | 16 GB    | only with `NVH_INSTALL_FULL_CAPABILITY=1` | `nvh studio --install comfy -y` (+ FLUX template)    |
| Video generation (5B)       | ComfyUI + Wan 2.2 5B                   | 8 GB     | only with `NVH_INSTALL_FULL_CAPABILITY=1` (24+ GB tier) | `nvh studio --install comfy -y` (+ Wan 5B)        |
| Video generation (14B)      | ComfyUI + Wan 2.2 14B i2v              | 24 GB    | only with `NVH_INSTALL_FULL_CAPABILITY=1` (24+ GB tier) | `nvh studio --install comfy -y` (+ Wan 14B)       |
| Speech — TTS (cloud, free)  | Edge TTS (`nvh/core/voice.py`)         | 0 GB     | yes (always; needs no GPU)           | always on; `nvh` voice replies                            |
| Speech — STT (cloud, free)  | Groq Whisper API                       | 0 GB     | requires Groq API key (`nvh groq`)   | `nvh groq` to add the free key                            |
| Speech — STT/TTS (local)    | WhisperX + faster-whisper (+ TTS opts) | 8 GB     | only with `NVH_INSTALL_FULL_CAPABILITY=1` (24+ GB tier) | `nvh studio --install music-producer-lab -y`     |
| Music generation (local)    | ACE-Step 1.5                            | 6 GB     | only with `NVH_INSTALL_FULL_CAPABILITY=1` (24+ GB tier) | `nvh studio --install music -y`                  |

## The auto-enable rules in `install.sh`

The default install always lands a working **chat + Wizard vision**
on whatever GPU it finds — no opt-in needed. That's the existing
`DEFAULT_OLLAMA_MODEL` picker around line 460.

Image generation, video generation, and local speech are gated
behind the `NVH_INSTALL_FULL_CAPABILITY=1` environment variable
because they cumulatively download tens of gigabytes; we don't want
to surprise a student on school Wi-Fi.

When you DO opt in with `NVH_INSTALL_FULL_CAPABILITY=1`, the rules
are:

```
VRAM >= 8 GB  -> stage ComfyUI starter (Z-Image-Turbo text→image)
VRAM >= 12 GB -> + image-edit profile (Qwen Image Edit 2509)
VRAM >= 16 GB -> + control profile (FLUX.1 ControlNet)
VRAM >= 24 GB -> + video profile (Wan 2.2 5B)
                + speech pack (music-producer-lab: WhisperX, faster-whisper)
                + music pack (ACE-Step)
VRAM >= 40 GB -> + video-pro profile (Wan 2.2 14B i2v)
```

These are all *staged*, not pulled. Staging means: the install
script writes a one-line marker
(`$NVH_HOME/state/capability/auto-enable.json`) recording the
qualifying capability tokens and pack ids. The marker is written for
a future WebUI/Wizard consumer — **nothing reads it yet**, so on its
own `NVH_INSTALL_FULL_CAPABILITY=1` has no runtime effect beyond the
file. The only knob with a runtime effect today is the companion
`NVH_INSTALL_FULL_CAPABILITY_DOWNLOAD=1`, which pulls the staged
packs inline at install time.

To force-pull everything inline at install time (slow, big disk
hit, only for headless cloud images that won't have a browser
later):

```
NVH_INSTALL_FULL_CAPABILITY=1 NVH_INSTALL_FULL_CAPABILITY_DOWNLOAD=1 bash install.sh
```

## Honest scope notes

- **Speech is the weakest leg.** nvHive treats speech as a
  music-lab side capability today (WhisperX rides in the
  `music-producer-lab` pack). There is no dedicated `speech` pack
  and no local TTS model gated by VRAM. The default voice path
  uses Edge TTS (cloud, free) and Groq Whisper (cloud, free with a
  key). Those work on any GPU including no GPU at all. If
  speech-on-the-GPU becomes a hard requirement, the next step is
  splitting `music-producer-lab` into a `speech-lab` (WhisperX +
  Piper / XTTS) so it can be enabled without the music-generation
  cost.
- **Image generation requires a separate diffusion model.**
  Nemotron Omni does NOT generate images. It can describe an image
  for you and the Wizard can then pass that description into a
  ComfyUI prompt, but the actual pixels come out of ComfyUI's
  backend, not Omni.
- **Cloud GRID GPUs.** Cloud sessions on GRID/virtual GPUs report
  VRAM through `nvidia-smi` the same way as real cards; the matrix
  applies as written.

## See also

- `install.sh` — the `DEFAULT_OLLAMA_MODEL` picker and the new
  `stage_full_capability_for_vram_tier` helper.
- `nvh/integrations/installs/studio_packs.py` — `STUDIO_PACKS` and
  `STUDIO_MODELS` with `recommended_vram_gb` on every entry.
- `nvh/integrations/installs/comfyui.py` — `TRENDING_COMFYUI_EXAMPLES`
  with `install_profile` and `recommended_vram_gb`.
- `nvh/core/voice.py` — cloud-first speech defaults.
