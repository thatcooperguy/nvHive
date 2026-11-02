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
| AI Wizard | `/wizard` | Tool-using assistant grounded in live workspace state: refreshes models, runs safe repairs, ingests dropped files (PDF included), searches the web, cites sources, shows cost and latency per reply. By default the concierge picks a hidden specialist from the Agent Library for each turn and the reply credits it; pin one under **Advanced**. Drop, paste or attach images and the Wizard can look at them (the per-message limit is in [CONFIGURATION.md](CONFIGURATION.md#code-and-images-from-the-wizard-chat)). A tool with side effects waits on a confirm card (**Run** / **Skip**); a privileged, sudo-class tool waits on a red approval card that lists the exact commands, and `NVH_ALLOW_PRIVILEGED=0` switches those off. Since 0.46 the Wizard can ask you a question (a card; your answer is your next message), save a note to your vault (`remember`: the card shows the note before it is written) and generate an image (`generate_image`: the card's plan line names the backend and the cost verdict) — the API's rules for the three cards are in [CONFIGURATION.md](CONFIGURATION.md#questions-memories-and-images-from-the-wizard-chat). **Convene council** hands the draft to the chat page |
| Agents | `/agents` | The Agent Library — profiles grouped by category, each mappable to a local or cloud model; copy one into `$NVH_HOME/agent-profiles/` to customise; the API provides each profile's `starter` prompt, plus the built-ins' `background` / `accent` colours, on `GET /v1/wizard/profiles` (`POST` accepts `starter`) |
| Models | `/models` | The Model Manager: installed models, fit-ranked catalog, one-click install with live progress ([MODELS.md](MODELS.md)) |
| Setup | `/setup` | Onboarding: storage check, GPU detection, local AI, free providers, ComfyUI and studio packs as resumable background jobs; **Advanced Details** exposes diagnostics and **Copy Error Report**; the page lists install jobs only (`GET /v1/jobs?exclude_kind=schedule-run`) |
| My Computer | `/system` | GPU, storage, runtime, cache and budget status with recommendations |
| Memory Vault | `/vault` | Markdown notes under `$NVH_HOME/vault`, queryable from the Wizard through RAG; the API provides the note list behind the page — `GET /v1/vault/notes` (headers only; filters `category`, `tag`, `q`, `memory_only`, `limit`), `GET /v1/vault/notes/{id}` and `DELETE /v1/vault/notes/{id}`, which also drops the note's RAG chunks; process home only — with `nvh vault list` / `nvh vault forget <id>` as the CLI twins ([CONFIGURATION.md](CONFIGURATION.md#memory-records-nvh-memory1)) |
| AI Connections | `/providers` | Provider cards: add a key, **Test Connection**, health and model listings |
| Usage | `/analytics` | Spend, tokens and latency per provider and per agent |
| Preferences | `/settings` | Theme, response cache, browser data. Model defaults and budgets live in `config.yaml` (`nvh config`) |
| Developer Tools | `/integrations` | Detect and connect Claude Code, Cursor, OpenClaw, NemoClaw; attach external MCP servers and refresh their tools |
| Schedules | `/schedules` | Scheduled prompts the API runs while it is up; the API provides `GET`/`POST /v1/schedules`, `GET`/`PATCH`/`DELETE /v1/schedules/{id}`, `POST /v1/schedules/{id}/run` and `GET /v1/schedules/{id}/runs` (all authenticated): list, add with `30m`/`1h`/`1d`, enable, disable, run now (a 409 naming the job while a run of that schedule is still in flight), delete, the last status with its cost, and a run log of the last 20 runs; `mode: do` is refused by the API with a terminal hint ([CONFIGURATION.md](CONFIGURATION.md#schedule-records-nvh-schedules2)) |

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
- Session cost pill (0.46): the API provides `GET /v1/budget/status?since=<when the tab opened>` — `session.spend_usd` is the machine-wide ledger since that instant (CLI, other tabs, proxy clients and scheduled runs all count), `cost_known` the catalog's verdict on every model that answered, `local_only` true only when every provider was local, `hourly_rate_usd` the `budget.hourly_rate_usd` you configured (never guessed; the desktop term is that rate × the tab's age) and `platform.is_owned` whether the box is yours ([CONFIGURATION.md](CONFIGURATION.md#schema-defaults))
- Mascot: a sprite in the bottom-right corner mirrors what the Wizard is doing
  (thinking, running a tool, waiting for your approval, done, error); click it
  to ask the Wizard or hide it. States, manifest and how to swap the art:
  [MASCOT.md](MASCOT.md)

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
