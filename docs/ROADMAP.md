# Roadmap

What nvHive is for, what is planned, and what is deliberately out of scope.
The evidence and the multi-release plan behind this page are in
[proposals/SIMPLIFICATION_PLAN_2026-09.md](proposals/SIMPLIFICATION_PLAN_2026-09.md);
this page is the short version. Features are listed once each.

## Where nvHive already wins

- One-curl rootless install on a rented GPU desktop with VRAM-tiered model
  selection; the whole lab is provisioned, not just a chat window.
- Smart multi-provider routing with learned quality: routing outcomes and
  quality benchmarks drive cost- and quality-aware model choice instead of
  making the user pick a model every time.
- Council mode: multi-persona, multi-model deliberation with a synthesised
  verdict, not a side-by-side comparison.
- Reconnect survival built for ephemeral rigs: `NVH_HOME` on the persistent
  volume, server-side chat history, pinned conversations, `nvh snapshot`.
- A Wizard that operates the workspace — refreshes models, repairs the
  install, validates and saves provider keys — rather than only chatting.
- Studio packs: one-command ComfyUI, game-dev and music environments on the
  same rig, all rootless.
- nvHive is itself an MCP server, so Claude Code and Cursor can route through
  its router and council; it is also an MCP client for the Wizard.
- Agentic coding with a sandboxed `run_code`/`shell`, guardrails and
  cross-session agent memory; per-message cost, tokens and latency with
  per-profile cost ceilings.
- Everything works as an unprivileged user: cron-free scheduler,
  zero-dependency SQLite RAG, env-file secrets with an opt-in keyring.

## Plan by release

| Release | Theme | Contents |
|---|---|---|
| 0.41.1 | hotfix | retired default models on most cloud providers; `$0` cost accounting; unreachable `nvh mcp` / `nvh agent`; `nvh snapshot` archiving the wrong tree; GitHub Models removed |
| 0.42 | subtract | dead orchestration modules, Docker/compose, stale installers and demo assets; `~/.hive`-era modules folded into their `NVH_HOME` successors; provider adapters collapsed into `ProviderSpec` rows; CLI query modes into `nvh ask` flags and diagnostics into `nvh status`; `/query` and `/council` pages removed; docs 33 → 12 with parity and link tests |
| 0.43 | refresh | one `ProviderSpec`-derived provider doc; price, context and capability flags derived from LiteLLM at load; a live `/v1/models` intersection and a model-currency CI test; a regenerated cloud catalog; `nemotron3:33b` local tiers; `num_ctx` passed to Ollama from the VRAM tier; Perplexity's 2026-09-27 Chat Completions sunset handled |
| 0.44 | add | Wizard `run_code`/`shell` with approval cards and `ask_user`; a `remember` tool and a Memory filter on `/vault`; `generate_image` (NVIDIA-hosted or local ComfyUI); one chat surface; standard-path OpenAI endpoints and `nvh launch <tool>`; durable schedules under `NVH_HOME` with `/v1/schedules`; session cost against the hourly desktop rate; push-to-talk STT and optional TTS in the WebUI |

## Features

Status: **shipped** · **planned** (in a release above) · **present-but-hidden**
(exists in the engine or CLI, not yet surfaced in the WebUI) · **open**
(wanted, unscheduled) · **declined** (see Non-goals).

| Feature | Status | Notes |
|---|---|---|
| Chat history sidebar — browse, resume, pin, search | shipped 0.41 | one server-side store, one-time localStorage import in 0.42 |
| In-app model browser with VRAM-fit preview and live progress | shipped 0.41 | the Model Manager ([MODELS.md](MODELS.md)) |
| MCP client — external tool servers in the Wizard | shipped 0.41 | `mcp-servers.json`, confirm-by-default, per-server auto-approve |
| Agent Library — bundled profiles grouped by category | shipped 0.41 | `nvh/catalog/agent-library.json`; copy into `$NVH_HOME/agent-profiles/` |
| Per-agent cost ceiling | shipped (non-streaming) | enforce on the streaming path once providers report per-chunk usage |
| Provider-key paste with auto-detect; GPU-fit recommender card; reconnect panel on every page | shipped | |
| Code execution from the Wizard, surfaced in chat | planned 0.44 | `run_code`/`shell` as confirm-class Wizard tools with approval cards |
| Persistent cross-chat memory in the WebUI | planned 0.44 | `remember` tool; the vault is already RAG-queryable |
| Image generation from chat | planned 0.44 | rename `generate_portrait` → `generate_image`; NVIDIA-hosted when `NVAPI_KEY` is set, else local ComfyUI |
| One chat surface (`/` and `/wizard` merged) | planned 0.44 | two surfaces confuse first-time users |
| Standard-path OpenAI endpoints; `nvh launch <tool>` for Claude Code / Codex / opencode / Cursor / Continue | planned 0.44 | `/v1/chat/completions` beside `/v1/proxy/...` |
| Scheduled prompts with a UI | planned 0.44 | durable jobs under `NVH_HOME`, `/v1/schedules`; today `nvh schedule` only |
| Session cost vs hourly desktop rate | planned 0.44 | the "am I wasting GPU money" pill |
| Voice in the WebUI (push-to-talk STT, optional TTS) | planned 0.44 | `nvh voice` exists on the CLI |
| Quantisation variant picker; context length / GPU offload / KV-cache controls | open | `num_ctx` from the VRAM tier lands in 0.43; the UI is unscheduled |
| Import an arbitrary GGUF or HF repo into Ollama | open | partial today via `nvh models pull <tag>` |
| Pack uninstall and disk reclamation; per-app start/stop/logs; update detection | open | receipts already carry uninstall and repair plans |
| Hybrid BM25 + vector RAG with reranking | open | SQLite store today; embedding-model swap by VRAM is the cheap first step |
| Vision input and OCR in chat | present-but-hidden | `analyze_image` / `read_text_from_image` tools; the Wizard picker exposes vision models |
| Chat folders and tags | open | pin only today |
| Artifacts — live HTML/React/Mermaid rendering | open | |
| Shareable conversation links and export/import | open | Markdown export exists |
| Avatar in chat bubbles; per-agent accent colour; provider status pill on the avatar | open | `AgentAvatar` exists, `MessageBlock` does not use it yet |
| Skill-style agent profiles (SKILL.md-compatible metadata, progressive disclosure of the profile index) | open | keep the YAML fields under `metadata:`; the full restructure and any installer are declined below |
| Per-profile model *tier* (local-VRAM / cheap-cloud / frontier) instead of a hardcoded model | open | maps onto the VRAM tier table |
| "View the effective system prompt" inspector for a profile | open | persona + tools + guardrails as the model sees them |
| Per-profile brevity control | open | an output-discipline knob beside temperature and `max_tokens` |
| `$NVH_HOME/skills/` shared with other agents on the rig | open | symlink into the harness-standard locations during install |
| VS Code extension on the Marketplace | open | ships in-repo, run from source |

### Known follow-ups

- **mcp SDK 2.x** — all `mcp` pins are `>=1.0,<2`: 2.x removed
  `mcp.server.fastmcp` and reshaped the client API. Migrate
  `nvh/mcp_server.py` and `nvh/integrations/mcp_client.py` together, then
  lift both pins.
- **Perplexity Chat Completions sunset (2026-09-27)** — the `perplexity` row in
  `nvh/providers/specs.py` carries `sunset_date`; before that date verify
  LiteLLM's Perplexity route on the pinned version or move web-grounded
  queries to `nvh/integrations/web_search/`.
- **Streaming cost ceiling** — see the per-agent row above.

## Non-goals

The operating envelope is fixed: single user, single VM, SQLite only,
API-key auth, noncommercial (PolyForm-NC). Requests outside it are declined,
not deferred.

- **Enterprise and multi-tenant** (SSO/OIDC/LDAP/SCIM, team channels,
  multi-user login/RBAC/admin panel, embeddable widget,
  Kubernetes/Helm/Postgres/S3/Redis/OpenTelemetry, pluggable vector DBs) —
  the license forbids the buyer, the product runs on one renter's VM, and
  the SQLite/rootless wins listed above are exactly what these would undo.
- **Alternative inference backends, native desktop apps, i18n** — nvHive
  is a router over Ollama + NIM; extra backends compete for the same VRAM
  budget, `install.ps1`/`install-mac.sh` stay best-effort for x86 Windows
  and macOS, and the LLM already answers in the user's language. One
  planned exception (owner decision 2026-09-02, #136): NVIDIA's
  unified-memory desktops — DGX Spark (Linux aarch64) becomes a supported
  target in 0.43, and RTX Spark (Windows on Arm) becomes one when the
  hardware ships this fall; both are the owned counterpart to the rented
  GPU desktop, not a new platform category.
- **Marketplace surfaces** (community app catalog, agent marketplace,
  preset hub, in-UI tool editor, vertical packs, patterns dir, the full
  SKILL.md restructure, a skill installer) — the scan/quarantine/SHA-pin/
  key-deny moderation such a surface requires is more than a solo
  maintainer can carry; `nvh agents export/import <url>` for one YAML
  profile is the ceiling.
- **Docker/compose as a supported deployment path** — README says "No
  Docker" twice and nothing in CI builds images. One installer plus
  `pip install nvhive`.
- **Hand-typed model facts** (pricing, context windows, capability flags,
  default-model tables, marketing counts, command docs) — every one drifted
  within five months; anything derivable from LiteLLM, provider `/models`,
  the Typer registry or the spec tables is generated and parity-tested.
- **New parallel implementations** (another memory store, agent dataclass,
  sandbox, tool protocol, chat store, or `~/.something` home) — every
  high-impact defect in the audit was a CLI/API/Wizard triple copy that had
  already drifted; new features plug into the single instance or are
  declined.

## Threats to watch

- Open WebUI shipping a rootless cloud-desktop installer with reconnect
  survival.
- Ollama bundling a polished GUI and first-party web search.

Back to [README](../README.md)
