# Architecture

How a prompt becomes an answer, where state lives, and which module owns what.

## Overview

```
nvh CLI · Web UI · Python SDK · MCP clients · OpenAI/Anthropic clients
  │
  ├── API server (FastAPI, :8000) ── REST · WebSocket · /v1/proxy · /v1/anthropic
  │     └── AI Wizard ── tool loop over workspace state, RAG, web search, MCP tools
  │
  ├── Engine
  │     ├── Router ── task classifier · advisor scorer (capability, cost, latency, health)
  │     │            · learned quality · budget gate · response cache · fallback chain
  │     ├── Orchestrator ── local model classifies, rewrites, evaluates, synthesises
  │     ├── Council ── parallel dispatch to N advisors → weighted synthesis (streaming)
  │     ├── Agents ── persona pool, cabinet presets, auto-generated panels
  │     └── Agent loop ── plan → tool calls → verify, with guardrails and a sandbox
  │
  ├── Providers
  │     ├── OpenAICompatibleProvider ── one adapter, one ProviderSpec row per cloud provider (LiteLLM)
  │     ├── OllamaProvider ── local GPU, model discovery, VRAM fit
  │     └── TritonProvider ── on-prem TensorRT-LLM
  │
  └── Workspace (NVH_HOME) ── config · SQLite state · models · vault · RAG · jobs · receipts · logs
```

## Request flow

1. **Entry.** The CLI (`nvh/cli/main.py`), the API (`nvh/api/server.py`), the
   SDK (`nvh/sdk.py`) and the MCP server (`nvh/mcp_server.py`) all build one
   `Engine` (`nvh/core/engine.py`) from `config.yaml` and the provider
   registry.
2. **Context.** `HIVE.md` and `.nvh/context/*.md` files are injected into the
   system prompt (`nvh/core/context_files.py`).
3. **Routing.** `Router` (`nvh/core/router.py`) classifies the task, scores
   every enabled advisor, applies routing rules, the budget and the cache, and
   picks provider + model. With a local model present the `Orchestrator`
   (`nvh/core/orchestrator.py`) refines that choice and can rewrite the prompt.
4. **Dispatch.** The chosen `Provider` (`nvh/providers/`) calls LiteLLM; on
   failure the fallback chain tries the next advisor. Council mode
   (`nvh/core/council.py`) fans out to several advisors and synthesises;
   throwdown runs it twice with a critique pass.
5. **Accounting.** Tokens, cost (`litellm.cost_per_token`), latency and the
   routing decision are recorded in SQLite (`nvh/storage/`), the learning loop
   (`nvh/core/learning.py`) updates per-provider quality, and webhooks fire.
6. **Presentation.** The CLI prints metadata; the API streams over SSE or
   WebSocket; the WebUI persists the conversation server-side.

## Key modules

| Area | Module | Role |
|---|---|---|
| Engine | `nvh/core/engine.py` | orchestration entry point; owns router, cache, budget, history |
| Routing | `nvh/core/router.py`, `nvh/core/learning.py`, `nvh/core/free_tier.py` | scoring, learned quality, free-tier preference order |
| Local intelligence | `nvh/core/orchestrator.py`, `nvh/core/smart_query.py` | VRAM-tiered local orchestration |
| Council | `nvh/core/council.py`, `nvh/core/agents.py` | streaming council pipeline; persona pool and `COUNCIL_PRESETS` |
| Agentic coding | `nvh/core/agent_loop.py`, `agentic.py`, `agent_review.py`, `agent_testgen.py`, `agent_guardrails.py` | `nvh do`, `nvh agent run`, `nvh review`, `nvh test-gen` |
| Tools | `nvh/core/tools.py`, `system_tools.py`, `browser_tools.py`, `vision_tools.py` | `ToolRegistry`; three safety classes (`auto`, `confirm`, `privileged`), HMAC approval tokens for privileged calls, the `NVH_ALLOW_PRIVILEGED` kill switch |
| Sandbox | `nvh/sandbox/executor.py` | Docker isolation with an audited subprocess fallback |
| Compute admission | `nvh/core/atohi.py` | `AdmissionBroker` protocol, `ResourcePaused`; a shared-compute admission boundary, disabled by default; no broker ships (the `atohi:` stanza, [CONFIGURATION.md](CONFIGURATION.md#atohi-admission-disabled)) |
| Providers | `nvh/providers/specs.py`, `openai_compatible.py`, `registry.py`, `ollama_provider.py`, `triton_provider.py` | the `ProviderSpec` table, the one cloud adapter, lazy registration |
| Config | `nvh/config/settings.py`, `nvh/config/capabilities.yaml`, `nvh/config/model_overrides.json` | Pydantic schema, env interpolation, the hand-maintained half of the model catalog (LiteLLM supplies price, context and flags; see [CONFIGURATION.md](CONFIGURATION.md#model-catalog)) |
| Storage | `nvh/storage/` | SQLAlchemy + aiosqlite: conversations, routing outcomes, costs |
| API | `nvh/api/server.py`, `nvh/api/deps.py`, `nvh/api/proxy.py`, `nvh/api/services/query_service.py`, `nvh/api/routes/` (`outputs.py`, `vault_notes.py`, `schedules.py`, `voice.py` — one module per surface, each behind `require_auth`) | FastAPI app, compatible proxies, shared query service, the 0.46 routers |
| Wizard | `nvh/integrations/wizard/` (`chat.py`, `tools.py`, `profiles.py`, `concierge.py`, `system_settings.py`, `sandbox_tools.py`, `vision_bridge.py`) | chat loop, tools, profiles, the concierge that picks a hidden specialist per turn, privileged system-settings tools, the sandbox and vision bridges, troubleshooter, reconnect, mission builder |
| Workspace | `nvh/integrations/workspace/` | `NVH_HOME` layout, mount autopilot, snapshot, vault, passport |
| RAG | `nvh/integrations/rag/` | SQLite store, Ollama embeddings, chunker, vault bridge |
| Installs | `nvh/integrations/installs/` | studio packs, ComfyUI, Node runtime, workstation, OpenClaw |
| Spark playbooks | `nvh/integrations/installs/playbooks.py` | NVIDIA's DGX Spark playbooks as plans: plan, approve (red card or the CLI prompt), install with a receipt and a vault audit note; extracted from the upstream READMEs, not yet exercised by maintainers on hardware |
| Services | `nvh/integrations/services/`, `nvh/cli/services.py` | background jobs, the schedule runner (0.46), receipts, service registry, the bring-up pipeline |
| Diagnostics | `nvh/integrations/diagnostics/` | boot preflight, smoke tests, model fit, production readiness, redacted reports |
| MCP | `nvh/mcp_server.py`, `nvh/integrations/mcp_client.py` | server for coding tools; client for external tool servers |
| CLI | `nvh/cli/main.py`, `repl.py`, `setup.py`, `completions.py` | Typer app, REPL, free-tier wizard |
| WebUI | `web/` | Next.js + Tailwind dashboard |

## Persistence

Everything lives under `NVH_HOME` (layout in
[CONFIGURATION.md](CONFIGURATION.md#nvh_home-layout)): config and `.env`
under `config/`, the SQLite database at `state/nvhive.db`, Ollama models under
`models/ollama`, the Markdown vault and RAG index, install receipts and job
logs. `nvh snapshot save` bundles the vault, RAG index, receipts and the
conversations database so a workspace moves to a new VM; config is excluded
on purpose because it can hold raw keys.

## Service pipeline

Three local services start in a fixed order with health gates — Ollama
(`:11434`), the API (`:8000`), the WebUI (`:3000`) — followed by an
end-to-end Wizard smoke test; the browser opens only when every gate is
green. The contract, signals and env knobs are in
[MAINTAINERS.md](MAINTAINERS.md#service-order).

## Diagram

```mermaid
graph TB
    subgraph Clients
        CLI[nvh CLI]
        WEB[Web UI :3000]
        SDK[Python SDK / OpenAI clients]
        CC[Claude Code / Cursor / OpenClaw]
        NC[NemoClaw agent]
    end

    subgraph nvHive
        API[API server :8000]
        PROXY[/v1/proxy · /v1/chat/completions · /v1/anthropic]
        MCP[MCP server]
        WIZ[AI Wizard]
        ENGINE[Engine: router · orchestrator · council · agent loop]
    end

    subgraph Providers
        LOCAL[Ollama on your GPU]
        CLOUD[OpenAICompatibleProvider via LiteLLM]
        TRITON[Triton / TensorRT-LLM]
    end

    CLI --> ENGINE
    WEB -->|REST + WebSocket| API
    SDK -->|OpenAI-compatible| PROXY
    NC -->|OpenShell gateway| PROXY
    CC -->|MCP stdio| MCP
    API --> WIZ
    API --> ENGINE
    PROXY --> ENGINE
    MCP --> ENGINE
    ENGINE --> LOCAL
    ENGINE --> CLOUD
    ENGINE --> TRITON

    style LOCAL fill:#76B900,color:#000
```

## Removed in 0.42

The "subtract" release deleted a second, parallel product that nothing in
`nvh/` imported: the Docker/compose family and stale installers; the
`~/.hive`-era core modules whose `NVH_HOME` successors already existed
(knowledge → RAG, memory → vault, the `~/.hive` scheduler →
`nvh/core/scheduler.py`, smoke_test →
diagnostics, templates → agent profiles, docker_sandbox →
`SandboxExecutor.run_shell`); the zero-importer orchestration modules and
`/v1/locks`; a remote-desktop input-injection toolkit (`tools/`, moved to a
separate private repository; history remains in this repo before the 0.42
removal) and the Bun channel plugin (`channel-plugin/`); and the `/query` and
`/council` web pages.
The schedules store itself stayed: `nvh/core/scheduler.py` keeps
`state/schedules.json` (`nvh-schedules/2`), and since 0.46 each run is a
`schedule-run` job under `jobs/`, written by
`nvh/integrations/services/schedule_runner.py`, which the API lifespan runs
in-process behind the heartbeat lock `state/scheduler.lock` (`NVH_SCHEDULER=0`
turns that off; `nvh schedule start` is the same runner for a box without the
API).
One-release hidden aliases cover the renamed CLI spellings
([COMMANDS.md](COMMANDS.md#deprecated-spellings)). The full audit is
[proposals/SIMPLIFICATION_PLAN_2026-09.md](proposals/SIMPLIFICATION_PLAN_2026-09.md).

Back to [README](../README.md)
