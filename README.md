# nvHive

**One command. Every AI model you have. Automatically assembled into the best team for each task.**

![version](https://img.shields.io/badge/version-0.33.1-blue) ![python](https://img.shields.io/badge/python-3.11%2B-blue) ![license](https://img.shields.io/badge/license-MIT-green) ![ci](https://img.shields.io/badge/CI-Linux%20%7C%20Windows%20%7C%20macOS-blue)

```bash
nvh "What is a binary search tree?"              # → answers (single best advisor)
nvh "Fix the timeout bug in council.py"          # → auto-detects coding task → agent mode
nvh "Should we use Redis or Postgres?"           # → auto-detects debate → council (3+ advisors)
nvh "take a screenshot and describe my desktop"  # → desktop agent (vision + tools)
nvh "setup comfyui"                              # → agent installs, configures, launches
```

<p align="center">
  <img src="docs/screenshots/terminal-demo-v2.gif" alt="nvHive CLI" width="640">
</p>

---

## Install

Three ways to get nvHive — pick the one that matches your setup. No Docker, no container runtime, no root required.

### Option 1 — One-line installer (recommended for GPU VMs)

```bash
curl -sSL https://raw.githubusercontent.com/thatcooperguy/nvHive/main/install.sh | bash
```

Works on any Linux box with no root. Installs to `NVH_HOME` when set, otherwise `~/.nvh/` for new installs, uses Python `venv` + `pip` by default, offers a rootless micromamba fallback only when the cloud image needs it, pulls Ollama if you have an NVIDIA GPU, and writes a sensible default config.

Windows: `iwr -useb https://raw.githubusercontent.com/thatcooperguy/nvHive/main/install.ps1 | iex`
macOS: `curl -sSL https://raw.githubusercontent.com/thatcooperguy/nvHive/main/install-mac.sh | bash`

### Option 2 — Single-file binary (no Python needed)

Fully standalone. No Python install, no pip, no venv. Click your OS:

<p align="center">
  <a href="https://github.com/thatcooperguy/nvHive/releases/latest/download/nvh-linux-x86_64">
    <img src="https://img.shields.io/badge/Download%20for%20Linux-76B900?style=for-the-badge&logo=linux&logoColor=black" alt="Download for Linux (x86_64)">
  </a>
  &nbsp;
  <a href="https://github.com/thatcooperguy/nvHive/releases/latest/download/nvh-macos-arm64">
    <img src="https://img.shields.io/badge/Download%20for%20macOS-76B900?style=for-the-badge&logo=apple&logoColor=black" alt="Download for macOS (Apple Silicon)">
  </a>
  &nbsp;
  <a href="https://github.com/thatcooperguy/nvHive/releases/latest/download/nvh-windows-x86_64.exe">
    <img src="https://img.shields.io/badge/Download%20for%20Windows-76B900?style=for-the-badge&logo=windows&logoColor=black" alt="Download for Windows (x86_64)">
  </a>
</p>

On Linux/macOS after download: `chmod +x nvh-* && ./nvh-*`. Full asset list (wheel, sdist, checksums) lives on the [Releases page](https://github.com/thatcooperguy/nvHive/releases/latest).

### Option 3 — pip from PyPI (for existing Python environments)

```bash
pip install nvhive              # core
pip install "nvhive[vision]"    # + desktop agent (screenshot, click, type)
pip install "nvhive[browser]"   # + headless browser (playwright)
pip install "nvhive[all]"       # everything
```

### First run

```bash
nvh                              # guided setup — GPU detect, provider keys, local model pulls
nvh workstation --all -y         # Linux GPU desktop: launcher + WebUI + ComfyUI + studio packs
nvh webui                        # Setup > Models lets you choose exact local downloads
nvh studio --install starter -y  # rootless LLMs + agents + ComfyUI nodes + game-dev tools
nvh "your question"              # just ask — nvHive figures out the rest
```

For a fresh Linux cloud desktop where only a mounted file volume persists,
choose that mount before installing large local assets:

```bash
export NVH_HOME=/mnt/persist/nvhive
curl -sSL https://raw.githubusercontent.com/thatcooperguy/nvHive/main/install.sh | bash
source "$NVH_HOME/nvh-env.sh"
nvh workstation --home-dir "$NVH_HOME" --all -y
```

`nvh workstation --all -y` creates a desktop launcher, starts the WebUI, prepares rootless local model tooling, installs ComfyUI with nvHive starter workflow examples, and adds AI Studio packs for LLMs, agents, ComfyUI nodes, and Linux game projects.

Use packs directly when you want a specific no-root lab:

```bash
nvh studio --list
nvh studio --models
nvh studio --install-models recommended -y
nvh studio --install llms -y
nvh studio --install agents -y
nvh studio --install comfy -y
nvh studio --install game -y
nvh studio --install python-runtime-fallback -y  # optional rescue pack, not the default path
```

The WebUI setup wizard includes a model picker with GPU-fit badges, disk
estimates, installed status, and persistent install jobs saved under
`$NVH_HOME/jobs`. ComfyUI, AI Studio packs, and local model downloads keep
showing progress after a browser refresh and can be canceled from the wizard.
The ComfyUI step lets students select workflow examples and save a model
download plan with source links, because many image/video weights are large or
require upstream terms.

On first run, `nvh` launches a guided 3-step setup — GPU detection, provider keys, local model pulls. Works immediately with local models (no signup needed). Every step is skippable. Run `nvh setup` anytime to reconfigure.

<p align="center">
  <img src="docs/screenshots/setup-flow.svg" alt="nvHive 3-Step Setup Flow" width="900">
</p>

### WebUI

`nvh webui` launches a full-screen dashboard at `localhost:3000` — chat, council mode, advisor status, analytics, system stats, and setup flows. NVIDIA corporate theme, keyboard-first (Ctrl+K command palette, Ctrl+B collapse sidebar).

<p align="center">
  <img src="docs/screenshots/webui-walkthrough.gif" alt="nvHive WebUI walkthrough" width="900">
</p>

**GPU tier → model recommendations:**

| VRAM | Text Model | Vision Model | Behavior |
|------|-----------|-------------|----------|
| 0 GB (no GPU) | Cloud only | Cloud fallback | Free tiers first (Groq, LLM7, GitHub) |
| 4-8 GB | `nemotron-mini` | `moondream` | Basic local + desktop agent |
| 12-16 GB | `qwen2.5-coder:7b` | `minicpm-v` | Coding + vision local |
| 24 GB | `gemma2:27b` | `llama3.2-vision` | Strong text + best vision |
| 48 GB | `llama3.3:70b` | `llama3.2-vision` | Full power local |
| 96+ GB | Multiple 70B models | `llama3.2-vision` | Full local council, $0 |

Setup auto-detects your VRAM and recommends models that fit concurrently. No root/sudo needed for nvHive packs: tools install under `NVH_HOME` (`bin`, `models`, `runtimes`, `apps`, `webui`, `studio`, `comfyui`, `cache`, and `config`). [Full GPU guide](docs/GPU_DETECTION.md)

---

## Why nvHive

**Council scored 68% higher than a single model — at $0 cost.** Three free providers running in parallel outperformed a single model on accuracy, completeness, and coherence. [Benchmark details below.](#benchmark-results)

- **Smart team assembly.** nvHive generates expert agents for your question and matches each to the best LLM for their specialty — a "Security Engineer" agent routes to a security-strong provider, a "Database Expert" to one suited for database queries.
- **Automatic orchestration.** Coding tasks get a planner + coder + reviewer. Complex questions get a council. Simple questions get the fastest advisor. All automatic.
- **Scales with what you have.** 1 provider → single-model answers. 3+ providers → council on complex questions. Local GPU → free inference alongside cloud. DGX Spark → three 70B models in parallel, fully local.
- **4-layer safety guardrails.** Command blocklist, filesystem boundary enforcement, secrets redaction, and resource limits.

<p align="center">
  <img src="docs/screenshots/smart-router.svg" alt="nvHive Smart Router" width="900">
</p>

---

## Architecture

<p align="center">
  <img src="docs/screenshots/architecture.svg" alt="nvHive Full Stack Architecture" width="900">
</p>

9 layers from `pip install` to GPU inference — install, setup, 4 user interfaces, intent detection, 5 execution modes, smart routing, tool registry, 23+ AI providers, and the hardware stack. Local-first with cloud fallback. [Architecture docs](docs/ARCHITECTURE.md)

---

## Features

### Desktop Agent

AI that sees your screen, controls mouse/keyboard, installs software, and navigates browsers — powered by local vision models.

```bash
nvh "take a screenshot and describe my desktop"
nvh "setup comfyui"                    # agent: git clone → pip install → launch → verify
nvh "open firefox and go to github.com"
```

Vision pipeline: screenshot → local vision model (llama3.2-vision / minicpm-v) → coordinate estimation → action → verify. Falls back to cloud vision if no local model. Works on Linux (X11), macOS, and Windows. [Desktop agent docs](docs/LINUX_DESKTOP.md)

### Agentic Coding

Multi-model coding agent with dynamic expert referral, iterative QA, parallel execution, and vision/browser tools.

```bash
nvh agent "Fix the streaming timeout bug in council.py"
nvh agent "Add unit tests for auth" --dir ./myproject
nvh agent "Build the notification service" --sandbox     # Docker-isolated
nvh review                     # multi-model code review
nvh test-gen nvh/core/council.py     # AI test generation
```

Key capabilities: dynamic expert referral, iterative QA refinement, parallel pipeline, Docker sandbox, execution checkpoints with rollback, LLM drift detection, multi-repo workspaces, and VS Code extension. Scales from no-GPU (fully cloud) to DGX Spark (3 local 70B models). [Agentic coding docs](docs/TOOLS.md)

### Council Mode

Run the same query through multiple providers in parallel, then synthesize. Expert personas generated per query, each assigned to a different model. Responses analyzed for agreement, synthesized by a non-member provider with a confidence score.

```bash
nvh convene "Should we use Redis or Postgres for sessions?"   # 3 models → synthesis
nvh throwdown "Review this architecture for scalability"      # 3-pass deep analysis with critique
```

Different models have different blind spots — council surfaces all perspectives. Council with 3 free providers costs $0. [Council docs](docs/COUNCIL.md)

### Smart Routing

Each request is scored across capability (40%), cost (30%), latency (20%), and health (10%), then routed to the highest-scoring provider. Routing improves over time — after 20 queries per provider, it's fully data-driven.

```bash
nvh ask --escalate "Design a distributed lock manager"    # try free first, upgrade if uncertain
nvh ask --verify "Is eval() safe in Python?"              # cross-model verification
nvh routing-stats    # see learned vs static scores
nvh health           # provider resilience dashboard
```

Local-first with NVIDIA GPUs: simple queries route to your GPU via Ollama — no cloud, no cost, no data leaving your machine. `--prefer-nvidia` gives a 1.3x routing bonus to NVIDIA hardware. [Routing docs](docs/ARCHITECTURE.md)

---

## Providers

**23 providers. 63 models. 25 free — no credit card required.**

| Tier | Providers | Rate Limits |
|------|-----------|-------------|
| **Free (no signup)** | Ollama (local), LLM7 | Unlimited / 30 RPM |
| **Free (email signup)** | Groq, GitHub Models, Cerebras, SambaNova, Cohere, AI21, SiliconFlow, HuggingFace | 15-30 RPM |
| **Free (account)** | Google Gemini, Mistral, NVIDIA NIM | 15-1000 RPM |
| **Paid** | OpenAI, Anthropic, DeepSeek, Fireworks, Together, OpenRouter, Grok | Pay per token |

[Full provider guide](docs/PROVIDERS.md)

---

## Integrations

nvHive exposes a CLI (`nvh`), web dashboard (`nvh webui`), Python SDK (`import nvh`), MCP server for Claude Code, and OpenAI/Anthropic-compatible API proxies.

```python
import nvh

response = await nvh.complete([{"role": "user", "content": "Explain quicksort"}])
result = await nvh.convene("Architecture review", cabinet="engineering")
```

| Integration | Setup |
|-------------|-------|
| Anthropic SDK | `ANTHROPIC_BASE_URL=http://localhost:8000/v1/anthropic` |
| OpenAI SDK | `OPENAI_BASE_URL=http://localhost:8000/v1/proxy` |
| Claude Code | `claude mcp add nvhive -- python -m nvh.mcp_server` |
| NemoClaw | `nvh nemoclaw --start` — [NemoClaw docs](docs/NEMOCLAW.md) |

[SDK & API reference](docs/SDK_API.md) | [Claude Code integration](docs/CLAUDE_CODE_INTEGRATION.md) | [OpenClaw migration](docs/OPENCLAW_MIGRATION.md)

---

## Benchmark Results

Real data from NVIDIA DGX Spark (GB10, 120GB). 16 prompts across code generation, debugging, reasoning, math, creative writing, and Q&A. Judged by OpenAI with ground truth verification.

| Mode | Accuracy | Completeness | Coherence | **Overall** | Cost |
|------|----------|-------------|-----------|---------|------|
| Single Model (Nemotron Super) | 5.5 | 5.7 | 5.0 | **5.1** | $0.00 |
| **Council (Ollama + Groq + Google)** | **9.0** | **8.0** | **9.0** | **8.6** | **$0.00** |

```bash
nvh bench              # GPU speed (tokens/sec)
nvh bench -q           # speed + quality comparison
nvh health             # provider resilience
```

Results vary by hardware and workload — run `nvh bench` to measure on your setup.

---

## Core Commands

| Command | What It Does |
|---------|-------------|
| `nvh "question"` | Smart route to best available model |
| `nvh convene "question"` | Council consensus (3+ models) |
| `nvh throwdown "question"` | Three-pass deep analysis with critique |
| `nvh agent "task"` | Agentic coding with expert referral + QA |
| `nvh review` | Multi-model code review |
| `nvh test-gen file.py` | AI test generation with verification |
| `nvh safe "question"` | Local only — nothing leaves your machine |
| `nvh serve` | Start API server (OpenAI + Anthropic proxy) |
| `nvh webui` | Launch web dashboard |
| `nvh health` | Provider resilience dashboard |
| `nvh bench` | GPU speed test (tokens/sec) |
| `nvh setup` | Interactive provider setup |
| `nvh doctor` | Full diagnostic dump |

[Full command reference](docs/COMMANDS.md) (50+ commands)

---

## Documentation

| Guide | Description |
|-------|-------------|
| [Getting Started](docs/GETTING_STARTED.md) | First-time setup |
| [Student GPU Cloud / Linux Desktop](docs/LINUX_DESKTOP.md) | No-root NVIDIA Linux workstation and ComfyUI guide |
| [Commands](docs/COMMANDS.md) | Full CLI reference (50+ commands) |
| [Providers](docs/PROVIDERS.md) | 23 providers, rate limits, free tiers |
| [Council System](docs/COUNCIL.md) | Multi-LLM consensus with confidence scoring |
| [Architecture](docs/ARCHITECTURE.md) | System design and adaptive routing |
| [GPU Detection](docs/GPU_DETECTION.md) | Auto-detection, model selection, OOM protection |
| [SDK & API](docs/SDK_API.md) | Python SDK, REST API, proxies |
| [Agent Tools](docs/TOOLS.md) | Agent tools and capabilities |
| [Configuration](docs/CONFIGURATION.md) | Configuration reference |
| [Web UI](docs/WEBUI.md) | Web dashboard |
| [Deploy Without Root](docs/DEPLOY_NO_ROOT.md) | No-root install on servers |
| [Windows Troubleshooting](docs/TROUBLESHOOTING_WINDOWS.md) | Encoding, segfaults, port issues |
| [Releasing](docs/RELEASING.md) | Release runbook |

---

## Important Notes

- **Data Privacy**: Cloud providers transmit queries to third-party APIs subject to each provider's privacy policy. Use `nvh safe` or `--prefer-nvidia` to keep inference local.
- **AI Accuracy**: AI-generated outputs may contain errors. Review agent-modified files before committing to production.
- **Security**: Safety guardrails use pattern-matching heuristics. For sensitive environments, use `--sandbox` with Docker isolation.
- **Benchmarks**: Results measured on NVIDIA DGX Spark reference hardware. Results vary by hardware, provider, and workload.

## License

MIT License. See [LICENSE](LICENSE) for details.
