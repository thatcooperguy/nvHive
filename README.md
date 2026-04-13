# nvHive

**One command. Every AI model you have. Automatically assembled into the best team for each task.**

![version](https://img.shields.io/badge/version-0.16.0-blue) ![python](https://img.shields.io/badge/python-3.11%2B-blue) ![license](https://img.shields.io/badge/license-MIT-green) ![tests](https://img.shields.io/badge/tests-1086%20passing-brightgreen) ![providers](https://img.shields.io/badge/providers-23-orange) ![coverage](https://img.shields.io/badge/coverage-62%25-green) ![ci](https://img.shields.io/badge/CI-Linux%20%7C%20Windows%20%7C%20macOS-blue)

## Why nvHive

```bash
nvh "What is a binary search tree?"              # → answers (single best advisor)
nvh "Fix the timeout bug in council.py"          # → auto-detects coding task → agent mode
nvh "Review my staged changes"                   # → auto-detects review → multi-model review
nvh "Add tests for the auth module"              # → auto-detects test request → test generation
nvh "Should we use Redis or Postgres?"           # → auto-detects debate → council (3+ advisors)
```

**You type one command. nvHive figures out the rest.** It detects what you're asking for, checks which advisors are healthy, and assembles the best team for the task — automatically. More advisors connected = smarter behavior, with zero configuration.

**What makes it different:**

- **Smart team assembly.** nvHive doesn't just route to one model — it generates expert agents based on your question and matches each one to the best LLM for their specialty. A "Security Engineer" agent gets the LLM that scores highest on security tasks. A "Database Expert" gets the best at database queries. All based on real performance data from the learning engine.
- **Automatic orchestration.** Coding tasks get a planner + coder + reviewer. Complex questions get a council of specialists. Simple questions get the fastest advisor. All automatic based on intent detection and available advisors.
- **Scales with what you have.** 1 provider? Single-model answers. 3+ providers? Council automatically on complex questions, multi-model verification on code. Local GPU? Free inference alongside cloud. DGX Spark? Three 70B models in parallel, fully local.
- **Performant by default.** Uses all available advisors within reason. Simple questions don't trigger council. Budget limits always enforced. Switch to cost mode for minimal spend.
- **4-layer safety guardrails.** Command blocklist, filesystem boundary enforcement, secrets redaction, and resource limits — the agent can't `rm -rf /` even with `--yes`.

```mermaid
flowchart LR
    QUERY[User Query] --> INTENT[Intent Detection<br/>coding · review · council]
    INTENT --> AGENTS[Generate Expert Agents<br/>based on topic keywords]

    AGENTS --> MATCH[Match Agents to LLMs<br/>learning engine scores]

    MATCH --> A1[Security Engineer<br/>→ Claude<br/>best at security]
    MATCH --> A2[Database Expert<br/>→ GPT-4o<br/>best at databases]
    MATCH --> A3[Backend Architect<br/>→ Llama 70B local<br/>strong coding]

    A1 --> SYNTH[Synthesize<br/>best of each expert]
    A2 --> SYNTH
    A3 --> SYNTH

    SYNTH --> RESULT[Unified Answer<br/>all perspectives integrated]

    style MATCH fill:#1a1a2e,color:#76B900,stroke:#76B900
    style SYNTH fill:#1a1a2e,color:#3b82f6,stroke:#3b82f6
    style RESULT fill:#76B900,color:#000
```

<p align="center">
  <img src="docs/screenshots/terminal-demo.gif" alt="nvHive CLI" width="640">
</p>

<p align="center">
  <img src="docs/screenshots/webui-walkthrough.gif" alt="nvHive Web Dashboard" width="640">
</p>

---

## Agentic Coding

> Multi-model coding agent with recursive spawning, iterative QA convergence, parallel execution, and vision/browser tools. Scales from no-GPU to DGX Spark.

```bash
# One-time setup: pulls the right models for your GPU
nvh agent --setup

# Run a coding task
nvh agent "Fix the streaming timeout bug in council.py"
nvh agent "Add unit tests for the auth middleware" --dir ./myproject
nvh agent "Refactor the router to use health-aware selection" -y

# Advanced: sandbox, workspace, parallel pipeline
nvh agent "Build the notification service" --sandbox     # Docker-isolated execution
nvh agent "task" --workspace ./api,./frontend             # multi-repo context
```

**How it works:** Intent detection classifies the task, the orchestrator generates expert agents matched to the best LLMs, agents run in parallel where possible, recursive referral spawning fills knowledge gaps on-demand, and an iterative QA loop refines until convergence.

```mermaid
flowchart LR
    TASK[Task] --> INTENT[Intent Detection<br/>13 task types]

    INTENT --> DECOMPOSE[Decompose<br/>into subtasks]

    DECOMPOSE --> MATCH[Match Agents → LLMs<br/>learning engine scores]

    MATCH --> PAR[Parallel Pipeline<br/>independent subtasks<br/>run concurrently]

    PAR --> RECURSE[Recursive Spawning<br/>agents request specialists<br/>on-demand via REFER:]

    RECURSE --> QA{Iterative QA<br/>PASSED?}

    QA -->|PARTIAL / FAILED| FEEDBACK[QA feedback<br/>→ new agents<br/>→ next round]
    FEEDBACK --> PAR

    QA -->|PASSED| DONE[Converged<br/>Files modified<br/>Git commit]

    style INTENT fill:#1a1a2e,color:#76B900,stroke:#76B900
    style PAR fill:#1a1a2e,color:#3b82f6,stroke:#3b82f6
    style RECURSE fill:#1a1a2e,color:#a855f7,stroke:#a855f7
    style QA fill:#1a1a2e,color:#f59e0b,stroke:#f59e0b
    style DONE fill:#76B900,color:#000
```

### Key Capabilities (post-0.11.1)

| Feature | What It Does |
|---------|-------------|
| **Recursive Agent Spawning** | Agents self-identify knowledge gaps and emit `REFER: Need a Database Expert for sharding` — the system dynamically spawns the specialist, gets the answer, and feeds it back. Max depth prevents infinite recursion. |
| **Iterative QA Convergence** | Generate agents → run with referrals → post-QA reviews → if gaps found, spawn new agents informed by feedback → repeat until PASSED or budget exhausted. |
| **Parallel Pipeline** | Decomposes tasks into independent subtasks, runs them concurrently (bounded semaphore), respects dependencies, VRAM-aware model swapping with context preservation. |
| **Vision + Desktop Control** | Screenshot capture, image analysis via vision LLMs (GPT-4o, Claude, Gemini, LLaVA), OCR, mouse/keyboard automation with pyautogui. Agents can see and interact with GUIs. |
| **Browser Automation** | Headless browser navigation, screenshots, form filling via Playwright. HTTP requests, process management, Docker tools. |
| **Docker Sandbox** | `--sandbox` flag runs agent shell commands inside a Docker container — memory-limited, CPU-limited, no network by default, non-root user. Falls back to local if Docker unavailable. |
| **Execution Checkpoints** | File state snapshots before execution. Automatic rollback on failure — restores modified files, deletes newly created ones. |
| **LLM Drift Detection** | Monitors provider quality over time using EMA. Alerts when a provider drops >20% vs historical average. Auto-reroutes traffic away from degraded providers. |
| **Code Analysis** | Static analysis for code smells (long functions, deep nesting, complex conditionals, magic numbers, missing docstrings), tech debt scoring, complexity hotspots, missing test detection. |
| **Multi-Repo Workspaces** | `--workspace` aggregates multiple repos into a single agent context. Cross-repo import detection, language detection, shared file patterns. Read-only support for reference repos. |
| **VS Code Extension** | Agent tasks, code review, test generation, council queries, and explain — all from the VS Code sidebar. Auto-starts `nvh serve` if needed. |

**Scales with your hardware — 6 tiers from no-GPU to DGX Spark:**

| GPU | VRAM | Tier | Models | Mode |
|-----|------|------|--------|------|
| DGX Spark | 128 GB | Tier 5 | Nemotron 70B + Llama 70B + Qwen 72B (3 models, all local) | Multi |
| RTX 6000 Pro BSE | 96 GB | Tier 4 | Cloud planner + Llama 70B coder + Qwen 32B reviewer (dual local) | Multi |
| A100 / A6000 | 48-80 GB | Tier 3 | Cloud planner + Llama 70B coder (`--mode multi` for dual local) | Auto |
| RTX 3090 / 4090 | 24 GB | Tier 2 | Cloud planner + Gemma 2 27B coder | Single |
| RTX 4060 Ti | 16 GB | Tier 1 | Cloud planner + Qwen Coder 7B | Single |
| No GPU | — | Tier 0 | Fully cloud | Single |

```bash
nvh agent --setup                    # pull recommended models
nvh agent --remove                   # clean up models
nvh agent "task" --mode multi        # force multi-model (Tier 3+)
nvh agent "task" --mode single       # force single model
nvh agent "task" --git               # auto-branch + commit changes
nvh agent "task" --no-quality        # skip lint/syntax gates
```

**Multi-model mode** (Tier 4-5, or `--mode multi` on Tier 3): a DIFFERENT model reviews the coder's output, catching bugs the coder's architecture has blind spots for. Cross-model verification is measurably better than self-review.

**Quality gates**: after the agent modifies files, ruff lint + syntax checks run automatically. If they fail, the agent gets the errors and fixes them in a feedback loop.

### Code Review (`nvh review`)

```bash
nvh review                     # review staged changes
nvh review HEAD~3..HEAD        # review last 3 commits
nvh review 42                  # review GitHub PR #42
nvh review --mode multi        # two models review independently
```

Multi-model code review: two different LLM architectures review independently, then findings are synthesized. Catches bugs that self-review and single-model review miss.

### Test Generation (`nvh test-gen`)

```bash
nvh test-gen nvh/core/council.py     # generate tests for a file
nvh test-gen --coverage-gaps         # find and fill coverage gaps
```

Reads your code, identifies untested paths, generates pytest tests, runs them, and iterates until they pass. The agent that improves itself — it writes the tests that verify its own future changes.

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
    
    LOCALCHECK -->|Simple query| GPU[NVIDIA GPU via Ollama<br/>Nemotron + Gemma 4<br/>Two architectures locally]
    LOCALCHECK -->|Complex query| SCORE[Score All Providers<br/>capability · cost · latency · health]
    
    SCORE --> ROUTE{Pick Best<br/>Provider}
    
    ROUTE --> FREE[Free Providers<br/>LLM7 · Groq · GitHub]
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
    QUERY[User Query] --> A1[Expert 1 - Nemotron<br/>local GPU]
    QUERY --> A2[Expert 2 - Gemma 4<br/>local GPU]
    QUERY --> A3[Expert 3 - Groq<br/>cloud free]
    
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

`nvh setup` detects your NVIDIA GPU, selects which models fit in your VRAM, and pulls them automatically. Supports both [NVIDIA Nemotron](https://build.nvidia.com/) and [Google Gemma 4](https://ai.google.dev/gemma) (NVIDIA-optimized) for local council with two different architectures.

<p align="center">
  <img src="docs/screenshots/gpu-detection-demo.gif" alt="nvHive GPU Detection & Model Selection" width="640">
</p>

```bash
nvh setup
# Step 3/3: Local GPU inference
#   Detected: NVIDIA GeForce RTX 4090 (24GB VRAM)
#   Models: nemotron-small, gemma4:26b
#   Pulling nemotron-small... ✓
#   Pulling gemma4:26b... ✓
#   Local council ready — multiple models for consensus
```

**What `nvh setup` handles:**

```mermaid
flowchart TB
    SETUP[nvh setup] --> DETECT[GPU Detection<br/>pynvml reads VRAM · driver · CUDA]
    
    DETECT --> VRAM{Available VRAM?}
    
    VRAM -->|< 6 GB| MINI[nemotron-mini<br/>+ gemma4:e2b]
    VRAM -->|6 – 12 GB| SMALL[nemotron-small<br/>+ gemma4:e4b]
    VRAM -->|12 – 48 GB| CHOICE{User choice}
    VRAM -->|48 GB+| FULL[nemotron 70B<br/>+ gemma4:31b]

    CHOICE -->|Both for council| DUAL[nemotron-small<br/>+ gemma4:26b]
    CHOICE -->|Single model| SINGLE[nemotron 70B only]

    MINI --> CHECK{Ollama running?}
    SMALL --> CHECK
    DUAL --> CHECK
    SINGLE --> CHECK
    FULL --> CHECK
    
    CHECK -->|Not installed| INSTALL[Show install command]
    CHECK -->|Not running| START[Show: ollama serve]
    CHECK -->|Running| PULL[Auto-pull all<br/>models that fit]
    
    PULL --> READY[Ready ✓<br/>Local council enabled]
    
    READY --> ROUTE[nvHive Router<br/>Two model architectures<br/>Learning loop active]
    
    style SMALL fill:#76B900,color:#000
    style DUAL fill:#76B900,color:#000
    style READY fill:#76B900,color:#000
    style ROUTE fill:#1a1a2e,color:#76B900,stroke:#76B900
```

**After setup, routing is automatic:**
- Simple queries → local Nemotron or Gemma 4 on your GPU (free, private)
- Council mode → both models collaborate locally, catching different blind spots
- Complex queries → cloud providers when local quality isn't sufficient
- `nvh bench` measures your GPU's actual tok/s with community baselines
- The learning loop measures each model's quality on YOUR hardware

[Full GPU detection + VRAM guide](docs/GPU_DETECTION.md)

### NVIDIA Inference Stack

| Layer | Technology | Hardware | Use Case |
|-------|-----------|----------|----------|
| **Local** | Ollama + Nemotron | Consumer GPUs (RTX 3060+) | Default local inference, privacy mode |
| **Local** | Ollama + Gemma 4 | Consumer GPUs (RTX 3060+) | NVIDIA-optimized, reasoning + multimodal |
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
        CLI[nvh CLI<br/>agent · review · test-gen]
        WEBUI[Web Dashboard<br/>nvh webui]
        SDK[Python SDK<br/>import nvh]
        CC[Claude Code<br/>MCP]
        NC[NemoClaw<br/>Agent]
        CU[Cursor]
        APP[Your App<br/>OpenAI SDK]
    end

    subgraph nvHive Engine
        API[API Server<br/>:8000]
        MCP[MCP Server<br/>stdio]
        PROXY_OAI[OpenAI Proxy<br/>/v1/proxy]
        PROXY_ANT[Anthropic Proxy<br/>/v1/anthropic]
        AGENT[Agent Loop<br/>plan · execute · verify]
        ROUTER[Adaptive Router<br/>+ Learning Loop]
        COUNCIL[Council Engine<br/>+ Confidence]
        GUARD[Guardrails<br/>4-layer safety]
    end

    subgraph Providers
        GPU[Your GPU<br/>Ollama · Nemotron]
        FREE_P[Free Cloud<br/>Groq · GitHub · LLM7<br/>Google · Cerebras]
        PAID_P[Paid Cloud<br/>OpenAI · Anthropic<br/>DeepSeek · Mistral]
        NIM[NVIDIA NIM<br/>Triton]
    end

    CLI --> API
    WEBUI --> API
    SDK --> API
    CC --> MCP
    NC --> PROXY_OAI
    CU --> MCP
    APP --> PROXY_OAI
    APP --> PROXY_ANT

    MCP --> API
    PROXY_OAI --> API
    PROXY_ANT --> API
    API --> AGENT
    API --> ROUTER
    API --> COUNCIL
    AGENT --> GUARD
    GUARD --> ROUTER
    ROUTER --> GPU
    ROUTER --> FREE_P
    ROUTER --> PAID_P
    ROUTER --> NIM

    style GPU fill:#76B900,color:#000
    style NIM fill:#76B900,color:#000
    style ROUTER fill:#1a1a2e,color:#76B900,stroke:#76B900
    style COUNCIL fill:#1a1a2e,color:#00bcd4,stroke:#00bcd4
    style AGENT fill:#1a1a2e,color:#a855f7,stroke:#a855f7
    style GUARD fill:#1a1a2e,color:#ef4444,stroke:#ef4444
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

## Core Commands

### Agentic Coding

| Command | What It Does |
|---------|-------------|
| `nvh agent "task"` | Recursive agents + iterative QA convergence (6 GPU tiers) |
| `nvh agent --setup` | Pull recommended local models for your GPU |
| `nvh agent --mode multi` | Force multi-model: separate planner, coder, reviewer |
| `nvh agent --sandbox` | Execute shell commands inside a Docker container |
| `nvh agent --workspace ./a,./b` | Multi-repo context for cross-project tasks |
| `nvh agent --git` | Auto-create branch + commit changes |
| `nvh review` | Multi-model code review (staged changes, PRs, commit ranges) |
| `nvh test-gen file.py` | AI test generation with automatic verification |
| `nvh analyze ./src` | Code smells, tech debt score, complexity hotspots |
| `nvh drift` | Check for LLM quality degradation across providers |

### Queries & Council

| Command | What It Does |
|---------|-------------|
| `nvh "question"` | Smart route to best available model |
| `nvh convene "question"` | Council consensus (3+ models collaborate) |
| `nvh throwdown "question"` | Two-pass deep analysis with critique |
| `nvh poll "question"` | Side-by-side provider comparison |
| `nvh safe "question"` | Local only — nothing leaves your machine |
| `nvh ask --escalate` | Try free first, escalate if uncertain |
| `nvh ask --verify` | Cross-model verification |

### Infrastructure

| Command | What It Does |
|---------|-------------|
| `nvh serve` | Start the API server (OpenAI + Anthropic compatible proxy) |
| `nvh webui` | Launch the web dashboard |
| `nvh health` | Provider resilience dashboard |
| `nvh nvidia` | NVIDIA GPU infrastructure status |
| `nvh bench` | GPU speed test (tokens/sec) |
| `nvh setup` | Interactive provider setup |
| `nvh doctor` | Full diagnostic dump for troubleshooting |

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

## Benchmark Results

Real data from NVIDIA DGX Spark (GB10, 120GB). Judged by OpenAI with ground truth verification on math prompts.

### Quality: Council vs Single Model

| Mode | Accuracy | Completeness | Coherence | **Overall** | Cost |
|------|----------|-------------|-----------|---------|------|
| Single Model (Nemotron Super) | 5.5 | 5.7 | 5.0 | **5.1** | $0.00 |
| **Council (Free: Ollama + Groq + Google)** | **9.0** | **8.0** | **9.0** | **8.6** | **$0.00** |

Council consensus scored **68% higher** than a single model on the same prompts. Ground truth verification on math problems caught errors the single model made that an LLM judge alone wouldn't have flagged.

### Speed: Models on DGX Spark

| Model | Size | tok/s |
|-------|------|------:|
| gemma3 | 3.3 GB | **119.3** |
| nemotron-mini | 2.7 GB | **85.7** |
| gemma4 (e4b) | 9.6 GB | **61.7** |
| llama3.1 | 4.9 GB | **48.2** |
| nemotron-3-super | 86 GB | **23.6** |

### Run It Yourself

<p align="center">
  <img src="docs/screenshots/bench-demo.gif" alt="nvHive Benchmark Demo" width="640">
</p>

```bash
nvh bench              # GPU speed (tokens/sec)
nvh bench -q           # speed + quality comparison
nvh health             # provider resilience
nvh why                # explain last routing decision
nvh estimate --gpu rtx_4090   # predict tok/s on any GPU
```

16 prompts across code generation, debugging, reasoning, math, creative writing, and Q&A. LLM judge + ground truth verification. Run it yourself. Publish the results.

## Learn More

| Guide | Description |
|-------|-------------|
| [Getting Started](docs/GETTING_STARTED.md) | First-time setup |
| [Commands](docs/COMMANDS.md) | Full CLI reference (50+ commands) |
| [Providers](docs/PROVIDERS.md) | 23 providers, rate limits, free tiers |
| [Council System](docs/COUNCIL.md) | Multi-LLM consensus with confidence scoring |
| [Releasing](docs/RELEASING.md) | Release runbook, version bumps, PyPI publishing |
| [Windows Troubleshooting](docs/TROUBLESHOOTING_WINDOWS.md) | Encoding, segfaults, port 80, nvh.exe locks |
| [GPU Detection](docs/GPU_DETECTION.md) | Auto-detection, model selection, OOM protection |
| [Claude Code](docs/CLAUDE_CODE_INTEGRATION.md) | MCP server setup |
| [NemoClaw](docs/NEMOCLAW.md) | NVIDIA NemoClaw integration |
| [OpenClaw Integration](docs/OPENCLAW_MIGRATION.md) | Works alongside OpenClaw |
| [SDK & API](docs/SDK_API.md) | Python SDK, REST API, proxies |
| [Deploy Without Root](docs/DEPLOY_NO_ROOT.md) | No-root install on servers (Ollama, keyring, systemd user service) |
| [Architecture](docs/ARCHITECTURE.md) | System design and adaptive learning |

## License

MIT License. See [LICENSE](LICENSE) for details.
