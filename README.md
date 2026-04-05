# nvHive

**Multi-provider LLM routing that learns from every query.**

![version](https://img.shields.io/badge/version-0.5.0-blue) ![python](https://img.shields.io/badge/python-3.11%2B-blue) ![license](https://img.shields.io/badge/license-MIT-green) ![tests](https://img.shields.io/badge/tests-217%20passing-brightgreen) ![providers](https://img.shields.io/badge/providers-23-orange) ![models](https://img.shields.io/badge/models-63-purple)

nvHive routes LLM queries across 23 providers. It tracks which providers actually perform well for which task types, and adjusts routing based on measured quality — not static config. When a provider is rate-limited, down, or underperforming, queries automatically fail over to the next best option.

```bash
pip install nvhive
nvh "What is machine learning?"
# → Routed to groq/llama-3.3-70b (free, 520ms)
```

No API keys needed. Works immediately with free providers (Groq, GitHub Models, LLM7).

---

## Coming from OpenClaw or NemoClaw?

Anthropic dropped OpenClaw support. NemoClaw agents that routed through OpenClaw to Claude are affected. nvHive replaces that path — and gives you more than OpenClaw had.

```bash
pip install nvhive
nvh migrate --from openclaw    # imports your API keys
nvh health                     # shows your provider resilience
```

**For NemoClaw users** — nvHive plugs directly into OpenShell Gateway. No OpenClaw dependency:
```bash
nvh nemoclaw --start           # start nvHive proxy for NemoClaw
# NemoClaw agents now route through 23 providers + your local GPU
```

**For Anthropic SDK users** — one env var, zero code changes:
```bash
export ANTHROPIC_BASE_URL=http://localhost:8000/v1/anthropic
nvh serve
```

**What you get that OpenClaw didn't have:**

| Feature | OpenClaw | nvHive |
|---------|----------|--------|
| Providers | Claude only | 23 providers (25 free) |
| Failover | None | Automatic across all providers |
| Local GPU | No | Ollama/Nemotron, private, free |
| Multi-model consensus | No | Council mode (3+ models) |
| Adaptive routing | No | Learns from every query |
| Cost control | No | Budget limits + free routing |
| Provider health | No | `nvh health` dashboard |

**What's the catch?** nvHive doesn't give you free Claude access — that was OpenClaw's deal with Anthropic, and it's over. If you need Claude specifically, bring your own API key. For everything else, nvHive routes to the best available provider automatically. Free tiers have rate limits (Groq: 30 RPM, Google: 15 RPM). For unlimited local inference, run Ollama.

[Full migration guide (OpenClaw + NemoClaw)](docs/OPENCLAW_MIGRATION.md)

---

## How the Router Works

Most LLM routers use static config: "send code questions to GPT-4, everything else to Claude." nvHive is different — it measures actual performance and adapts.

**Task classification:** TF-IDF cosine similarity against a 90-example training corpus (13 task types). Not keyword matching — semantic understanding of what your query needs. Falls back to regex for edge cases.

**Provider scoring:** Weighted composite of four signals:
- **Capability** (40%): How good is this provider at this task type? Starts from static estimates, converges to measured scores via exponential moving average as queries flow.
- **Cost** (30%): Cheaper providers score higher. Free providers score maximum.
- **Latency** (20%): Faster providers score higher. Measured from actual response times.
- **Health** (10%): Circuit breaker tracks recent failures. Unhealthy providers get deprioritized automatically.

**Adaptive learning loop:** After every query, nvHive records the outcome (quality evaluation, latency, success/failure) and updates the provider's capability score for that task type. By 20 queries per provider/task pair, routing is fully data-driven — the static estimates are replaced by measured performance.

```bash
# See what the router has learned
nvh routing-stats

# Provider  Task Type        Static  Learned  Samples  Delta
# groq      code_generation   0.78    0.84      67     +0.06
# openai    reasoning         0.85    0.82      18     -0.03
```

**Failover:** If a provider fails, nvHive tries the next in the fallback chain. It prefers providers NOT already used in the current session (to avoid hitting the same rate limit). Every failure is recorded and feeds back into the health score.

**Local-first:** Queries estimated under 500 tokens on task types the local model handles well (conversation, Q&A, summarization) route to Ollama/Nemotron on your GPU. No network, no cost, no data leaving your machine. Complex queries escalate to cloud. The thresholds adapt as the learning loop measures local model quality.

---

## Council Mode

When one model isn't enough, nvHive runs the same query through multiple providers in parallel, then synthesizes their responses into a single answer.

**Why this works:** Different models have different strengths and blind spots. GPT-4o might miss a security issue that Llama catches. Claude might structure an answer better but miss an edge case. Council mode surfaces all perspectives and synthesizes the best of each.

**What it costs:** Council with 3 free providers (Groq + GitHub + Google) costs $0. Council with 3 premium providers costs roughly 3x a single query. The synthesis step uses a provider NOT used as a council member to avoid rate limit conflicts.

**Confidence scoring:** Every council response includes an agreement metric: "3/3 agreed on core approach" vs "split decision — 2 models recommend X, 1 recommends Y." This tells you when to trust the consensus and when to dig deeper.

```bash
nvh convene "Should we use Redis or Postgres for session storage?"
# → 3 models debate → synthesis with confidence score

nvh throwdown "Review this architecture for scalability issues"
# → Pass 1: 3 models analyze → Pass 2: critique each other → final synthesis
```

**Rate-limit aware:** Council members sharing the same provider are staggered by 2 seconds. Synthesis retries across different providers with backoff if rate-limited. Designed to work reliably on free tiers.

---

## Core Commands

| Command | What It Does |
|---------|-------------|
| `nvh "question"` | Smart route to best available model |
| `nvh convene "question"` | Council consensus (3+ models) |
| `nvh throwdown "question"` | Two-pass deep analysis with critique |
| `nvh safe "question"` | Local only — nothing leaves your machine |
| `nvh health` | Provider resilience dashboard |
| `nvh routing-stats` | Learned vs static routing scores |
| `nvh benchmark` | Quality benchmark suite (16 prompts, blind judge) |
| `nvh nvidia` | NVIDIA GPU infrastructure status |
| `nvh migrate` | Import from OpenClaw / Claw Code / Claude Desktop |
| `nvh setup` | Interactive provider setup (validates keys on save) |

[Full command reference](docs/COMMANDS.md) (50+ commands)

## Providers

**23 providers. 63 models. 25 free — no credit card required.**

| Tier | Providers | Rate Limits |
|------|-----------|-------------|
| **Free (no signup)** | Ollama (local), LLM7 | Unlimited / 30 RPM |
| **Free (email signup)** | Groq, GitHub Models, Cerebras, SambaNova, Cohere, AI21, SiliconFlow, HuggingFace | 15-30 RPM |
| **Free (account)** | Google Gemini, Mistral, NVIDIA NIM | 15-1000 RPM |
| **Paid** | OpenAI, Anthropic, DeepSeek, Fireworks, Together, OpenRouter, Grok | Pay per token |

Run `nvh setup` to configure. The router handles the rest.

---

## For Tool Builders

nvHive is a routing layer, not a tool. Any AI application can add multi-provider routing:

```python
import nvh

# Drop-in OpenAI-compatible interface
response = await nvh.complete([
    {"role": "user", "content": "Explain quicksort"}
])
# → Routed through 23 providers with automatic failover

# Inspect routing without executing
decision = await nvh.route("complex question about databases")
# → {"provider": "anthropic", "model": "claude-sonnet-4", "reason": "..."}

# Council consensus
result = await nvh.convene("Architecture review", cabinet="engineering")
# → 3 expert personas debate, synthesize, report confidence

# Provider health check
status = await nvh.health()
# → {"groq": {"healthy": true, "latency_ms": 45}, ...}
```

**API Proxies** — point existing SDKs at nvHive with zero code changes:

| SDK | Configuration |
|-----|--------------|
| Anthropic | `ANTHROPIC_BASE_URL=http://localhost:8000/v1/anthropic` |
| OpenAI | `OPENAI_BASE_URL=http://localhost:8000/v1/proxy` |
| Claude Code | `claude mcp add nvhive -- python -m nvh.mcp_server` |
| Cursor | `nvh integrate --auto` |

[SDK & API reference](docs/SDK_API.md)

---

## NVIDIA GPU Support

nvHive routes to your NVIDIA GPU first. Cloud is the fallback, not the default.

```bash
nvh nvidia              # GPU hardware + inference stack status
nvh bench               # tokens/sec benchmark with community baselines
nvh --prefer-nvidia     # 1.3x routing bonus for NVIDIA providers
```

Supports Ollama/Nemotron (consumer GPUs), NVIDIA NIM (cloud API), and Triton Inference Server (enterprise). Integrates with [NemoClaw](docs/NEMOCLAW.md) as both inference provider and MCP tool server.

---

## Verify It Yourself

```bash
# Run the quality benchmark
nvh benchmark --mode council-free     # free council vs single model
nvh benchmark --mode all --export results.md

# Check provider resilience
nvh health
# → "3/3 providers healthy. Resilient — survives any single provider outage."

# See the learning in action
nvh routing-stats
# → Shows measured vs predicted scores after enough queries
```

16 prompts across code generation, debugging, reasoning, math, creative writing, and Q&A. Blind LLM judge scores on accuracy, completeness, actionability, and coherence. Run it yourself. Publish the results.

---

## Install

```bash
pip install nvhive
nvh setup    # interactive provider configuration with key validation
```

Or one-line with auto-migration from OpenClaw:
```bash
curl -fsSL https://nvhive.dev/install | sh
```

## Learn More

| Guide | Description |
|-------|-------------|
| [Getting Started](docs/GETTING_STARTED.md) | First-time setup |
| [Commands](docs/COMMANDS.md) | Full CLI reference (50+ commands) |
| [Providers](docs/PROVIDERS.md) | 23 providers, rate limits, free tiers |
| [Council System](docs/COUNCIL.md) | Multi-LLM consensus with confidence scoring |
| [Claude Code Integration](docs/CLAUDE_CODE_INTEGRATION.md) | MCP server + migration guide |
| [NemoClaw](docs/NEMOCLAW.md) | NVIDIA NemoClaw integration |
| [SDK & API](docs/SDK_API.md) | Python SDK, REST API, proxies |
| [Architecture](docs/ARCHITECTURE.md) | System design and adaptive learning |

## License

MIT License. See [LICENSE](LICENSE) for details.
