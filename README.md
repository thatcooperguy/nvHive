# nvHive

**One curl command turns a rented Linux GPU desktop — GeForce NOW, RunPod, Lambda, Vast — into a working AI lab. No root, no Docker, survives reconnects.**

[![PyPI](https://img.shields.io/pypi/v/nvhive)](https://pypi.org/project/nvhive/)
[![License: PolyForm NC](https://img.shields.io/badge/license-PolyForm%20Noncommercial-blue)](LICENSE)
[![CI](https://github.com/thatcooperguy/nvHive/actions/workflows/ci.yml/badge.svg)](https://github.com/thatcooperguy/nvHive/actions/workflows/ci.yml)

```bash
curl -sSL https://raw.githubusercontent.com/thatcooperguy/nvHive/main/install.sh | bash
```

That one command gives you:

- **A local multimodal LLM**, auto-picked for your GPU's VRAM, served by Ollama — chat and image understanding with nothing leaving the machine
- **A web dashboard** at `localhost:3000` with an AI Wizard that knows your workspace and can fix it
- **A multi-LLM router** across 23 providers (Ollama, Groq, Gemini, NVIDIA NIM, OpenAI, Anthropic, ...) — many with free tiers
- **Persistent storage layout** under `NVH_HOME`, so models and chats survive when the cloud desktop resets

Everything installs to user-owned paths on the persistent volume, and the browser only opens after every service passes a health check. You pay for GPU time, not debugging time.

---

## What happens when you run it

Three visible steps — nothing hidden, everything skippable:

**1. Model download countdown.** You're told what's downloading, how big it is, and how to skip:

```text
AI Wizard local brain: llama3.2-vision (~7.9 GB)
  This is the model the Wizard chats with. Smaller models load fast on CPU;
  bigger ones are stronger on GPU. You can change it later from the WebUI.
  Starting in 10s... press [s] to skip
```

Models are picked by VRAM: `moondream` (~1.7 GB, runs on CPU) → `minicpm-v` (12 GB+) → `llama3.2-vision` (16 GB+) → NVIDIA `nemotron-3-nano-omni` (24 GB+) → `nemotron-omni` (40 GB+). If a pull fails, the installer falls through to the next smaller model instead of dying. Headless installs can opt out with `NVH_INSTALL_MODEL_DOWNLOAD=0`.

**2. Verified bring-up.** Services start in dependency order with real health gates, shown live:

```text
                       nvHive bring-up
 Service                    Port    Status     Detail
 Local AI brain (Ollama)    11434   ✓ ready    /api/tags responding
 nvHive backend (API)       8000    ✓ ready    /v1/health ok
 Web dashboard (WebUI)      3000    ✓ ready    serving
 End-to-end test            —       ✓ ready    Wizard answered
```

The fourth row is a real smoke test: it POSTs a chat message to the Wizard and waits for an answer.

**3. Browser opens only on green.** Your first sight of the dashboard is a working dashboard — never a red "API offline" banner. If anything fails, you get the failing step, the log path, and the last 25 lines of that log inline.

---

## What you can do with it

### Local AI, immediately

The dashboard's Wizard chat runs against your local model first — $0, private, offline-capable. The CLI works the same way:

```bash
nvh "summarize this error log"      # routes to the best available model
nvh safe "review this contract"     # local only — nothing leaves the machine
```

### AI Wizard

A streaming, tool-using assistant that reads live workspace state. It can refresh models, repair the workspace, RAG over files you drag into the chat (PDFs included), and search the web — citing sources and showing cost and latency per response. Attach external [MCP tool servers](docs/MCP.md) and their tools join the Wizard's toolset too. Slash commands in chat: `/help`, `/save`, `/pin`, `/clear`, `/tools`.

### Multi-provider routing

One interface over 23 providers and 63 models. Requests are scored on capability, cost, latency, and provider health, then routed — free tiers first when you have no keys, your GPU first when you do have one. Add keys with `nvh setup`. [Provider guide](docs/PROVIDERS.md)

### Model Manager

nvHive detected your GPU at install, so the **Models** page shows whether each catalog model fits your VRAM and how much disk it needs *before* you download — then installs it with one click and live progress. Same thing from the terminal:

```bash
nvh models list --all               # fit-ranked catalog for your GPU
nvh models pull gemma3:4b           # install with live progress
```

[Model Manager guide](docs/MODELS.md)

### Agents and council mode

An **Agent Library of 100+ profiles** across 38 categories (coding, research, creative, GPU media, ops, education, …) plus six core built-ins — each mappable to a local or cloud model; your own profiles live in `$NVH_HOME/agent-profiles/`. Council mode runs one question through multiple models in parallel and synthesizes the answers:

```bash
nvh convene "Redis or Postgres for session storage?"   # multi-model deliberation
nvh agent "add unit tests for auth" --dir ./myproject  # agentic coding with QA
```

[Council docs](docs/COUNCIL.md) · [Agent tools](docs/TOOLS.md)

### Creative and studio packs

Rootless one-command installs for ComfyUI, Blender, game-dev tooling, and music production (stem splitting, transcription, generation):

```bash
nvh studio --list
nvh studio --install comfy -y
nvh studio --install creative -y
nvh studio --install music -y
```

### Built for machines that disappear

Cloud GPU desktops reset. nvHive plans for it:

- Everything that matters lives under `NVH_HOME` on the persistent volume — models, config, chats, vault, logs, jobs
- Long downloads run as resumable jobs that survive browser refreshes and reconnects
- `/pin` a conversation and a **Welcome Back** panel resumes it on the next session
- `nvh snapshot save` tarballs your state; `nvh snapshot restore` resumes it on a brand-new VM

If your persistent mount isn't auto-detected, set it before installing:

```bash
export NVH_HOME=/mnt/persist/nvhive
```

---

## Requirements

- **Linux x86_64** (the primary target; Windows and macOS installers exist — see [Releases](https://github.com/thatcooperguy/nvHive/releases/latest))
- **No root.** Everything installs to user-owned paths. No Docker required.
- **Python 3.11+** — or none at all: the installer can fetch a single-file binary (`NVH_USE_BINARY=1`)
- **GPU optional.** CPU-only machines get `moondream` locally plus cloud free tiers. An NVIDIA GPU unlocks the larger local models.
- **Disk:** ~2 GB minimum for the smallest local model; up to ~35 GB for the largest tier. The installer checks free space and tells you sizes before downloading.

Already have a Python environment? `pip install nvhive` (extras: `[vision]`, `[browser]`, `[rag]`, `[all]`).

---

## When something breaks

Three places to look, in order:

```bash
nvh services status        # per-service health table
nvh services smoke-test    # "can the Wizard actually answer?" end-to-end check
nvh doctor                 # full diagnostic
```

In the dashboard, the **Debug Report** button generates a redacted report (secrets and local paths stripped) you can paste into an issue. Logs live under `$NVH_HOME/logs/` (`ollama.log`, `api-server.log`, `model-pull.log`). `nvh services restart` recycles the stack; `nvh repair` runs safe rootless fixes.

---

## Command reference

| Command | What it does |
|---|---|
| `nvh "question"` | Route to the best available model |
| `nvh safe "question"` | Local inference only |
| `nvh convene "question"` | Multi-model council with synthesis |
| `nvh agent "task"` | Agentic coding with review loop |
| `nvh webui` | Open the dashboard |
| `nvh services start` | Verified bring-up (Ollama → API → WebUI → smoke test) |
| `nvh services stop` | Stop the stack (keeps Ollama's warm model cache) |
| `nvh studio --install <pack> -y` | Install a rootless tool pack |
| `nvh snapshot save` / `restore` | Persist state across ephemeral VMs |
| `nvh setup` | Configure providers and keys |

Full reference: [docs/COMMANDS.md](docs/COMMANDS.md)

## Documentation

| Guide | What's inside |
|---|---|
| [Linux GPU Desktop](docs/LINUX_DESKTOP.md) | The no-root cloud workstation path in depth |
| [GPU Tier Matrix](docs/GPU_TIER_MATRIX.md) | Which capabilities unlock at which VRAM |
| [Providers](docs/PROVIDERS.md) | All 23 providers, free tiers, rate limits |
| [Council](docs/COUNCIL.md) | Multi-LLM deliberation design |
| [Architecture](docs/ARCHITECTURE.md) | Routing, layers, system design |
| [SDK & API](docs/SDK_API.md) | Python SDK, REST API, OpenAI/Anthropic-compatible proxies |
| [Configuration](docs/CONFIGURATION.md) | Every knob, including `NVH_HOME` and install env vars |

## Notes

- Cloud providers receive the queries you route to them, under their own privacy policies. Use `nvh safe` to keep inference local.
- AI output can be wrong. Review agent-modified files before shipping them.

## License

**PolyForm Noncommercial 1.0.0** — you can use, modify, and share nvHive freely for any noncommercial purpose. **Selling this code or using it commercially is not permitted.** See [LICENSE](LICENSE) and [NOTICE](NOTICE.md). (Versions 0.40.0 and earlier were released under MIT and remain MIT.)

The license does not grant rights to the nvHive name or logos; forks should use distinct names and channels. See [TRADEMARKS](TRADEMARKS.md).
