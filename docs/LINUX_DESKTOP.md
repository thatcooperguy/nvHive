# For Linux Desktop

nvHive is designed for deployment on Linux Desktop instances.

## Features

- Auto-detects cloud sessions and adapts to the available GPU tier
- All tools operate at user level -- no root, no sudo
- Session-aware: handles ephemeral environments with mounted home directories
- Auto-healing: reconnects to Ollama if the instance restarts
- GPU VRAM management: models unload after inactivity so games can reclaim VRAM

## What Happens Automatically

When you install nvHive, everything configures itself:

```
Install runs:
  1. Detects your GPU (NVIDIA via pynvml, Apple Silicon via sysctl)
  2. Reads available VRAM / unified memory
  3. Downloads the right NVIDIA Nemotron model for your hardware:

     GPU Memory        Model Auto-Downloaded         Size    Speed
     ─────────────────────────────────────────────────────────────
     < 4 GB or CPU     nemotron-mini (4B)            ~2 GB   ~30 tok/s
     4–6 GB            nemotron-mini (GPU accel.)     ~2 GB   ~50 tok/s
     6–12 GB           nemotron-small (recommended)   ~5 GB   ~75 tok/s
     12–24 GB          nemotron-small + codellama     ~9 GB   ~110 tok/s
     24–48 GB          nemotron 70B (quantized)       ~40 GB  ~40 tok/s
     48–80 GB          nemotron 70B (full quality)    ~40 GB  ~120 tok/s
     80+ GB            nemotron 120B (flagship)       ~70 GB  ~180 tok/s

  4. Installs Ollama (local model server) — no root needed
  5. Creates config with Ollama + LLM7 (anonymous, free) enabled
  6. Pulls model in background — you can start chatting immediately
  7. Adds 'nvh' to your PATH

First time: ~60 seconds. Reconnect (new VM): ~3 seconds.
```

**You never pick a model.** The platform reads your hardware and downloads the best one. On Apple Silicon, it uses Metal via Ollama with unified memory. On NVIDIA, it uses CUDA. On CPU-only systems, it uses free cloud providers.

---

Back to [README](../README.md)
