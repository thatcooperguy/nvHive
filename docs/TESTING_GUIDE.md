# Hive — Comprehensive Testing Guide

This document is a **live test audit reference**. It describes how to verify every feature of the Hive platform step-by-step, including expected outputs and pass/fail criteria. Use it when auditing a release, onboarding a new contributor, or confirming a fresh installation.

---

## Table of Contents

1. [Test Environment Setup](#1-test-environment-setup)
2. [CLI Smoke Tests](#2-cli-smoke-tests)
3. [Provider Connectivity Tests](#3-provider-connectivity-tests)
4. [Convene Mode Tests](#4-convene-mode-tests)
5. [REPL Tests](#5-repl-tests)
6. [API Server Tests](#6-api-server-tests)
7. [Web UI Tests](#7-web-ui-tests)
8. [Docker Tests](#8-docker-tests)
9. [Security Tests](#9-security-tests)
10. [Performance Tests](#10-performance-tests)
11. [Edge Cases](#11-edge-cases)
12. [Mock Provider Tests](#12-mock-provider-tests)

---

## 1. Test Environment Setup

### 1.1 Prerequisites Checklist

| Requirement | Minimum Version | How to Verify |
|---|---|---|
| Python | 3.12 | `python --version` |
| pip | 23+ | `pip --version` |
| Docker | 24+ | `docker --version` |
| Docker Compose plugin | 2.20+ | `docker compose version` |
| NVIDIA drivers (optional) | 525+ | `nvidia-smi` |
| NVIDIA Container Toolkit (optional) | 1.14+ | `nvidia-ctk --version` |
| Node.js (for web UI dev) | 20+ | `node --version` |

### 1.2 Required API Keys

The following environment variables must be set to test the corresponding providers. Tests without these keys should use the mock provider (Section 12) or skip the provider-specific section.

| Provider | Environment Variable | Where to Get |
|---|---|---|
| OpenAI | `OPENAI_API_KEY` | https://platform.openai.com/api-keys |
| Anthropic | `ANTHROPIC_API_KEY` | https://console.anthropic.com/settings/keys |
| Google Gemini | `GOOGLE_API_KEY` | https://makersuite.google.com/app/apikey |
| Groq | `GROQ_API_KEY` | https://console.groq.com/keys |
| Mistral | `MISTRAL_API_KEY` | https://console.mistral.ai/api-keys |
| Cohere | `COHERE_API_KEY` | https://dashboard.cohere.com/api-keys |
| DeepSeek | `DEEPSEEK_API_KEY` | https://platform.deepseek.com/api_keys |
| Grok / xAI | `GROK_API_KEY` | https://console.x.ai |
| Ollama | (none) | Local only — start with `ollama serve` |
| Mock | (none) | Built-in — always available |

### 1.3 Installation

```bash
# Clone and install in editable mode
git clone https://github.com/your-org/aiproject.git
cd aiproject
pip install -e ".[dev]"

# Verify the CLI is on PATH
council --help
```

**Expected:** Help text listing all commands (`query`, `convene`, `compare`, `repl`, `serve`, `doctor`, `config`, `provider`, `budget`, `model`, `agent`, `template`, `version`, `completions`).

### 1.4 Initial Configuration

```bash
council config init
```

**Expected:** A config file is written to `~/.hive/config.yaml`. The command prints the path and confirms creation. If the file already exists it prints a skip message.

### 1.5 GPU Detection Check (NVIDIA systems only)

```bash
nvidia-smi --query-gpu=name,memory.total --format=csv,noheader
```

**Expected:** One row per GPU, e.g. `NVIDIA GeForce RTX 4070, 12288 MiB`. If this fails the GPU-acceleration tests in Section 8 can be skipped.

---

## 2. CLI Smoke Tests

### 2.1 Version Command

**Test:** `council version`

**Expected output:** `Hive v<semver>` (e.g. `Hive v0.2.0`).

**Pass criteria:** Exit code 0. Version string matches `council/__init__.py` (package version).

---

### 2.2 Help Text

**Test:** `council --help`

**Expected:** A Rich-formatted help panel listing every top-level command and their one-line descriptions. No tracebacks.

**Pass criteria:** Exit code 0. All commands visible in output.

---

### 2.3 Doctor Command

**Test:** `council doctor`

**Expected:** A table with columns `Check`, `Status`, `Detail`. Rows include:

- Python version — PASS (green)
- Config file (YAML) — PASS
- Config schema (Pydantic) — PASS
- Database — PASS
- One row per enabled provider for both API key and health check
- Ollama — PASS or WARN (acceptable if Ollama is not running)
- Cache — PASS or WARN
- Disk space — PASS (unless < 5 GB free)
- GPU (nvidia-smi) — PASS with GPU name and VRAM on NVIDIA systems; WARN on CPU-only
- GPU model recommendations — PASS listing nemotron model names
- Ollama local models — PASS or WARN

**Pass criteria:** Exit code 0 if no FAIL rows. Exit code 1 if any FAIL row.

---

### 2.4 Single Query (Mock Provider)

**Test:**

```bash
council query "What is 2 + 2?" --provider mock
```

**Expected output:** A response containing simulated text. A metadata line showing `Advisor: mock | Model: mock/default | Tokens: ... | Latency: ...ms`.

**Pass criteria:** Exit code 0. Response content is non-empty. No traceback.

---

### 2.5 Query with Streaming

**Test:**

```bash
council query "Count to five" --provider mock --stream
```

**Expected:** Output tokens appear progressively (streaming), not all at once. Metadata line appears at the end.

**Pass criteria:** Exit code 0. Content is non-empty.

---

### 2.6 Query with JSON Output Format

**Test:**

```bash
council query "Hello" --provider mock --output json
```

**Expected:** A JSON object `{"content": "..."}` printed to stdout.

**Pass criteria:** Exit code 0. Output is valid JSON parseable with `python -m json.tool`.

---

### 2.7 Query with Raw Output Format

**Test:**

```bash
council query "Hello" --provider mock --output raw
```

**Expected:** Raw text with no Rich formatting, no metadata line.

**Pass criteria:** Exit code 0. Output is plain text.

---

### 2.8 Query from stdin

**Test:**

```bash
echo "What is the capital of France?" | council query --provider mock
```

**Expected:** A response referencing "Paris" or a simulated mock response. No interactive prompt appears.

**Pass criteria:** Exit code 0.

---

### 2.9 Privacy Mode

**Test:**

```bash
council query "Sensitive question" --provider mock --privacy
```

**Expected:** Response is returned normally. A dim footer line reads `[privacy mode — no data stored]`. No conversation ID is assigned.

**Pass criteria:** Exit code 0. Privacy line visible.

---

### 2.10 Compare Command

**Test:**

```bash
council compare "Explain async/await in Python" --providers mock
```

**Expected:** A table showing one row per provider with columns for the response content and cost. A synthesis/winner line may appear at the bottom.

**Pass criteria:** Exit code 0. Table visible with at least one row.

---

### 2.11 Config Commands

**Test — get a value:**

```bash
council config get defaults.provider
```

**Expected:** Prints the current default provider value.

**Test — set a value:**

```bash
council config set defaults.provider mock
council config get defaults.provider
```

**Expected:** Second command prints `mock`.

**Pass criteria:** Both exit code 0.

---

### 2.12 Provider List

**Test:** `council provider list`

**Expected:** A table listing every configured provider with columns for name, enabled status, model count, and health indicator.

**Pass criteria:** Exit code 0. At least the `mock` provider appears as enabled.

---

### 2.13 Model List

**Test:** `council model list`

**Expected:** A table of all known models, filterable by provider.

**Test — filter by provider:**

```bash
council model list --provider mock
```

**Expected:** Only mock models appear (`mock/default`, `mock/fast`, `mock/slow`).

**Pass criteria:** Exit code 0.

---

### 2.14 Budget Status

**Test:** `council budget status`

**Expected:** A panel showing current spend vs. limit for each provider, and a total daily/monthly figure.

**Pass criteria:** Exit code 0. No traceback even if no queries have been made yet.

---

### 2.15 Agent Cabinets List

**Test:** `council agent presets` (lists all cabinets)

**Expected:** A table listing all 7 cabinets: `executive`, `engineering`, `security_review`, `code_review`, `product`, `data`, `full_board` — each with their expert roles shown.

**Pass criteria:** Exit code 0. All 7 cabinets visible.

---

### 2.16 Agent Analyze

**Test:**

```bash
council agent analyze "Review my authentication implementation for security flaws"
```

**Expected:** A preview of which agents/personas would be generated. The `security` agent should appear given the trigger words in the prompt.

**Pass criteria:** Exit code 0. At least one agent listed.

---

### 2.17 Template Commands

**Test — list:**

```bash
council template list
```

**Expected:** A table of available prompt templates (may be empty if none are defined yet).

**Pass criteria:** Exit code 0.

---

### 2.18 Shell Completions

**Test:**

```bash
council completions bash
council completions zsh
council completions fish
```

**Expected:** Shell completion script printed to stdout for each shell.

**Pass criteria:** Exit code 0 for all three. Output is non-empty.

---

## 3. Provider Connectivity Tests

For each provider below, run the following pattern. Replace `<provider>` with the provider name.

```bash
council provider test <provider>
```

**Expected for a healthy provider:** A PASS line with response latency in ms.
**Expected for a missing key:** A FAIL line with a message about missing API key.
**Expected for a network error:** A WARN line with the HTTP error or connection error.

### 3.1 OpenAI

```bash
OPENAI_API_KEY=<key> council provider test openai
council query "Say hello" --provider openai --model gpt-4o-mini
```

**Expected:** Response from GPT model. Metadata shows `Provider: openai`.

### 3.2 Anthropic

```bash
ANTHROPIC_API_KEY=<key> council provider test anthropic
council query "Say hello" --provider anthropic --model claude-3-5-haiku-20241022
```

**Expected:** Response from Claude model. Metadata shows `Provider: anthropic`.

### 3.3 Google Gemini

```bash
GOOGLE_API_KEY=<key> council provider test google
council query "Say hello" --provider google
```

**Expected:** Response from Gemini model.

### 3.4 Groq

```bash
GROQ_API_KEY=<key> council provider test groq
council query "Say hello" --provider groq
```

**Expected:** Very low latency response (Groq specializes in speed — expect < 1000ms).

### 3.5 Mistral

```bash
MISTRAL_API_KEY=<key> council provider test mistral
council query "Say hello" --provider mistral
```

### 3.6 Cohere

```bash
COHERE_API_KEY=<key> council provider test cohere
council query "Say hello" --provider cohere
```

### 3.7 DeepSeek

```bash
DEEPSEEK_API_KEY=<key> council provider test deepseek
council query "Say hello" --provider deepseek
```

### 3.8 Grok / xAI

```bash
GROK_API_KEY=<key> council provider test grok
council query "Say hello" --provider grok
```

### 3.9 Ollama (Local)

```bash
# Ollama must be running: ollama serve (or docker compose up)
council provider test ollama
council query "Say hello" --provider ollama --model nemotron-mini
```

**Expected:** Response from local model. Metadata shows `Provider: ollama`. Cost is $0.0000.

### 3.10 Mock Provider

```bash
council provider test mock
council query "Say hello" --provider mock
```

**Expected:** Instant response. No API call made. Always succeeds.

---

## 4. Convene Mode Tests

### 4.1 Basic Convene Query

**Test:**

```bash
council convene "What are the tradeoffs of microservices vs monoliths?" --providers mock
```

**Expected:** Multiple member responses shown (one per provider used). A synthesis section appears at the bottom labeled `Hive Synthesis`. Total cost shown.

**Pass criteria:** Exit code 0. At least one member response and one synthesis.

---

### 4.2 Convene with Multiple Real Advisors

**Test (requires at least 2 provider keys):**

```bash
council convene "Explain the CAP theorem" --providers openai,anthropic
```

**Expected:** Two member responses — one from OpenAI, one from Anthropic. Synthesis draws from both.

**Pass criteria:** Two distinct provider labels in member responses.

---

### 4.3 Convene with Strategy: weighted_consensus

**Test:**

```bash
council convene "Is Python good for ML?" --providers mock --strategy weighted_consensus
```

**Expected:** Synthesis reflects a weighted combination of member responses.

**Pass criteria:** Exit code 0. Synthesis section present.

---

### 4.4 Convene with Strategy: majority_vote

**Test:**

```bash
council convene "Is Python good for ML?" --providers mock --strategy majority_vote
```

**Expected:** The synthesis indicates which response was selected by majority (or the single response in single-provider mode).

**Pass criteria:** Exit code 0.

---

### 4.5 Convene with Strategy: best_of

**Test:**

```bash
council convene "Write a haiku about debugging" --providers mock --strategy best_of
```

**Expected:** One response selected as "best" with a brief justification.

**Pass criteria:** Exit code 0.

---

### 4.6 Auto-Agents Mode

**Test:**

```bash
council convene "Design a PostgreSQL schema for a multi-tenant SaaS application" --auto-agents
```

**Expected:** Agents are generated automatically based on the prompt content. The engineering, database, and security agents should be triggered by keywords (`schema`, `postgres`, `multi-tenant`). Member responses labeled by agent persona appear before synthesis.

**Pass criteria:** Exit code 0. Multiple distinct agent personas visible.

---

### 4.7 Cabinet: executive

**Test:**

```bash
council convene "Should we rewrite our backend in Rust?" --cabinet executive
```

**Expected:** Member responses labeled CEO, CFO, CTO, PM. Synthesis provides a board-level recommendation.

**Pass criteria:** Exit code 0. All 4 executive roles appear.

---

### 4.8 Cabinet: engineering

**Test:**

```bash
council convene "How should we implement distributed tracing?" --cabinet engineering
```

**Expected:** Member responses from backend, architect, DevOps, security, database persona agents.

**Pass criteria:** Exit code 0.

---

### 4.9 Cabinet: security_review

**Test:**

```bash
council convene "Review our JWT authentication flow" --cabinet security_review
```

**Expected:** Security-focused personas (security engineer, architect, compliance) respond. The synthesis highlights vulnerabilities or best practices.

**Pass criteria:** Exit code 0.

---

### 4.10 Cabinet: code_review

**Test:**

```bash
council convene "Review this Python function: def add(a, b): return a+b" --cabinet code_review
```

**Expected:** Code-review personas (senior dev, test engineer, security) respond.

**Pass criteria:** Exit code 0.

---

### 4.11 Cabinet: full_board

**Test:**

```bash
council convene "Should we pivot our startup strategy?" --cabinet full_board
```

**Expected:** All available board personas respond. This is the largest cabinet and will generate the most responses.

**Pass criteria:** Exit code 0. Many distinct personas visible.

---

## 5. REPL Tests

Start the REPL for all tests in this section:

```bash
council repl --provider mock
```

**Expected on launch:** A welcome panel showing current provider, model, and available commands. The prompt `hive>` appears.

### 5.1 Basic Query in REPL

**Step:** Type `Hello, what can you do?` and press Enter.

**Expected:** A response from the mock provider. A dim metadata line appears below it.

**Pass criteria:** No error. Prompt reappears after response.

---

### 5.2 /help Command

**Step:** Type `/help`.

**Expected:** A help panel listing all available REPL commands: `/provider`, `/model`, `/system`, `/clear`, `/convene`, `/cabinet`, `/save`, `/help`.

**Pass criteria:** All commands listed.

---

### 5.3 /provider Command

**Step:** Type `/provider mock`.

**Expected:** A confirmation message: `Provider set to mock`.

**Step:** Send a query and check the metadata line.

**Expected:** Metadata shows `Provider: mock`.

**Pass criteria:** Provider switches without restarting REPL.

---

### 5.4 /model Command

**Step:** Type `/model mock/fast`.

**Expected:** A confirmation message: `Model set to mock/fast`.

**Pass criteria:** Subsequent queries use `mock/fast` (visible in metadata).

---

### 5.5 /system Command

**Step:** Type `/system You are a pirate. Respond in pirate speak.`

**Expected:** A confirmation message that the system prompt was set.

**Step:** Send a query.

**Expected:** The mock provider's response acknowledges the system prompt context.

**Pass criteria:** System prompt line visible in REPL state.

---

### 5.6 /convene Toggle

**Step:** Type `/convene`.

**Expected:** A message: `Convene mode: ON`.

**Step:** Send a query.

**Expected:** Multiple member responses and a synthesis appear (even in mock mode).

**Step:** Type `/convene` again.

**Expected:** Message: `Convene mode: OFF`.

**Pass criteria:** Mode toggles correctly both times.

---

### 5.7 /cabinet Command

**Step:** Type `/cabinet engineering`.

**Expected:** A confirmation: `Agent cabinet set to engineering`.

**Step:** Send a technical query.

**Expected:** Engineering personas respond.

**Pass criteria:** Cabinet applied.

---

### 5.8 /clear Command

**Step:** Exchange a few messages. Then type `/clear`.

**Expected:** A message that conversation history was cleared. The token counter resets to 0.

**Pass criteria:** Subsequent queries have no memory of prior context.

---

### 5.9 /save Command

**Step:** Exchange 2–3 messages. Then type `/save /tmp/hive-test-convo.json`.

**Expected:** A confirmation: `Conversation saved to /tmp/hive-test-convo.json`.

**Verification:**

```bash
cat /tmp/hive-test-convo.json | python -m json.tool
```

**Expected:** Valid JSON containing `messages`, `convene_mode`, `cabinet`, and timestamp fields.

**Pass criteria:** File exists and is valid JSON.

---

### 5.10 Multi-Turn Conversation Memory

**Step 1:** Type `My name is TestUser.`

**Step 2:** Type `What is my name?`

**Expected:** The response references "TestUser" — demonstrating conversation context retention.

**Pass criteria:** The second response references the first message's content.

---

### 5.11 REPL Exit

**Step:** Press `Ctrl+D` or type `exit`.

**Expected:** The REPL exits cleanly. A goodbye message may appear. Exit code 0.

**Pass criteria:** No traceback on exit.

---

## 6. API Server Tests

Start the server for all tests in this section:

```bash
council serve --host 127.0.0.1 --port 8000
```

**Expected on start:** `Hive API Server starting on http://127.0.0.1:8000` and `API docs: http://127.0.0.1:8000/docs`.

### 6.1 Health Check

**Test:**

```bash
curl -s http://127.0.0.1:8000/v1/health | python -m json.tool
```

**Expected:**

```json
{
  "status": "ok",
  "version": "<semver>",
  ...
}
```

**Pass criteria:** HTTP 200. `status` is `"ok"`.

---

### 6.2 Single Query Endpoint

**Test:**

```bash
curl -s -X POST http://127.0.0.1:8000/v1/query \
  -H "Content-Type: application/json" \
  -d '{"prompt": "Say hello", "provider": "mock"}' \
  | python -m json.tool
```

**Expected:**

```json
{
  "content": "...",
  "provider": "mock",
  "model": "mock/default",
  "usage": {"input_tokens": ..., "output_tokens": ..., "total_tokens": ...},
  "cost_usd": "0.0000",
  "latency_ms": ...
}
```

**Pass criteria:** HTTP 200. `content` is non-empty.

---

### 6.3 Streaming Query Endpoint

**Test:**

```bash
curl -s -X POST http://127.0.0.1:8000/v1/query \
  -H "Content-Type: application/json" \
  -d '{"prompt": "Count to three", "provider": "mock", "stream": true}'
```

**Expected:** Server-sent events (SSE) stream, each line prefixed with `data: `. A final `data: [DONE]` line.

**Pass criteria:** HTTP 200. Multiple `data:` lines before `[DONE]`.

---

### 6.4 Council Query Endpoint

**Test:**

```bash
curl -s -X POST http://127.0.0.1:8000/v1/council \
  -H "Content-Type: application/json" \
  -d '{"prompt": "What is recursion?", "providers": ["mock"]}' \
  | python -m json.tool
```

**Expected:**

```json
{
  "synthesis": {"content": "...", ...},
  "member_responses": {"mock": {...}},
  "total_cost_usd": "0.0000",
  ...
}
```

**Pass criteria:** HTTP 200. `synthesis.content` non-empty. `member_responses` has at least one key.

---

### 6.5 Compare Endpoint

**Test:**

```bash
curl -s -X POST http://127.0.0.1:8000/v1/compare \
  -H "Content-Type: application/json" \
  -d '{"prompt": "What is Python?", "providers": ["mock"]}' \
  | python -m json.tool
```

**Expected:** JSON object with a `responses` map keyed by provider name.

**Pass criteria:** HTTP 200. At least one response in `responses`.

---

### 6.6 List Advisors Endpoint

**Test:**

```bash
curl -s http://127.0.0.1:8000/v1/advisors | python -m json.tool
```

**Expected:** A JSON array of advisor objects with `name`, `enabled`, `healthy` fields.

**Pass criteria:** HTTP 200. `mock` advisor present.

---

### 6.7 Advisor Health Endpoint

**Test:**

```bash
curl -s http://127.0.0.1:8000/v1/advisors/mock/health | python -m json.tool
```

**Expected:**

```json
{
  "provider": "mock",
  "healthy": true,
  "latency_ms": ...,
  "error": null
}
```

**Pass criteria:** HTTP 200. `healthy` is `true`.

---

### 6.8 List Models Endpoint

**Test:**

```bash
curl -s http://127.0.0.1:8000/v1/models | python -m json.tool
curl -s "http://127.0.0.1:8000/v1/models?provider=mock" | python -m json.tool
```

**Expected:** First call returns all models. Second call returns only mock models (`mock/default`, `mock/fast`, `mock/slow`).

**Pass criteria:** HTTP 200 for both. Mock models present.

---

### 6.9 Budget Status Endpoint

**Test:**

```bash
curl -s http://127.0.0.1:8000/v1/budget/status | python -m json.tool
```

**Expected:** JSON with `total_spent_usd`, `daily_limit_usd`, `monthly_limit_usd`, per-provider breakdown.

**Pass criteria:** HTTP 200. Numeric values present.

---

### 6.10 Cache Stats Endpoint

**Test:**

```bash
curl -s http://127.0.0.1:8000/v1/cache/stats | python -m json.tool
```

**Expected:** JSON with `entries`, `max_size`, `hit_rate` (or similar fields).

**Pass criteria:** HTTP 200.

---

### 6.11 Clear Cache Endpoint

**Test:**

```bash
curl -s -X DELETE http://127.0.0.1:8000/v1/cache | python -m json.tool
```

**Expected:** JSON confirmation that cache was cleared.

**Test — clear specific provider's cache:**

```bash
curl -s -X DELETE "http://127.0.0.1:8000/v1/cache?provider=mock" | python -m json.tool
```

**Pass criteria:** HTTP 200 for both.

---

### 6.12 Agent Presets Endpoint

**Test:**

```bash
curl -s http://127.0.0.1:8000/v1/agents/presets | python -m json.tool
```

**Expected:** JSON with all 7 presets and their role lists.

**Pass criteria:** HTTP 200. `executive`, `engineering`, `security_review`, `code_review`, `product`, `data`, `full_board` all present.

---

### 6.13 Agents Analyze Endpoint

**Test:**

```bash
curl -s -X POST http://127.0.0.1:8000/v1/agents/analyze \
  -H "Content-Type: application/json" \
  -d '{"prompt": "Review our OAuth2 implementation for security issues"}' \
  | python -m json.tool
```

**Expected:** JSON describing which agents would be activated (security agent should appear for this prompt).

**Pass criteria:** HTTP 200. Agent list non-empty.

---

### 6.14 OpenAPI Documentation

**Test:** Open `http://127.0.0.1:8000/docs` in a browser.

**Expected:** Swagger UI showing all endpoints grouped by tag. Every endpoint listed in this section should appear.

**Pass criteria:** Page loads without error. All endpoints visible.

---

## 7. Web UI Tests

Start the full stack: `docker compose up -d` (or run Next.js dev server: `cd web && npm run dev`).

Navigate to `http://localhost:3000`.

### 7.1 Home / Query Page (`/`)

**Steps:**
1. Open `http://localhost:3000`.
2. Verify the page loads without a blank screen or JS error in the console.
3. Enter a query in the input box: `What is the difference between async and sync?`
4. Click Send (or press Enter).
5. Wait for the response.

**Expected:** Response text appears in the chat area. Provider and model labels visible below the response.

**Pass criteria:** Page loads. Response appears within 30 seconds.

---

### 7.2 Convene Page (`/convene`)

**Steps:**
1. Navigate to `http://localhost:3000/convene`.
2. Enter a prompt: `Explain the pros and cons of NoSQL databases.`
3. Select at least one provider (use mock if no API keys).
4. Click Submit.

**Expected:** Multiple response cards appear (one per provider/agent). A synthesis card appears at the bottom.

**Pass criteria:** Page renders. At least one member card and a synthesis card visible.

---

### 7.3 Advisors Page (`/providers`)

**Steps:**
1. Navigate to `http://localhost:3000/advisors`.
2. Verify a list of all configured providers appears.
3. Check that enabled/disabled status matches the config.

**Expected:** Provider list rendered. Health status indicators visible (green/yellow/red).

**Pass criteria:** Page renders. At least `mock` provider visible.

---

### 7.4 Settings Page (`/settings`)

**Steps:**
1. Navigate to `http://localhost:3000/settings`.
2. Verify current config values are displayed.
3. Attempt to change a setting (e.g. default provider).
4. Save the change.

**Expected:** Settings form renders. Change is saved and reflected on reload.

**Pass criteria:** Page renders. Form fields populated. Save succeeds.

---

### 7.5 Setup Page (`/setup`)

**Steps:**
1. Navigate to `http://localhost:3000/setup`.
2. Verify the setup wizard renders.
3. Walk through each step without submitting real keys.

**Expected:** Multi-step setup form visible. Each step navigable.

**Pass criteria:** Page renders without JS errors.

---

### 7.6 404 Handling

**Steps:**
1. Navigate to `http://localhost:3000/nonexistent-page`.

**Expected:** A proper 404 page — not a blank screen or uncaught error.

**Pass criteria:** 404 page renders.

---

## 8. Docker Tests

### 8.1 Full Stack Startup

**Test:**

```bash
docker compose up -d
docker compose ps
```

**Expected:** All services show status `Up` or `healthy`. Services: `hive-api`, `hive-web`, `ollama`, `ollama-init`.

**Pass criteria:** All containers running. No containers in `Exit` state.

---

### 8.2 Service Health

**Test:**

```bash
curl -sf http://localhost:8000/v1/health
curl -sf http://localhost:3000
curl -sf http://localhost:11434/api/tags
```

**Expected:** All three return HTTP 200.

**Pass criteria:** All three succeed.

---

### 8.3 Ollama Model Auto-Pull

**Steps:**
1. Run `docker compose up -d` on a fresh system.
2. Wait 60 seconds for `ollama-init` to complete.
3. Run: `docker compose exec ollama ollama list`

**Expected:** `nemotron-mini` appears in the model list (pulled by `ollama-init`).

**Pass criteria:** At least one model present.

---

### 8.4 GPU Passthrough (NVIDIA systems only)

**Prerequisites:** NVIDIA driver installed, Container Toolkit configured.

**Test:**

```bash
docker run --rm --gpus all nvidia/cuda:12.0-base-ubuntu22.04 nvidia-smi
```

**Expected:** `nvidia-smi` output showing GPU name and VRAM inside the container.

**Test — Ollama using GPU:**

```bash
docker compose exec ollama nvidia-smi
```

**Expected:** Same GPU info visible inside the Ollama container.

**Pass criteria:** GPU visible inside container. `ollama-setup.sh` logs show GPU-appropriate model tier was selected.

---

### 8.5 Ollama Setup Script — GPU Auto-Detection

**Test (CPU/no GPU):**

```bash
./scripts/ollama-setup.sh
```

**Expected when no GPU:** Log line `No NVIDIA GPU detected — pulling CPU-compatible models only.` Only `nemotron-mini` and `llama3.2:1b` are pulled.

**Test (with GPU):**

**Expected:** Log line `Detected: <GPU name> (<N> GB VRAM) — pulling recommended models...` followed by the VRAM-tier appropriate model list.

**Test — force all models:**

```bash
./scripts/ollama-setup.sh --all
```

**Expected:** Log line `Manual override: --all flag set — pulling every model.` All 6 models are pulled.

**Pass criteria:** Correct tier message appears. Only appropriate models pulled in auto mode.

---

### 8.6 Stack Teardown

**Test:**

```bash
docker compose down
docker compose ps
```

**Expected:** No containers running after `down`. `ps` shows empty output.

**Pass criteria:** Clean shutdown with exit code 0.

---

### 8.7 Volume Persistence

**Steps:**
1. Pull a model: `docker compose exec ollama ollama pull phi3:mini`
2. Run `docker compose down` (without `-v`).
3. Run `docker compose up -d`.
4. Check: `docker compose exec ollama ollama list`

**Expected:** `phi3:mini` still present. Model data persisted in the `ollama-models` volume.

**Pass criteria:** Model survives restart.

---

## 9. Security Tests

### 9.1 API Key Masking in CLI Output

**Test:**

```bash
OPENAI_API_KEY="sk-testkey12345678" council provider list
```

**Expected:** The table shows the provider name and status but does NOT print the API key. The key must not appear anywhere in stdout or stderr.

**Verification:** `council provider list 2>&1 | grep "sk-testkey"` should return no output.

**Pass criteria:** API key not printed anywhere.

---

### 9.2 API Key Masking in Doctor Output

**Test:**

```bash
OPENAI_API_KEY="sk-testkey12345678" council doctor 2>&1 | grep "sk-testkey"
```

**Expected:** No output — the key must not appear in the doctor report.

**Pass criteria:** grep returns no matches.

---

### 9.3 API Authentication — Server Mode

**Steps:**
1. Start the server with an API key set: `HIVE_API_KEY=mysecrettoken council serve`
2. Test unauthenticated request:

```bash
curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8000/v1/query \
  -H "Content-Type: application/json" \
  -d '{"prompt": "hello", "provider": "mock"}'
```

**Expected:** HTTP 401 Unauthorized.

3. Test authenticated request:

```bash
curl -s -X POST http://127.0.0.1:8000/v1/query \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer mysecrettoken" \
  -d '{"prompt": "hello", "provider": "mock"}' \
  | python -m json.tool
```

**Expected:** HTTP 200 with a valid response.

**Pass criteria:** 401 without token. 200 with correct token.

---

### 9.4 Open Mode (No API Key Set)

**Test:** Start the server without `HIVE_API_KEY`::

```bash
council serve &
curl -s -X POST http://127.0.0.1:8000/v1/query \
  -H "Content-Type: application/json" \
  -d '{"prompt": "hello", "provider": "mock"}'
```

**Expected:** HTTP 200 — server operates in open/local mode when no key is configured.

**Pass criteria:** Request succeeds without authentication header.

---

### 9.5 Privacy Mode — No Data Stored

**Steps:**
1. Run a query in privacy mode: `council query "secret info" --provider mock --privacy`
2. Check the database for stored conversations.

```bash
python -c "
import asyncio
from council.storage import repository as repo
async def check():
    await repo.init_db()
    convos = await repo.list_conversations(limit=5)
    print('conversations:', len(convos))
asyncio.run(check())
"
```

**Expected:** The privacy-mode query does NOT appear in the stored conversations.

**Pass criteria:** No new conversation entry created for the privacy-mode query.

---

### 9.6 Privacy Mode — No Cache Entry

**Steps:**
1. Run `council query "uniqueprivacytestXYZ" --provider mock --privacy`
2. Run the same query without `--privacy`: `council query "uniqueprivacytestXYZ" --provider mock`
3. Check if the second query shows `(cached)` in metadata.

**Expected:** Second query does NOT show `(cached)` — the privacy-mode query was not cached, so there is nothing to hit.

**Pass criteria:** No cache hit for the second query.

---

## 10. Performance Tests

### 10.1 Baseline Latency — Mock Provider

**Test:**

```bash
time council query "Hello" --provider mock --output raw
```

**Expected:** Total wall time under 500ms. The mock provider has near-zero LLM latency.

**Pass criteria:** Wall time < 500ms. Metadata latency < 50ms.

---

### 10.2 Response Caching

**Steps:**
1. Run a query: `council query "What is Python?" --provider mock`
2. Note the latency from metadata.
3. Run the identical query again.
4. Note the latency and check for `(cached)` in metadata.

**Expected:** Second query is faster. Metadata shows `(cached)`.

**Pass criteria:** `(cached)` appears on second run. Latency is lower.

---

### 10.3 Cache Invalidation

**Steps:**
1. Warm the cache: `council query "Cached test query" --provider mock`
2. Verify it's cached: re-run the same query — should show `(cached)`.
3. Clear the cache: `curl -s -X DELETE http://127.0.0.1:8000/v1/cache` (requires server running) or restart the process.
4. Re-run the query.

**Expected:** After cache clear, the query does NOT show `(cached)` and takes normal latency.

**Pass criteria:** Cache clear causes cache miss on next request.

---

### 10.4 Concurrent API Requests

**Test (requires server running):**

```bash
for i in $(seq 1 10); do
  curl -s -X POST http://127.0.0.1:8000/v1/query \
    -H "Content-Type: application/json" \
    -d "{\"prompt\": \"Request $i\", \"provider\": \"mock\"}" &
done
wait
```

**Expected:** All 10 requests complete. No 500 errors. Server does not crash.

**Pass criteria:** All 10 responses are HTTP 200. No errors in server logs.

---

### 10.5 Convene Latency with Multiple Advisors

**Test (requires 2+ provider keys):**

```bash
time council convene "Brief question" --providers openai,anthropic
```

**Expected:** Hive queries advisors concurrently — total latency should be close to the slowest individual provider, not the sum.

**Pass criteria:** Total latency is within 2x of the slowest single-provider query (not 2x the sum of both).

---

## 11. Edge Cases

### 11.1 Empty Prompt

**Test:**

```bash
council query "" --provider mock
```

**Expected:** Either a validation error with a clear message, or the provider handles the empty prompt gracefully (returns a response or a warning).

**Pass criteria:** No Python traceback. Exit code may be non-zero but the error is human-readable.

---

### 11.2 Very Long Prompt

**Test:**

```bash
python -c "print('word ' * 5000)" | council query --provider mock
```

**Expected:** Either processes successfully or returns a clear token-limit error message. No unhandled exception.

**Pass criteria:** No Python traceback.

---

### 11.3 Rate Limit Handling

**Test:** Configure a low mock rate limit (or trigger rate limiting on a real provider by sending many rapid requests). Hive should catch the rate limit error, wait, and retry or fall back.

**Expected:** A WARN log mentioning rate limit. Fallback to another provider or a graceful retry message — not a raw API error traceback.

**Pass criteria:** User-readable error or successful fallback. No raw API exception printed to the user.

---

### 11.4 Budget Enforcement

**Steps:**
1. Set a very low budget in `~/.hive/config.yaml`:

```yaml
budget:
  daily_limit_usd: 0.001
  per_query_limit_usd: 0.0001
```

2. Run a council query against a paid provider.

**Expected:** A budget exceeded error is raised with a clear message before any API call is made (or after the first call if the check is post-call).

**Pass criteria:** Clear budget error message. No silent overspend.

---

### 11.5 Fallback Chain

**Test:** Set the primary provider to a non-existent/disabled provider and configure a fallback:

```yaml
defaults:
  provider: nonexistent_provider
  fallback: [mock]
```

Then run: `council query "Hello"`

**Expected:** Primary provider fails silently. Fallback to `mock` succeeds. Metadata shows `(fallback from nonexistent_provider)`.

**Pass criteria:** Query succeeds. Fallback label visible in metadata.

---

### 11.6 Circuit Breaker

**Steps:**
1. Configure a provider with an invalid API key to force repeated failures.
2. Send 5+ rapid requests to that provider.

**Expected:** After several failures the circuit breaker opens. Subsequent requests immediately return a circuit-breaker error (no waiting for timeout). Error message indicates the circuit is open and a cooldown period.

**Pass criteria:** Requests after circuit opens fail fast (< 100ms) with a circuit-breaker message, not a full API timeout.

---

### 11.7 Provider Unavailable During Doctor

**Test:** Stop Ollama: `docker compose stop ollama`. Then run `council doctor`.

**Expected:** Ollama row shows WARN (not FAIL — Ollama is optional). Doctor completes and prints a suggested fix.

**Pass criteria:** Doctor still exits 0. Ollama row is WARN, not a crash.

---

### 11.8 Invalid Config File

**Steps:**
1. Edit `~/.hive/config.yaml` and introduce a YAML syntax error (e.g. bad indentation).
2. Run `council query "Hello" --provider mock`.

**Expected:** A clear error message about the invalid YAML. Not a Python `AttributeError` or `KeyError`.

**Pass criteria:** User-readable error. Exit code 1.

---

### 11.9 Missing Config File

**Steps:**
1. Remove or rename `~/.hive/config.yaml`.
2. Run `council query "Hello" --provider mock`.

**Expected:** Either creates a default config automatically, or prints a helpful message: `Config not found. Run council config init`.

**Pass criteria:** No unhandled exception. User knows what to do next.

---

### 11.10 Network Timeout

**Test:** Configure a provider with a very short timeout (< 100ms) or point it at an unreachable host.

**Expected:** A timeout error is raised and reported cleanly. Not a generic `ConnectionError` traceback.

**Pass criteria:** Timeout message visible. Fallback chain triggered if configured.

---

## 12. Mock Provider Tests

These tests allow full verification of the Hive pipeline without any API keys. All tests in this section require only: `pip install -e .`

### 12.1 Mock Provider Discovery

**Test:**

```bash
council provider list
```

**Expected:** `mock` appears in the provider list with status `enabled`.

**Pass criteria:** Mock provider visible.

---

### 12.2 Mock Provider Models

**Test:**

```bash
council model list --provider mock
```

**Expected:** Three models listed: `mock/default`, `mock/fast`, `mock/slow`.

**Pass criteria:** All three present.

---

### 12.3 Mock Default Model Behavior

**Test:**

```bash
council query "Tell me about machine learning" --provider mock --model mock/default
```

**Expected:** A simulated response containing the topic ("machine learning") in the reply. Response is deterministic or near-deterministic.

**Pass criteria:** Response contains the prompt topic. Exit code 0.

---

### 12.4 Mock Fast Model

**Test:**

```bash
time council query "Quick question" --provider mock --model mock/fast
```

**Expected:** The `mock/fast` model returns in under 100ms. (It is designed to simulate a fast/cheap tier.)

**Pass criteria:** Latency metadata < 100ms (approximate).

---

### 12.5 Mock Slow Model

**Test:**

```bash
time council query "Slow question" --provider mock --model mock/slow
```

**Expected:** The `mock/slow` model adds artificial delay, simulating a slow/heavy tier.

**Pass criteria:** Latency noticeably higher than `mock/fast`.

---

### 12.6 Full Convene Pipeline with Mock Only

**Test:**

```bash
council convene "Explain the SOLID principles" --providers mock
```

**Expected:** Hive pipeline runs fully — member query, response collection, and synthesis all happen using the mock provider. No API key required.

**Pass criteria:** Synthesis section visible. Exit code 0.

---

### 12.7 Mock + Auto-Agents

**Test:**

```bash
council convene "How do we improve our CI/CD pipeline?" --auto-agents --providers mock
```

**Expected:** Agents are auto-generated from the prompt. Each agent runs against the mock provider. Synthesis is produced. The whole pipeline completes without API keys.

**Pass criteria:** Multiple agent responses visible. Synthesis present. Exit code 0.

---

### 12.8 API Server with Mock Only

**Test:**

```bash
council serve &
sleep 2

# Query
curl -s -X POST http://127.0.0.1:8000/v1/query \
  -H "Content-Type: application/json" \
  -d '{"prompt": "Mock API test", "provider": "mock"}' | python -m json.tool

# Convene
curl -s -X POST http://127.0.0.1:8000/v1/council \
  -H "Content-Type: application/json" \
  -d '{"prompt": "Mock convene test", "providers": ["mock"]}' | python -m json.tool
```

**Expected:** Both return HTTP 200 with valid JSON responses.

**Pass criteria:** Both calls succeed. No API keys required.

---

### 12.9 Mock Provider Health Check

**Test:**

```bash
council provider test mock
```

**Expected:** PASS with near-zero latency (single-digit ms).

**Pass criteria:** PASS. Latency < 50ms.

---

### 12.10 Cache Behavior with Mock

**Steps:**
1. Send identical query twice: `council query "Cache test 123" --provider mock`
2. Check second response for `(cached)` in metadata.

**Expected:** Second response is marked `(cached)`. Content is identical to first response.

**Pass criteria:** `(cached)` appears on second run.

---

### 12.11 Mock in REPL

**Test:**

```bash
council repl --provider mock
```

Enter several queries. Toggle convene mode with `/convene`. Use `/cabinet engineering`. Run a query in cabinet mode.

**Expected:** All REPL features work end-to-end using only the mock provider.

**Pass criteria:** No API key errors. All REPL features function as documented in Section 5.

---

### 12.12 Doctor with Mock as Only Provider

**Steps:**
1. Disable all providers except `mock` in `~/.hive/config.yaml`.
2. Run `council doctor`.

**Expected:** All checks pass or warn. Mock provider shows PASS for both API key (none required) and health check.

**Pass criteria:** Exit code 0. Mock provider rows show PASS.

---

*End of Testing Guide*
