# nvHive

**Multi-provider LLM routing that learns from every query.**

![version](https://img.shields.io/badge/version-0.5.1-blue) ![python](https://img.shields.io/badge/python-3.11%2B-blue) ![license](https://img.shields.io/badge/license-MIT-green) ![tests](https://img.shields.io/badge/tests-225%20passing-brightgreen) ![providers](https://img.shields.io/badge/providers-23-orange) ![models](https://img.shields.io/badge/models-63-purple)

## Why nvHive

Most AI tools use a single provider. When that provider hits rate limits, changes pricing, or goes down, you're stuck.

nvHive routes queries to the best available provider automatically. It tracks which providers actually perform well for which task types, and adjusts routing based on measured quality — not static config.

**What makes it different:**
- **Learns from every query.** The router measures real provider performance. By 20 queries it's routing based on data, not guesses.
- **Council consensus.** 3+ models collaborate and synthesize. Different models catch different blind spots.
- **Confidence-gated escalation.** Tries a free model first. Escalates to premium only if the response is uncertain.
- **Cross-model verification.** A second model independently checks for errors and hallucinations.

<p align="center">
  <img src="docs/screenshots/terminal-demo.gif" alt="nvHive CLI" width="640">
</p>

<p align="center">
  <img src="docs/screenshots/webui-walkthrough.gif" alt="nvHive Web Dashboard" width="640">
</p>

---

## Get Started

```bash
pip install nvhive
nvh setup              # configure providers (validates keys)
nvh health             # check what's available
nvh "your question"    # try it
```

Works immediately with LLM7 (no signup). Run `nvh setup` to add free providers like Groq and GitHub Models.

<details>
<summary><b>NVIDIA GPU Quick Start</b> — local inference on your hardware</summary>

```bash
# 1. Install Ollama + Nemotron
curl -fsSL https://ollama.com/install.sh | sh
ollama pull nemotron-mini        # 4.1GB, runs on 8GB+ VRAM

# 2. Install nvHive
pip install nvhive

# 3. nvHive auto-detects your GPU and Nemotron
nvh nvidia                       # GPU info + inference stack status
nvh bench                        # benchmark your GPU (tokens/sec)

# 4. Queries route to your GPU by default
nvh "Explain quicksort"          # → local Nemotron, $0, private
nvh safe "Analyze this code"     # → forced local, nothing leaves machine
nvh --prefer-nvidia "question"   # → 1.3x bonus for NVIDIA providers

# 5. Council on your GPU — 3 models, $0, fully private
nvh convene "Redis vs Postgres for sessions?"
```

nvHive detects NVIDIA GPUs via pynvml (VRAM, driver, CUDA version, temperature, power draw) and selects the optimal Nemotron model for your hardware. Simple queries stay local. Complex queries escalate to cloud only when needed. The learning loop measures your GPU's quality over time and adjusts routing thresholds automatically.

</details>

---

## How It Works

### Query Pipeline

```mermaid
flowchart TB
    USER[User Query] --> CLASSIFY[Task Classifier<br/>TF-IDF · 13 task types]
    CLASSIFY --> LOCALCHECK{Local GPU<br/>good enough?}
    
    LOCALCHECK -->|Simple query| GPU[NVIDIA GPU<br/>Nemotron via Ollama<br/>pynvml detection]
    LOCALCHECK -->|Complex query| SCORE[Score All Providers<br/>capability · cost · latency · health]
    
    SCORE --> ROUTE{Pick Best<br/>Provider}
    
    ROUTE --> FREE[Free Providers<br/>LLM7 · Groq · GitHub<br/>signup required for some]
    ROUTE --> PAID[Premium Providers<br/>OpenAI · Anthropic · Google]
    ROUTE --> NIM[NVIDIA NIM<br/>Triton]
    ROUTE --> GPU
    
    FREE --> RESPONSE[Response]
    PAID --> RESPONSE
    NIM --> RESPONSE
    GPU --> RESPONSE
    
    RESPONSE --> LEARN[Learning Loop<br/>Record outcome · EMA update<br/>Adjusts GPU routing thresholds]
    LEARN -->|Feeds back into| SCORE
    
    RESPONSE -->|--verify flag| VERIFY[Cross-Model<br/>Verification]
    VERIFY --> FINAL[Verified Response]
    RESPONSE --> FINAL
    
    style GPU fill:#76B900,color:#000
    style NIM fill:#76B900,color:#000
    style LEARN fill:#1a1a2e,color:#76B900,stroke:#76B900
    style VERIFY fill:#1a1a2e,color:#00bcd4,stroke:#00bcd4
```

**Task classification:** TF-IDF cosine similarity against a 90-example training corpus (13 task types). Semantic understanding, not keyword matching.

**Provider scoring:** Weighted composite — capability (40%), cost (30%), latency (20%), health (10%). Capability scores start from static estimates and converge to measured performance via exponential moving average.

**Adaptive learning:** After every query, nvHive records the outcome and updates scores. By 20 queries per provider/task pair, routing is fully data-driven.

```bash
nvh routing-stats    # see learned vs static scores
nvh health           # provider resilience dashboard
```

**Failover:** If a provider fails, nvHive tries the next in the fallback chain. Every failure feeds back into the health score.

**Local-first with NVIDIA GPUs:** Simple queries route to Nemotron on your NVIDIA GPU via Ollama — no cloud, no cost, no data leaving your machine. GPU detection via pynvml reads VRAM, driver version, and CUDA version to select the optimal local model. The `--prefer-nvidia` flag gives a 1.3x routing bonus to keep inference on NVIDIA hardware whenever quality allows.

---

## Council Mode

```mermaid
flowchart TB
    QUERY[User Query] --> AGENTS[Generate Expert Personas<br/>e.g. Backend Engineer, Architect, DBA]
    
    AGENTS --> M1[Model 1<br/>Groq / Llama]
    AGENTS --> M2[Model 2<br/>Google / Gemini]
    AGENTS --> M3[Model 3<br/>GitHub / GPT-4o]
    
    M1 --> COLLECT[Collect Responses<br/>Rate-limit staggered]
    M2 --> COLLECT
    M3 --> COLLECT
    
    COLLECT --> AGREE[Agreement Analysis<br/>Keyword overlap + LLM judge]
    AGREE --> SYNTH[Synthesis<br/>Uses non-member provider]
    
    SYNTH --> RESULT[Council Response<br/>+ Confidence Score<br/>+ Individual Perspectives]
    
    style AGREE fill:#1a1a2e,color:#00bcd4,stroke:#00bcd4
    style SYNTH fill:#1a1a2e,color:#76B900,stroke:#76B900
```

When one model isn't enough, nvHive runs the same query through multiple providers in parallel, then synthesizes their responses.

**Why this works:** Different models have different blind spots. Council mode surfaces all perspectives and synthesizes the best of each.

**Confidence scoring:** Every council response includes an agreement metric — "3/3 agreed" vs "split decision." Tells you when to trust the consensus.

**Cost:** Council with 3 free providers costs $0. Council with 3 Nemotron variants on a single NVIDIA GPU costs $0 and never leaves your machine. Premium cloud council costs ~3x a single query.

```bash
nvh convene "Should we use Redis or Postgres for session storage?"
# → 3 models debate → synthesis with confidence score
```

### Throwdown Mode — Two-Pass Deep Analysis

Throwdown goes beyond council. Three passes, each building on the last:

```mermaid
flowchart TB
    QUERY[User Query] --> A1[Expert 1 - Groq]
    QUERY --> A2[Expert 2 - Google]
    QUERY --> A3[Expert 3 - GitHub]
    
    A1 --> S1[Pass 1 Synthesis]
    A2 --> S1
    A3 --> S1
    
    S1 --> B1[Expert 1 - Critiques]
    S1 --> B2[Expert 2 - Finds blind spots]
    S1 --> B3[Expert 3 - Challenges assumptions]
    
    B1 --> S2[Pass 2 Synthesis]
    B2 --> S2
    B3 --> S2
    
    S2 --> FINAL[Final Answer]
    
    style A1 fill:#1a1a2e,stroke:#76B900,color:#c8c8c8
    style A2 fill:#1a1a2e,stroke:#76B900,color:#c8c8c8
    style A3 fill:#1a1a2e,stroke:#76B900,color:#c8c8c8
    style B1 fill:#1a1a2e,stroke:#00bcd4,color:#c8c8c8
    style B2 fill:#1a1a2e,stroke:#00bcd4,color:#c8c8c8
    style B3 fill:#1a1a2e,stroke:#00bcd4,color:#c8c8c8
    style FINAL fill:#76B900,color:#000
```

```bash
nvh throwdown "Review this architecture for scalability issues"
# Pass 1: 3 experts analyze independently
# Pass 2: experts critique each other's analysis
# Pass 3: final synthesis integrating all perspectives
```

**Why throwdown beats single-model:** A single model gives you one perspective, once. Throwdown gives you three perspectives, challenged by three critiques, then synthesized. Errors get caught. Assumptions get questioned. The final answer is more thorough than any single pass.

---

## Smart Query Features

```bash
# Confidence-gated escalation: try free first, upgrade only if needed
nvh ask --escalate "Design a distributed lock manager"
# → groq (free, confidence: 42%) → auto-escalated to openai

# Cross-model verification: a second model checks the answer
nvh ask --verify "Is eval() safe in Python?"
# → groq answers → google verifies ✓ (9/10, no issues)

# Both together: cheapest possible verified answer
nvh ask --escalate --verify "Explain the CAP theorem"
```

---

## Local GPU Inference with Nemotron

`nvh setup` detects your NVIDIA GPU, recommends the right Nemotron model for your VRAM, and offers to pull it — all in one step. No manual configuration.

<p align="center">
  <img src="docs/screenshots/gpu-detection-demo.gif" alt="nvHive GPU Detection & Model Selection" width="640">
</p>

```bash
nvh setup
# Step 3/3: Local GPU inference
#   Detected: NVIDIA GeForce RTX 4090 (24GB VRAM)
#   Recommended: nemotron-small — great quality/speed balance
#   Pull nemotron-small now? [Y/n] y
#   ✓ nemotron-small ready
```

**What `nvh setup` handles:**

```mermaid
flowchart TB
    SETUP[nvh setup] --> DETECT[GPU Detection<br/>pynvml reads VRAM · driver · CUDA]
    
    DETECT --> VRAM{Available VRAM?}
    
    VRAM -->|< 6 GB| MINI[nemotron-mini]
    VRAM -->|6 – 12 GB| SMALL[nemotron-small<br/>recommended]
    VRAM -->|12 – 24 GB| DUAL[nemotron-small<br/>+ codellama]
    VRAM -->|24 GB+| FULL[nemotron 70B]

    MINI --> CHECK{Ollama running?}
    SMALL --> CHECK
    DUAL --> CHECK
    FULL --> CHECK
    
    CHECK -->|Not installed| INSTALL[Show install command]
    CHECK -->|Not running| START[Show: ollama serve]
    CHECK -->|Running| PULLED{Model pulled?}
    
    PULLED -->|Yes| READY[Ready ✓]
    PULLED -->|No| ASK[Pull now? Y/n]
    ASK --> READY
    
    READY --> ROUTE[nvHive Router<br/>Local GPU registered<br/>Learning loop active]
    
    style SMALL fill:#76B900,color:#000
    style READY fill:#76B900,color:#000
    style ROUTE fill:#1a1a2e,color:#76B900,stroke:#76B900
```

**After setup, routing is automatic:**
- Simple queries → local Nemotron on your GPU (free, private, no network)
- Complex queries → cloud providers when quality requires it
- `nvh bench` measures your GPU's actual tok/s with community baselines
- The learning loop adjusts routing thresholds based on measured quality on YOUR hardware

[Full GPU detection guide](docs/GPU_DETECTION.md)

### NVIDIA Inference Stack

| Layer | Technology | Hardware | Use Case |
|-------|-----------|----------|----------|
| **Local** | Ollama + Nemotron | Consumer GPUs (RTX 3060+, 8GB+ VRAM) | Default inference, privacy mode |
| **Cloud** | NVIDIA NIM API | NVIDIA cloud | Specialized models, 1000 free credits |
| **Enterprise** | Triton Inference Server | H100 / A100 / L40 | Production multi-model serving, TensorRT-LLM |
| **Agent** | NemoClaw / OpenShell | Any | Agent orchestration with nvHive routing |
| **Detection** | pynvml | Any NVIDIA GPU | VRAM, driver, CUDA, temp, power, PCIe |

`--prefer-nvidia` gives a 1.3x routing bonus to all NVIDIA-backed providers, keeping inference on NVIDIA hardware whenever quality allows.

---

## Integrations

### How nvHive Connects to Your Tools

```mermaid
flowchart LR
    subgraph Your Tools
        CLI[nvh CLI]
        SDK[Python SDK<br/>import nvh]
        CC[Claude Code<br/>MCP]
        OC[OpenClaw<br/>Agent]
        NC[NemoClaw<br/>Agent]
        CU[Cursor]
        APP[Your App<br/>OpenAI SDK]
    end

    subgraph nvHive Engine
        API[API Server<br/>:8000]
        MCP[MCP Server<br/>stdio]
        PROXY_OAI[OpenAI Proxy<br/>/v1/proxy]
        PROXY_ANT[Anthropic Proxy<br/>/v1/anthropic]
        ROUTER[Adaptive Router<br/>+ Learning Loop]
        COUNCIL[Council Engine<br/>+ Confidence]
        ESCALATE[Escalation<br/>+ Verification]
    end

    subgraph Providers
        GPU[Your GPU<br/>Ollama · Nemotron]
        FREE_P[Free Cloud<br/>Groq · GitHub · LLM7<br/>Google · Cerebras]
        PAID_P[Paid Cloud<br/>OpenAI · Anthropic<br/>DeepSeek · Mistral]
        NIM[NVIDIA NIM<br/>Triton]
    end

    CLI --> API
    SDK --> API
    CC --> MCP
    OC --> MCP
    NC --> PROXY_OAI
    CU --> MCP
    APP --> PROXY_OAI
    APP --> PROXY_ANT

    MCP --> API
    PROXY_OAI --> API
    PROXY_ANT --> API
    API --> ROUTER
    API --> COUNCIL
    API --> ESCALATE
    ROUTER --> GPU
    ROUTER --> FREE_P
    ROUTER --> PAID_P
    ROUTER --> NIM

    style GPU fill:#76B900,color:#000
    style NIM fill:#76B900,color:#000
    style ROUTER fill:#1a1a2e,color:#76B900,stroke:#76B900
    style COUNCIL fill:#1a1a2e,color:#00bcd4,stroke:#00bcd4
```

**API Proxies** — point existing SDKs at nvHive:

| SDK | Configuration |
|-----|--------------|
| Anthropic | `ANTHROPIC_BASE_URL=http://localhost:8000/v1/anthropic` |
| OpenAI | `OPENAI_BASE_URL=http://localhost:8000/v1/proxy` |
| Claude Code | `claude mcp add nvhive -- python -m nvh.mcp_server` |
| Cursor | `nvh integrate --auto` |

### Works With OpenClaw & NemoClaw

nvHive works alongside OpenClaw as a routing layer, and integrates with [NemoClaw](docs/NEMOCLAW.md) (NVIDIA's agent framework) as both inference provider and MCP tool server.

<p align="center">
  <img src="docs/screenshots/nemoclaw-demo.gif" alt="nvHive NemoClaw Integration" width="640">
</p>

```bash
nvh migrate --from openclaw    # import your existing API keys
nvh nemoclaw --start           # start proxy for NemoClaw agents
```

*Note: Anthropic recently changed billing for third-party tools. See the [integration guide](docs/OPENCLAW_MIGRATION.md) for details.*

---

## For Tool Builders

nvHive is a routing layer. Any AI application can add multi-provider routing:

```python
import nvh

# Drop-in OpenAI-compatible interface
response = await nvh.complete([
    {"role": "user", "content": "Explain quicksort"}
])

# Inspect routing without executing
decision = await nvh.route("complex question about databases")

# Council consensus
result = await nvh.convene("Architecture review", cabinet="engineering")

# Provider health check
status = await nvh.health()
```

[SDK & API reference](docs/SDK_API.md)

---

## Feature Matrix

| Feature | CLI | Python SDK | REST API | MCP | OpenClaw | NemoClaw |
|---------|:---:|:----------:|:--------:|:---:|:--------:|:--------:|
| Smart routing | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| Council consensus | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| Throwdown analysis | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| Confidence scoring | ✓ | ✓ | ✓ | ✓ | — | — |
| Escalation (--escalate) | ✓ | ✓ | ✓ | — | — | — |
| Verification (--verify) | ✓ | ✓ | ✓ | — | — | — |
| Local GPU inference | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| Adaptive learning | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| Provider health | ✓ | ✓ | ✓ | ✓ | — | — |
| Budget controls | ✓ | — | ✓ | — | ✓ | ✓ |
| Streaming | ✓ | ✓ | ✓ | — | — | ✓ |
| Privacy mode | ✓ | ✓ | ✓ | ✓ | — | ✓ |

---

## Core Commands

| Command | What It Does |
|---------|-------------|
| `nvh "question"` | Smart route to best available model |
| `nvh convene "question"` | Council consensus (3+ models) |
| `nvh throwdown "question"` | Two-pass deep analysis with critique |
| `nvh safe "question"` | Local only — nothing leaves your machine |
| `nvh ask --escalate` | Try free first, escalate if uncertain |
| `nvh ask --verify` | Cross-model verification |
| `nvh health` | Provider resilience dashboard |
| `nvh why` | Explain last routing decision |
| `nvh history` | Recent queries with costs and timing |
| `nvh bench` | GPU speed test (tokens/sec) |
| `nvh bench -q` | Speed + quality comparison |
| `nvh routing-stats` | Learned vs static routing scores |
| `nvh nvidia` | NVIDIA GPU infrastructure status |
| `nvh migrate` | Import keys from OpenClaw / Claude Desktop |
| `nvh setup` | Interactive provider setup |

[Full command reference](docs/COMMANDS.md) (50+ commands)

## Providers

**23 providers. 63 models. 25 free — no credit card required.**

| Tier | Providers | Rate Limits |
|------|-----------|-------------|
| **Free (no signup)** | Ollama (local), LLM7 | Unlimited / 30 RPM |
| **Free (email signup)** | Groq, GitHub Models, Cerebras, SambaNova, Cohere, AI21, SiliconFlow, HuggingFace | 15-30 RPM |
| **Free (account)** | Google Gemini, Mistral, NVIDIA NIM | 15-1000 RPM |
| **Paid** | OpenAI, Anthropic, DeepSeek, Fireworks, Together, OpenRouter, Grok | Pay per token |

---

## Verify It Yourself

<p align="center">
  <img src="docs/screenshots/bench-demo.gif" alt="nvHive Benchmark Demo" width="640">
</p>

```bash
nvh bench              # GPU speed (tokens/sec)
nvh bench -q           # speed + quality comparison
nvh health             # provider resilience
nvh why                # explain last routing decision
nvh routing-stats      # learning in action
nvh history            # recent queries with costs
```

16 quality prompts across code generation, debugging, reasoning, math, creative writing, and Q&A. Blind LLM judge. Run it yourself. Publish the results.

## Learn More

| Guide | Description |
|-------|-------------|
| [Getting Started](docs/GETTING_STARTED.md) | First-time setup |
| [Commands](docs/COMMANDS.md) | Full CLI reference (50+ commands) |
| [Providers](docs/PROVIDERS.md) | 23 providers, rate limits, free tiers |
| [Council System](docs/COUNCIL.md) | Multi-LLM consensus with confidence scoring |
| [OpenClaw Integration](docs/OPENCLAW_MIGRATION.md) | Works alongside OpenClaw |
| [Claude Code](docs/CLAUDE_CODE_INTEGRATION.md) | MCP server setup |
| [GPU Detection](docs/GPU_DETECTION.md) | Auto-detection, model selection, OOM protection |
| [NemoClaw](docs/NEMOCLAW.md) | NVIDIA NemoClaw integration |
| [SDK & API](docs/SDK_API.md) | Python SDK, REST API, proxies |
| [Architecture](docs/ARCHITECTURE.md) | System design and adaptive learning |

## License

MIT License. See [LICENSE](LICENSE) for details.
