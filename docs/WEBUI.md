# Web UI

The dashboard is a Next.js app served on `http://localhost:3000` that talks to
the API server on `:8000`. Launch it with:

```bash
nvh webui                  # installs Node deps on first run, starts the API if needed
nvh services start --open  # verified bring-up (Ollama → API → WebUI → smoke test)
```

First launch installs dependencies and builds the WebUI under `NVH_HOME`
(`$NVH_HOME/webui`); later launches run the production server. pip and binary
installs fetch the WebUI source with `git`, or a GitHub source archive when
`git` is absent — the installed release tag first, `main` only as a fallback
(`NVH_WEB_REF` overrides). If port 3000 is taken the API allows the fallback
ports (3001, 3002, 8080) automatically, on `localhost`, `127.0.0.1`,
`nvhive` and loopback IPv6.

## Pages

| Sidebar | Route | What it does |
|---|---|---|
| Chat | `/` | Prompts in single, council or compare mode with streaming. Council mode has an **Advanced** drawer for cabinet, member count, strategy, synthesis provider and per-provider weights |
| AI Wizard | `/wizard` | Tool-using assistant grounded in live workspace state: refreshes models, runs safe repairs, ingests dropped files (PDF included), searches the web, cites sources, shows cost and latency per reply. **Convene council** hands the draft to the chat page |
| Agents | `/agents` | The Agent Library — profiles grouped by category, each mappable to a local or cloud model; copy one into `$NVH_HOME/agent-profiles/` to customise |
| Models | `/models` | The Model Manager: installed models, fit-ranked catalog, one-click install with live progress ([MODELS.md](MODELS.md)) |
| Setup | `/setup` | Onboarding: storage check, GPU detection, local AI, free providers, ComfyUI and studio packs as resumable background jobs; **Advanced Details** exposes diagnostics and **Copy Error Report** |
| My Computer | `/system` | GPU, storage, runtime, cache and budget status with recommendations |
| Memory Vault | `/vault` | Markdown notes under `$NVH_HOME/vault`, queryable from the Wizard through RAG |
| AI Connections | `/providers` | Provider cards: add a key, **Test Connection**, health and model listings |
| Usage | `/analytics` | Spend, tokens and latency per provider and per agent |
| Preferences | `/settings` | Theme, response cache, browser data. Model defaults and budgets live in `config.yaml` (`nvh config`) |
| Developer Tools | `/integrations` | Detect and connect Claude Code, Cursor, OpenClaw, NemoClaw; attach external MCP servers and refresh their tools |

The `/query` and `/council` pages were removed in 0.42; their features are the
chat page's single and council modes.

## Chat history

Past conversations live in the sidebar on every page, grouped by date
(Pinned / Today / Yesterday / Previous 7 Days / Older) with search, inline
rename, and a right-click menu (Pin, Rename, Export as Markdown, Delete).

- **One store** — every chat surface persists to the API server (SQLite at
  `$NVH_HOME/state/nvhive.db`) through `/v1/conversations`; nothing is kept
  in the browser. A reload or a reconnect to the same workspace picks any
  thread back up.
- **Resume anywhere** — a conversation reopens on the surface that produced
  it: Wizard chats on `/wizard`, chat/council/compare threads on `/?c=<id>`.
- **Pin** (right-click, or `/pin` in Wizard chat) keeps a thread at the top
  and surfaces it after a reconnect.
- **Search** — the sidebar box queries `GET /v1/conversations/search?q=`
  (full message content with a snippet per hit) and matches titles instantly;
  `Ctrl+K` → "Search Conversations" focuses it.
- **Upgrading from 0.41** — chats older builds kept in localStorage are
  imported into the server store once, on the first page load after the
  upgrade, then the local copy is removed.

## Design

- NVIDIA-inspired dark theme with green (`#76B900`) accents and diamond status
  indicators
- Command palette (`Ctrl+K`); shortcuts `Ctrl+N` new chat, `Ctrl+B` sidebar,
  `Ctrl+/` help
- Streaming over SSE and WebSocket; responsive down to phone width

## Screenshots

| Chat | Integrations |
|:-:|:-:|
| ![Chat](screenshots/chat.png) | ![Integrations](screenshots/integrations.png) |

| System | Advisors |
|:-:|:-:|
| ![System](screenshots/system.png) | ![Advisors](screenshots/advisors.png) |

| Setup wizard | Model Manager |
|:-:|:-:|
| ![Setup](screenshots/setup.png) | ![Model Manager](screenshots/model-manager.png) |

## Developing the UI

```bash
nvh serve --port 8000          # terminal 1: the API
cd web && npm install && npm run dev   # terminal 2: dev server on :3000
```

`NEXT_PUBLIC_API_URL` (default `http://localhost:8000`) tells the browser
where the API is. `nvh webui --dev` runs the same dev server from the
installed copy; `nvh webui --clean` wipes `node_modules` and `.next` when the
bundled build drifts from the backend. CI type-checks (`npx tsc --noEmit`),
lints and builds the app on every push; the API's `ALLOWED_ORIGINS` must keep
covering the defaults above.

Back to [README](../README.md)
