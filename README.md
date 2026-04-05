# nvHive

**Your AI workflow shouldn't depend on one provider.**

![version](https://img.shields.io/badge/version-0.5.0-blue) ![python](https://img.shields.io/badge/python-3.11%2B-blue) ![license](https://img.shields.io/badge/license-MIT-green) ![tests](https://img.shields.io/badge/tests-217%20passing-brightgreen) ![providers](https://img.shields.io/badge/providers-23-orange) ![models](https://img.shields.io/badge/models-63-purple)

nvHive is an open-source routing layer that sits between your tools and 23 LLM providers. When one provider changes pricing, drops support, or goes down — your workflow doesn't break. Queries route to the best available model automatically, with council consensus when one model isn't enough.

```bash
pip install nvhive
nvh "What is machine learning?"
```

No API keys needed. Works immediately with free providers.

---

## Coming from OpenClaw?

Anthropic dropped OpenClaw support. Your workflow doesn't have to break.

```bash
# Option 1: Migrate in 60 seconds
pip install nvhive
nvh migrate --from openclaw

# Option 2: Drop-in API replacement (zero code changes)
# Point your Anthropic SDK at nvHive:
export ANTHROPIC_BASE_URL=http://localhost:8000/v1/anthropic
nvh serve
```

nvHive imports your API keys, routes across 23 providers (25 free), and your tools keep working. [Full migration guide](docs/CLAUDE_CODE_INTEGRATION.md)

---

## How It Works

1. You ask a question
2. **Adaptive router** classifies the task and scores all providers on capability, cost, latency, and health — using learned scores that improve with every query
3. **Local-first**: simple queries stay on your GPU (free, private, no network)
4. **Cloud when needed**: complex queries route to the best cloud model
5. **Council mode**: when one model isn't enough, multiple LLMs debate and synthesize

The router learns which providers actually deliver for which task types. Day 1 it uses static scores. By day 30 it's routing based on measured performance from your actual queries.

```bash
nvh routing-stats    # see learned vs static scores
nvh health           # provider resilience dashboard
```

## Core Commands

| Command | What It Does |
|---------|-------------|
| `nvh "question"` | Smart route to the best available model |
| `nvh convene "question"` | Council of experts debate and synthesize |
| `nvh throwdown "question"` | Two-pass deep analysis with critique |
| `nvh safe "question"` | Local only — nothing leaves your machine |
| `nvh benchmark` | Quality benchmark — prove council beats single models |
| `nvh health` | Provider resilience dashboard |
| `nvh routing-stats` | Learned vs static routing intelligence |
| `nvh nvidia` | NVIDIA GPU infrastructure status |
| `nvh migrate` | Import configs from OpenClaw, Claw Code, Claude Desktop |
| `nvh setup` | Interactive provider setup wizard |

[Full command reference](docs/COMMANDS.md)

## Providers

**23 providers. 63 models. 25 free — no credit card required.**

Ollama (local), OpenAI, Anthropic, Google Gemini, Groq, NVIDIA NIM, Triton, DeepSeek, GitHub Models, LLM7, Mistral, Cohere, Cerebras, SambaNova, and more.

The router picks the best one. Or go direct: `nvh ask --advisor groq "question"`.

[Full provider table](docs/PROVIDERS.md)

---

## For Tool Builders

nvHive is infrastructure. Any AI tool, IDE, or agent can add multi-provider routing in 3 lines:

```python
import nvh

# Drop-in replacement for OpenAI
response = await nvh.complete([
    {"role": "user", "content": "Explain quicksort"}
])
print(response.content)
# → Routed to best available provider, with automatic fallback
```

```python
# Council consensus — 3 models collaborate
result = await nvh.convene("Should we use Rust or Go?")
print(result.synthesis.content)
print(f"Confidence: {result.confidence_score:.0%}")

# Check what's available
providers = await nvh.health()
decision = await nvh.route("complex question")
```

**API Proxies** — point any existing SDK at nvHive:

| SDK | Configuration |
|-----|--------------|
| OpenAI | `base_url="http://localhost:8000/v1/proxy"` |
| Anthropic | `base_url="http://localhost:8000/v1/anthropic"` |
| Claude Code | `claude mcp add nvhive -- python -m nvh.mcp_server` |
| Cursor | `nvh integrate --auto` |
| Any MCP client | `nvhive-mcp` entry point |

[SDK & API reference](docs/SDK_API.md)

---

## NVIDIA GPU Support

nvHive makes every NVIDIA GPU an AI inference engine. Local-first by default.

```bash
nvh nvidia              # GPU hardware + inference stack status
nvh bench               # benchmark your GPU (tokens/sec)
nvh --prefer-nvidia     # 1.3x routing bonus for local NVIDIA inference
```

| Provider | Hardware | Use Case |
|----------|----------|----------|
| Ollama/Nemotron | Consumer GPUs (RTX 3060+) | Default local inference |
| NVIDIA NIM | Cloud API | Specialized models |
| Triton Server | Enterprise GPUs (H100/A100) | Production serving |

Integrates with [NemoClaw](docs/NEMOCLAW.md) as both an inference provider and MCP tool server.

---

## Quality Proof

Don't take our word for it. Run the benchmark yourself:

```bash
nvh benchmark --mode council-free    # free council vs single model ($0)
nvh benchmark --mode all             # full comparison across all modes
nvh benchmark --export results.md    # publish the results
```

16 real-world prompts across code generation, debugging, reasoning, math, and more. Blind LLM judge scores responses on accuracy, completeness, actionability, and coherence.

---

## Web Dashboard

```bash
nvh webui
```

9 pages: Chat, Council, Advisors, Analytics, Integrations, System, Settings, Setup Wizard, Query Builder.

---

## Install

```bash
# PyPI
pip install nvhive

# One-line installer (detects OS, auto-migrates from OpenClaw)
curl -fsSL https://nvhive.dev/install | sh
```

## Learn More

| Guide | Description |
|-------|-------------|
| [Getting Started](docs/GETTING_STARTED.md) | First-time setup |
| [Commands](docs/COMMANDS.md) | Full CLI reference (50+ commands) |
| [Providers](docs/PROVIDERS.md) | 23 providers, 63 models |
| [Council System](docs/COUNCIL.md) | Multi-LLM consensus with confidence scoring |
| [Claude Code Integration](docs/CLAUDE_CODE_INTEGRATION.md) | MCP server + migration guide |
| [NemoClaw](docs/NEMOCLAW.md) | NVIDIA NemoClaw integration |
| [SDK & API](docs/SDK_API.md) | Python SDK, REST API, proxies |
| [Architecture](docs/ARCHITECTURE.md) | System design and adaptive learning |

## License

MIT License. See [LICENSE](LICENSE) for details.
