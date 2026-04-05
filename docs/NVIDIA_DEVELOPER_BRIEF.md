# nvHive + NVIDIA: Open-Source LLM Orchestration for NVIDIA AI Infrastructure

## Executive Summary

nvHive is an open-source multi-LLM orchestration platform that drives developer adoption of NVIDIA GPU inference. It routes AI queries across 23 providers — with a **local-first architecture that prioritizes NVIDIA hardware**: Ollama with Nemotron models, NVIDIA NIM, and Triton Inference Server.

When a developer installs nvHive, their NVIDIA GPU becomes the default inference engine. Cloud APIs are the fallback, not the primary path. This inverts the industry norm where cloud is default and local is an afterthought.

**Key metrics:** MIT licensed, on PyPI (`pip install nvhive`), 63 models, 25 free providers, 217 tests passing, CLI + WebUI + SDK + API.

---

## Why This Matters for NVIDIA

### 1. Developer Adoption Funnel

Every nvHive user with an NVIDIA GPU runs local inference by default:

```
Developer installs nvHive
    → nvHive auto-detects NVIDIA GPU
    → Downloads optimal Nemotron model for their VRAM
    → Simple queries run locally (free, fast, private)
    → Complex queries escalate to cloud only when needed
    → Developer experiences NVIDIA inference quality firsthand
```

This is a frictionless path from "has an NVIDIA GPU" to "actively using it for AI inference daily."

### 2. Multi-GPU and Fleet Orchestration

nvHive's routing engine scores providers on capability, cost, latency, and health. For NVIDIA infrastructure, this means:

- **Single GPU workstation**: Ollama + Nemotron, automatic model selection based on VRAM
- **Multi-GPU server**: Triton Inference Server with TensorRT-LLM, nvHive load-balances across models
- **NIM deployment**: NVIDIA NIM endpoints integrated as a first-class provider
- **Hybrid fleet**: Local GPUs for simple queries, NIM for specialized models, cloud for overflow

### 3. NemoClaw Integration

nvHive is already integrated with NVIDIA NemoClaw (OpenShell):

- **Inference provider**: NemoClaw agents route all LLM calls through nvHive's smart router
- **MCP tool server**: Agents can call `council()` and `throwdown()` for multi-model consensus
- **Privacy routing**: `x-nvhive-privacy: local-only` header forces all inference to stay on-device

### 4. Council Consensus on NVIDIA Hardware

nvHive's council mode runs multiple models **on the same GPU** in sequence, then synthesizes their responses. A single RTX 4090 can run 3 different Nemotron variants and produce consensus — no cloud required, no data leaving the machine.

This is unique: **multi-model AI consensus running entirely on NVIDIA consumer hardware.**

---

## Technical Architecture

```
┌─────────────────────────────────────────────────┐
│                  nvHive Engine                    │
├─────────────────────────────────────────────────┤
│  Smart Router → Local-First Broker → Fallback   │
│  Council Orchestrator → Multi-Model Consensus   │
│  Budget Controls → Cost Optimization            │
└───────────┬─────────────┬──────────────┬────────┘
            │             │              │
     ┌──────▼─────┐ ┌────▼────┐  ┌─────▼──────┐
     │   Ollama    │ │ NVIDIA  │  │   Triton   │
     │  Nemotron   │ │   NIM   │  │  TRT-LLM   │
     │ (Consumer)  │ │ (Cloud) │  │ (On-Prem)  │
     └────────────┘ └─────────┘  └────────────┘
      RTX 3060+      API Credits   Enterprise GPU
      8GB+ VRAM      1000 free     H100/A100/L40
```

### NVIDIA Provider Stack

| Provider | Hardware | Use Case |
|----------|----------|----------|
| **Ollama/Nemotron** | Consumer GPUs (RTX 3060+) | Default local inference, privacy mode |
| **NVIDIA NIM** | Cloud API | Specialized models (code, biology, chemistry) |
| **Triton Server** | Enterprise GPUs (H100/A100) | Production deployment, multi-model serving |
| **--prefer-nvidia** | All NVIDIA | 1.3x routing bonus for NVIDIA providers |

### GPU-Aware Features

- **Auto-detection**: pynvml integration detects GPU model, VRAM, driver, CUDA version
- **Model recommendation**: Selects optimal Nemotron variant for available VRAM
- **GPU benchmarking**: `nvh bench` measures tokens/sec with community baselines per GPU
- **VRAM monitoring**: Tracks memory usage, prevents OOM by routing to cloud when GPU is busy
- **Multi-GPU**: Detects all GPUs, can distribute models across devices

---

## The --prefer-nvidia Flag

nvHive's routing engine includes a `--prefer-nvidia` flag that gives a 1.3x scoring bonus to all NVIDIA-backed providers (Ollama, NIM, Triton). This means:

```bash
nvh --prefer-nvidia "Analyze this code"
# Routes to local Nemotron even if cloud would be marginally better
# Only escalates to cloud when quality truly requires it
```

This can be set as the default in config:
```yaml
defaults:
  prefer_nvidia: true
```

---

## Ecosystem Integration

### Works With

| Platform | Integration | Status |
|----------|-------------|--------|
| **NemoClaw/OpenShell** | Inference provider + MCP tools | Shipped |
| **Claude Code** | MCP server + Channel plugin | Shipped |
| **Cursor** | Auto-configured via `nvh integrate` | Shipped |
| **Any OpenAI client** | Drop-in proxy at `/v1/proxy` | Shipped |
| **Any Anthropic client** | Drop-in proxy at `/v1/anthropic/messages` | Shipped |
| **Python apps** | `pip install nvhive` + 3-line SDK | Shipped |

### Developer Experience

```bash
# Install
pip install nvhive

# Automatic: detects GPU, downloads Nemotron, enables local inference
nvh setup

# Queries automatically route to local NVIDIA GPU
nvh "Explain quicksort"
# → [ask → ollama/nemotron-mini] (local, 0ms network, $0.00)

# Council runs 3 models on your GPU
nvh convene "Review this architecture"
# → 3 Nemotron variants debate → synthesis
# → Confidence: 87% — Strong consensus

# Benchmark your GPU
nvh bench
# → RTX 4090: 142 tok/s ⭐⭐⭐⭐⭐ Outstanding
```

---

## NVIDIA Inception Program Fit

### What We Bring

1. **Open-source developer tool** driving NVIDIA GPU inference adoption
2. **Local-first architecture** — every nvHive user with an NVIDIA GPU uses it daily
3. **NemoClaw integration** — already in NVIDIA's agent ecosystem
4. **Multi-provider routing** solving a real market need (135K displaced OpenClaw users)
5. **MIT licensed** — no friction for enterprise adoption

### What We'd Use

1. **NVIDIA DGX Cloud credits** for benchmark suite and premium NIM testing
2. **Developer relations support** — co-marketing with NemoClaw team
3. **Technical integration** — deeper NIM API access, DCGM integration for fleet monitoring
4. **Distribution** — listing on NVIDIA developer tools page

### Roadmap Aligned with NVIDIA

| Feature | Timeline | NVIDIA Alignment |
|---------|----------|-----------------|
| NIM Fleet Discovery | Q2 2026 | Auto-detect and route to all NIM endpoints on network |
| DCGM Integration | Q2 2026 | GPU fleet health monitoring via NVIDIA DCGM |
| Cost Autopilot | Q2 2026 | Auto-degrade to local GPU when cloud budget runs low |
| TensorRT-LLM Optimization | Q3 2026 | Recommend and auto-configure TRT-LLM for user's GPU |
| Multi-GPU Council | Q3 2026 | Distribute council members across GPU fleet |

---

## Traction

- **PyPI**: Published, `pip install nvhive`
- **Testing**: 217 tests, CI clean (Python 3.11 + 3.12)
- **Providers**: 23 integrated, 25 free tiers
- **CLI**: 50+ commands
- **WebUI**: 9-page dashboard
- **Integrations**: Claude Code, Cursor, NemoClaw, OpenClaw

---

## Contact

- **PyPI**: https://pypi.org/project/nvhive/
- **GitHub**: https://github.com/thatcooperguy/nvhive
- **Author**: Cooper (@thatcooperguy)
