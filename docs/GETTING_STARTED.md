# Getting Started with NVHive

NVHive is a multi-LLM orchestration platform that routes queries to the best AI model, convenes a council of AI advisors, and seamlessly falls back between providers.

---

## Quick Start (3 minutes)

### Option A: Docker (recommended for Ubuntu/Linux)

```bash
# Clone the repo
git clone https://github.com/thatcooperguy/nvhive.git
cd nvhive

# Copy the example env file and add your API keys
cp .env.example .env
nano .env   # Add at least one API key (OPENAI_API_KEY, ANTHROPIC_API_KEY, etc.)

# Start everything (API + Web UI + local Ollama)
docker compose up -d

# Open the web UI
xdg-open http://localhost:3000    # Linux
open http://localhost:3000        # macOS
```

That's it. The web UI is at **http://localhost:3000** and the API is at **http://localhost:8000**.

### Option B: One-line setup (Ubuntu with NVIDIA GPU)

```bash
curl -sSL https://raw.githubusercontent.com/thatcooperguy/nvhive/main/scripts/setup.sh | bash
```

This installs Docker (rootless, no root needed), starts NVHive, and pulls a local AI model.

### Option C: pip install (CLI only)

```bash
pip install -e .
nvh setup       # One-shot free tier wizard — no credit card needed
nvh "Hello!"
```

---

## The `nvh setup` Wizard

`nvh setup` is the fastest path from zero to working. Run it once after install:

```bash
nvh setup
```

The wizard automatically:

1. Detects if Ollama is running locally (unlimited free inference)
2. Enables **LLM7** — anonymous access, no account, 30 req/min
3. Enables **GitHub Models** — free frontier models if you have a GitHub account
4. Enables **SiliconFlow** — 1000 RPM free tier, permanent
5. Walks you through **Groq**, **Google Gemini**, and **Mistral** free tiers
6. Sets your `defaults.mode` (ask / convene / poll / throwdown)

After setup, `nvh "question"` works immediately with no further configuration.

---

## Your First Query

### Smart default (routes to the best available advisor)

```bash
nvh "What is the CAP theorem?"
```

Output:
```
[ask → groq/llama-3.3-70b]

The CAP theorem states that a distributed system can only guarantee
two of three properties: Consistency, Availability, and Partition tolerance...

Advisor: groq | Model: llama-3.3-70b | Tokens: 42 in / 187 out | Cost: $0.0000 | 94ms
```

### Ask a specific advisor

```bash
nvh ask "Write a Python binary search" -p anthropic
nvh ask "Explain quantum computing" -p groq     # Ultra-fast
nvh ask "Review this code" -p deepseek          # Very cheap
```

### Use advisor shortcuts (fastest syntax)

```bash
nvh anthropic "Explain monads"
nvh groq "What HTTP status code means rate limiting?"
nvh ollama "Analyze this private document"
```

### Pipe files in

```bash
cat main.py | nvh ask "Review this code for bugs"
nvh ask "Summarize this document" --file report.pdf
```

---

## Convene Mode — Your AI Advisory Board

Convene mode sends your question to multiple LLMs simultaneously, then synthesizes a weighted consensus answer.

### Basic convene

```bash
nvh convene "Should we use PostgreSQL or MongoDB for our SaaS app?"
```

Each advisor responds independently, then a synthesis LLM combines the best insights.

### Auto-generated expert agents

```bash
nvh convene "Should we migrate to microservices?" --auto-agents
```

NVHive analyzes your question and generates relevant expert personas:
- **Software Architect** — system design, scalability
- **DevOps/SRE Engineer** — operational complexity
- **CTO** — long-term technical vision

Each LLM adopts a unique expert perspective, creating productive tension in the analysis.

### Use a cabinet

```bash
# Executive team reviews your business plan
nvh convene "Is this SaaS pricing model viable?" --cabinet executive

# Engineering team reviews your architecture
nvh convene "How should we design the API?" --cabinet engineering

# Security team audits your auth flow
nvh convene "Review our OAuth implementation" --cabinet security_review
```

Available cabinets: `executive`, `engineering`, `security_review`, `code_review`, `product`, `data`, `full_board`

### Preview which agents would be generated

```bash
nvh agent analyze "How to scale our database to 1M users?" -n 5
```

```
Auto-generated council for: How to scale our database to 1M users?

┃ # ┃ Role                  ┃ Expertise                    ┃
┡━━━╇━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┩
│ 1 │ Database Administrator│ database design, replication  │
│ 2 │ Software Architect    │ system design, scalability    │
│ 3 │ Performance Engineer  │ load testing, optimization    │
│ 4 │ DevOps/SRE Engineer   │ infrastructure, monitoring    │
│ 5 │ Senior Backend Eng.   │ APIs, databases, performance  │
```

---

## Throwdown Mode — Deep Analysis

Throwdown runs a two-pass synthesis for the highest-quality output on hard questions:

```bash
nvh throwdown "Best database architecture for a 50k DAU SaaS product?"
```

**How it works:**
1. **Pass 1** — Auto-generated expert council produces initial analysis
2. **Pass 2** — A second council critiques Pass 1: what did they miss? what's wrong?
3. **Final synthesis** — Integrates both passes into a definitive answer

Higher cost than a single query, but significantly better reasoning on complex decisions.

---

## Safe Mode — Local & Private

```bash
nvh safe "Analyze this confidential contract"
cat sensitive.txt | nvh safe "Summarize this"
```

`nvh safe` routes exclusively to locally-running Ollama models. No data leaves your machine:
- No API calls to external services
- No logging or conversation persistence
- No analytics or telemetry

Use it whenever the content is sensitive. Requires Ollama installed locally:

```bash
nvh ollama          # Check Ollama status and available models
curl -fsSL https://ollama.com/install.sh | sh   # Install if not present
ollama pull llama3.1:8b                          # Pull a model
```

---

## Poll Mode

See how different advisors answer the same question side-by-side:

```bash
nvh poll "Write a merge sort in Python"
```

Shows parallel panels with each advisor's response, tokens, cost, and latency.

---

## Quick Mode

Route to the fastest or cheapest available advisor:

```bash
nvh quick "What does HTTP 429 mean?"
```

Uses routing strategy `fastest` — picks the lowest-latency provider with available credits. Ideal for simple factual questions where speed matters.

---

## Interactive REPL

Launch an interactive session with multi-turn conversation:

```bash
nvh repl
nvh          # Same thing — no args launches the REPL
```

```
╭─ NVHive REPL ────────────────────────────────────╮
│ Advisors: groq, github, ollama                   │
│ Type /help for commands. Ctrl+D to exit.         │
╰──────────────────────────────────────────────────╯

[groq/llama-3.3-70b #1] > What is a monad?

A monad is a design pattern from functional programming...

[groq/llama-3.3-70b #2] > Can you give me a Python example?

Here's a simple Maybe monad in Python...

[groq/llama-3.3-70b #3] > /convene
Convene mode: ON

[groq/llama-3.3-70b #3] [convene] > Now explain it to a 5-year-old

--- Software Architect ---
Imagine you have a magic box...

--- Senior Backend Engineer ---
Think of it like a lunchbox...

--- Synthesis ---
Both experts agree: a monad is like a special container...
```

### REPL commands

| Command | Description |
|---|---|
| `/advisor anthropic` | Switch advisor |
| `/model gpt-4o-mini` | Switch model |
| `/system You are a pirate` | Set system prompt |
| `/convene` | Toggle convene mode |
| `/auto-agents` | Toggle auto-agent generation |
| `/cabinet executive` | Set agent cabinet |
| `/cost` | Show session cost |
| `/history` | Show conversation |
| `/save chat.json` | Export conversation |
| `/clear` | Start fresh |
| `/help` | All commands |
| `/quit` | Exit |

---

## Advisor Setup

### All 22 supported advisors

| Provider | Free Tier | Get API Key | Best For |
|---|---|---|---|
| **OpenAI** | No | [platform.openai.com/api-keys](https://platform.openai.com/api-keys) | Code, multimodal, general |
| **Anthropic** | No | [console.anthropic.com/settings/keys](https://console.anthropic.com/settings/keys) | Reasoning, long-form, code review |
| **Google Gemini** | Yes — 15 req/min | [aistudio.google.com/apikey](https://aistudio.google.com/apikey) | Long context, multimodal |
| **Groq** | Yes — 30 req/min | [console.groq.com/keys](https://console.groq.com/keys) | Ultra-fast inference |
| **Grok (xAI)** | No | [console.x.ai](https://console.x.ai) | Reasoning, real-time data |
| **Mistral** | Yes — 2 RPM | [console.mistral.ai/api-keys](https://console.mistral.ai/api-keys) | Multilingual, EU-hosted |
| **Cohere** | Yes — trial key | [dashboard.cohere.com/api-keys](https://dashboard.cohere.com/api-keys) | RAG, summarization |
| **DeepSeek** | No (very cheap) | [platform.deepseek.com](https://platform.deepseek.com) | Code, math ($0.07/M tokens) |
| **Ollama** | Yes — unlimited | [ollama.com/download](https://ollama.com/download) | Free, private, local |
| **Perplexity** | No | [perplexity.ai/settings/api](https://www.perplexity.ai/settings/api) | Search-augmented, cited answers |
| **Together AI** | No | [api.together.xyz](https://api.together.xyz/settings/api-keys) | Open-source models |
| **Fireworks AI** | Yes | [fireworks.ai](https://fireworks.ai/account/api-keys) | Fast open-source inference |
| **OpenRouter** | No | [openrouter.ai/keys](https://openrouter.ai/keys) | Meta-router, model diversity |
| **Cerebras** | Yes — 30 req/min | [cloud.cerebras.ai](https://cloud.cerebras.ai) | Fastest inference hardware |
| **SambaNova** | Yes | [cloud.sambanova.ai](https://cloud.sambanova.ai) | Enterprise-scale models |
| **Hugging Face** | Yes — Inference API | [huggingface.co/settings/tokens](https://huggingface.co/settings/tokens) | Research, open weights |
| **AI21 Labs** | Yes | [studio.ai21.com](https://studio.ai21.com/account/api-key) | Summarization, Jamba models |
| **GitHub Models** | Yes — 50–150 req/day | [github.com/marketplace/models](https://github.com/marketplace/models) | Frontier models for GitHub users |
| **NVIDIA NIM** | Yes — 1000+ credits | [build.nvidia.com](https://build.nvidia.com) | GPU-optimized NVIDIA models |
| **SiliconFlow** | Yes — 1000 RPM | [cloud.siliconflow.cn](https://cloud.siliconflow.cn) | Chinese + global open models |
| **LLM7** | Yes — no signup | [llm7.io](https://llm7.io) | Anonymous, 30 RPM, zero friction |
| **Mock** | Yes — unlimited | n/a | Testing and development only |

### Add an advisor via CLI shortcut

```bash
# Use the advisor name as a command with no question → setup wizard
nvh openai          # Opens OpenAI setup: shows URL, prompts for API key
nvh anthropic       # Opens Anthropic setup
nvh github          # Opens GitHub Models setup (free for GitHub users)
nvh llm7            # Enables LLM7 (no API key needed)
nvh ollama          # Checks Ollama connectivity, lists local models
```

### Add via environment variables

```bash
export OPENAI_API_KEY=sk-...
export ANTHROPIC_API_KEY=sk-ant-...
export GROQ_API_KEY=gsk_...
export GOOGLE_API_KEY=AIza...
export GITHUB_TOKEN=ghp_...
```

### Add via the Web UI

1. Open **http://localhost:3000/advisors**
2. Click on an advisor card
3. Enter your API key
4. Click "Test Connection"

---

## Running Local AI with Ollama

Ollama lets you run AI models locally — free, private, no API key needed.

### With Docker (automatic)

Docker Compose starts Ollama automatically and pulls a small model:

```bash
docker compose up -d    # Ollama starts on port 11434
```

### Pull more models

```bash
# Quick start (1.3 GB)
docker compose exec ollama ollama pull nemotron-mini

# Better quality (4.7 GB)
docker compose exec ollama ollama pull llama3.1:8b

# Code-focused (3.8 GB)
docker compose exec ollama ollama pull codellama

# Or use the setup script
./scripts/ollama-setup.sh
```

### Without Docker

```bash
# Install Ollama
curl -fsSL https://ollama.com/install.sh | sh

# Pull a model
ollama pull llama3.1:8b

# NVHive auto-detects Ollama at localhost:11434
nvh ollama          # Check status
nvh safe "Test question"    # Use it in safe mode
```

---

## Budget Controls

Set spending limits to avoid surprises:

```bash
# Set limits
nvh config set budget.daily_limit_usd 5.00
nvh config set budget.monthly_limit_usd 50.00

# Check spending
nvh budget status
```

```
┃ Metric         ┃   Value ┃     Limit ┃
┡━━━━━━━━━━━━━━━━╇━━━━━━━━━╇━━━━━━━━━━━┩
│ Daily spend    │ $0.4230 │    $5.00  │
│ Monthly spend  │ $12.850 │   $50.00  │
│ Daily queries  │      47 │       —   │
```

When limits are hit, NVHive blocks further queries (configurable: `budget.hard_stop: false` to just warn).

See your cumulative savings from free-tier routing:

```bash
nvh savings
```

---

## Configuration

### Config file location

```
~/.nvhive/config.yaml
```

### Key settings

```yaml
# Default advisor and mode
defaults:
  provider: groq
  model: llama-3.3-70b
  temperature: 1.0
  max_tokens: 4096
  mode: ask        # ask | convene | poll | throwdown

# Council mode settings
convene:
  default_weights:
    openai: 0.40
    anthropic: 0.35
    google: 0.25
  synthesis_provider: anthropic
  strategy: weighted_consensus
  quorum: 2

# Auto-routing rules
routing:
  rules:
    - match: { task_type: code_generation }
      provider: anthropic
    - match: { task_type: math }
      provider: openai
    - match: { task_type: fast_lookup }
      provider: groq
```

### Useful config commands

```bash
nvh config init                              # Interactive setup wizard
nvh config get defaults.provider             # Read a value
nvh config set defaults.provider anthropic   # Write a value
nvh config edit                              # Open in $EDITOR
```

### Named profiles

```bash
# Use a cost-optimized profile
nvh ask "Quick question" --profile cost_optimized

# Use a quality-focused profile
nvh convene "Important decision" --profile quality
```

---

## HIVE.md — Project Context Injection

Place a `HIVE.md` file in your project root. NVHive automatically injects it as a system prompt when you run any `nvh` command from that directory:

```markdown
# HIVE.md
This is a Python 3.12 microservices project using FastAPI and PostgreSQL.
Coding standards: PEP 8, type hints required, 100% test coverage target.
Never suggest JavaScript. Always use async/await. PostgreSQL preferred over SQLite.
```

Any `nvh "question"` in that directory starts with full project context automatically.

---

## REST API

Start the API server:

```bash
nvh serve --port 8000
```

### Endpoints

```bash
# Simple query
curl -X POST http://localhost:8000/v1/query \
  -H "Content-Type: application/json" \
  -d '{"prompt": "Hello world", "provider": "openai"}'

# Council mode with auto-agents
curl -X POST http://localhost:8000/v1/council \
  -H "Content-Type: application/json" \
  -d '{"prompt": "Should we use GraphQL?", "auto_agents": true}'

# Poll advisors
curl -X POST http://localhost:8000/v1/compare \
  -H "Content-Type: application/json" \
  -d '{"prompt": "Write a sort function", "providers": ["openai", "anthropic"]}'

# List advisors
curl http://localhost:8000/v1/advisors

# Budget status
curl http://localhost:8000/v1/budget/status

# Agent cabinet presets
curl http://localhost:8000/v1/agents/presets
```

API docs with interactive Swagger UI: **http://localhost:8000/docs**

---

## Web UI

The web dashboard runs at **http://localhost:3000** and provides:

- **Dashboard** — Advisor status, quick query, budget summary
- **Query** — Full query interface with mode switching (Ask / Convene / Poll / Throwdown)
- **Council** — Multi-panel view showing each expert's streaming response
- **Advisors** — Add/configure advisors, test connectivity, browse model catalog
- **Settings** — Configure defaults, budgets, and council weights

### Running the web UI

```bash
# With Docker (automatic)
docker compose up -d
# → http://localhost:3000

# Without Docker
cd web
npm install
npm run dev
# → http://localhost:3000
# (requires nvh serve running on port 8000)
```

---

## Conversation Management

```bash
# List recent conversations
nvh conversation list

# Show a full conversation
nvh conversation show abc123

# Search conversations
nvh conversation search "database migration"

# Export to markdown
nvh conversation export abc123 --format markdown --output chat.md

# Delete a conversation
nvh conversation delete abc123
```

---

## Diagnostics

```bash
nvh doctor          # Full health check: config, API keys, connectivity
nvh status          # Quick advisor status overview
```

`nvh doctor` checks:
- Config file validity
- API key presence for each configured advisor
- Network connectivity to each provider
- Ollama local status
- Budget limits

---

## Example Workflows

### Code Review

```bash
cat pull_request.diff | nvh convene "Review this PR for bugs, security issues, and code quality" \
  --cabinet code_review --auto-agents
```

### Architecture Decision

```bash
nvh convene "Should we use event sourcing for our e-commerce platform? We have 50k DAU." \
  --cabinet engineering --auto-agents
```

### Quick Research

```bash
# Fast answer via Groq (sub-100ms)
nvh quick "What HTTP status code for rate limiting?"

# Deep analysis via throwdown
nvh throwdown "Compare REST vs GraphQL vs gRPC for a microservices platform"
```

### Private / Confidential Work

```bash
# Local only — nothing leaves your machine
nvh safe "Analyze this confidential report" --file report.txt
cat sensitive_code.py | nvh safe "Find security vulnerabilities"
```

### Cost-Conscious Usage

```bash
# Use free providers for simple tasks
nvh quick "Fix this typo: recieve"

# Use throwdown only for important decisions
nvh throwdown "Should we rewrite the core engine in Rust?"
```

### Student / Zero-Budget Setup

```bash
nvh setup                               # Enable all free providers
nvh status                              # Verify what's active
nvh convene "Explain Big O notation"    # Uses only free providers
nvh savings                             # See how much you've saved
```

---

## Troubleshooting

### "No advisors available"

```bash
nvh setup          # Run the free tier wizard
nvh status         # See which advisors are configured
nvh doctor         # Full diagnostics
```

### "Advisor X is unhealthy"

```bash
nvh doctor         # Check which advisors are failing and why
# Circuit breakers auto-reset after a 30s cooldown
```

### "Budget limit reached"

```bash
nvh budget status                              # Check current spend
nvh config set budget.daily_limit_usd 20.00   # Increase limit
nvh config set budget.hard_stop false          # Warn only, don't block
```

### Docker issues

```bash
# Check service logs
docker compose logs nvhive-api
docker compose logs nvhive-web
docker compose logs ollama

# Restart everything
docker compose restart

# Full rebuild
docker compose down && docker compose up -d --build
```

### Ollama not detected

```bash
# Check if Ollama is running
curl http://localhost:11434/api/tags

# Start Ollama
docker compose restart ollama

# Pull a model if none exist
docker compose exec ollama ollama pull llama3.1:8b

# Or via CLI
nvh ollama
```

---

## What's Next?

- Browse available models: `nvh advisor info <name>`
- Try different cabinets: `nvh agent analyze "your question"`
- Set up budget alerts: `nvh config set budget.daily_limit_usd 5.00`
- Configure routing rules for your workflow: `nvh config edit`
- Explore the API docs at **http://localhost:8000/docs**
- Try `nvh bench` when you want to benchmark your local GPU *(coming soon)*
