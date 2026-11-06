# Changelog

## [Unreleased]

### Fixed
- **The WebUI reaches the API on Windows.** Every web-side default pointed
  at `localhost:8000` — the Next.js rewrite, the config and debug-report
  routes, the inline runtime base, the health banner and the system
  console — and on Windows 11 `localhost` resolves to IPv6 first while the
  API binds IPv4 loopback, so a page opened at `localhost:3000` probed
  `::1`, timed out and showed "API down" beside a running server. The
  defaults are `127.0.0.1:8000` now and a page opened at `localhost` derives
  its API base as `127.0.0.1` too.
- **The chat home's top bar no longer sits under the system console's
  strip** (the mode switch and the pills were hidden behind it).
- **The first-run model line reads the installed Ollama models.** It looked
  for local rows in the cloud catalog (`GET /v1/models` has none) and said
  "No local model yet" with two models installed; it reads
  `GET /v1/ollama/models` and skips embedding models.

### Changed
- **One header and plain-word states.** Every page under the shell uses the
  shared page header (Setup gained it; its section titles stay), and the
  empty, loading and error states of Memory, Schedules, Agents and Models
  each end with the one next step in plain words — "Save a note from the
  chat with /save, or add one above", "Create one below — ask or convene,
  every 30m, 1h or 1d", "Press + Create your first agent to add one",
  "Start the API from the console at the top, then reload" — with no reason
  codes and no pointer to logs. The Schedules page no longer names the CLI
  twin in its empty state.

### Added
- **A "first answer" smoke check.** `nvh test` / `nvh status --smoke` gain
  `wizard_first_answer`: it posts a one-word question to `POST /v1/wizard/chat`
  with no profile and passes on a non-empty answer or on the deliberate
  local-only refusal (both mean the path works), fails on an HTTP error, a
  transport error or an empty body with "run `nvh services restart` and
  `nvh status --deep`" as the next step, and skips when nothing listens.
- **The session cost block counts its pairs.** `GET /v1/budget/status?since=`
  returns `pairs` (every request/response row in the window, failed ones
  included) and `cost_known` is `null` when nothing was counted, so the
  pill reads `local only` only when something ran locally: an idle tab shows
  the plain age and a tab whose only call was a failed local one reads
  `local only` instead of `cloud $0.0000`. An older API without `pairs`
  keeps the previous inference from `by_provider`.
- **The chat's empty state is a guided first run.** Three lines derived
  from the machine, not typed: your local model (ready, pulling with its
  size, or — without a GPU — the free cloud tier with a pointer to add a
  key), three starters drawn from the agent profiles' `starter` field
  (the hand-typed list is gone), and where keys, ports and packs live
  (Settings → Get started). The mascot says "Ask me what this machine can
  do" once per session. The rules are the pure `web/lib/firstRun.ts` with
  a Node test; the component reads the models, the install jobs and the
  Wizard context through calls that already existed.
- **One next action on every banner.** The API-offline banner ends with
  **Start the API** (the same bridge the system console's Restart uses) and
  the welcome-back panel ends with **Open the console**. A deliberate
  local-only refusal in the thread adds "Add a key in Settings →
  Connections, or install a local model in Models." under the bubble; the
  reason code is never shown as text.
- **`x-api-key` is a second spelling of the API credential.** `require_auth`
  accepts `x-api-key: <key>` exactly as `Authorization: Bearer <key>` and
  `X-Hive-API-Key`, so the Anthropic SDKs' `ANTHROPIC_API_KEY=<key>` works
  against nvHive's Anthropic paths instead of earning a 401; the 401 detail
  names all three spellings. INTEGRATIONS.md and the `HIVE_API_KEY` row say
  so.
- **A transcript that ends in a tool message is forwarded as history.** An
  agent loop posting tool results to `/v1/chat/completions` (and the stream
  path, and `Engine.query(history=...)`) gets them forwarded with an empty
  prompt — `_build_messages` appends no empty user turn — instead of the
  pre-0.46 flattened string; only an empty transcript is a 400.
- **`Scheduler._save` holds a file lock.** `<store>.lock` (an
  `O_CREAT|O_EXCL` file, portable to Windows) covers the read-modify-write,
  a lock older than thirty seconds is broken as stale, and under it any
  record another process saved since this instance loaded is folded in, so
  `nvh schedule add` racing the API runner keeps both records; a record this
  instance removed stays removed.

### Changed
- **The Groq keyring lookup honours `NVH_USE_KEYRING`** exactly as the
  provider registry does (`1`, `true` or `yes`); with it off, `keyring` is
  never imported by the voice path.
- **`nvh launch` targets:** `claude` is verified against Claude Code 2.1.257
  (`verified_on: 2026-11-05`); the `codex` row records that Codex CLI
  0.152.1 refuses `wire_api = "chat"` and speaks only the Responses API,
  which nvHive does not serve yet, so that snippet does not load on current
  Codex (a `/v1/responses` endpoint is the follow-up).

### Added (earlier in this cycle)
- **The chat home follows a new Wizard thread and switches modes on a
  phone.** When the Wizard creates its conversation on the first message the
  sidebar highlights it at once (the page reads the `?c=` the thread wrote
  when the conversation list changes), and below the `sm` breakpoint the
  Wizard · Ask · Convene · Poll switch is a select beside the menu button.
  WEBUI.md says `/wizard` is a redirect too and that the three redirects
  retire in 0.48 (O14).
- **`nvh launch claude` is verified; the Codex snippet is known not to load
  on current Codex.** Against Claude Code 2.1.257 the registration command
  nvHive runs (`claude mcp add nvhive -- nvhive-mcp`) is accepted and the
  server lists (pending approval until you open `claude` once), so the
  `claude` row carries `verified_on: 2026-11-05`. Codex CLI 0.152.1 refuses
  `wire_api = "chat"` ("no longer supported") and speaks only the Responses
  API, which nvHive does not serve; the `codex` row says so in its notes and
  stays unverified, and a `/v1/responses` endpoint is the follow-up.
- **One chat home.** `/` hosts the Wizard as its default mode, with Ask
  (one model), Convene (council) and Poll (compare) as the other modes of
  the same page, switched in the top bar; a wizard thread opened from the
  sidebar or a link resumes at `/?c=<id>` in Wizard mode and the other
  modes keep their own threads. `/wizard` is a one-release redirect that
  renames `?conversation=` to `?c=` and keeps `starter`, `profile` and
  `issue`; every link that pointed at it (the Agents page, the Setup page,
  the Schedules page's Open thread, the mascot's Ask the Wizard, the
  command palette, the shared sidebar) now points at `/`. Enter sends in
  every mode and Shift+Enter makes a new line. The C1b shell merge held
  out of 0.46 (O9), done the cheap way: the chat page keeps its chrome and
  mounts `WizardThread` for the default mode.

### Changed
- **The README says what happens.** Rewritten for the ten-second read: one
  sentence, the one command, four lines on what the installer does before the
  browser opens, what you get in the user's words, who it is for, how to get
  help, one grouped line of docs links. The 0.46 release notes that had grown
  into the AI Wizard blurb, the hand-typed profile count and the two long
  tables are gone; every `docs/<page>` is still named, every link resolves,
  and the Spark playbooks keep their "not yet exercised on hardware" sentence.
  The plan behind it is `docs/proposals/SIMPLIFY_0_47_DESIGN_2026-11.md`.
- **The sidebar shows five destinations and a Settings group.** Chat, Models,
  Agents, Memory, Schedules, then **Settings ▸** holding Get started
  (`/setup`), Connections (`/providers`), Developer tools (`/integrations`),
  This computer (`/system`), Usage (`/analytics`) and Preferences
  (`/settings`). Every route is unchanged — the group opens itself while one
  of its pages is active — and the command palette lists each page under its
  new name and, for one release, the old one. WEBUI.md's page table uses the
  new labels. The `/query` and `/council` redirect comment in
  `web/next.config.js` now carries the O14 date (they retire with the
  `/wizard` redirect that follows the shell merge).

## [0.46.0] - 2026-11-02

Seven of the eight add items of #133, on their second re-scope (the eighth,
one chat surface, is held below), and the five items held out of 0.45. The
Wizard asks instead of guessing (`ask_user` puts one clarifying question on a
card and waits for the answer), keeps memory notes (`remember` writes an
`nvh-memory/1` record that `/vault` lists behind a Memory toggle and `nvh
vault list` / `forget` / `upgrade` manage) and makes images (`generate_image`
is a confirm card with a plan line: NVIDIA-hosted when `NVAPI_KEY` is set,
else local ComfyUI, and never the paid path under a profile cost ceiling while
the price is unknown); schedules run inside the API process behind a heartbeat
lock, with the seven `/v1/schedules` routes, `schedule-run` jobs and the
`/schedules` page; the standard `/v1/chat/completions`, `/v1/completions` and
`/v1/anthropic/v1/messages` paths sit beside `/v1/proxy/...` and `nvh launch
<tool>` points Codex, opencode, Continue and Cursor at them (each prints its
snippet and starts nothing) and gives Claude Code nvHive's MCP server instead
(`claude` registers it and launches; inference stays with Anthropic, O11); the
session cost pill reads `GET /v1/budget/status?since=` against
`budget.hourly_rate_usd`; and the composer gains a hold-to-talk mic (the
transcript is inserted, never sent) and a Read aloud toggle through the
browser's `speechSynthesis`, with local speech-to-text from the `voice` extra
(faster-whisper on the CPU, int8, `small.en`, weights fetched only by an
explicit download) and Groq Whisper as the cloud fallback. Underneath: every
vault memory note is an `nvh-memory/1` record and `state/schedules.json` an
`nvh-schedules/2` store, `require_auth` and `key_required()` live in
`nvh/api/deps.py`, `/v1/health` discloses `bind_host`, `auth_mode` and
`key_required`, the Anthropic proxy and the integrations scan are behind the
key, and streamed proxy and Wizard turns are budget-checked and written to the
ledger. Carried from 0.45: `cost_known` beside every `cost_usd`, so a free
reply and a `$0` nobody priced read differently, the process-group kill of an
in-flight playbook command, `--verify` honouring `exclude_providers`, the last
bundled-file readers going through `nvh.data`, and the pre-0.44 tool-call
forms dropped from the agent loop's parser. O6 is decided by the arm64 CI
step: `pip install -e ".[voice]"` installed and imported faster-whisper 1.2.1
(ctranslate2 4.8.4) on the `ubuntu-24.04-arm` runner, so the `voice` extra
ships as the local speech path on Linux arm64, the DGX Spark target (verified
on the CI runner, not yet on Spark hardware); its requirement carries
`sys_platform != 'win32' or platform_machine != 'ARM64'` (the design's `not
(…)` spelling is not valid PEP 508), so on Windows on Arm the extra installs
nothing and `nvh voice --status` says why. The design is
`docs/proposals/ADDS_0_46_DESIGN_2026-11.md`.

Not in this release: the `/` + `/wizard` shell merge (C1b in the design).
Since Wave C the thread is `web/components/chat/WizardThread.tsx` and
`/wizard` mounts it, but O9 was not confirmed, so the mount on `/` is held for
0.47; `/wizard` stays a page until then, and the 0.42 `/query` and `/council`
redirects in `web/next.config.js` stay with it, re-dated to retire together
with the one-release `/wizard` redirect that follows the merge (O14; its 0.47
date assumed a 0.46 merge and is the owner's to re-set). A `LOCAL_MODEL_TIERS`
review for qwen3.6 stays an owner decision, and Phase 4 (the Spark binary and
the unified-memory probe) waits on hardware facts.

### Added
- **Cost pills tell a free reply from a $0 nobody priced.** Every API
  envelope that carries `cost_usd` now carries `cost_known` beside it: the
  Wizard chat envelope, its persisted meta tail and the stream `done` event,
  `/v1/query` (JSON, SSE `done`, WS `complete`), `/v1/compare`, `/v1/council`
  (members, synthesis and the total, on REST and on the WebSocket's
  `member_complete` / `synthesis_complete` / `council_complete` events),
  persisted chat rows (recomputed on read from the stored model, provider,
  cost and tokens) and the `/v1/smart` agent envelope (each step now records
  the model and provider that answered, so a free local run is a known $0).
  The verdict derives from the catalog lookup that priced the request
  (`nvh.core.agent_loop.model_cost_known`, re-exported by
  `nvh.core.agentic`), never a hand-typed table: a non-zero bill is known; a
  priced model's `$0` is known only when the provider metered the usage; a
  `zero_cost` provider's `$0` and a cache hit are known whatever id the
  provider echoed; an id the YAML catalog lacks falls back to LiteLLM's own
  price table and the served spelling falls back to the routed model; a
  council in which nothing was priced is unknown. The web UI renders three
  states from it (new `web/lib/cost.ts`, shared by the Wizard footer, the
  chat and compare cards and the council expert panel): a priced cost as
  before, a priced `$0` as `$0.0000` instead of the old green `FREE`, and
  `cost_known: false` as `cost unknown` with a hover title saying the model
  has no price in the catalog. A reloaded turn restores the same pill; a
  persisted `$0` row that predates the field keeps its historical "priced"
  reading rather than being called unknown.
- **Cancelling a playbook job stops the command that is running**, not just
  the next one. Each job-path step runs in its own process group through
  `run_host_command_killable` (same deny list and `sudo -n` rule as
  `run_host_command`, shared via `_resolve_host_command`); the worker
  re-reads the job's `cancel_requested` flag every 0.5 s and, when it is up,
  polls once (a command that exited on its own is recorded as its plain exit,
  never as a kill), sends the group SIGTERM, waits 5 s, then SIGKILL, and for
  a `sudo` step whose root-owned command outlives SIGKILL escalates with
  `sudo -n kill -KILL` on the group. The receipt records `stopped during step
  N (command killed)` with the signal's exit code and the output that had
  arrived (`killed` on the step row and on `in_flight`), the engine yields
  its own `canceled` event so the job log, the job's message and the vault
  `Decisions/` note carry the same account, and the between-steps wording
  `stopped after step N` is unchanged. When the signals cannot be shown to
  have stopped the command (alive after all of them, or a process outside
  nvHive's reach keeps its pipes) the record is `kill_failed` with the reason
  and the receipt says the command may still be running on the host, rather
  than claiming a kill; a step whose own deadline fires as the cancel lands is
  recorded as a step that ran and timed out, with receipt and audit written;
  a repeat cancel during the bounded wait keeps the runner's report; the wait
  is the runner's worst case plus slack from one formula
  (`system_settings.stop_worst_case_s`). A task cancelled from outside the
  job (a server shutdown) still leaves the command alone and records it as
  in flight. Windows keeps importing the module; the engine refuses off Linux
  as before. Tests: fake-process unit tests for the runner and the engine's
  cancel decision on every platform, plus a POSIX-only integration test that
  kills a real `sleep 30` and a SIGTERM-ignoring `bash` with their groups
  (its first real run is ubuntu CI; the root escalation is exercised through
  a fake process only).
- **`tests/test_bundled_data_access.py`** guards that `nvh/data/__init__.py`
  is the only module under `nvh/` that touches `importlib.resources` (any
  spelling, the `importlib_resources` backport and `pkgutil.get_data`
  included) and pins that the three routed loaders below return byte for
  byte what the direct read returned.

- **Vault memory notes are `nvh-memory/1` records.** `append_vault_memory`
  writes an `Id:` (`uuid4().hex`), a `Schema:` and a `Source:` line into the
  header paragraph it already wrote, and the new readers `parse_vault_note`,
  `list_vault_notes`, `get_vault_note`, `delete_vault_note` and
  `derived_note_id` give every existing note a stable id without touching
  the files: a pre-0.46 note reads as `schema: legacy` with a path-derived
  id, a seeded note as `doc`. `vault_status()` reports `memory_files`; the
  RAG bridge gained `index_vault_note` and `forget_vault_note` (exported
  from `nvh.integrations.rag`) so one note can be indexed or dropped
  without re-ingesting the vault, which is what the REPL's `/remember` and
  `/forget` now do. The layout is the generated `memory-record` block in
  CONFIGURATION.md.
- **`state/schedules.json` is versioned as `nvh-schedules/2`.** The file is
  a `{schema, updated_at, schedules}` wrapper; each record gains `name`,
  `profile`, `conversation_id`, `updated_at`, `last_status`, `last_error`,
  `last_job_id`, `last_cost_usd`, `last_cost_known`, `run_count` and `extra`
  (unknown keys, round-tripped); `Scheduler` gains `get`, `set_enabled`,
  `update` and `mark_run(status=ok|error|skipped|deferred, job_id, error,
  cost_usd, cost_known, conversation_id)`, with `mark_completed` kept as
  the alias of `mark_run(status="ok")`; new ids are 32-hex `uuid4().hex`.
  Pinned by the new `tests/test_scheduler.py`; the layout is the generated
  `schedule-record` block in CONFIGURATION.md.
- **Three generated record-schema blocks in CONFIGURATION.md.**
  `memory-record` from `MemoryRecord`, `schedule-record` from
  `ScheduledTask` and `wizard-meta` from `WIZARD_META_KEYS`, rendered by
  `scripts/gen_config_doc.py` from `dataclasses.fields()` and each field's
  `metadata["doc"]` (types and defaults derived, never typed) and pinned by
  `tests/test_config_doc_parity.py`, beside prose for the note header and
  filename rule, the v2 schedules wrapper with its read-compat and
  quarantine, and the wizard-meta presence rule. This closes the 0.46
  ROADMAP obligation to document both stores before reshaping them; the
  design they come from is `docs/proposals/ADDS_0_46_DESIGN_2026-11.md`.
- **The Wizard's persisted `wizard-meta` tail is versioned** (`v: 1`,
  `WIZARD_META_VERSION`) and described by `WIZARD_META_KEYS` in
  `nvh/integrations/wizard/chat.py`, the source of the generated block.
- **`nvh/api/deps.py` and the standard SDK paths.** `require_auth`, the
  bearer scheme, the response envelope, `get_engine`, `auth_mode()` and
  `key_required()` live in `nvh/api/deps.py`; `nvh/api/server.py` re-exports
  every name, so existing imports and test patches keep working.
  `POST /v1/chat/completions`, `POST /v1/completions` and
  `POST /v1/anthropic/v1/messages` are the same handler objects as
  `/v1/proxy/chat/completions`, `/v1/proxy/completions` and
  `/v1/anthropic/messages`, which stay, so
  `OPENAI_BASE_URL=http://127.0.0.1:8000/v1` and
  `ANTHROPIC_BASE_URL=http://127.0.0.1:8000/v1/anthropic` work as the SDKs
  build them; `GET /v1/models` keeps the nvHive envelope (the OpenAI list is
  `/v1/proxy/models`). `GET /v1/health` reports `bind_host` (`null` when the
  app was not started by `run_server`), `auth_mode` (`open`, `key` or
  `users`) and `key_required`.
- **`PlatformFacts.is_owned`** and the single `OWNED_DEVICE_CLASSES` set in
  `nvh/utils/platform_facts.py` (the Wizard findings import it;
  `to_dict()` carries it); the Wizard context's platform block reports
  `is_cloud`, `cloud_provider` and `is_owned`.
- **`redact_secrets` knows three more key shapes:** `nvapi-…` (NVIDIA),
  `gsk_…` (Groq) and `hf_…` (Hugging Face) are replaced like the OpenAI and
  Anthropic prefixes already were.
- **docs/TESTING.md** documents the `mcp-v1` CI job and the config-doc parity
  guard; **docs/MAINTAINERS.md** names `MemoryRecord`, `ScheduledTask` and
  `WIZARD_META_KEYS` as regeneration triggers and `mcp-v1` as a release
  blocker.

- **The Wizard can ask, remember and draw.** Three confirm-class tools in
  `nvh/integrations/wizard/tools.py`. `ask_user` puts one clarifying
  question on a card and ends the turn; the answer arrives as the next
  message and as a `TOOL_RESULT ask_user` history line. It is offered to
  every profile, `strict-tools` included, whenever a human is present and
  never on an unattended turn, and a second question in the same turn lands
  in `deferred_tool_calls` with `reason: question_pending`. `remember`
  composes a note the card shows before anything is written and saves it
  as an `nvh-memory/1` record under `$NVH_HOME/vault/Wizard Memory/` only
  (tags `wizard` plus up to eight from the model, `Source: wizard`, indexed
  into the vault RAG collection best-effort); a model-supplied `home_dir`
  or `category` is dropped and a body carrying `approval_token` or the
  privileged terminal hint is refused. `generate_image` plans before it
  draws: the card's `plan` names the backend, the model and the cost
  verdict, the server pins the turn's profile (and its home) onto the call
  so the profile's cost ceiling is enforced on Run, and a request without a
  valid pin is refused rather than run without a ceiling. `tool_call`
  stream events carry `safety_class`; `AgentProfile.starter` holds the six
  core built-ins' starter prompts and `GET /v1/wizard/profiles` rows carry
  `starter`, `background` and `accent` (`POST` accepts `starter`);
  `daily-notes-coach` gains `remember`.
- **One image planner and renderer** (`nvh/integrations/imagegen.py`).
  `plan_image_backend` picks NVIDIA-hosted when `NVAPI_KEY` is set (and
  only that variable: a NIM chat key never moves a box onto the paid path),
  else the local ComfyUI at the host and port nvHive recorded when it
  started it, else says plainly that no backend is available; a profile
  with a per-turn cost ceiling never takes the hosted path while its price
  is unknown, and the card says why. `render_image` saves under
  `$NVH_HOME/outputs/images/<scope>/`, the authenticated
  `GET /v1/outputs/images/{scope}/{filename}` (`nvh/api/routes/outputs.py`)
  serves the file with a private cache header, and the Wizard's
  `analyze_image` may read that folder, so a generated image can be looked
  at in the same chat. Nothing prunes the folder in 0.46; the result says
  so.
- **Schedules run inside the API process.**
  `nvh/integrations/services/schedule_runner.py` turns each due schedule
  into a `schedule-run` job under `$NVH_HOME/jobs/`: `mode: ask` is an
  unattended Wizard turn in the schedule's own conversation under the
  profile's whitelist intersected with the read-only `SCHEDULE_AUTO_TOOLS`
  roster (a confirm or privileged request is recorded on the run with no
  plan and no approval token, and the run reads `deferred`); `mode:
  convene` calls the council; `mode: do` is refused by the API runner with
  a terminal hint, since only `nvh schedule start` can show its approval
  prompt. Each run records `last_status` (`ok`, `deferred`, `skipped`,
  `error`) and its cost on the schedule, and the last twenty runs per
  schedule are kept. One runner per store: a heartbeat lock at
  `state/scheduler.lock`, stale by age only and never by a pid probe, that
  a side task beats while a run is in flight, that a runner whose lock was
  taken over stands down from, and that the API loop waits on while
  `nvh schedule start` holds it. The lifespan starts the loop once the
  engine is ready, polling every `NVH_SCHEDULER_POLL_S` seconds (default
  60); `NVH_SCHEDULER=0` keeps it off; at shutdown a run in flight gets five
  seconds, then the task is cancelled and the lock released. Seven
  authenticated routes: `GET`/`POST /v1/schedules`, `GET`/`PATCH`/`DELETE
  /v1/schedules/{id}`, `POST /v1/schedules/{id}/run` (202, or 409 naming
  the job while a run of that schedule is in flight) and
  `GET /v1/schedules/{id}/runs`; `GET /v1/jobs` takes `exclude_kind` and
  `schedule_id`. On the CLI: `nvh schedule add --name --profile`,
  `nvh schedule enable|disable|run <id>`, a `schedule list` with Name,
  Status, Last and Last cost columns, and `nvh schedule start` deferring to
  a live API runner (`--once` beside one runs only the `do` schedules). The
  doctor's Scheduler row says which process runs schedules and names a
  quarantined `schedules.json`.
- **`nvh vault` group**: `status`, `init`, `list [--category --tag
  --memory-only --limit]`, `forget <id>` and `upgrade --add-ids
  [--dry-run]`, which gives a pre-0.46 note its `Id:` / `Schema:` /
  `Source:` header lines (the path-derived id the readers already use, so
  links keep resolving) without touching a byte of the body or its line
  endings. The same notes are served by the authenticated
  `GET /v1/vault/notes` (headers, newest first; `memory_only` drops seeded
  `doc` pages), `GET /v1/vault/notes/{id}` and `DELETE /v1/vault/notes/{id}`
  (which also drops the note's RAG chunks and reports `forgotten`); all
  three read the process home only and match ids against the notes' own
  `Id:` lines, never as paths. `POST /v1/vault/memory` indexes the saved
  note through the engine's admission and returns `indexed`.
- **Session cost facts for the pill.** `budget.hourly_rate_usd` (optional,
  never guessed; `nvh config set budget.hourly_rate_usd 2.10`) and
  `GET /v1/budget/status?since=<epoch_ms>`, which adds `hourly_rate_usd`,
  `daily_cost_known`, `platform` (device class and label, cloud, owned) and
  a `session` block summing the machine-wide ledger since that instant:
  `spend_usd` and `by_provider` from successful rows, `cost_known` the
  catalog's verdict on every model that answered, `local_only` true only
  when every provider was local and never because the sum is zero. A
  `since` in epoch seconds is a 422 rather than a sum over the whole
  ledger. Repository readers `get_spend_since` and `distinct_models_since`
  back it.
- **History and tools through the OpenAI-compatible handler.**
  `/v1/chat/completions` and `/v1/proxy/chat/completions` accept `tools`,
  `tool_choice`, `stream_options` and `response_format`, and message
  `name`, `tool_calls` and `tool_call_id`; a native tool call comes back as
  `message.tool_calls` with `finish_reason: "tool_calls"` (one
  `delta.tool_calls` chunk on a stream), `stream_options.include_usage`
  adds the usage-only chunk before `[DONE]`, and `GET /v1/proxy/health`
  lists `standard_paths`, `tools` and `history` under `features`. Streams
  through an OpenAI-compatible provider request
  `stream_options.include_usage` whenever LiteLLM's parameter table says
  the route takes it, so their ledger rows carry the provider's token
  counts (the Wave A follow-up); a caller's own `stream_options` is never
  overridden.
- **Docs for all of it.** CONFIGURATION.md gains "Questions, memories and
  images from the Wizard chat", the unattended-turn rule and the
  `SCHEDULE_AUTO_TOOLS` roster, the schedule runner's prose, the
  `NVH_SCHEDULER` / `NVH_SCHEDULER_POLL_S` rows, the budget fields and the
  vault routes, with the regenerated `wizard-tools` and `wizard-meta`
  blocks; INTEGRATIONS.md documents history forwarding and the tools
  round-trip; WEBUI.md lists the `/schedules` and `/vault` APIs and the
  pill's data source; ARCHITECTURE.md corrects "scheduler → jobs";
  TESTING.md records the `NVH_SCHEDULER=0` autouse fixture. New tests:
  `tests/test_imagegen.py`, `tests/test_schedule_runner.py`,
  `tests/test_vault_notes_route.py`, `tests/test_cli_vault.py`,
  `tests/test_repl_do_confirm.py`.
- **The Wizard's question card.** When the Wizard calls `ask_user`, the web
  thread shows the question, up to six choice chips, a free-text box unless
  the Wizard disallowed one, and Answer / Skip. Answer sends the text as
  your next message and folds `{name: 'ask_user', ok: true, summary}` into
  the request history so the Wizard knows which question was answered; Skip
  records "skipped by the user; not answered". Nothing is posted to the
  tool-execute route for a question, and the calls the Wizard held behind
  one appear as "not run: <tool> — held until you answer the question
  above". The mascot says "the Wizard has a question for you" when every
  pending card is a question (the pure `web/lib/mascotRules.ts`, with
  `web/lib/mascot.test.mjs`).
- **Plan line and inline image on confirm cards.** A confirm card that
  carries a planner verdict (`generate_image`) shows a `plan:` line — the
  server's summary of backend, cost verdict and GPU note — above Run / Skip;
  Run echoes the card's arguments unchanged, the server-pinned `profile`
  and `profile_home` included. After Run the image renders inside the card,
  fetched with the API key headers (a bare link would 401) and released on
  unmount, with backend · model · size, the three-state cost pill, the saved
  path and the server's note that nvHive does not prune that folder. A
  saved `remember` card links "Open in vault" and notes redacted spans.
- **The `/vault` notes list.** The page lists the vault's notes newest
  first, with folder chips from the server's `directories` roster, tag
  chips from the listed notes, a title/tag search box and a Memory toggle
  that keeps records only (`nvh-memory/1` and `legacy`; `/vault?memory=1`
  opens with it on); a row expands to the body and path; Forget confirms,
  then removes the file and its search-index entry and says when the entry
  could not be dropped; `/vault?id=<id>` — the link a `remember` card hands
  out — scrolls to that note and clears any filter hiding it, fetching it
  on its own when it is outside the loaded page. A Memory stat card counts
  the records. The save form's folder list comes from the same roster, tags
  start empty and saves are stamped `Source: web`. Helpers in
  `web/lib/vaultNotes.ts` with a Node test.
- **The session cost pill.** `Session 4h 12m · desktop ≈ $3.37 · cloud
  $0.42` on a rented desktop (the desktop term is `budget.hourly_rate_usd`
  × the time since this tab opened, or "set your hourly rate" when none is
  set), `Session 4h 12m · cloud $0.42` on owned hardware, `local only` when
  every counted model ran locally, `cloud ≥ $Y · cost unknown` when a model
  has no catalog price, "spend unavailable" when the server could not read
  the ledger, and the plain age when the API is unreachable; amber only
  after fifteen minutes when cloud spend per hour exceeds the rate. It
  polls `GET /v1/budget/status?since=` once a minute and sits in the shared
  top bar on every page except `/setup`, and in the chat page's own bar.
  `/settings` gains a read-only Costs card with the configured rate (or
  "not set") and the `nvh config set budget.hourly_rate_usd 2.10` command.
  Rules in `web/lib/sessionCost.ts` with a Node test.
- **The `/schedules` page** (sidebar entry "Schedules") for the schedules
  the API server runs: the list with interval, mode, profile, next run,
  last status and a last-cost pill (nothing for a schedule that never ran);
  Enable / Disable, Run now (an in-flight run is reported with its job id
  from the server's 409), Delete with an inline confirm, Open thread into
  the schedule's conversation, and a run drawer that tails a run started
  from the page and lists the recorded runs with cost, answer preview and
  deferred tool requests; an add form with `30m` / `1h` / `1d` intervals
  validated in the browser, mode `ask` or `convene` only (do-mode stays
  CLI-only) and a profile picker; a runner line naming who runs schedules
  (the API server, `nvh schedule start`, or nobody) with the read-only
  roster an unattended turn may call. Helpers in `web/lib/schedules.ts`
  with a Node test. `/integrations` gains an "Any OpenAI-compatible client
  (standard paths)" snippet with the `OPENAI_BASE_URL` / `OPENAI_API_KEY`
  pair and the Messages-API `ANTHROPIC_BASE_URL` / `ANTHROPIC_AUTH_TOKEN`
  pair.
- **The web's typed API layer for the 0.46 routes** (`web/lib/types.ts`,
  `web/lib/api.ts`, the pure `web/lib/apiShapes.ts` with a Node test):
  vault notes, schedules (`runSchedule` resolves to `{started}` or, from
  the 409, `{conflict}` naming the in-flight job), `fetchApiBlob` for
  auth-gated binaries, `exclude_kind` / `schedule_id` on `getInstallJobs`,
  `getBudgetStatus(since)` with the session, platform, hourly-rate and
  `daily_cost_known` blocks (a seconds-scale `since` throws client-side),
  `safety_class` on streamed `tool_call` events, the image planner's
  verdict on `WizardToolPlan`, `question_pending` among the deferred
  reasons and `starter` / `background` / `accent` on agent profiles.
- **Docs and guards for the web surfaces.** WEBUI.md describes the question
  card, the plan line and image render, the `/vault` list, the Costs card,
  the `/schedules` page, the `/setup` job filter, the standard-path snippet
  and the pill's states as page behaviour, and its Developing section names
  `npm test` and the `web/components/chat/*` map; MASCOT.md gains the rows
  for a `tool_call` whose `safety_class` is not `auto` (no change — the
  card decides) and for `confirm_required` carrying only `ask_user` cards;
  TESTING.md lists the web checks and the guard rows. New
  `tests/test_web_surfaces.py` pins the surfaces: `/wizard` mounts
  `WizardThread` and nothing imports the retired `WizardChat`, the question
  card never reaches the execute route, `ScheduleMode` has no `do`, the
  `/vault` page has no hand-typed folder list, and every new pure
  `web/lib/*.ts` helper has a Node test beside it and imports nothing a
  Node run cannot resolve.
- **`nvh launch <tool>` starts a coding tool against the local API, or
  prints what to set.** Five targets ship as data in
  `nvh/data/launch_targets.json` (read through `nvh.data`, packaged by the
  `data/*.json` glob and smoked from the installed wheel by the build job's
  `nvh launch --list`): `claude` registers nvHive's MCP server and launches
  Claude Code with no nvHive variable set, so inference stays with Anthropic
  (O11); `codex`, `opencode`, `continue` and `cursor` print the config
  snippet to paste and start nothing — nvHive never writes a third-party
  config file. `--print` renders any target without an API; `--list` shows
  the roster; `--inference-proxy` prints the `ANTHROPIC_BASE_URL` stanza for
  nvHive's Anthropic proxy (unverified); arguments after the target go to
  the tool. Before a launch the command reads `GET /v1/health` and refuses
  (exit 2) an open-mode API bound off-loopback, a keyed API when
  `HIVE_API_KEY` is unset in the launching shell (the placeholder it would
  print only earns a 401) and a users-mode API with no token; `--start-api`
  (`-y`) starts `nvh serve` on loopback when nothing listens on `--port`
  and refuses a port held by anything that is not a healthy nvHive API.
  Ctrl-C goes to the launched tool, whose exit code is returned; the tool
  is resolved the way the shell resolves it (an npm `claude.cmd` shim on
  Windows launches); the health probe ignores proxy settings. Every row
  carries `verified_on`, and all five ship `null`, which prints as
  "unverified" until a maintainer runs the tool against its current
  version. The `/integrations` standard-path snippet ends with the
  one-liner; INTEGRATIONS.md gains `### nvh launch`.
- **A local-first speech-to-text backend and the `/v1/voice` routes.**
  `nvh/core/voice.py` gains faster-whisper (CPU, int8, `small.en` by
  default; `NVH_VOICE_STT_MODEL` or `--stt-model` picks another size, a
  `Systran/` or `mobiuslabsgmbh/` repo, or a converted directory spelled as
  a path) behind a lazy import, loaded once per process from
  `$NVH_CACHE/huggingface` and never downloaded implicitly: `nvh voice
  --download` or `POST /v1/voice/download` (a `voice-model-download` job;
  202 with the job id, 409 while one runs) is how the weights arrive, and
  `model_present` requires the complete snapshot so a damaged one reads as
  not downloaded. `speech_to_text(..., provider="auto")` returns a
  `TranscriptResult` (text, provider, model, latency, cost and whether the
  cost is known) and picks local when the weights are present, else Groq
  Whisper (`whisper-large-v3`, cost unknown) when a key resolves, else
  raises `VoiceUnavailable` carrying `voice_status()`. The router
  (`nvh/api/routes/voice.py`, every route behind `require_auth`):
  `POST /v1/voice/transcribe` (multipart `file`; `provider` `auto`, `local`
  or `groq`; 415 for a non-audio part, 413 the moment the body passes
  20 MB, 503 `{status, downloading, hint}` when no path exists, 502 when
  the chosen path failed), `GET /v1/voice/status` (the paths this box has,
  the in-flight download's job id and the hint), `POST /v1/voice/download`
  and `POST /v1/voice/speak` (`audio/mpeg` through `edge-tts` when it is on
  PATH, else 501 — the browser does the reading). Transcription runs under
  `asyncio.to_thread` behind a per-process `Semaphore(1)`, so the one
  uvicorn loop keeps answering; the clip is a temporary file unlinked on
  every path. `nvh voice` gains `--stt auto|local|groq|whisper-cli`
  (default `auto`), `--stt-model`, `--status` (a table of the speech paths
  this box has) and `--download`, and its transcript line names the path,
  latency and cost (`$0, on this device` or `cost unknown`). An orphaned or
  canceled `voice-model-download` job says to start it again (files
  already fetched are reused).
- **A hold-to-talk mic and a Read aloud toggle in the Wizard composer.**
  Hold the mic (or Space in the empty box, after a 200 ms hold — a tap
  still types a space), speak, release: the browser's `MediaRecorder` clip
  (60 s cap) goes to `POST /v1/voice/transcribe` and the transcript is
  inserted into the draft, never sent — a privileged request is still
  reviewed and clicked. The status line names the path that will answer
  and its cost (`$0, on this device`, `Groq Whisper · cost unknown`),
  offers **Download model (~480 MB)** when the `voice` extra is installed
  without weights (the job it starts, or one another tab started, is
  tailed through `/v1/jobs`), shows the install hint when no path exists
  and `voice needs https or localhost` outside a secure context; the
  mascot's bubble says "Listening…" while recording and the sprite stays
  put. The **Read aloud** toggle speaks each finished answer through the
  browser's `speechSynthesis` (offline; Markdown flattened, fenced code
  read as "code omitted"; the preference lives in the browser-local
  `nvh.voice.readAloud`, which **Clear browser data** on `/settings`
  drops). `web/components/VoiceButton.tsx`, `ReadAloudToggle.tsx`, the
  pure `web/lib/voice.ts` with `voice.test.mjs`, and `getVoiceStatus` /
  `transcribeAudio` / `startVoiceDownload` in `web/lib/api.ts`.
- **The `voice` extra and its CI steps.** `pip install 'nvhive[voice]'`
  brings faster-whisper for the local path and stays outside `all` (O6);
  its requirement carries `sys_platform != 'win32' or platform_machine !=
  'ARM64'` (the design's `not (…)` spelling is not valid PEP 508), so on
  Windows on Arm — where CTranslate2 and PyAV ship no wheels — the extra
  installs nothing instead of failing and `nvh voice --status` /
  `GET /v1/voice/status` say why. `ci.yml`: the `ubuntu-24.04-arm` matrix
  entry runs an informational `pip install -e ".[voice]"` plus an import
  (`continue-on-error`) whose log decides at release time whether the extra
  ships for arm64; the `security` job installs the extra in its own
  non-blocking step before `pip-audit`, so ctranslate2, huggingface_hub and
  tokenizers are audited when a wheel exists for the runner's Python; the
  `build` job runs `nvh launch --list` from the installed wheel.
- **Docs and guards for launch and voice.** INTEGRATIONS.md (`### nvh
  launch`: what launches, what prints, the refusals, the live key in
  `--print` output), CONFIGURATION.md (the `NVH_VOICE_STT_MODEL` row and a
  "Voice from the Wizard chat" section), MODELS.md (the speech rows: local
  STT is the `voice` extra on the CPU, cloud STT is Groq Whisper, TTS is
  the browser or Edge TTS from the CLI, no local TTS pack), MASCOT.md (the
  recording row), WEBUI.md (the mic, the toggle, the Setup job list, the
  cleared key, the `nvh launch` one-liner, the new test files),
  ARCHITECTURE.md (`nvh/api/routes/` with one module per surface) and
  TESTING.md (guard rows for `test_launch.py`, `test_voice.py`,
  `test_cli_voice.py` and the CI changes). New `tests/test_launch.py`
  (the bundled rows carry their keys and `verified_on`; the rendered
  environment holds only the row's variables and never a provider secret;
  the refusals mirror `/v1/health`; launch merges the environment over
  `os.environ` with no shell; `claude` registers before launching),
  `tests/test_voice.py` (a fake `faster_whisper` in `sys.modules` and a
  weights marker under the hermetic cache: `auto` picks local only when
  both exist; the streaming 413; the model allow-list; the TTS argument
  path) and `tests/test_cli_voice.py` (the `nvh voice` flags); voice rows
  in `tests/test_api.py`; `tests/test_release_hardening.py` pins the
  marker by evaluation, `voice` outside `all`, the security job's step
  order and the wheel smoke; `tests/test_web_surfaces.py` pins the
  composer mount, the insert-never-send rule, the `download_job_id`
  contract and the docs rows; the Node suite grows by the voice rows.

### Changed
- **`--verify` honours `exclude_providers`, and is independent or not at
  all.** The cross-check takes the exclusion set the answer was produced
  under (the engine forwards it from both verify sites, escalate and plain
  path), never picks an excluded advisor as the verifier (the filter applies
  before the health pick, so the all-unhealthy fallback cannot resurrect
  one) and passes the set into its own `engine.query` so the verifier's
  fallback chain cannot land there either: a `--focus research` answer,
  grounded or in privacy mode, is never re-checked by Perplexity. When the
  only advisor left is the answer's own provider (an exclusion that leaves
  just the answerer, or a single-provider install) the result is
  `unverified` with `No independent verifier available` instead of the old
  same-provider self-check wearing a `Verified` badge. Pinned by
  `tests/test_verify_exclude_providers.py` and the forwarding tests in
  `tests/test_engine_query.py`.
- **The last three bundled-file readers go through `nvh.data`.** The setup
  catalog (`nvh/catalog/nvhive-catalog.json`), the Agent Library
  (`nvh/catalog/agent-library.json`) and the Wizard system prompt
  (`nvh/prompts/nvwizard_system.md`) read through `read_json` / `read_text`
  like every other shipped file; their fallbacks (generated catalog, empty
  library with one warning, built-in prompt with one warning) are unchanged.

- **Streamed proxy completions are budget-checked and logged.** On every
  OpenAI- and Anthropic-compatible path the stream checks `budget.hard_stop`
  before the first provider call (one `budget_exceeded` error event instead
  of tokens) and writes its spend to the query ledger when it ends
  (`mode: proxy`; the non-streamed call on the same route goes through
  `Engine.query` and stays `mode: simple`, and the budget totals sum both).
  Until now a streaming client could spend past the budget unrecorded.
  Coding tools stream by default, so this landed before the standard paths
  did. A stream that ends in a provider error leaves no success row; a
  provider that reports no usage on its final chunk still logs an
  estimated output count and `0` input tokens (ROADMAP, Known follow-ups).
- **Streamed Wizard turns reach the cost ledger.** A turn served by the
  streaming endpoint is written once as `QueryLog` mode `wizard-chat`
  (totals summed over its iterations) before `done`, and when a later
  iteration fails the earlier iterations' metered spend is logged before the
  `error` event; the assistant row's own `input_tokens`, `output_tokens`,
  `cost_usd` and `latency_ms` columns are filled from the meter (the tail is
  no longer the only carrier of cost).
- **Vault memory writes.** Titles are collapsed to one line; title and body
  pass through `redact_secrets` before the write and the result reports the
  count as `redacted`; a note that is nothing but a secret is refused
  (`nothing to remember after redacting secrets`, a 400 on
  `POST /v1/vault/memory`, which also accepts `source`); two notes saved
  with the same title in the same second no longer overwrite each other (the
  second file gains a `-<id8>` suffix); notes are written to a temp file and
  moved into place; `import_legacy_memories`, `/remember` and the
  privileged-change audit stamp `Source: import` / `repl` / `audit`.
- **The schedules store's legacy handling.** A pre-0.46 bare-array file
  still loads unchanged (legacy 8-character ids kept) and is rewritten in the
  v2 shape on the next save; a file that cannot be parsed is moved aside to
  `schedules.json.corrupt-<timestamp>` with one log line instead of being
  silently emptied; a record with no `id` reads with the same derived 32-hex
  id in every process, one missing its prompt or interval loads disabled;
  `Scheduler.update` refuses wrong-typed values before touching the record;
  saves use a private per-process temp file, `os.replace` and sorted keys,
  retry briefly when Windows reports the file open, and remove the temp file
  on every failure.
- **The CLI schedule runner records each run's outcome.** `nvh schedule
  start` calls `mark_run(status="ok")` after a run and
  `mark_run(status="error", error=...)` when it raises, so a failed run no
  longer reads as completed in `nvh schedule list`.
- **Docs and strings.** The proxy docstring and docs/INTEGRATIONS.md say
  `OPENAI_BASE_URL` (LangChain and LiteLLM also accept the older
  `OPENAI_API_BASE`) and gained a "Standard paths" section; the `NVH_HOME`
  layout prose says the record schemas are documented below instead of
  promising them, and the `state/` row names `scheduler.lock`, the transient
  and quarantined `schedules.json` siblings and the pre-0.42-only role of
  `memory/`; the Wizard context reads `vault_status()`'s `memory_files`
  directly; `nvh/core/voice.py`'s stale strings match the code
  (`nvh advisor add groq`; sox or arecord, nothing else is tried).

- **`imagine` is confirm-class** (O3): the hosted image providers bill the
  user's key, so `nvh do` and the REPL's `/do` ask before it runs (an
  `Allow? [y/N]` prompt; the REPL gained the prompt in this wave), while
  the agent loops that have no confirmation channel, a `mode: do` schedule
  under `nvh schedule start` and `/v1/smart`, answer `Tool requires
  approval: imagine` instead of generating. Any confirm-class tool with a
  planner now shows its dry run on the card as `plan` (no pins folded, no
  token minted).
- **The shared OpenAI-compatible chat handler forwards a transcript as
  history by default** (O12): the last `user` message is the prompt, every
  `system` message joins the system prompt, and earlier turns, assistant
  tool calls and tool results reach the model in order. Pre-0.46 flattened
  the transcript into one `User:`/`Assistant:` string; the header
  `x-nvhive-history: flatten` restores that, and council / throwdown and a
  transcript with no trailing user turn still receive it.
- **A schedule run that raises is recorded as `error`**, never as a
  completed run; an `ask` run whose turn no model answered is `error` with
  the reason, never `ok` with the offline helper's text; a schedule's
  `advisor` is honoured on `ask` runs again; `convene` now convenes; a run
  the server cancelled while stopping says so instead of "Install job
  canceled by user"; and `nvh schedule add --mode do` warns that only the
  CLI runner runs it. Writes to `schedules.json` inside the API process
  hold one lock, and the runner re-reads the store at every write.
- **Portraits go through the image planner.** `comfyui.generate_portrait`
  is a shim over `render_image` (same `(bytes, suffix)` result and hint),
  keeps a copy of each portrait under `outputs/images/avatars/`, and
  reaches ComfyUI at the host and port nvHive recorded when it started it
  instead of the default port. The avatar route plans first, so its error
  reads `Image generation failed (<backend>): …`.
- **`nvh serve --reload` runs no schedule runner** unless `NVH_SCHEDULER=1`
  is exported first: a worker killed on every save would leave runs
  interrupted and the lock to age out, so the reloader parent defaults the
  variable to `0` and `nvh schedule start` is the runner for a dev session.
- **tests/test_agent_library.py** derives the valid tool whitelist from the
  registry roster instead of a hand-typed list; `tests/conftest.py` sets
  `NVH_SCHEDULER=0` and `NVH_RAG_AUTO_PULL=0` for every test.
- **The Wizard chat surface moved** from `web/components/WizardChat.tsx` to
  `web/components/chat/WizardThread.tsx`, with the view components split
  out beside it (`MessageBlock`, `ToolCard`, `ServerToolTrace`,
  `SourcesFooter`, `DeferredToolNotes`, `EmptyState` and the `Message`
  model in `chat/types.ts`); behaviour is unchanged. `WizardThread` takes a
  `conversationParam` and resumes from `?conversation=` or `?c=`; its
  `send` accepts `{text?, historyPatch?}` so a card can send text without
  touching the composer and fold its outcome into the history first (the
  `?issue=` / `?starter=` deep links send through it instead of seeding the
  draft). The `/save` slash command writes to the vault's `Wizard Memory`
  folder with tags `wizard`, `chat` and `Source: web` — the same record as
  the `remember` tool — instead of the old `wizard-chats` relabel. The web
  pin tests read the new paths.
- **The chat page's `USD $X.XX today` pill is gone**; the session cost pill
  replaces it and the session-age pill (`SessionAgePill.tsx` is now a
  re-export). The daily figure stays on `/system`'s Budget widget, which
  says "Budget figures are unavailable right now" instead of showing a $0
  ledger when the API is down or the ledger unreadable, and whose title
  says it covers cloud-model spend, not desktop metering. A failed
  `getBudgetStatus` resolves to a zero ledger flagged `unavailable` instead
  of posing as nothing spent.
- **A streamed `tool_call` whose `safety_class` is not `auto` no longer
  moves the mascot to `working`** (the card decides) and the status line
  reads "proposing <tool>…"; the Create Agent portrait fallback reads
  "Image generation isn't configured (no NVIDIA API key, no local
  ComfyUI). Upload an image instead." and names the backend and plan the
  API reported. The Setup page lists install jobs with
  `exclude_kind=schedule-run`, so hourly schedules no longer push install
  jobs out of its window. `applyHistoryPatch` lives in the pure
  `web/lib/privileged.ts` with a Node row.
- **`speech_to_text` is synchronous and returns a `TranscriptResult`.** It
  was `async def speech_to_text(audio_path, provider="groq") -> str`; it is
  now `speech_to_text(audio_path, provider="auto", *, mime_type, filename,
  model)`, run in a thread from async code (the API and `nvh voice` both
  use `asyncio.to_thread`). `provider="local"` means in-process
  faster-whisper; the pre-0.46 openai-whisper subprocess is
  `provider="whisper-cli"`, reachable from `nvh voice` only, and it now
  writes its transcript into a temporary directory rather than the
  working directory. An unknown provider raises `ValueError`;
  `VoiceUnavailable` subclasses `ValueError`, so the 0.45 Groq-key error
  still matches. Groq's timeout is 60 s (was 30), the CLI's 300 s.
  `nvh voice --stt` defaults to `auto` (was `groq`); `VoiceConfig.
  stt_provider` keeps `groq` and is read by neither surface. The Groq key
  lookup also honours `COUNCIL_GROQ_API_KEY` and the keyring entry
  `nvh advisor add groq` writes.
- **The `security` CI job installs the `voice` extra in its own non-blocking
  step** after the hard `.[dev]` install and before `pip-audit`, so a
  missing native wheel never turns the job red or skips the audit of
  everything else. `register_claude_code` resolves `claude` through
  `shutil.which`, so an npm shim registers on Windows. `/setup`'s job list
  shows `voice-model-download` jobs beside installs (deliberate: it is the
  one place a download can be watched outside the composer).
- **The `security` job's `pip-audit` runs without `--strict`.** 0.44.1 made
  the step install the project before the audit (a bare `pip-audit` checks
  only the interpreter's own tree) and added `--skip-editable` beside the
  `--strict` already there; in an editable checkout the two cannot both
  hold — `--skip-editable` turns the `nvhive` checkout into a skipped
  dependency and `--strict` makes any skipped dependency fatal — so every
  run since exited at `nvhive: distribution marked as editable` without
  printing a result, and the `::warning::` fired unconditionally. The run
  line is now `pip-audit --desc --skip-editable || echo "::warning::…"`:
  the editable checkout is skipped non-fatally, the audit of the installed
  set completes, and the warning means a finding. The step stays
  `continue-on-error`; `docs/MAINTAINERS.md` item 6 and its local command
  drop `--strict` with it.

### Fixed
- **`GET /v1/health` no longer calls an unknown bind host loopback.** When
  the app was not started by `run_server` (a bare
  `uvicorn nvh.api.server:app`, an embedding, a test client) `bind_host` is
  `null` instead of the fabricated `127.0.0.1`, so a client treats it as
  not-loopback.
- **REPL memory.** `/remember` derives the note title from the redacted
  text, so a key split by the 60-character title cut cannot leak its prefix
  into the heading, the file name or the RAG index; `/forget` unlinks the
  matched file rather than deleting by id (a duplicate `Id:` copy no longer
  makes it delete the wrong note); the legacy `memories.json` import skips a
  secret-only row instead of aborting, leaving the marker unwritten and
  re-importing on every start; `/remember` followed by `/exit` or Ctrl+D
  still indexes the note (background tasks are drained before the loop
  closes).
- **Vault readers.** A hand-written page whose first paragraph is a single
  `Created:` or `Source:` line stays a `doc` (a record's header carries at
  least two known keys), and a symlink or junction that leaves the vault is
  never listed, read or deleted as a note.
- **Order-dependent GPU flake in `tests/test_wizard_context.py`.** The
  real `wizard_context()` memoises nvidia-smi rows for five seconds, which
  made the platform tests that run next on a GPU box see a real GPU; the
  file now cold-starts that cache (autouse fixture).

- **Two differently worded `ask_user` calls in one turn** no longer both
  become cards, and a model re-emitting a confirm call with a server-pinned
  argument no longer duplicates the card.
- **A streamed multi-turn proxy request** no longer reaches the provider as
  only the last user turn: the stream generator takes the same history the
  blocking path does.
- **Streamed calls through an OpenAI-compatible provider** no longer log
  `0` input tokens when the provider can report usage (see Added).
- **The schedule runner's lock** no longer reads stale during a long run, a
  lock that exists but cannot be read counts as held rather than stale, and
  the API server's loop no longer gives up until a restart when
  `nvh schedule start` held the lock at startup: it waits and takes over
  polling when the lock is released.
- **One run per schedule at a time.** Run now on an in-flight schedule
  answers 409 naming the job, the poll skips it, and the poll leaves a due
  `mode: do` schedule for the CLI instead of failing it every interval.
- **`GET /v1/budget/status?since=`** refuses a value in epoch seconds (422)
  instead of summing the whole ledger; **`POST /v1/vault/memory`** indexes
  with the engine's admission and survives a paused resource.
- **The `scheduler` doctor row and `nvh schedule list`** name the
  `schedules.json.corrupt-<timestamp>` file after a quarantine and the row
  turns `warn` (the Wave A follow-up).
- **Web review pass on the Wave C surfaces.** A question-card answer or
  `?starter=` text beginning with `/` (a path such as `/mnt/nvme0`) is sent
  to the Wizard instead of being swallowed as an unknown slash command —
  only the composer issues slash commands; `/clear` drops both
  `?conversation=` and `?c=` so a cleared thread is not resurrected on
  reload; the question card's Answer is disabled while a turn streams
  instead of ignoring the click; the pill reads `cloud $0.0000`, not
  `local only`, for a tab in which nothing has run yet, and a configured
  rate of exactly 0 never turns it amber; `/vault?id=` clears a filter
  hiding the linked note before scrolling to it; the `/schedules` live
  panel tells a watcher that lost the API (amber "lost contact" with a
  Reconnect button — the run keeps going on the server) from a job that
  failed; the Create Agent modal renders the avatar planner's `Plan:` line,
  which the API sends as a string; a `remember` result with `saved: false`
  renders as a failure instead of Done.
- **Review pass on the launch and voice surfaces.** `nvh launch` no longer
  kills the launched tool on the first Ctrl-C (`subprocess.call` did; the
  parent now ignores SIGINT while the child runs and returns the child's
  own code), resolves the command the way the shell does (an npm
  `claude.cmd` shim never launched through `CreateProcess`), probes
  `/v1/health` with `trust_env=False` so a system proxy cannot make the
  health line and the refusal disagree, refuses a keyed API when
  `HIVE_API_KEY` is unset instead of printing the placeholder, and with
  `--start-api` refuses a held port instead of handing it to the
  stale-instance recovery that frees a port by killing its holder.
  `POST /v1/voice/transcribe` reads the stream itself: FastAPI's
  `UploadFile` had parsed and spooled the whole body before the 20 MB cap
  was checked. `nvh voice --download` repairs an incomplete snapshot
  instead of reporting "already downloaded" whenever any `model.bin`
  exists. The mic tails a download another tab started (it read
  `status.job_id`; the server sends `download_job_id`), keeps the in-flight
  flag after a 503 instead of overwriting it with the bare snapshot, says
  `$0, on this device` after a local transcript instead of `$0.0000`, and
  swallows Space auto-repeat while recording (every repeat keydown had
  typed a space into the box). Test hygiene: no test patches
  `sys.platform` process-wide any more (the voice tests patch
  `platform_has_wheel`), the non-blocking-loop test waits on an event
  instead of a sleep, the stale event-loop scaffolding around the Groq-key
  test is gone, and the web pins match `new MediaRecorder(` and
  `window.speechSynthesis.speak(` rather than a header comment.

### Security
- **Two routes that answered without the key now require it.**
  `POST /v1/anthropic/messages` (and its alias `/v1/anthropic/v1/messages`)
  and `GET /v1/integrations/scan`, which returns the config paths of the
  tools it detects, go through `require_auth` like the rest. A keyed
  deployment that points an Anthropic-compatible client at nvHive sends the
  key as a bearer token (`ANTHROPIC_AUTH_TOKEN=<key>`); the SDKs' `x-api-key`
  header is not a credential nvHive reads. In open mode nothing changes.
- **`/v1/health` discloses `bind_host`, `auth_mode` and `key_required` on
  purpose:** anyone who can reach the port learns them from the connection
  and the 401 anyway, and `nvh launch` needs them to refuse an off-loopback
  open-mode API before pointing a coding tool at it. SECURITY.md says so.
- **Every vault memory write is redacted** (best-effort pattern pass; the
  confirm card and the saved file remain the human check);
  `delete_vault_note` refuses ids it does not know and paths that resolve
  outside the vault root, and `index_vault_note` refuses files outside
  `$NVH_HOME/vault`.
- **The streamed-budget bypass is closed** (see Changed): a coding tool
  streaming through the proxy can no longer spend past `budget.hard_stop`
  unrecorded.

- **Nothing unattended mints a token.** A scheduled Wizard turn runs with
  no human present: it never calls a confirm or privileged tool, never
  mints, plans or stores an approval token, and never asks a question
  (`ask_user` is not offered to it). A confirm or privileged request the
  profile's own whitelist allows is recorded on the run with its string
  arguments passed through `redact_secrets` before any job event is
  written, and a request outside the whitelist is refused like any other
  call (`tests/test_schedule_runner.py` greps every byte under `jobs/`).
  With a human present, a confirm or privileged call made in the same turn
  as an `ask_user` question is held in `deferred_tool_calls`, with no card,
  no plan and no token, instead of being surfaced beside an unanswered
  question.
- **Generated images, vault notes and schedules are served only through
  `require_auth`.** `GET /v1/outputs/images/{scope}/{filename}` validates
  `scope` and `filename` by regex and confines the resolved path to
  `outputs/images/<scope>` (404 for `..`, a foreign scope, an unknown
  suffix, an escaping symlink or a missing file); the `/v1/vault/notes`
  routes take no `home_dir` and match ids against the notes' own `Id:`
  lines; the seven `/v1/schedules` routes refuse `mode: do` with a 422
  before anything is stored. A keyed deployment answers 401 to all of them
  without the key.
- **`remember` writes only under the process home's `vault/Wizard
  Memory/`** (a model-supplied `home_dir` or `category` is dropped), refuses
  a body carrying `approval_token` or the privileged terminal hint, and
  runs `redact_secrets` over the note before it is written; pattern
  redaction is best-effort, so the confirm card showing the note is the
  check, and every record is listed and deletable through `/v1/vault/notes`
  and `nvh vault forget <id>`.
- **The `generate_image` profile pin cannot be chosen by the model**: an
  unknown or missing profile is a refusal, never "no ceiling", and the card
  pins the request's `home_dir` beside the profile so Run resolves the
  profile the chat did. A ComfyUI runtime file naming a non-loopback host
  is ignored by the probe and the renderers, so the image path stays local;
  `upgrade_vault_note` refuses a path outside the vault; `/v1/smart`
  refuses the confirm-class `imagine` and never bills an image key
  unattended. SECURITY.md documents the pin's trust boundary and the
  absolute `$NVH_HOME` paths API responses carry.
- **The web's image fetch is pinned to the server's URL shape.**
  `isImageResult` accepts only the exact `/v1/outputs/images/<scope>/<file>`
  form the server mints (no dot segments, query, fragment or extra depth),
  so the authenticated fetch cannot be steered at another route; the bytes
  travel through the same auth-header path every API call uses, to the API
  origin only. The question card never reaches the tool-execute route and
  mints nothing; a confirm card's Run echoes the server-pinned `profile`
  and `profile_home` unchanged.
- **Speech never leaves the machine unless you chose a path that sends
  it.** Every `/v1/voice` route requires the API credential. Audio is never
  stored: the clip is a temporary file unlinked on every path, no request
  can name a path on disk, and the browser keeps no object URL or storage
  of a recording. The clip goes to Groq only when the person pressed the
  mic (or ran `nvh voice`), the local path is absent and a Groq key is
  configured — the status line says which path will answer before you
  press. Over HTTP `provider` is `auto`, `local` or `groq` (`whisper-cli`
  is the CLI's alone) and `model` is a faster-whisper size or a `Systran/`
  / `mobiuslabsgmbh/` repo — a bare name is never read as a directory under
  the server's working directory, so a caller cannot make
  `POST /v1/voice/download` fetch an arbitrary repository or probe the
  server's folders. The weights download only on an explicit request. The
  system text-to-speech fallbacks (`espeak`, `say`) read the text from
  stdin and edge-tts gets `--text=`, so caller text is never an argument.
- **`nvh launch` renders only nvHive's own credential.** The environment
  or snippet it builds holds the row's variables with the API base and
  `HIVE_API_KEY` (or the `nvhive` placeholder in open mode); no provider
  key is read or rendered, no shell is involved, and the four refusals
  above run client-side before a tool is pointed at the server. `--print`,
  a snippet-kind target and `--inference-proxy` show the live
  `HIVE_API_KEY` in clear text on the terminal — keep that output out of
  pasted logs. `--start-api` never frees a port something else holds.
- **The first real `pip-audit` result is read and recorded.** With `--strict`
  gone the `security` job's audit lists thirteen advisories against
  `filelock` 3.32.7 (CVE-2026-112907 through CVE-2026-112940, fixed in
  4.2.1). nvHive names and imports no `filelock` API; the package arrives
  through `litellm`, which pins `filelock<4.0`, so the fix cannot be
  installed yet. The advisories need a local user able to plant names in
  the directory that holds a lock, and nvHive's caches live under the
  user-owned `NVH_HOME` layout, so the finding is knowingly accepted for
  0.46.0 (SECURITY.md records it) and the floor moves to `filelock>=4.2.1`
  when litellm lifts its pin.

### Removed
- **The pre-0.44 tool-call forms.** The agent loop no longer reads the
  fenced `tool_call` block or the bare `{"tool": ..., "args": {...}}` object
  (deprecated in 0.44, still parsed on the agent-loop path in 0.45).
  `nvh.core.tools.parse_tool_calls` lost its `legacy` parameter (passing it
  is a `TypeError`); the only grammar on every path is the `TOOL_CALL:
  {json}` line (the `{"tool", "args"}` key spelling inside that line is
  still read) plus a provider's native `tool_calls`. A fenced block or bare
  object a model still writes is ordinary text: it stays in the reply, runs
  no tool, and the loop treats the reply as its final answer.

## [0.45.0] - 2026-11-01

Refresh completion and Spark hygiene. The model catalog reads price, context
window and the tool / vision / JSON flags from LiteLLM at load (a row nothing
prices says "cost unknown" instead of a known $0), every LiteLLM-backed
provider's model list is live and `GET /v1/models` returns only what the
provider still serves where its live list can be read (a provider whose list
cannot be read, for want of a key or URL or on any probe error, passes its
catalog rows through unfiltered), the cloud catalog is regenerated with a
`source:` line on every override, and a weekly model-currency job in CI
covers the cloud defaults and the Ollama tags: the three refresh items
re-scoped from 0.44 are done, and the last hand-typed provider copies derive
from `ProviderSpec` behind a single-table guard. The Spark playbooks are
re-extracted from NVIDIA's current upstream READMEs and a drift check follows
them, every script taken from the upstream repositories is sha256-verified
(the ollama, Claude Code and LM Studio installers are downloaded, flagged
`unpinned` and run from disk, never piped), a run can be cancelled between
steps, the privileged tier refuses in plain words on Windows and macOS
before anything spawns, and `install.ps1` is ARM64-aware. `nvh agent`
reports a derived cost instead of a hardcoded `$0`, every test runs under a
throwaway home, and the installers write nvh's own config template so a
fresh install never ships a retired model id.

Not in this release: the web cost pill's "cost unknown" state, a
process-group kill of an in-flight playbook command (cancel takes effect
between steps; a running command is recorded, not stopped), the `--verify`
cross-check honouring `exclude_providers`, `setup_catalog`, the profiles and
`setup_agent` reading their bundled data through `nvh.data`, the removal of
the pre-0.44 fenced `tool_call` block and bare `{"tool": ..., "args": ...}`
object from the agent loop's parser (0.44 said one release; the loop still
accepts them under `legacy=True`), and the eight #133 add items; all of
these move to 0.46. A `LOCAL_MODEL_TIERS` review for qwen3.6 is an owner
decision still open, not 0.46 scope, and Phase 4 (the Spark binary and the
unified-memory probe) waits on hardware facts.

### Added
- **LiteLLM-derived model facts.** LiteLLM's model table is now the one
  source for price, context window, max output and the tool / vision / JSON
  flags of every catalog row; `nvh/config/capabilities.yaml` shrinks to the
  hand-maintained fields (display name, capability scores, latency,
  `status: retired` plus `successor`) and explicit overrides, each carrying a
  `source:`. New `nvh/config/model_overrides.json` holds sourced
  `litellm.register_model` payloads for the six spec defaults and fallbacks
  LiteLLM does not carry (AI21, NVIDIA NIM, OpenRouter gpt-oss), applied only
  while LiteLLM lacks the id. New `scripts/gen_capabilities.py` (`--check` in
  CI) normalises the catalog, derives retirement from `RETIRED_MODEL_RENAMES`
  and Ollama vision flags from the tier table, and refuses an override
  without a source; `tests/test_capabilities_catalog.py` pins that every spec
  default and fallback prices on LiteLLM 1.99.0 and 1.106.6. `ModelInfo`
  gains `supports_function_calling`, `deprecation_date` and `cost_known`: a
  row nothing prices loads as "cost unknown" (logged once per model), the
  router scores it neutrally rather than as free, and `nvh model list` shows
  "cost unknown" and a Status column.
- **Live model lists.** `list_models` on every LiteLLM-backed provider makes
  one `GET /models` (the spec's `models_probe`, 5 s timeout, cached for the
  health interval) shared with `health_check`; without a URL or key, or on
  any error, the static default/fallback pair is returned and the list is
  reported as unknown. `GET /v1/models` intersects the catalog with each
  provider's live list (exact or tail match), fails open on an unknown list
  and on `ResourcePaused`, hides retired rows and adds `warnings` for a
  configured default the provider no longer serves plus the
  `supports_function_calling`, `cost_known`, `deprecation_date` and
  `successor` fields; `GET /v1/proxy/models` lists the registry catalog. The
  probe streams and caps the body (8 MiB, 10k ids) and parses defensively.
  `nvh status --deep` gains a "Default models" finding, one network call per
  configured cloud provider shared with the advisor health probe.
- **Weekly model currency.** `.github/workflows/model-currency.yml` (Mondays
  and on dispatch, non-blocking): LLM7's anonymous model list, every Ollama
  tier tag, the catalog's overrides against today's LiteLLM and
  `gen_capabilities --check`; a keyed Groq/NVIDIA tier runs only when the
  `GROQ_API_KEY` and `NVIDIA_API_KEY` secrets exist and exposes them to the
  test step alone. `tests/test_model_currency.py` opts in with
  `NVH_NETWORK_TESTS=1`.
- **One provider table.** `ProviderSpec` is the only place a provider's
  display name, signup/docs URL, key variable and prefix, `GET /models` probe
  or signup friction is written (new fields `docs_url`, `logo_slug`,
  `key_prefixes`, `key_placeholder`, `models_probe`, `signup_tier`,
  `task_strengths`, `metered_upstream`), and the last hand-typed copies
  derive from it: the `nvh config init` advisors template, the API's
  key/URL/logo/default maps and `/v1/setup/free-providers` (every keyable
  provider including Grok and the optional LLM7 token, grouped by signup
  tier, with `free_info`, `default_model` and `fallback_model`), the
  `nvh setup` / `nvh keys` / `nvh advisor login` ladders, the key-format and
  `/models` probes (one `probe_models_endpoint` reusing the adapter's
  request), the agent matcher's static strengths and the web setup page's
  cloud list. New guard `tests/test_single_provider_table.py` fails when a
  provider fact is typed for two or more providers outside
  `nvh/providers/specs.py`.
- `docs/CONFIGURATION.md` "Model catalog" section; `docs/TESTING.md`
  "Catalog and model currency" recipe; `docs/MAINTAINERS.md` pre-tag steps
  for `gen_capabilities.py --check` and the latest model-currency run.

- **Spark playbooks follow today's upstream.** All playbooks are re-extracted
  from NVIDIA's current `nvidia/playbook-<name>/` READMEs (ids stable,
  `upstream_folder` recorded on every plan, catalogue row and receipt): the
  ollama playbook is the DGX Spark branch of the "Run Local LLMs" guide,
  comfy-ui the Z-Image-Turbo quick start, vllm the 128 GB Qwen3.8 27B NVFP4
  row, and the deferred reasons are re-read from each README. New
  `scripts/check_playbook_upstream.py` (`--check`, `--update`, `--show`)
  snapshots every folder README and pinned asset into
  `scripts/playbook_upstream_snapshot.json`; the weekly model-currency job
  runs the check, and any HTTP answer other than 200 or 404 is "unreachable"
  (exit 2, snapshot untouched), never false drift.
- **Verified downloads.** Every script a playbook step takes from the
  upstream repositories (ComfyUI's `setup.sh` and `launch.sh`, the vLLM
  launch script) is sha256-pinned in `UPSTREAM_ASSETS`, downloaded to
  `<file>.download` and moved into place only by the verify step, so the
  gate runs on every run; the VS Code `.deb` is verified against Microsoft's
  stable manifest with explicit exits under `python3 -I -c`
  (`PYTHONOPTIMIZE` cannot strip it) and its product version recorded. No
  step is `unpinned` except the documented pipe-to-shell installers (ollama,
  Claude Code, LM Studio), which are downloaded and run from disk.
- **Cancel between playbook steps.** The engine re-reads the job's cancel
  flag before each check probe, after it and after every step, passed or
  failed; the receipt says `stopped after step N` (an in-flight command is
  recorded as such), the vault note is written even when the job consumer
  closes the stream early, and the job log keeps the engine's own account.
- **Honest refusals off Linux.** `system_settings_apply`, `apt_install`,
  `snap_install`, `service_enable` and any `sudo` step refuse before
  anything spawns with one sentence ("System changes are not available on
  Windows yet", macOS variant), never prepend `sudo` and write no vault
  note; plans still render with the sentence as a note; `system_settings_get`
  exports `platform.os`, `sudo.host_probe_pending` and `system_changes`.
  `nvh playbook plan` / `install` refuse the same way (exit 1) while
  `playbook list` still shows the catalogue; the web setup page reads the
  host OS from `/v1/wizard/context` and shows the sentence in place of the
  Spark playbooks section.
- **Arch-aware `install.ps1`.** Detects Windows on Arm, prefers a native
  arm64 Python (falling back once to x64 under emulation when pip cannot
  build a dependency), installs Ollama's `ollama-windows-arm64.zip` verified
  against the release's `sha256sum.txt` instead of the x64 installer
  (`NVH_OLLAMA_VERSION` pins a release), says that GPU acceleration is
  unverified on Arm and prints an honest message when `nvidia-smi` is
  absent; the x64 path is unchanged. `ci/test-windows-installer.ps1` gains a
  mocked ARM64 case.
- **`nvh status --deep` "Legacy homes (pre-0.44)" row** (`legacy_homes`):
  a read-only view of the import (copied / merged / skipped counts, old
  roots still present, items a run would still copy, items startup could
  not copy with the reason, `NVH_LEGACY_MIGRATION=0` as a skipped row, a
  still-set `HIVE_DATA_DIR`), backed by `legacy_migration_status()` so a
  diagnostic can never start a second migration.
- **Derived agent cost.** Every agent-loop step carries the provider's
  reported `cost_usd` and `AgentResult.total_cost_usd` is the None-tolerant
  sum; `nvh agent` derives `CodingResult.total_cost_usd` from the plan,
  every worker, review round and quality-gate retry instead of a hardcoded
  `$0`. `nvh do`, `nvh agent run`, the REPL agent task and the API agent
  envelope print it.
- **One set of tool instructions under native function calling.** When the
  provider takes `tools=`, the Wizard's system prompt carries a short native
  block (safety-class rules, tool names by class, the TOOL_RESULT and
  privileged-outcome instructions) instead of the `TOOL_CALL:` text protocol
  and schema list; the channel is decided once per turn and both are still
  read. `Engine.query` / `query_stream` gain `exclude_providers`, honoured
  by the router, the offline and orchestrator picks and the fallback chain,
  and `nvh ask --focus research` passes the search-native set so a grounded
  or privacy-mode prompt never falls back to Perplexity.
- **Generated `docs/CONFIGURATION.md` blocks.** The Wizard tool roster (by
  safety class, from `default_registry()`) and the council cabinet table
  (from `COUNCIL_PRESETS` / `PRESET_DESCRIPTIONS`) are rendered by new
  `scripts/gen_config_doc.py` (`--check` in CI) and parity-tested by
  `tests/test_config_doc_parity.py`; the doc also documents the
  `legacy_homes` row, the privileged tier off Linux and the native prompt
  variant. The `--check` / rewrite contract every generator shares is one
  function, `scripts/_gendoc.check_or_write`; `scripts/gen_capabilities.py`
  uses it and gains `--drift` (the overrides-vs-LiteLLM report the weekly
  job prints, moved out of the test module).
- **Bundled data through `importlib.resources`.** New `nvh.data` helpers
  (`bundled`, `bundled_path`, `read_text`, `read_json`, `read_yaml`) replace
  `Path(__file__)` arithmetic in the catalog, benchmark and workflow
  loaders; `tests/test_single_root.py` pins that every `package-data` glob
  ships something, every data file is covered and no module anchors a path
  on `__file__` outside a two-row allow table.

### Changed
- **Installers write nvh's own config template.** `install.sh`, `install.ps1`
  and `install-mac.sh` no longer carry a hand-typed `config.yaml`: after the
  venv install they run the installed package's `generate_default_config()`
  (the file `nvh config init` writes, every advisor stanza rendered from the
  provider spec table, so LLM7 starts on the served `gpt-oss:20b` rather than
  the retired id the heredocs still shipped) and then rewrite the whole Ollama
  `default_model` line with the machine's `nvh models tiers` pick as before.
  The Ollama stanza, which the template writes disabled, is switched on
  wherever the installer sets Ollama up (every Linux install, a Windows box
  with an NVIDIA GPU, an Apple Silicon Mac), tier pick or not; Intel Macs and
  GPU-less Windows keep it as nvh renders it. A failed or blank render writes
  nothing, prints one line naming `nvh config init` (and, on Linux, the
  `pip-install.log` that holds the reason) and the install continues; the
  rewrite is atomic and a failed rewrite leaves the rendered file in place and
  says so, on fresh installs and on the re-run path alike. A fresh install's
  config therefore also picks up the template's long-standing defaults the
  heredocs had drifted from: budget 5 USD/day and 20 USD/month with a 0.80
  alert threshold, Ollama at `127.0.0.1:11434`, all 22 advisor stanzas (cloud
  ones disabled until a key is added) and the council, routing and logging
  sections. `tests/test_release_hardening.py` pins the contract for all three
  installers and runs each shell installer's rewrite against the real
  template; `ci/test-windows-installer.ps1` asserts the stderr merge, the
  exit-code read, the UTF-8 swap-and-restore and the BOM-free write (23
  cases).
- Importing `nvh` exports `LITELLM_LOCAL_MODEL_COST_MAP=True` unless the
  variable is already set: LiteLLM's table is the one bundled with the
  installed release, no price map is fetched from GitHub at import (an
  offline or firewalled box waited on that timeout), and the catalog, the
  bill and the weekly report describe the same table. Export `False` before
  importing `nvh` for the live map.
- Hugging Face rows load as "cost unknown" (`ProviderSpec.metered_upstream`)
  instead of a known $0 and are no longer registered with LiteLLM, so a
  catalog load makes no network request.
- `gemini/gemini-2.0-pro`, `groq/mixtral-8x7b-32768` and `xai/grok-3` (no
  LiteLLM price on 1.99.0 or 1.106.6) are retired with successors;
  `nvh config migrate` rewrites them and `nvh status --deep` flags them. The
  catalog keeps its 80 non-mock rows.
- Generated config: the Google, Cohere, Perplexity and Fireworks key
  references carry their alternate variables as nested `${PRIMARY:-${ALT}}`
  defaults, which env interpolation now resolves with brace matching (a set
  primary no longer yields a key with a trailing brace); Ollama's `base_url`
  is `http://127.0.0.1:11434`; Triton's row reads `TRITON_URL` then
  `TRITON_ENDPOINT` with the one default the adapter uses.
- `nvh/config/model_overrides.json` ships in the wheel (`config/*.json`
  package data).

- LLM7 currency from its anonymous model list (2026-11-01): default
  `gpt-oss:20b`, fallback `minimax-m3`; the bare `gpt-oss` and `minimax-m2.7`
  ids are retired with successors in `RETIRED_MODEL_RENAMES` and the catalog
  (`nvh config migrate` rewrites them), the llm7 strengths copy no longer
  names DeepSeek-R1, and `docs/PROVIDERS.md` is regenerated.
- The cloud vision fallback (`nvh/core/vision_tools.py`) and the web
  providers page's `.env` example derive from the spec table; the
  single-provider-table guard scans every `web/app/**/*.tsx` page.
- Tests: every test runs under a throwaway home. The autouse
  `_hermetic_home` fixture in `tests/conftest.py` points `NVH_HOME` at a
  fresh per-test directory, fakes `Path.home()` / `HOME` / `USERPROFILE`,
  clears the storage knobs and re-derives the settings defaults, so no test
  reads or writes the developer's `~/.nvh`, `~/.cursor`, `~/.hive` or
  `~/.council` (the API's `require_auth` used to open the real state
  database on every TestClient request); a module opts out with
  `HERMETIC_HOME = False`. The per-file sweep before and after shows no
  regression. The platform-detector tests assert real detection against
  config files in the fake home with no `claude mcp list` subprocess.
- `docs/GETTING_STARTED.md` Spark playbooks section (upstream folders and
  the drift check, checksums, cancelling, Linux only) and its "Windows on
  Arm" paragraph (the arch-aware `install.ps1`, Ollama only when an NVIDIA
  GPU is detected, device settings refusing off Linux); `docs/MAINTAINERS.md`
  pre-tag steps name `gen_config_doc.py`, gain a
  `check_playbook_upstream.py --check` step (exit 2 is unreachable, not
  drift) and a security step (`SECURITY.md` re-read against the release, the
  `security` job's `pip-audit` findings read, no red Dependabot pull
  request), describe `installer.yml`'s push-to-`main` trigger and say that
  pre-tag fixes land in their own commits before the release commit;
  `docs/TESTING.md` documents the three autouse fixtures, the opt-outs and
  `gen_capabilities.py --drift`.

### Fixed
- `install.ps1` writes `config.yaml` as UTF-8 without a byte-order mark
  (Windows PowerShell 5.1's `Set-Content -Encoding UTF8` wrote one) and tests
  the config path literally, so an existing config under a bracketed
  `NVH_HOME` is never overwritten. The config loader reads `config.yaml` as
  UTF-8 with an optional BOM and `save_config` writes UTF-8 explicitly, so a
  file saved by a BOM-writing editor no longer loads its first key as
  `\ufeffversion` and non-ASCII values round-trip on every locale.
- The `GET /models` probe body is streamed and capped, a deeply nested body
  no longer escapes `health_check` as a RecursionError, and the key never
  lands in the probe URL or the health error text; the `httpx` logger stays
  at WARNING in the CLI, the API server and `setup_logging` so request URLs
  with query strings are not logged.
- Dead `_PROVIDER_KEY_URLS` / `_PROVIDER_DOC_URLS` / `_PROVIDER_LOGO_SLUGS`
  maps removed from the API server; a bare `deprecation_date:` YAML override
  now loads; `docs/COMMANDS.md` regenerated (`Ollama (local)`).

- The council drawer's cabinet picker renders again: `GET /v1/agents/presets`
  answers with a dict but the web typed it as a list, so the Preset select
  never appeared; `web/lib/cabinets.ts` adapts the dict in server order and
  the `CabinetPicker` component is tested from a server-shaped payload.
- `PRESET_DESCRIPTIONS` name the actual members of each council preset (the
  `executive` blurb named roles it does not hold, `full_board` said "all");
  the MCP `list_cabinets` table and the generated doc show the corrected
  copy.
- The workflow and benchmark YAML loaders read UTF-8 regardless of the
  locale codec.
- Three module-level `_hermetic_home` fixtures that shadowed the shared one
  are removed and the opt-out is pinned by
  `tests/test_conftest_hermetic_home_optout.py`.

## [0.44.1] - 2026-11-01

Currency and loopback defaults; no feature changes.

### Added
- **SECURITY.md**: the trust boundary as the code enforces it (open mode and
  `HIVE_API_KEY`, loopback defaults, `HIVE_CORS_ORIGINS` /
  `HIVE_CORS_ORIGIN_REGEX`, the privileged tier with approval tokens and
  Host/Origin refusals, both deny lists, Docker-only `run_code` from chat,
  the vault audit) and how to report a vulnerability; linked from the README.
  PRIVACY.md and SECURITY.md record that the `nvhive[mcp]` extra's
  `opentelemetry-api` (mcp 2.x line only) is API-only with no exporter and
  no telemetry egress.
- `nvh status` prints the installed `mcp` SDK version on one line when the
  package imports (`nvh.integrations.mcp_client.sdk_version()`).
- `tests/test_litellm_surface.py` pins the LiteLLM surface the provider
  layer relies on (`aresponses`, `get_llm_provider`'s 4-tuple, `model_cost`,
  `register_model`, `supports_function_calling`, `supports_vision`) without
  network, on both 1.99.0 and 1.106.6, and documents the default/fallback
  ids LiteLLM's model DB does not price (they bill from the
  `capabilities.yaml` rates). LiteLLM is verified at 1.106.6; the floor
  stays `>=1.99`.

### Changed
- **The `mcp` extra spans both SDK majors**: `mcp>=1.0,<3` (the `[cli]` extra
  is dropped; nvHive never shells out to `mcp dev`). `nvh/mcp_server.py`
  binds `mcp.server.mcpserver.MCPServer` (2.x) before
  `mcp.server.fastmcp.FastMCP` (1.x), probes constructor keywords and
  advertises `version=nvh.__version__` where accepted;
  `nvh/integrations/mcp_client.py` reads `input_schema` / `is_error` before
  `inputSchema` / `isError` and extracts text from `.content` blocks.
  Pinned by fake-SDK tests plus the stdio round trip on mcp 1.30.0 and 2.3.0.
  Dependabot #140 is superseded.
- CI: `workflow_dispatch` for manual re-runs, `--durations=25` on the test
  matrix, an `mcp-v1` job that runs the MCP test files on the 1.x SDK so both
  branches of the shim stay exercised, the `security` job installs the
  project before `pip-audit` so the audit covers nvHive's resolved
  dependency set, and the installer contracts also run on pushes to main
  that touch an installer; the workflow shapes are pinned by
  `tests/test_release_hardening.py`.
- Tests: a registered `slow` marker (`-m "not slow"` is the quick local run)
  on the subprocess-heavy CLI, live-uvicorn and stdio MCP tests; the
  LiteLLM live-map check is `network`-gated; test processes use LiteLLM's
  bundled model DB (`LITELLM_LOCAL_MODEL_COST_MAP`) instead of fetching it.
- docs/INTEGRATIONS.md and the CLI hint now say `pip install "nvhive[mcp]"`
  and show the loopback default for `nvh mcp -t streamable-http`.

### Fixed
- `nvh mcp -t streamable-http` no longer fails with `TypeError` on the mcp
  1.x SDK: the bind is applied through `settings.host` / `settings.port` on
  1.x and forwarded as `run(host=, port=)` on 2.x, pinned for both shapes.
- The streaming Wizard cost ceiling is pinned across two iterations.

### Security
- `run_server` and the MCP HTTP transport (`nvh mcp -t streamable-http`,
  `nvh openclaw --start --http`) bind `127.0.0.1` by default; both MCP
  commands gain `--host`, and exposing any of them on the LAN takes an
  explicit `--host 0.0.0.0`. The WebUI (Next.js) still binds every
  interface; SECURITY.md says so.
- The privileged tier's bind-host refusal treats "no `HIVE_API_KEY`" as open
  mode regardless of registered users (`_auth_open_mode` mirrors
  `require_auth`).

## [0.44.0] - 2026-10-31

The first release published since 0.42.0. It ships everything merged to main
since then, including the 0.43.0 section below, which was merged on
2026-09-03 but never tagged or published. Refresh wave 1 lands (#132):
provider facts live on `ProviderSpec` and `docs/PROVIDERS.md` is generated
from them, `NVH_HOME` is the only root with a one-shot migration of the
legacy homes, and one `Tool` / `ToolRegistry` serves the agent and the
Wizard, with native function calling where the model can take it. The Spark
concierge gains its privileged tier (Phase 2a: system settings tools behind
red approval cards, `NVH_ALLOW_PRIVILEGED=0` as the kill switch), the Spark
playbooks as approved, audited runs (Phase 2b) and the bridge that brings
`shell`, `run_code` and image analysis into the Wizard chat (Phase 3). Spark
playbooks are extracted from upstream READMEs and have not yet been
exercised by maintainers on DGX Spark hardware. PRs #138, #141 and #142 add
council correctness fixes, legacy-migration and installer hardening, and a
shared-compute admission boundary, disabled by default; no broker ships (the
`atohi:` stanza and `nvh/core/atohi.py`). This release is pilot-ready, not
production-ready: the target-VM acceptance checklist in
[docs/MAINTAINERS.md](docs/MAINTAINERS.md#production-readiness) has not been
run on this tree.

### Added
- **Privileged Wizard tools (Spark concierge Phase 2a).** A third safety
  class, `privileged`, for changes to the machine nvHive runs on. An
  unconfirmed call returns a red-card payload with the exact command plan; a
  confirmed call runs and is recorded under the vault's `Decisions/` with the
  commands, exit codes, redacted output and the device label, and the result
  is fitted to the tool-result window. `NVH_ALLOW_PRIVILEGED=0` (or
  false/no/off) disables the tier at the single enforcement point
  (`WizardToolRegistry.execute`) on both the card and the confirmed path;
  the tools stay listed with `enabled: false`, and `/v1/wizard/tools`
  reports `privileged_count` / `privileged_enabled`
  ([proposal](docs/proposals/SPARK_CONCIERGE_2026-09.md) §3.4, #137).
- **System settings tools** (`nvh/integrations/wizard/system_settings.py`):
  `system_settings_get` and `system_settings_plan` (auto, read-only) plus
  `system_settings_apply`, `apt_install`, `snap_install` and
  `service_enable` (privileged) over a fixed catalogue — the headless
  auto-suspend fix, the docker group, SSH, a Tailscale-only firewall, the
  hostname, and an `apt-mark hold` of the installed NVIDIA driver packages.
  The host runner uses `sudo -n` only where passwordless sudo was probed,
  hands sudo-group users the exact command for a terminal, refuses
  shutdown/reboot, disk formatting, firewall-off, driver removal, a bare
  `apt upgrade` and `rm` outside `NVH_HOME` in code, and never prompts for,
  sees or stores a password. `apt_install` refuses NVIDIA driver,
  `cuda-drivers` and kernel packages with the DGX OS warning. 78 hermetic
  tests (faked subprocess, seeded platform facts, `NVH_HOME` in tmp).
- **Red approval card in the WebUI.** A privileged call renders a red card:
  a PRIVILEGED badge, a "runs with sudo on this machine" note, the exact
  commands in a `<pre>` block and an "Approve and run" button. The card
  never auto-runs and never previews: showing the plan costs no execute
  call, so the browser never sends an unconfirmed privileged call. When
  sudo needs a password, nvHive hands over the exact command in a copyable
  block instead of running it (a hand-off, not an error);
  `NVH_ALLOW_PRIVILEGED=0` greys the card and removes its Run button; an
  unknown safety class still renders as an ordinary confirm card. Pure
  helpers in `web/lib/privileged.ts` with 22 node tests; the mascot's bubble
  says the Wizard needs approval for a privileged change.
- **`device-settings` Agent Library profile** (Ops; the library is now 105
  profiles): a protective DGX OS / Windows settings helper that reads the
  platform block, says whether it can elevate or only hand over the
  command, and walks get → plan → apply with the undo shown before the red
  card. It knows the DGX OS traps — no bare `apt upgrade`, the GDM greeter
  suspending a headless Spark, the OOBE user's missing docker group, UFW
  scoped to `tailscale0`, per-user Wi-Fi — and never asks for or accepts a
  password.
- **A shared-compute admission boundary, disabled by default; no broker
  ships** (#141). `nvh/core/atohi.py` defines the `AdmissionBroker`
  protocol, `ResourcePaused` and a per-engine admission policy, configured
  by the top-level `atohi:` stanza (`enabled: false`,
  `managed_providers: []`; profile overlays cannot replace it). Enabled, the
  Ollama and Triton provider types, any provider named in
  `managed_providers`, local RAG embeddings, built-in vision and the
  benchmark helpers require admission, and the API and Wizard pass the
  owning Engine's policy through ingestion, vault indexing, retrieval and
  recall; each Engine binds its own policy, and a bound policy cannot be
  changed afterwards (create a new scoped registry or restart). Revocation
  or uncertain ownership raises `ResourcePaused` outside the ordinary
  provider errors: chat never falls back to cloud, embeddings never retry or
  auto-pull, councils cancel and drain their cooperating tasks, CLI async
  commands exit 1, HTTP query, council, comparison, Wizard and RAG endpoints
  return 409 with code `resource_paused`, status `paused` and
  `automatic_retry: false`, streaming HTTP and WebSocket endpoints send a
  terminal error carrying the same fields and no completion event, and the
  compatible chat APIs report the pause the same way with no success marker.
  Enabled without a broker it fails closed (`broker_unavailable`): the
  shipped application supplies no broker, endpoint, token or granted flag,
  so leave it disabled
  ([docs/CONFIGURATION.md](docs/CONFIGURATION.md#atohi-admission-disabled)).
- **Managed model calls bind to allocation-owned transports** (#142). In
  shared mode a call reaches a model only through a typed, in-process
  `ModelSession` issued by the trusted broker: one provider, model and
  operation (complete, stream, health check or embeddings) bound to a
  job/allocation/generation identity, a process-local monotonic lease
  deadline and the issuer's `OwnedModelTransport`. Registry providers, local
  RAG embeddings and built-in vision use only that transport; they never
  retarget a shared URL, auto-pull or fall back to another model after a
  bound call fails, and automatic model selection is refused on this path.
  A missing, mismatched, replaced, expired or revoked session pauses the
  call with `ResourcePaused`, including revocation during cleanup before
  the final result or final stream chunk. Disabled mode keeps the ordinary
  providers unchanged. This is the application-side dispatch boundary only:
  the production broker and session issuer, and any proof of physical GPU
  release, remain outstanding.

### Changed
- The Wizard concierge routes settings questions to `device-settings`: the
  docker *group* moves there from the container wrangler (the daemon and
  its socket errors stay), while hardware faults, pip installs, model picks
  and claims stay with their own specialists. A new `privileged_allowed`
  state predicate honours `NVH_ALLOW_PRIVILEGED` and `in_sudo_group`, so a
  stock DGX OS owner who needs a password is still offered the change. The
  routing probe grows from 70 to 82 rows.
- The Wizard's tools-block rule for `privileged` tools: explain the exact
  command and why, the user approves a red card, show a hand-off command
  verbatim, never ask for a password. `docs/CONFIGURATION.md` documents
  `NVH_ALLOW_PRIVILEGED`.

- **Spark playbooks** (`nvh/integrations/installs/playbooks.py`): the
  first tier of NVIDIA's official DGX Spark playbooks — `ollama`,
  `cli-coding-agent`, `open-webui`, `comfy-ui`, `dgx-dashboard`, `vscode`,
  `tailscale`, `vllm`, `llama-cpp`, `lm-studio`, `openclaw`, `nemoclaw` (ids
  are the upstream folder names) — as approved, audited runs. Steps compile
  into the system-settings plan, so the deny list, the sudo matrix, closed
  stdin, timeouts and redaction are inherited; a test renders every step,
  check and verify command and asserts none is denied. Policies baked in:
  pipe-to-shell installers are downloaded under `NVH_HOME` and run from the
  file, shown verbatim and flagged "pipe-to-shell: unpinned" (`comfy-ui`
  verifies the README's sha256); Docker playbooks start with a
  `usermod -aG docker` step and halt for the re-login (never `newgrp`);
  browser logins, TUIs and foreground servers stay manual; undo is preview
  text; the DGX Dashboard "Update Now" path is never automated; `HF_TOKEN` /
  `NGC_API_KEY` are declared, never prompted for. `sglang`, `nim-llm`,
  `nemotron`, `unsloth`, `connect-two-sparks` and `connect-to-your-spark` are
  listed as deferred with reasons. Wizard tools `playbook_list` and
  `playbook_plan` (auto) and `playbook_install` (privileged: red card with the
  compiled plan, then a `playbook-run` job that streams each step); receipts
  of kind `playbook` with an honest `no_root`; every run that touched the
  host is recorded under the vault's `Decisions/`. When sudo needs a
  password the hand-off is one command: `nvh playbook install <id>`.
- **`nvh playbook list | plan <id> | install <id> [-y]`** brings the same
  playbooks to the CLI: `list` is a table (sudo steps, manual steps,
  estimated time, installed from the receipt) plus the deferred guides with
  their reason; `plan` prints every command tagged sudo / user / manual with
  its skip check, then verify and undo (preview only); `install` prints the
  plan the Wizard card shows, asks once, runs the steps with `sudo`
  prompting in your own terminal (nvHive never sees the password), streams
  each step and ends with the receipt path and the audit path. The CLI path
  is not gated by `NVH_ALLOW_PRIVILEGED` (documented): in a terminal, sudo
  asks you directly. `docs/COMMANDS.md` regenerated; GETTING_STARTED gains a
  "Spark playbooks" section.
- **`app-installer` Agent Library profile** (Ops; the library is now 106
  profiles): the playbooks' guide — lists what ships, prefers a rootless
  pack when one exists, shows the compiled plan (sudo steps, manual steps,
  time, disk, undo) and installs one app at a time behind the red card. The
  Setup Concierge gains the read-only `playbook_list` / `playbook_plan` so
  the first-run tour can propose an install and hand the run to the App
  Installer. Concierge routing: "install ollama on my spark", "set up open
  webui on this machine", "which playbooks can I install?" route to the App
  Installer; a bare "install ollama" keeps its old route; models, Python
  packaging, faults, settings and the smart home keep their owners.
- **WebUI: Spark playbooks.** The red approval card follows a confirmed
  `playbook_install` into its job and shows a live step log (step, command,
  exit code, throttled log lines) ending in Done, "Stopped — your turn"
  (the docker-group re-login halt), "Stopped: needs a terminal — run:
  `nvh playbook install <id>`" (copyable), or "Failed at step N". The setup
  page's AI Studio step gains a "Spark playbooks" section below the pack
  grid: catalogue cards (sudo and manual-step chips, estimated time and
  disk, rootless-alternative link, INSTALLED from the receipt) with Plan →
  red approval → Approve and run streaming the same job. Nothing runs
  without a click; `NVH_ALLOW_PRIVILEGED=0` hides the run button. Shared
  `PlaybookRunLog` / `PlanDetails` components; 32 new node tests.

- **The bridge (Spark concierge Phase 3): code and images from the Wizard
  chat.** The agent's `shell` and `run_code` reach the Wizard behind
  approval cards (`nvh/integrations/wizard/sandbox_tools.py`). `shell` is
  privileged: the red card renders the exact command, the working
  directory, the timeout and the isolation the run will actually get
  (Docker sandbox, or "directly on this machine as <user>, no Docker
  isolation" with a warning); the isolation is pinned into the approved call
  and re-checked at run time; both deny lists refuse before anything spawns
  and see inside `sh -c` / `&&` / `|` chains and wrappers; `sudo`, `su`,
  `doas` are refused outright; the host fallback closes stdin and strips
  keys and tokens from the environment; output is redacted before it is
  cut; every run is written to the vault under `Decisions/`. `run_code` is
  confirm-class and Docker-only: without Docker it refuses in band, names
  Docker, points at the Open WebUI / vLLM playbooks and the terminal, and
  executes nothing. The core `ToolRegistry` guardrail hook read `command`
  for `run_code` too, so snippets had no blocklist coverage; it now checks
  `code` (pinned by tests). JSON-Schema parameters are translated into the
  Wizard shape by one helper (`parameters_from_json_schema`).
- **The Wizard can look at images.** `analyze_image` and
  `read_text_from_image` (auto-class, `nvh/integrations/wizard/vision_bridge.py`)
  bridge the core vision tools behind a path allowlist: only real images
  (extension and signature) under `$NVH_HOME/rag/uploads`, the agent
  workspace (`NVH_PROJECTS`) or directly in the OS temp folder (screenshots)
  are read, symlinks are resolved first, the guardrails' sensitive-file list
  applies, and bytes go to a cloud vision model only for images attached in
  the chat; anything else is an in-band refusal naming the allowed roots.
  Results keep the `[Vision: <model>, N KB]` line and add `model`,
  `provider` and `bytes`. `POST /v1/wizard/chat` and `/chat/stream` accept
  `attachments` (the `/v1/query` shape; at most 6 images, 20 MB each,
  png/jpeg/gif/webp/bmp with a matching signature); images land under
  `$NVH_HOME/rag/uploads/wizard/<conversation id>/` with safe names, the
  user turn gains one line naming the paths, and the response echoes
  `attachment_paths` so follow-up turns keep them.
- **WebUI: images in the composer.** Drop, paste or click the paperclip to
  attach up to six images; they show as chips with a thumbnail, travel as
  `attachments` on the next turn (stream and non-stream) and appear in your
  bubble, while dropped documents still go to RAG ingest. An in-band
  `refused` answer (run_code without Docker, a vision path outside the
  allowed roots) reads "Refused — nothing ran" in amber instead of
  "Failed"; the shell card's isolation line renders through the plan notes.
  Pure helpers in `web/lib/attachments.ts` with node tests.
  `docs/CONFIGURATION.md` documents the four bridged tools and the
  `attachments` field.

- **Provider facts live on `ProviderSpec` (#132 a).** Display name, key
  variable, signup and billing URLs, free tier and its note, ladder rank,
  cost tier, routing weights, strengths/weaknesses and model families are
  fields on each `nvh/providers/specs.py` row (plus `BESPOKE_SPECS` for
  Ollama, Triton and Mock and the `PROVIDER_FACTS` union).
  `FREE_TIER_ADVISORS`, `PROVIDER_QUOTAS`, `ADVISOR_PROFILES` (now the spec
  rows themselves) and the OpenAI-compatible proxy's model→provider map are
  derived from those rows; the proxy also asks `litellm.get_llm_provider()`
  for ids the specs do not name. `docs/PROVIDERS.md`'s provider table and
  free-tier ladder are generated by `scripts/gen_providers_doc.py`
  (`--check` in `tests/test_provider_docs_parity.py`); the generator
  plumbing is shared with `gen_models_doc.py` in `scripts/_gendoc.py`. Quota
  text keeps provider specifics (Gemini's daily cap, NIM's non-expiring
  credits, DeepSeek's input price) via per-spec `reset_hint` /
  `upgrade_hint`. The remaining hand-typed copies (the config template, the
  server's key/URL maps, the CLI's `KNOWN_ADVISORS`, the setup page's
  provider cards) follow in the next round.
- **`NVH_HOME` is the only root (#132 e).** `storage_layout()` is the single
  path oracle: `settings.DEFAULT_CONFIG_DIR` derives from its `config_dir`,
  and the state DB, schedules, `last_query.json`, REPL memories, workflows,
  plugins, `user.json`, `global_context.md`, benchmark exports and the
  NVIDIA bug report all live under it. `HIVE_DATA_DIR` is retired (use
  `NVH_STATE`), `NVH_CONFIG` is the config override (`HIVE_CONFIG_HOME` kept
  as an alias), and `StorageLayout` gains `plugins_dir` (`NVH_PLUGINS`). A
  one-shot legacy migration (`nvh/integrations/workspace/migrate_legacy.py`,
  the only module that spells the old names) copies `~/.hive`,
  `~/.council/council.db` (with WAL sidecars) and
  `~/nvh/nvidia-bug-report.log.gz` into the layout once: symlinks stay
  links, `~/.hive/.env` keys and `config.yaml` stanzas the layout lacks are
  merged in (with `.pre-0.44-merge` backups), a failed copy is retried next
  time, nothing is written to or deleted from the old roots, and
  `$NVH_STATE/legacy-migration.json` records what moved;
  `NVH_LEGACY_MIGRATION=0` turns it off (the test suite runs with it off).
  Project overlays are `.nvh.yaml` / `.nvh/config.yaml`, `.nvh/context|rules`,
  `.nvh.md`, `.nvh/workflows` (the `.hive` names still read as fallbacks).
  `install.ps1` and `install-mac.sh` honour `NVH_HOME` like `install.sh`. A
  repo-wide guard (`tests/test_single_root.py`) fails on any new legacy path.
- **One `Tool`, one `ToolRegistry` (#132 f).** `nvh.core.tools.Tool` carries
  `safety_class` auto | confirm | privileged (`safe` derived), a JSON-Schema
  `input_schema` whose prompt/UI shape is derived by one translation helper,
  `summary_template` and `planner`; `ToolRegistry.execute()` is the single
  enforcement point (kill switch, cards, 15-minute single-use approval
  tokens, vault audit, tool window) and runs the core guardrails on the
  right argument per tool; `WizardTool` / `WizardToolRegistry` are thin
  subclasses and a Wizard tool must declare its safety class (a plugin that
  forgets it is skipped, never auto). Two instances remain: the agent
  registry for `nvh do` / the REPL and the Wizard's curated set; desktop
  tools never enter the Wizard's. One plugin walk (`$NVH_HOME/plugins/`,
  `[project.entry-points."nvhive.plugins"]`) is shared by `nvh plugins` and
  the Wizard, and provider plugins are never executed by a chat turn. Native
  function calling degrades: `Provider.complete/stream` take `tools=` /
  `tool_choice=`, responses and stream chunks carry `tool_calls`, `nvh do`
  offers its catalogue natively where the model can take it (never on the
  Responses surface; a query carrying tools bypasses the response cache),
  and the text protocol remains the fallback. One text protocol: the Wizard
  parses only `TOOL_CALL: {json}` (prose mentions untouched); the agent loop
  keeps the fenced form for one release. One tool-description prompt; one
  `web_search` implementation (`NVH_SEARXNG_URL` / `BRAVE_API_KEY`, no public
  SearXNG default). Every tool result is redacted and capped whatever its
  shape. MCP tools can join the agent registry with `NVH_AGENT_MCP_TOOLS=1`.

### Fixed
- MCP and OpenAI/Anthropic-compatible throwdown requests now share an
  actual two-round analysis/critique workflow with a final answer. Both
  rounds and their reported costs are retained; failed quorum stops further
  stages (#138).
- Majority voting now counts matching answers, labels ties and pluralities,
  and uses weights only to break ties. Streaming honors the selected
  strategy (#138).
- Council accounting includes agreement calls, intermediate syntheses and
  reported partial-stream usage; total latency includes synthesis and
  retries. Compatible API usage and the budget ledger include this
  auxiliary work (#138).
- **Legacy-home migration hardening** (#138). The one-shot import of the
  pre-0.44 homes ("Legacy homes (pre-0.44)" in
  [docs/CONFIGURATION.md](docs/CONFIGURATION.md#legacy-homes-pre-044))
  copies the state database as a consistent SQLite snapshot through the
  backup API, so committed WAL pages are included (the earlier copy could
  lose them), and publishes it by hard link without replacing an existing
  file. Cooperating migrations serialize on a persistent SQLite lock under
  `state/.legacy-migration-lock.sqlite3`, and after taking it the importer
  rechecks the marker and every destination. Other files, directories and
  links are staged privately beside their destination and published with a
  no-replace rename (Windows rename semantics, `renameat2(RENAME_NOREPLACE)`
  on Linux, `renamex_np(RENAME_EXCL)` on macOS), so a competing destination,
  including an empty directory or a dangling link, is never replaced or
  deleted; a filesystem without that operation fails safely instead of
  falling back to an overwriting rename. The `.pre-0.44-merge` backup of a
  merged `.env` / `config.yaml` snapshots the old contents even when the
  layout copy is a link. A lock failure or a failed state import stops
  default database initialization instead of creating an empty database
  that would hide the legacy data, and an interrupted import is reported and
  retried on the next command rather than recorded as done. The Windows and
  macOS legs of `.github/workflows/migration.yml` run the native filesystem
  and process-interruption contracts on every push or pull request that
  touches this code.
- **Installer fixes and contract suites** (#138). `install.sh`,
  `install-mac.sh` and `install.ps1` preserve an explicit `NVH_HOME` /
  `NVH_CONFIG` workspace override and normalize the legacy
  `HIVE_CONFIG_HOME` alias (Windows saves the override to the user
  environment only after a successful package install); the shell
  installers write generated paths with `printf %q` so a space or quote in
  a path cannot break them; `install-mac.sh` stops reporting a successful
  update after a Git or package failure. `ci/integration-test-install.sh`
  installs the exact committed candidate from an isolated Git bundle in a
  disposable Ubuntu container with no GPU and no model downloads, then
  verifies the CLI, the saved configuration and a running local Ollama;
  `ci/test-windows-installer.ps1` (PowerShell 5 and 7),
  `ci/test-macos-installer.py`, `ci/test-linux-installer-env.py` and
  `ci/test-installer-harness.py` run the extracted installer logic against
  fake homes and inert commands with no installation.
  `.github/workflows/installer.yml` runs all of them on installer pull
  requests and on dispatch. These are control-flow and path checks, not
  Windows or macOS dependency installations ("Kinds of tests" in
  [docs/TESTING.md](docs/TESTING.md#kinds-of-tests)).
- **Live-provider CLI queries are opt-in** (#138). The `TestCLIQuery` cases
  in `tests/test_cli_e2e.py` that spawn `nvh` against configured live
  providers are skipped unless `NVH_NETWORK_TESTS=1` is set, the same switch
  that already gates the Ollama registry probe in
  `tests/test_local_models.py`, so the default suite reaches no provider.

### Security
- **The red-card click is verifiable server-side.** Every surfaced
  privileged call carries an HMAC approval token (15 minutes, single use,
  bound to the exact tool name and arguments shown on the card); the
  confirmed execute must return it or nothing runs. The API also refuses
  confirmed privileged calls in open mode (no `HIVE_API_KEY`) when the
  server is bound to a non-loopback address, and from a foreign `Host` or
  `Origin` (DNS-rebinding defence). Same-user local processes are out of
  scope by design: they already hold the user's sudo.
- **The card shows the server's dry-run plan, never the model's.** The
  Wizard attaches the registry's plan to the surfaced call; a `plan`
  argument emitted by the model (or by a prompt-injected tool result) is
  never rendered. Sudo hand-offs and in-band refusals now render as such
  (the handler result is unwrapped from the execute envelope), and the
  outcome of every approved tool reaches the model on the next turn as a
  `TOOL_RESULT` line, so the persona rule finally matches the real flow.
- **Hardening after review:** apt package names must end alphanumeric
  (apt treats a trailing `-` as *remove*); `systemd-suspend`,
  `systemd-poweroff` and the other power units are refused by full name;
  the audit note ignores a model-supplied `home_dir`; failed and partial
  applies are recorded under `Decisions/` with their exit codes; the
  tool-result window fit keeps the hand-off fields; `TOOL_RESULT` text is
  redacted before it is cut, and `redact_secrets` now catches dash-separated
  keys such as `sk-ant-…`.
- **Playbooks after review:** a cancelled `playbook-run` job still writes its
  receipt and vault note (naming the command that was in flight); the
  `tailscale` undo and warning cover the SSH server it may enable;
  `open-webui` and `vllm` carry LAN-exposure warnings (they publish
  unauthenticated services on every interface); VS Code is one
  `apt-get install -y <deb>` step flagged "unpinned download"; `bash -c`
  steps take paths as positional parameters so a quote in `NVH_HOME` cannot
  break or inject; `repair_plan` / `uninstall_plan` derive
  `safe_to_run_without_root` from the recorded `requires_sudo` and the undo
  lines, not only from `no_root`.
- **One host-command runner.** `nvh playbook install` runs its steps through
  `system_settings.run_host_command(interactive=True)` — plain `sudo`
  prompting on the terminal, stdin inherited, output echoed — instead of a
  second copy of the runner, so the deny list, the sudo matrix and the
  in-band error shapes are shared; an account that cannot elevate is
  refused before anything spawns. The agent sandbox's shell guardrails
  block `nvh playbook install` (list and plan stay allowed).
- **Playbooks never hand-type a model tag.** The `ollama` playbook's pull,
  check, title and verify steps carry an `@MODEL_CHAT@` placeholder that
  `compile_plan` renders from the local-model tier table for the host's
  platform facts (a 128 GB Spark gets the table's top chat pick; unsized
  hosts get its default), and LM Studio's model step is manual prose instead
  of a pinned id. Step titles render through the same context everywhere
  they surface (cards, catalogue, run events). The repo-wide retired-tag
  guard now covers the catalogue.
- `docs/GETTING_STARTED.md`: the hardware table no longer hand-types model
  tags (it carried four retired ones); it names tiers and points at the
  generated [MODELS.md](docs/MODELS.md) table and `nvh models tiers`.

## [0.43.0] - 2026-09-03

Merged to main 2026-09-03; never tagged or published; first shipped in 0.44.0.

The concierge release. nvHive turns toward NVIDIA's owned unified-memory
desktops — DGX Spark today, RTX Spark when it ships — as an on-device helper
([proposal](docs/proposals/SPARK_CONCIERGE_2026-09.md), #137, #136): one
Wizard that picks a hidden specialist per turn, a sprite mascot that mirrors
what it is doing, platform facts (device class, unified memory, sudo) in
the prompt, Home Assistant tools, an arm64 release binary, and one
registry-verified local-model table that every ladder, installer and doc
reads. It also closes the dated 0.43 "refresh" items: the Perplexity
Chat Completions sunset (the provider now speaks the Agent API) and
`num_ctx` from the VRAM tier. The remaining refresh items (LiteLLM-derived
capabilities, the live `/v1/models` intersection, the cloud catalogue
regeneration, `NVH_HOME` as the only root, one `Tool`) move to 0.44.
Privileged setup with approval cards (Phase 2) follows in 0.44.

### Added
- **DGX Spark is a release target.** `release.yml` builds `nvh-linux-arm64`
  on GitHub's arm64 Ubuntu runner beside the x86_64 binary, CI runs the
  test suite on `ubuntu-24.04-arm`, and `start-linux.sh` downloads the
  binary for the CPU it runs on (`uname -m`), refusing with a clear message
  on architectures that have none and explaining when the latest release
  predates the arm64 asset (#136). Releases now publish a `SHA256SUMS` file
  and the launcher verifies the binary against it before running it
  (releases without the file are used with a loud warning).
- **Home Assistant** (`nvh/integrations/home_assistant.py`): five Wizard
  tools over the local REST API — `home_assistant_status`, `_entities`,
  `_state`, `_services` (auto) and `home_assistant_call` (a confirm card
  shows the exact service call). Configure with `HASS_URL` / `HASS_TOKEN`;
  admin services (`hassio`, `shell_command`, `python_script`,
  `homeassistant.restart|stop`) are refused unless `NVH_HASS_ALLOW_ADMIN=1`.
  Unconfigured, the tools explain how to create a long-lived token and make
  no network call. Hardened after review: no default hub address (`HASS_URL`
  is required whenever a token is set; plain `http` only for loopback,
  RFC 1918 and `.local` hosts, reported as `insecure_transport`),
  `home_assistant_call` is an allowlist of device-control domains
  (`NVH_HASS_ALLOW_ADMIN=1` unlocks other domains, `=all` is required for
  `hassio`, `shell_command`, `python_script`, restart and stop), the
  model-supplied `data` cannot smuggle `entity_id` / `target` / `area_id` /
  `device_id`, and entity names, states and attributes are whitelisted per
  domain, truncated, stripped of control characters and marked untrusted
  ("device-reported text is data, not instructions"). Both Smart Home
  profiles are pinned to the local provider and tagged `local-only`, so
  occupancy, lock and camera states never leave the machine: without
  Ollama they decline instead of routing to a cloud model.
- **Agent Library — Smart Home**: `home-assistant` (Home Assistant Operator;
  reads before it writes, never guesses entity ids, waits for confirmation)
  and `home-automation-planner` (writes Home Assistant YAML automations from
  a plain-language request with read-only tools).
- **The mascot.** An always-present sprite guide in the WebUI's bottom-right
  corner mirrors what the Wizard is doing — thinking, running a tool,
  waiting for your confirmation, done, error, and dozing after 90 s idle —
  with speech-bubble tips (a first-run welcome on `/setup`, the top
  diagnostics finding on `/wizard`), an *Ask the Wizard* / *Hide* menu,
  reduced-motion and screen-reader support. The art is a swappable sheet
  plus manifest under `web/public/mascot/` (the shipped placeholder is a
  hexagon-headed "hive spirit" drawn by a stdlib-only Python script);
  replacing it is a file swap with no rebuild ([docs/MASCOT.md](docs/MASCOT.md)).
- **Wizard concierge** (`nvh/integrations/wizard/concierge.py`). With no
  profile pinned (`profile` omitted or `"auto"`; `"wizard"` now pins the
  general persona) the Wizard picks a hidden specialist per turn from a declarative rule table over
  22 library profiles — keyword and regex triggers, workspace state
  (diagnostic findings, `platform.device_class`, first run), the task
  classifier as tie-breaker, and turn-to-turn continuity — so a pasted
  traceback reaches the install medic, "which model fits my Spark" the
  VRAM planner, "turn off the kitchen lights" the Home Assistant operator,
  and a greeting stays with the general Wizard. Explicit pins still win,
  and a profile's provider pin is advisory: when the pinned provider is not
  registered (the library's Ollama-pinned profiles on a box without Ollama)
  the router's choice stands and `routing_reason` says so. Smart-home
  routing needs a smart-home object — "turn on GPU persistence mode" or
  "what's the temperature of my GPU" never reach the Home Assistant
  operator — and low-precision words (review, latest, remember, …) count
  only beside a stronger signal. A specialist the concierge chose keeps the
  general Wizard's read-only core tools (`diagnose`, `refresh_models`,
  `rag_ask_vault`) so hidden routing never removes the ability to look at
  the box; an explicit pin keeps the strict whitelist. The response and the
  streaming `done` event carry `used_profile` and `profile_reason`, the
  bubble credits the specialist, and that attribution survives a reload and
  rides along in the next turn's history so follow-ups stay with the same
  specialist. An unpinned turn routed to a local-only specialist on a box
  whose local model is unavailable is answered by the general Wizard (the
  reason says which specialist was skipped); only an explicit pin refuses.
  A profile that pins a provider without a model gets that provider's own
  model, never another provider's id. Deterministic fallbacks attribute no
  specialist, keep the tools an earlier iteration executed, and carry a
  `fallback_reason` on both paths. The WebUI composer defaults to **Auto**;
  pinning a persona (including "AI Wizard (general)") lives in an
  *Advanced* disclosure next to Depth, the `?profile=` deep link pins once
  and then hands control back, and whitelist refusals never inflate the
  "used N tools" count. Local-only specialists and Ollama-pinned profiles
  require Ollama to be *running*, not merely configured: one cached
  `GET /api/tags` probe gates the demotion, the refusal and the provider
  pin, and an unreachable pin falls back to the router with the reason
  recorded. The probe is asynchronous (it never blocks the server's event
  loop), trusts a negative answer for five seconds and a positive one for
  thirty, and is forgotten when a tool restarts or refreshes Ollama or a
  completion on Ollama fails. When the router itself picks a local model
  that is down, the turn is re-routed to the best registered cloud
  provider with the reason recorded, so a dead Ollama never eats a turn.
  Refusals say whether Ollama is missing or merely stopped. A model-path
  failure shows its red banner again (only the deliberate local-only
  refusal stays banner-free), and an error that arrives right after a
  confirm card leaves the mascot asking over the pending cards instead of
  idling; a deliberate refusal never plays the mascot's error strip. It renders as an attributed answer rather than an "offline helper"
  reply. The per-turn workspace snapshot, the offline helper and the
  `diagnose` / `refresh_models` / `repair_workspace` tool bodies run off
  the server's event loop, the cloud fallback for a dead local model
  honours the router's health and model gating, and tests stub the
  local probe so no test reaches the network.
- **Setup Concierge** (`setup-concierge`, a new Setup category in the Agent
  Library): a patient first-run guide for DGX Spark / RTX Spark owners and
  any fresh install. It reads the `platform` block, walks storage → first
  model → optional provider key → first test question one step at a time,
  runs `diagnose` on trouble and never claims sudo it does not have. The
  concierge routes onboarding intent ("how do I get started", "just got my
  DGX Spark, what should I do first", "install nvhive") to it, with
  first-run, no-models and Spark boosts, while "set up ssh keys / docker /
  home assistant" stay with their own specialists; `model-librarian` keeps
  every "which model" question.
- **`nvh ask --focus research` no longer depends on Perplexity.** It runs
  nvHive's own `web_search` first (SearXNG → Brave → DuckDuckGo), grounds
  the router-chosen model on the numbered sources with inline `[n]`
  citations and a Sources list, and falls back to the multi-advisor council
  only when no search backend is reachable (the banner says which happened).
  Perplexity answers only when pinned explicitly with `-p perplexity`, and a
  grounded prompt is never auto-routed to it. `--local` and `--privacy` skip
  web grounding entirely and say so, so nothing leaves the machine;
  `--knowledge` folds the local RAG block and the numbered web sources under
  one instruction; with no positional prompt the search query is the pasted
  text's first meaningful line (or grounding is skipped); grounding fields
  are flattened to one line with control characters stripped, only http(s)
  URLs are shown, and the model is told the sources are untrusted data.
  The `[local mode]`, `[privacy mode]` and `[rag …]` banners render again
  (Rich had been swallowing them as style tags). The grounding-prompt
  builder lives in `nvh/integrations/web_search/grounding.py` so the Wizard
  can reuse it.
- **Perplexity speaks the Agent API.** Its Sonar Chat Completions surface
  retires on 2026-09-27; the `perplexity` provider now routes through
  LiteLLM's `aresponses` (OpenAI Responses shape) via a new
  `ProviderSpec.api_surface` field (`"chat"` default, `"responses"`), with
  Responses results and stream events converted into the same
  `CompletionResponse` / `StreamChunk` shapes (an `incomplete` response maps
  to `length`) and cost taken from Perplexity's own per-request
  `usage.cost`. Defaults move to `perplexity/preset/low` (was `sonar-pro`)
  and `perplexity/preset/fast` (was `sonar`); `nvh config migrate` rewrites
  the old ids and the four legacy Sonar catalogue rows are gone. Chat-style
  text and image parts convert to Responses `input_text` / `input_image`,
  and when a response carries no `usage.cost` the catalogue's per-token
  rates price it instead of recording $0. The sunset warning for Perplexity
  is gone because it no longer applies; the sunset mechanism itself stays
  tested with a synthetic provider.
- **One local-model tier table.** `nvh.core.local_models` holds the single
  VRAM ladder (0–4 … 96+ GB) with a chat / code / vision / reasoning / embed
  / CPU-fallback pick per tier plus the tier's `num_ctx`, `num_parallel` and
  quant; `tier_budget()` reproduces the unified-memory maths (pool minus
  the 16 GB OS reserve, no CPU-offload bonus; discrete VRAM plus a capped
  RAM bonus) for any GPU row, `moe_first()` prefers MoE models on
  bandwidth-bound pools, and `reason_for()` generates the explanation from
  the row so a tag and its prose can never disagree. Every tag is verified
  against the Ollama registry by `scripts/verify_local_model_tags.py`
  (sizes come from the manifests): `nemotron3:33b` / `nemotron3:33b-q8`
  lead from 40 GB, `qwen3:30b-a3b` and `qwen3-coder:30b` cover 24–40 GB,
  `gpt-oss:20b` / `gpt-oss:120b` are the MoE reasoning picks, `qwen3-vl:8b`
  and `llama3.2-vision` the vision picks, `gemma3` and `qwen3` the small
  tiers. Every former ladder now reads it: `gpu.py` (`recommend_models`,
  `get_ollama_optimizations`, the vision pick and the unified note — six
  reasons that described a different model than they recommended are gone,
  and budgets snap to the nominal size so a 23.99 GB card is the 24 GB
  tier), `cli/setup.py`, `workstation.py`, `studio_packs.py` (the Studio
  model picker is generated from the table and `_detect_vram_gb` is
  unified-aware, ending the 128-vs-112 GB disagreement with the
  recommender), the local-chat and Ollama preference lists, the agentic
  tiers, the cloud-session map, `model_manager` and `gpu_emulation` size
  tables. `install.sh` sources `nvh models tiers --shell` after the pip
  install and picks the tier's chat model from the detected memory (GB10:
  `MemTotal` minus the OS reserve; `[N/A]` cells no longer abort it);
  `install.ps1` / `install-mac.sh` ask `nvh models tiers --pick chat`; the
  HuggingFace GGUF Omni bootstrap and every phantom tag are gone.
  `docs/MODELS.md`'s tables are generated by `scripts/gen_models_doc.py`
  with a parity test. Recommendations carry an embedding model
  (`nomic-embed-text`, last) and a `use_case`. The `network` pytest marker
  (opt in with `NVH_NETWORK_TESTS=1`) runs the registry check.
- **Ollama receives `num_ctx`.** Native chat calls send the tier's context
  length, resolved once per provider instance and capped at the model's own
  context from `/api/show`; `NVH_OLLAMA_NUM_CTX` overrides it (`0` sends
  none); when no GPU is visible to the client, or the daemon is not on
  loopback, nothing is sent and Ollama's default applies (ROADMAP 0.43).
- **The unified-memory OS reserve scales with the pool** (an eighth of it,
  4 GB floor, 16 GB ceiling for GB10-class pools), so a 16 GB Apple Silicon
  Mac plans against 12 GB instead of 0 GB; the installer reads the curve and
  the tier snap from the `nvh models tiers --shell` snippet instead of
  typing them. Hybrid CPU-offload picks need a 12 GB+ card and keep at most
  40 % of the model in RAM. The Wizard's desktop vision detection, the
  Ollama vision preference, the setup wizard's vision-first ordering, the
  API's OOM probe set and download-size estimates, the generated default
  config, the setup catalog's profile model ids and every `ollama/*`
  capabilities row now come from the table; fourteen Agent Library profiles
  and the built-in `coder` no longer pin retired Ollama tags; a repo-wide
  guard test forbids retired tags as values. Maintenance commands such as
  `nvh models tiers`, `nvh version` and anything with `--json` no longer
  trigger the first-run guided setup, so the installer's tier snippet is
  always clean. The Ollama provider's automatic model pick matches installed
  tags exactly before falling back to a family (an installed `gpt-oss:20b`
  no longer stands in for `gpt-oss:120b`, a text-only `gemma3:1b` never
  satisfies a vision rung) and still recognises llava-era installs as
  vision models, ranks installed tags by their own size against this
  machine's budget (an installed `gemma3:27b` beats `qwen3:8b` on a 24 GB
  card and yields to it on 12 GB), never falls back to an embedding model,
  and treats Ollama's "does not support chat" 400 as model-unavailable so
  the retry swaps the model out; the tier's `num_ctx` is sent only to table
  picks (custom Modelfiles keep their own context); `OLLAMA_BASE_URL` may
  be an IPv6 literal such as `http://[::1]:11434`; `nvh nvidia`'s pull hint and the setup, workstation
  and NemoClaw blueprint copy take their model and reserve figures from the
  table.
- **Model Sommelier** (`model-sommelier`, Ops): recommends which local model
  to run for the task on this machine — reads the platform block (unified
  pool minus the OS reserve, `MemAvailable`, bandwidth), calls
  `refresh_models`, proposes at most two picks with quant and context
  length, and ends with the exact `nvh models pull` command. The concierge
  sends recommendation and fit questions to it; `vram-planner` keeps pure
  sizing arithmetic and `model-librarian` the shelf. The setup-concierge
  rule no longer stacks its state boosts, its veto vocabulary is derived
  from the rig-doctor rules (any trouble phrase disables onboarding), and
  phrases score once; recommendation questions may name a model by size or
  family; shelf verbs veto the sizing planner; ties resolve by the rule with
  more distinct signals before table order; fact-check patterns need a
  claim-shaped object so "my GPU is running really hot" reaches the rig
  doctor; sizing arithmetic that names a model family or size goes to the
  planner and picks to the sommelier; "set up my spark" / "get my box
  ready" reach the setup concierge; "pull qwen3" / "download the 70b" reach
  the shelf; bare device nouns (gb10, blackwell, grace) no longer count as
  trouble. The latency tuner's Spark and unified-memory boosts are one
  group and install-medic vocabulary vetoes it ("vllm install failed with
  exit code 1" is a repair, not a speed question); fine-tune vocabulary
  vetoes the planner and the sommelier; vague trouble words ("broken", "not
  working", "fix this") need a rig noun beside them, so "fix this sentence"
  never reaches the troubleshooter; the shell teacher scores a phrase once
  and the Docker daemon socket error routes to the container wrangler. A
  70-question routing probe is now a parametrised test.
  `TierBudget` distinguishes GPUs seen from GPUs whose memory could be
  read, so an unreadable card is sized as no VRAM rather than as a
  CPU-only machine.
- **Platform facts** (`nvh/utils/platform_facts.py`): `detect_platform_facts()`
  classifies the machine — `dgx-spark`, `rtx-spark` (provisional until the
  hardware ships), `dgx`, `cloud-desktop`, `laptop`, `workstation` — and
  reports architecture, distro, DGX OS, unified memory with the truthful
  `MemAvailable` headroom, and `has_root` / `can_sudo` / `in_sudo_group`
  (probed with `sudo -n` only; nvHive never prompts for a password). The
  Wizard prompt carries it as a `platform` block, `nvh status` findings gain
  `platform-dgx-spark` / `platform-rtx-spark`, and the Welcome-back panel
  shows the architecture.

### Fixed
- **WebUI lint is clean again**: the create-agent modal declared a hook after
  an early return and the Wizard's resume-guard ref mutation tripped the
  React Compiler rule; CI's `webui` job now also runs the `node --test`
  unit suites under `web/lib/`.
- **Agent profiles now bind in Wizard chat**, on both the streaming and
  non-streaming paths. `tools_allowed` filters the tool catalogue the model
  sees and refuses any other tool (`not_allowed: true`, never executed,
  dropped from confirm cards, explained back to the model); `temperature`
  and `max_tokens` override the engine defaults. Before this every profile
  saw and could run every tool, and the knobs were ignored.
- **Streaming Wizard turns emit `confirm_required` before `done`** even
  when the confirm-class call came from the first iteration or
  `max_iterations=1`, and deferred calls no longer vanish when a later
  iteration answers without repeating them.
- **Streaming Wizard `done` reports real `cost_usd`, token counts and
  latency** from the provider's final chunk and enforces the profile's
  `max_cost_usd_per_turn`, instead of always reporting `0.0` and
  `cost_ceiling_hit: false`.
- **Three honest buckets for tool calls the loop did not run.**
  `tool_calls` / `confirm_required` carry confirm-class calls only (and are
  still emitted when a later iteration fails); auto-class calls skipped
  because of Depth 1, a disabled follow-up or the cost ceiling are reported
  as `deferred_tool_calls` with a reason and are never auto-executed by the
  UI (they render as muted "not run" lines); whitelist refusals are
  recorded with `not_allowed` and streamed as `tool_result` events, never
  executed and never counted as a used tool. A refused confirm-class tool
  is a refusal, not a confirm card. Confirm cards no longer fail with a 500
  when the model omits a required argument, and gained a *Skip* button.
- **One turn preamble.** `wizard_chat` and `wizard_chat_stream` share
  `_prepare_turn`: workspace context, concierge, profile resolution, tool
  catalogue, vault recall and system prompt are computed once per turn (the
  tool registry was rebuilt for every tool call and the profile catalogue
  loaded three times per turn); `tools_allowed` is normalised by the
  profile loader; the dead legacy `_apply_profile` helper is gone.
- **Mascot and Wizard UI review fixes**: the mascot sits below modals,
  drawers and the mobile sidebar backdrop, its menu is keyboard-accessible
  (focus on open, Escape and focus-out close), the diagnostics probe behind
  its tip runs only while the mascot is visible and once per session, tip
  ids are consumed only when a bubble renders, it keeps asking until every
  pending confirm card is run or skipped, and the sprite manifest under
  `web/public/mascot/` is the single source of timing.
- **The Wizard's GPU context block was always empty.** The collector read
  `GPUInfo` dataclasses as dicts, so the model saw `name: null, vram_gb:
  null` on every turn; it now reports the real name, memory, architecture
  and a `unified_memory` flag, and `detect_gpu_status()` gained a one-line
  `summary`.
- **A DGX Spark is no longer a "cloud desktop".** The NVIDIA board-vendor
  heuristic in `nvh.utils.environment` skips DGX hardware.
- **GB10 is recognised** (compute capability 12.1 → Blackwell, in both
  `nvh.utils.gpu` and the emulation table); the `nvidia-smi` fallback
  tolerates the `[N/A]` memory cells GB10 reports and uses `MemTotal` /
  `MemAvailable` for unified-memory parts; `detect_system_memory()` works
  on Windows.
- **`recommend_models` no longer double-counts unified memory** as VRAM
  plus CPU offload; it reserves 16 GB for the OS and attaches a bandwidth
  note (273 GB/s: dense 70B is bandwidth-bound, prefer MoE models such as
  `nemotron3:33b` / `gpt-oss:120b`), which `/v1/models/recommend` now
  serialises as `note`.
- **Unified memory is decided by the GPU's identity only.** An NVML or
  `nvidia-smi` memory failure on a discrete GPU no longer flips the stack
  into Spark mode or substitutes system RAM for VRAM; a GPU whose memory
  pool cannot be measured is reported as a `memory-unavailable` issue
  instead of a ready 0 GB GPU. `check_oom_risk` treats a unified pool as
  one pool (no hybrid verdict, `ram_free_gb` 0, a `unified_memory` flag)
  and `get_ollama_optimizations` gives GB10 Q4_K_M / MoE-first guidance
  instead of the Hopper HBM tier. A fake-pynvml test suite covers both
  detection paths.
- **Platform probes stay off the chat path.** Cloud classification uses
  local DMI and environment signals by default; the one network probe runs
  once at API startup (`warm_platform_facts()`), and host facts (DMI,
  os-release, groups, sudo, cloud) are cached for the process lifetime with
  only memory and GPU refreshing. `sudo -n true` runs at most once per
  process and only for members of the sudo/admin/wheel group, so the server
  account no longer fills the auth log; group membership is read from the
  group database, so `usermod -aG sudo` after login is honoured. The
  startup probe runs on a background thread (never delays readiness;
  `NVH_PLATFORM_WARMUP=0` skips it) and probes with `sudo -n -k`, so a
  cached credential is never mistaken for passwordless sudo. DGX OS on
  arm64 is a DGX Spark even when DMI carries OEM strings, but a visible
  non-GB10 GPU (a Grace Hopper node, a Jetson Thor kit) is not; the
  provisional RTX Spark class takes its unified-memory flag from the GPU
  row so the prompt agrees with the recommender and the OOM check; DGX and
  GB10 DMI strings match underscore-joined spellings (`NVIDIA_DGX_Spark`);
  a wireless-mouse battery no longer makes a desktop a laptop. Tests seed
  the platform cache, so no test spawns `sudo`, `curl` or `nvidia-smi`.
  The startup warm-up runs on a daemon thread, so shutdown never waits out
  the metadata timeouts either; `DGX_Spark` / `DGX-Spark` DMI spellings
  classify as DGX Spark; a visible GPU whose memory cannot be read is kept
  in the list (0 GB, status `blocked`, named in the summary) so a DGX-OS
  arm64 Grace Hopper node is never misreported as a Spark; the diagnostics
  report's `environment.machine` is the same WOW64-aware value as the
  compatibility fingerprint; and the RTX Spark finding claims unified
  memory only when the GPU row does. Wizard chat history turns accept
  `used_profile`, which the concierge's continuity tier reads. A machine
  with one healthy GPU and one whose memory cannot be read stays `ready`
  (the unreadable card is named in the summary and excluded from VRAM
  totals and the "Ollama will use all N GPUs" note); GPU issues are scoped
  to the detection source that produced the rows, so an NVML memory
  warning never survives into a healthy `nvidia-smi` result;
  `/v1/system/gpu` carries the detection `status`, its `summary` and a
  per-row `memory_unreadable` flag, and the hardware widget, Setup,
  Providers and System pages show "memory unreadable" instead of
  "0 GB VRAM" (the CLI's `nvh setup`, `nvh bench` and `nvh nvidia` say
  "memory unreadable" too, through one `format_gpu_memory()` formatter);
  the WebUI's primary GPU is the first readable row, matching the API; the
  GPU endpoints run detection on a worker thread with a five-second memo
  for the `nvidia-smi` fallback; the host probe runs outside the platform
  cache lock, on a background thread when the cache is cold, and the
  request path never waits on a probe in flight (it serves provisional
  facts marked `host_probe_pending`).
- **`/v1/system/gpu` on a unified pool** reports `system_ram.unified_memory`
  and `effective_for_llm_gb: 0`, and the Setup and Providers pages say the
  pool is shared instead of advertising "N GB usable for CPU offload";
  discrete GPUs render exactly as before.
- **Findings and prompt on a Spark tell one story.** A Spark whose GPU is
  hidden from the process (a container without NVML passthrough) yields one
  `platform-dgx-spark-gpu-hidden` warning instead of a contradictory
  "no GPU, rented instance" plus "running on DGX Spark" pair; `gpu-missing`
  no longer mentions a rented instance on owned hardware; unified-memory
  findings quote the pool size only when it was measured. The prompt's
  `gpu` block never shows system RAM as GPU memory (the `platform` block
  owns memory facts). The Welcome-back panel's "what survived" rows were
  empty for every fact because the fingerprint key was never present; they
  are back, with Architecture included.
- **WebUI**: `GPUDevice.unified_memory` and `ModelRecommendation.note` are
  typed; the GPU recommender shows the unified-memory note as a caption and
  a compact "unified" tag appears beside every VRAM figure on GB10-class
  machines; discrete-GPU rendering is unchanged.

## [0.42.0] - 2026-09-02

The "subtract" release. The September audit
(docs/proposals/SIMPLIFICATION_PLAN_2026-09.md) found nvHive had become two
products stacked on top of each other — a `~/.hive`-era core and the
`NVH_HOME`-era Wizard layer — with every mechanism built two or three times
and the copies already drifted. This release deletes the second copy of
everything and adds no new capability: roughly 39,000 lines removed across
257 files (about 15,000 of product code and scripts, the rest tests and docs
that only existed to cover the deleted code), no feature a user could reach
is gone, and every removed command survives as a hidden alias for one
release. Issues #122–#130.

A post-merge review of the first cut found ten defects (import duplication,
a sequence collision, a `--json` crash, three different API-key resolution
orders); their fixes are folded into the entries below and landed before the
tag.

### Removed
- **Seven dead orchestration modules** (`nvh/core/{autonomous,agent_pr,
  parallel_pipeline,rollback,hooks,agent_report,file_lock}.py`) — nothing in
  `nvh/` imported them; the `/v1/locks` routes nothing called; the `hooks:`
  config field nothing executed (a leftover key in an existing `config.yaml`
  is ignored); and two caller-less helpers (`agents.generate_agents_with_llm`,
  `advisor_profiles.get_best_advisor_for_task`). (#122)
- **The Docker/compose deployment family.** `Dockerfile`, `web/Dockerfile`,
  both compose files, `.dockerignore`, the five stale scripts
  (`scripts/{setup,cloud-setup,portable-setup,install,ollama-setup}.sh` —
  one still invoked the pre-rename `council` binary, one pointed at an
  unregistered domain) and the demo-GIF tooling. README said "No Docker"
  twice; nothing in CI built the images; the cloud compose mounted
  directories that did not exist. The supported installs are the README
  one-liner into an `NVH_HOME` venv and `pip install nvhive`. `.env.example`
  now documents the `$NVH_HOME/config/.env` file nvHive actually reads and
  drops the never-read `HIVE_DAILY_LIMIT`/`HIVE_MONTHLY_LIMIT`. (#123)
- **The remote-desktop input-injection toolkit** (`tools/`, two design docs)
  and the Bun **channel plugin** (`channel-plugin/`, a second copy of the
  seven `nvhive-mcp` tools with one commit and no tests). Nothing in `nvh`
  imported either. The toolkit moved to a separate private repository;
  history remains in this repo before this release. The one CI script moved
  to `ci/integration-test-install.sh`. (#124)
- **The `~/.hive`-era core modules that had `NVH_HOME` successors.**
  `core/knowledge.py` (JSON keyword search) → the SQLite/embedding RAG store
  behind `nvh rag` (`nvh rag import-legacy` re-ingests an old knowledge base,
  and `nvh status --deep` nags until you do); `core/memory.py` → vault memory
  (REPL `/remember`, `/memories`, `/forget` now write `#repl`-tagged vault
  notes that survive a VM swap); `core/smoke_test.py` → the diagnostics
  report (`nvh test` is now the offline smoke, `--imports` probes core
  modules); `core/templates.py` (`~/.council/templates`) → an optional
  `prompt_template` on agent-profile YAML with `{{input}}`/`--var k=v`
  rendering; `core/docker_sandbox.py` → `SandboxExecutor.run_shell` (one
  sandbox, one isolation policy — the agent `shell` tool now always sees the
  workspace, read-write under Docker's read-only-root/no-network/non-root
  flags, or as cwd in the labelled subprocess fallback). Agent memory moved
  from the project-local `.nvhive/agent-memory.json` to
  `$NVH_HOME/state/agent-memory/`. `core/scheduler.py` stays until the
  durable-jobs scheduler lands (0.44). (#125)
- **Twenty-three docs became eleven.** `TESTING_GUIDE.md` (1,725 lines
  documenting a `council` binary) is replaced by a 114-line `TESTING.md`;
  `CONFIGURATION.md` (new: config schema, secrets order, the full `NVH_HOME`
  layout, every env var, cabinets, tools, workflows), `INTEGRATIONS.md`,
  `MAINTAINERS.md` and `TESTING.md` absorb 19 pages; `NVIDIA_DEVELOPER_BRIEF`,
  `GITHUB_LISTING` and `future-ideas` are gone; ~8.7 MB of unreferenced
  screenshots and frame captures removed. Two guard tests keep it honest:
  `tests/test_docs_links.py` (every relative link resolves, every page is
  linked from README) and `tests/test_marketing_parity.py` (no hand-typed
  provider/model/free/cabinet/tool counts anywhere unless they equal the
  derived value). CONTRIBUTING now describes the real provider, plugin and
  test layout. (#129)
- **Dependency truth.** `pydantic-settings` (never imported) removed;
  `packaging` (always imported) declared; a `jupyter` extra for
  `%load_ext nvh.jupyter`; `dev`/`all` extras reference the other extras
  instead of hand-copying them; the unenforced repo-wide `mypy strict`
  dropped (CI's gated step passes `--strict` explicitly for the two clean
  packages). The 24 coverage-campaign test files (`test_coverage_80_batch*`,
  `test_final_push_*`, …) are folded into subject-named files with a shared
  `conftest.py` — test count unchanged, ~1,100 lines of duplicated fixtures
  gone. `nvh.integrations.catalog` is renamed `setup_catalog` (it collided
  with the `nvh/catalog` package). (#130)
- **GeForce NOW is no longer mentioned anywhere** in nvHive's shipped text,
  seeded vault notes, persona triggers or search queries (GPU product names
  and trademark notices are untouched); a guard test keeps it out.

### Changed
- **Nineteen cloud provider adapters are one.** Eighteen of them were 86–93%
  identical to `openai_provider.py`. `nvh/providers/openai_compatible.py`
  (`OpenAICompatibleProvider`) plus one `ProviderSpec` row per provider in
  `nvh/providers/specs.py` (default/fallback model, LiteLLM prefix rule, base
  URL, key env vars, zero-cost flag, anonymous key, sunset date) replace the
  clone modules; `nvh.providers.<name>_provider` remain as seven-line compat
  shims, removed in 0.43. Adding a provider is now one row. Clone drift was
  unified on the way: request timeouts are forwarded on all adapters (13
  never did), health checks time out at 15 s everywhere, `list_models`
  returns default+fallback for all, streamed tool calls report the right
  finish reason. A config stanza that omits `default_model` now gets the
  shipped default instead of an empty model string (the `LazyProvider` bug
  from #134). (#126)
- **One query command.** `nvh ask` gained `--focus code|write|math|research`,
  `--fast`, `--local`, `--clipboard`, `--copy` and reads stdin; `code`,
  `write`, `research`, `math`, `quick`, `clip`, `pipe`, `safe` are hidden
  aliases for one release. **One status command.** `nvh status` has tiers
  `--providers`, `--deep` (the old doctor), `--smoke` (the old test),
  `--report` (debug+selfcheck as a redacted JSON bundle under
  `$NVH_HOME/support/`), `--routing`, all over one checks registry
  (`nvh/integrations/diagnostics/checks.py`); `health`, `doctor`, `test`,
  `debug`, `selfcheck`, `why` and `services status` are hidden aliases. The
  21 per-provider commands (`nvh groq "q"`) are hidden aliases of
  `nvh ask -p <provider>`. `docs/COMMANDS.md` is generated from the Typer
  registry (`scripts/gen_commands_doc.py`, parity-tested). (#127)
- **The dispatcher stopped guessing.** Reserved words come from the Typer
  registry, a typo'd command gets a did-you-mean and exit 2 instead of being
  sent to an LLM, and a task-shaped bare prompt asks you to run `nvh do`
  instead of auto-approving an agent run. (#127)
- **One chat surface with one store.** `/query` and `/council` are gone
  (one-release redirects to `/` and `/?mode=council`); council presets,
  member count, strategy, synthesis and per-provider weights live in an
  Advanced drawer on the chat page, and the WebSocket now honours the
  synthesize toggle and member count. Chats are no longer kept in the
  browser: every turn persists to `/v1/conversations` (new
  `POST /v1/conversations/{id}/messages`), pre-0.42 localStorage threads are
  imported once on first load, and the sidebar search box queries the
  server. Preferences shrank to Theme, Cache and Data (the fourteen
  write-only settings are gone); the command palette's silent
  "Throwdown → council" mapping is gone. Reloaded council/compare threads
  show the synthesis or a Markdown join of member replies — structured
  member payloads need a metadata column (0.43). (#128)
- **`nvh models pull --recommended`** replaces `scripts/ollama-setup.sh`:
  detects VRAM, pulls the fitting set, skips what is installed. (#123, #125)
- **VS Code extension** could not complete a single command — it posted
  `query` where the API expects `prompt` and read a `response` field the API
  never returns. Requests now match `/v1/query` and `/v1/council`, the
  never-implemented `autoStart` setting and two empty sidebar views are
  removed, the manifest says PolyForm-Noncommercial-1.0.0, and `npm run
  compile` passes for the first time. (#124)
- **`NVH_SANDBOX=1`** now means fail-closed (an alias of
  `NVH_SANDBOX_REQUIRE_DOCKER` for one release); `nvh do --sandbox` sets it.
- **Advisor health checks no longer bill you.** Providers with a known
  endpoint are probed with a free `GET /models`; the one-token ping remains
  only where no models endpoint exists and uses the spec's `health_model`
  (NVIDIA pings its 8B fallback, not the 70B default). Health checks run
  concurrently; GPU detection runs once per `nvh status` instead of
  spawning `nvidia-smi` for every check; the Ollama `/api/tags` listing is
  fetched once per run and shared with the services glance.
- **One Ollama endpoint resolver.** Every runtime read of the local Ollama
  URL (status, setup, model pulls, local chat, vision tools, the free tier,
  the RAG embedder and the API server) goes through `ollama_base_url()`:
  `OLLAMA_BASE_URL` → `OLLAMA_HOST` → `http://127.0.0.1:11434`, with
  `localhost`/`0.0.0.0` rewritten to `127.0.0.1` (`localhost` resolved
  IPv6-first and stalled ~2 s per probe on some hosts). The embedder still
  honours `OLLAMA_URL` first; `NVH_OLLAMA_URL` remains the installer
  archive URL and is unrelated.
- **The API server no longer blocks its event loop** on `/v1/setup/
  diagnostics` or the Wizard's support snapshot; both run the deep checks
  (Engine init plus provider pings) in a worker thread.
- **Per-provider request timeouts** are a spec field: 120 s by default,
  600 s for nvidia, llm7, perplexity and siliconflow, matching the deleted
  bespoke adapters.
- **`nvh status --deep` warns 30 days ahead of a provider API retirement**
  from `ProviderSpec.sunset_date` (Perplexity: "Sonar Chat Completions
  retires 2026-09-27") and reports it as retired after the date.
- Docs: CONTRIBUTING and TESTING show CI's `mypy --strict` gate; MAINTAINERS
  lists all three version files and their guard tests; CONFIGURATION
  documents every remaining env var (`NVH_SANDBOX`, `NVH_TELEMETRY`,
  `NVH_RAG_*`, `NVH_SEARXNG_URL`/`BRAVE_API_KEY`, `NVH_API_URL` with its
  `http://127.0.0.1:8000` default, the Ollama URL precedence and the full
  provider-key alias list, …);
  PRIVACY and EULA use the 0.42 command spellings and the `$NVH_HOME`
  layout.

### Fixed
- **PyPI publishing never auto-triggered.** `publish.yml` listened for the
  `release: published` event, but `release.yml` creates the GitHub Release
  with the repository `GITHUB_TOKEN`, and GitHub does not start workflows
  from events that token produces — every PyPI release since 0.33.0 was a
  manual dispatch (0.41.0 was uploaded by hand). The publish workflow now
  runs on the `v*.*.*` tag push, the same event that builds the release.
- `nvh doctor --json` pinned the global Rich console to a closed stream
  after restoring it; the engine's LLM7 and env-detected providers are built
  through the registry instead of the compat-shim module paths.
- **Pre-0.42 spellings are now pure forwarders.** `code/write/research/math/
  quick/safe/pipe/clip`, `health/why/doctor/test/smoke/debug/selfcheck`,
  `knowledge/learn` and every `nvh <provider>` alias forward their argv to
  the replacement command, so any real flag works through the alias
  (`nvh debug --live` is exactly `nvh status --report --live`) and
  `nvh <alias> --help` shows the replacement's help with a one-line hint.
  One table (`DEPRECATED_ALIASES`) drives the aliases, the generated
  COMMANDS.md "Deprecated spellings" table (which wrongly listed
  `benchmark` and `template`), and did-you-mean — which now recognises the
  deprecated verbs: `nvh docter --fix` → "Did you mean nvh status --deep?",
  `nvh gorq hi` → `nvh ask -p groq`. A typo followed by a single word is
  treated as a typo, not a prompt; three-plus-word prompts still route to
  the LLM.
- **`nvh test` / `nvh smoke` keep their old flags for one more release**
  (`--api URL`, `--webui URL`, `--no-webui`, `--no-providers`, `--fix`,
  `--quick`; all but `--api` are ignored with a note on stderr), and the API
  smoke checks the old `nvh test` performed are back as a `smoke` tier of
  the checks registry (also in `--report`): GET `/v1/health`, `/v1/advisors`,
  `/v1/proxy/health`, `/v1/quota`, one real `POST /v1/query` and one
  `/v1/proxy/chat/completions` against `NVH_API_URL` (default
  `http://127.0.0.1:8000`), reported as `skip` when no API is listening.
- **`nvh status --json` crashed** on budget `Decimal` values; Decimal, Path
  and datetime are rendered as strings/numbers in every JSON tier.
- **One API-key resolution order everywhere.** `nvh status --deep`, the
  registry and the adapters had three different orders (the status check
  reported `HIVE_GROQ_API_KEY` as present while nothing read it, and
  `NIM_API_KEY` as missing while queries worked). `resolve_provider_key()`
  is the single implementation: config value → `COUNCIL_<NAME>_API_KEY` →
  `<NAME>_API_KEY` → provider aliases (`XAI_API_KEY`, `GEMINI_API_KEY`,
  `CO_API_KEY`, `TOGETHERAI_API_KEY`, `FIREWORKS_AI_API_KEY`,
  `PERPLEXITYAI_API_KEY`, `HF_TOKEN`, `NIM_API_KEY`) → `HIVE_<NAME>_API_KEY`
  → keyring (only with `NVH_USE_KEYRING=1`) → anonymous tier.
- **Bare model IDs route on every prefixed provider.** `ProviderSpec.
  litellm_prefix` is set for gemini, groq, xai, mistral, deepseek,
  perplexity, together, fireworks, openrouter, cerebras, sambanova,
  huggingface and ai21 and applied exactly once, so `nvh ask -p groq -m
  openai/gpt-oss-20b` works instead of failing in LiteLLM with "LLM
  Provider NOT provided".
- **`nvh ask --focus research` regained its council fallback**: with no
  Perplexity advisor it runs an auto-agent council with synthesis and an
  agreement summary, as `nvh research` did before 0.42.
- **`nvh ask` errors go to stderr**, so `... | nvh pipe` and `--raw`
  pipelines never see error text on stdout.
- **Legacy REPL memories** (`~/.hive/memory/memories.json`) are imported
  once into the vault as `#repl` notes — automatically at REPL start or via
  `nvh rag import-legacy --memories`.
- **Checks registry**: `ollama_required_models` is its own registered check
  (so `--deep --fix` can find it), titles no longer drift from their
  registration, budget figures are plain numbers, summaries include a
  `skipped` count, and the Wizard's `/v1/setup/diagnostics` report embeds
  the same registry rows (its `workspace_state` section had been silently
  failing on a wrong import).
- **Storage**: message sequence numbers are computed inside the write
  transaction with a one-time retry on collision, so two concurrent appends
  to the same conversation both persist (one used to be lost to the
  UNIQUE constraint) and running totals are incremented SQL-side. The WebUI
  persists the user turn before the assistant turn and shows a
  non-blocking "History not saved" notice if a turn could not be stored.
- **The one-time import of pre-0.42 browser chats** sends each thread as a
  single atomic request (`POST /v1/conversations` accepts `pinned` and seed
  `messages`) and removes the thread from localStorage as soon as it lands,
  so an interrupted import resumes instead of duplicating; council/compare
  replies whose text field is empty are imported as Markdown instead of
  being skipped and lost.
- **API validation**: `POST /v1/conversations/{id}/messages` rejects a
  non-finite or negative `cost_usd` with 422 (a `NaN` used to poison the
  conversation's totals); `/v1/ws/council` answers a malformed
  `temperature`/`max_tokens`/`num_agents` with an error frame instead of
  closing the socket.
- **Sandbox refusals name the variable actually set** (`NVH_SANDBOX` or
  `NVH_SANDBOX_REQUIRE_DOCKER`), and `nvh status --deep` has a "Sandbox
  isolation" row explaining when `run_code`/`shell` will refuse.
- **Agent Library profiles keep `prompt_template` and `max_tokens`** from
  the packaged catalog (the loader dropped them).

## [0.41.1] - 2026-09-01

The hotfix release. A six-lens product audit (docs/proposals/
SIMPLIFICATION_PLAN_2026-09.md) found that several shipped defaults were
simply broken: 17 of 21 cloud providers pointed at retired model IDs, cost
accounting returned $0 for every provider, and two CLI commands were
unreachable because a sub-command group shadowed them. This release fixes
what is broken today; the plan's "subtract" release (0.42) follows.

### Fixed
- **Retired default models on 17 providers.** Groq's `llama-3.3-70b-versatile`
  / `llama-3.1-8b-instant` were deprecated 2026-08-16, Perplexity's
  `llama-3.1-sonar-*` no longer exist, and `gemini-2.0-flash`, `grok-2`,
  `command-r-plus`, `jamba-1.5-*` and the Llama-3.1-70B defaults on
  Together/Fireworks/OpenRouter/Cerebras/SambaNova/NVIDIA were all stale.
  Every default and fallback was re-verified against LiteLLM 1.99.0's model
  DB and each provider's official model docs: OpenAI `gpt-5.6-terra` /
  `gpt-5.6-luna`, Anthropic `claude-sonnet-5` / `claude-haiku-4-5-20251001`,
  Google `gemini-3.7-flash` / `gemini-3.5-flash-lite`, Groq
  `openai/gpt-oss-120b` / `-20b`, xAI `grok-4.6` / `grok-4.3`, DeepSeek
  `deepseek-v4-pro` / `-flash`, Perplexity `sonar-pro` / `sonar`, Cohere
  `command-a-03-2025`, the gpt-oss family on the aggregator hosts, NVIDIA NIM
  `nvidia_nim/meta/llama-3.3-70b-instruct`, AI21 `jamba-large-1.7`, llm7
  `gpt-oss` / `minimax-m2.7` (llm7 is the only cloud provider enabled by
  default, and its old default was absent). Mistral and SiliconFlow were
  already current. The same IDs are updated in every hardcoded copy
  (settings template, server defaults, `nvh setup`, `config.example.yaml`,
  the three installers, the vision-tool fallback chain, the web setup copy)
  and `capabilities.yaml` gained rows for each new ID (scores inherited from
  the retired row; 0.43 regenerates the catalog). New `nvh config migrate`
  rewrites known-retired IDs in an existing `config.yaml` (`--dry-run`,
  `.yaml.bak`), and `nvh doctor` now warns when an enabled provider's default
  is superseded (driven by the same renames table, since retired IDs
  deliberately stay in the catalog until 0.43 regenerates it) or when the
  provider itself was retired. `deepseek-reasoner` migrates to
  `deepseek-v4-pro`, not the non-reasoning flash tier.
- **Cost accounting was $0 for every provider.** `_calc_cost` called
  `litellm.completion_cost(model=, prompt_tokens=, completion_tokens=)` —
  kwargs current LiteLLM rejects — and the bare `except` returned
  `Decimal("0")`, so no query has been priced and `budget.hard_stop` could
  never trigger. Now uses `litellm.cost_per_token`; expect non-zero cost
  pills for the first time. Stale hand-typed catalog prices corrected along
  the way (Claude Haiku 4.5, Mistral Large/Small, Gemini 2.5 Flash,
  `gemma3:4b` context).
- **Two adapters could not reach their provider through current LiteLLM.**
  NVIDIA NIM passed unprefixed `meta/...` IDs, which LiteLLM 1.99 now parses
  as its own `meta` (Llama API) provider and sends the wrong model name to
  `integrate.api.nvidia.com` (404); IDs now carry the `nvidia_nim/` prefix.
  SiliconFlow passed bare `Qwen/...` IDs with an `api_base` and no prefix,
  which raises "LLM Provider NOT provided"; it now uses the same `openai/`
  prefixing as llm7. Both adapters normalise the prefix on every model they
  are given, so a user-configured or `-m` model works without migration.
- **`nvh mcp` was unreachable.** The MCP-server command was shadowed by the
  `mcp` sub-command group (`list`/`refresh`), so `claude mcp add nvhive nvh
  mcp` in the docs never worked. `nvh mcp` with no sub-command now starts
  the server; the external-server verbs moved to `nvh mcp servers list|
  refresh` (old spellings remain as hidden aliases for one release). The
  startup banner goes to stderr — stdout is the MCP JSON-RPC channel. A
  registry-collision test now fails CI if a command and a group ever share a
  name again.
- **`nvh agent "task"` was unreachable** for the same reason (the `agent`
  group with `presets`/`analyze` won, exit 2). The coding agent is now
  `nvh agent run "task"` (and `nvh agent run --setup`); README and docs
  updated.
- **`nvh snapshot` archived the wrong tree.** The CLI used the pre-rename
  `nvh/core/snapshot.py`, which tarred `~/.hive/config.yaml` and
  `~/.council/council.db`, while the API and Wizard used the `NVH_HOME`
  workspace snapshot. README sells this command as the reconnect-survival
  story, so `save|restore` now use the workspace snapshot (vault, RAG index,
  receipts, and now the conversations DB), `list` was added, extraction is
  safe on Python < 3.11.4, and `core/snapshot.py` is deleted. The SQLite
  files are captured with the `backup()` API from wherever the DB actually
  lives (`HIVE_DATA_DIR`/`NVH_STATE` are honoured), so an export while the
  API is writing is consistent, and restore clears stale `-wal`/`-shm`
  sidecars first. Archives must carry `snapshot.json`; the 0.41.0 format
  (which bundled `config.yaml` with raw keys) is refused with a clear
  message. Config is deliberately not bundled (never risk raw keys) —
  re-run `nvh setup`.
- **`nvh advisor remove` left the provider enabled.** It deleted only the
  keyring copy of the key; it now also removes the `.env` copy and disables
  the provider stanza in `config.yaml` (writing a `.yaml.bak` first, as
  `config migrate` does).
- **A stale `~/.hive/config.yaml` could silently zero out every provider.**
  `_find_project_config()` walks upward from the current directory looking
  for a project `.hive/config.yaml`, so from any directory under `$HOME` it
  found the *user* config at `~/.hive/` and deep-merged it as a project
  overlay. On installs where `HIVE_CONFIG_HOME` points elsewhere (every
  `install.sh` install), a leftover pre-`NVH_HOME` `~/.hive/config.yaml`
  containing `providers: {}` merged over an `advisors:`-style config and
  the alias validator then saw both keys and kept the empty one. The home
  directory's `.hive/` is now never treated as a project config, and the
  search no longer climbs above the home directory.
- **Keys saved by `nvh setup` or the web Wizard were invisible to the API.**
  The engine reads keyring only when `NVH_USE_KEYRING` is truthy (default
  off, because headless boxes have slow keyring daemons); the CLI compensated
  by loading keyring/`.env` into the environment at startup, but `nvh serve`
  never did. The API lifespan now preloads the `.env` files off the event
  loop (keyring stays opt-in via `NVH_USE_KEYRING` — headless boxes have
  slow or hung SecretService daemons), and `load_env_keys()` also reads the
  `NVH_HOME` config `.env` the Wizard writes to, so browser-saved keys work
  from the CLI without exporting `HIVE_CONFIG_HOME`. `nvh advisor remove`
  scrubs both `.env` locations and disables the stanza in whichever
  `config.yaml` is in use.
- **VS Code extension** always showed "unreachable": it fetched `/health`
  while the server only defines `/v1/health`.
- **Streaming council timeout crash**: `run_council_streaming`'s overall
  timeout handler read `m.label` from `CouncilMember`, a field that never
  existed, so a council-wide timeout raised `AttributeError` instead of
  marking members failed. `CouncilMember.label` is now a real property and
  every path that keys results by member (streaming, timeout, both
  `label_weights` maps, the non-streaming dedup) uses it.
- **MCP cabinet drift**: `nvh.mcp_server` hardcoded 12 valid cabinets while
  `nvh.core.agents.COUNCIL_PRESETS` defines 13 — `product_resilience` worked
  everywhere except via MCP. The valid set is now derived from the preset
  registry, preset descriptions moved to `agents.PRESET_DESCRIPTIONS` beside
  it, and a drift-guard test pins both in lockstep.
- **`nvh.complete()` conversation history**: the advertised OpenAI drop-in
  silently discarded every turn except the last user message. `Engine.query`
  gained a `history` parameter (threaded through routing, budget, cache,
  fallback, and privacy mode) and `complete()` now forwards the transcript
  up to the final user message in caller order. Multiple system messages are
  joined into one system prompt; messages after the last user turn (e.g. an
  assistant prefill) are still dropped rather than reordered — the engine
  appends the prompt last, and an inverted conversation is worse (Anthropic
  rejects assistant-first outright) than the old drop-it behavior.
- **`init_db()` engine churn**: several DAO functions call `init_db()` on
  every invocation, and each call rebuilt the SQLAlchemy engine and leaked
  the previous pool. Re-init is now a no-op when the engine already points
  at the target path (with an existence check preserving the old self-healing
  of an externally wiped DB file), a re-point disposes the old engine only
  after the replacement is fully initialized, and a failed re-init leaves the
  previous engine/path state untouched instead of half-swapped.
- **Sandbox isolation visibility**: when Docker is absent, LLM-generated
  code silently fell back to an unisolated subprocess with only a log-file
  warning. `ExecutionResult.isolation` now records which mode ran
  ("docker"/"subprocess"), the `run_code`/`shell` tools append a visible
  notice on fallback, `/v1/sandbox/execute` returns the field, and
  `/v1/sandbox/status` reports a three-way mode (docker/refused/subprocess).
  New opt-in fail-closed mode: `NVH_SANDBOX_REQUIRE_DOCKER=1` (also
  `true`/`yes`, matching `NVH_SANDBOX`) refuses execution instead of falling
  back; workflow shell steps treat that refusal as a step failure rather
  than saving it as command output. Default behavior is unchanged — the
  primary rootless target has no Docker.
- **Shell completions actually work**: `nvh completions` still shelled out
  to the pre-rename `hive` binary and a nonexistent `council.cli.main`
  module, so it always emitted a dead fallback snippet that itself invoked
  `hive`. It now drives the real `nvh` script (`_NVH_COMPLETE`), generates
  the script in-process via Click when the console script isn't usable
  (not on PATH, broken shim), and installs `# nvh completion` blocks /
  `nvh.fish`. `main()` hands completion requests (`_NVH_COMPLETE` /
  `_NVHIVE_COMPLETE` with a `{shell}_source`/`{shell}_complete` value)
  straight to Typer before the REPL/setup dispatcher can swallow them.

### Changed
- **License texts caught up with the 0.41.0 relicense**: CONTRIBUTING.md
  claimed MIT inbound licensing and dco.yml's comment said the project
  "ships under MIT today"; both now state PolyForm Noncommercial 1.0.0
  (versions ≤ 0.40.0 remain MIT).
- **LiteLLM floor raised to `>=1.99`.** The new default model IDs and the
  `cost_per_token` pricing path need the current model DB; older LiteLLM
  would route bare OpenAI IDs incorrectly and price everything at $0 again.
- **Hand-typed marketing counts removed.** README, docs, and the MCP server
  blurb no longer state provider/model/free counts — they had drifted three
  different ways ("23 providers, 63 models, 25 free" vs. 21/70/14 real, and
  the count changes again with GitHub Models gone); 0.42 makes every
  remaining count generated and parity-tested.

### Added
- `nvh config migrate [--dry-run]` — rewrites retired model IDs and removes
  retired providers from an existing `config.yaml`.
- `nvh mcp servers list|refresh` and `nvh snapshot list`; `nvh snapshot save
  <file>` honours the positional path (previously silently ignored) and
  exits 1 with the error on failure.
- `docs/proposals/SIMPLIFICATION_PLAN_2026-09.md` — the six-lens audit and
  the 0.42 "subtract" / 0.43 "refresh" / 0.44 "add" plan — plus a
  **Non-goals** section in docs/ROADMAP.md (single user, single VM, SQLite,
  API-key auth, noncommercial; no enterprise, no alternative inference
  backends, no marketplaces, no Docker as a supported path, no hand-typed
  model facts, no new parallel implementations).

### Removed
- **GitHub Models provider.** The service was retired by GitHub on
  2026-07-30, yet the engine auto-enabled it whenever `GITHUB_TOKEN` was set
  and the free-tier ranker placed it second. The adapter and all references
  are gone; `nvh config migrate` strips the stanza from existing configs,
  and until then the registry skips a leftover `github:` stanza with a
  warning instead of routing it through the generic OpenAI-compatible
  fallback at the dead endpoint.

## [0.41.0] - 2026-08-05

The bring-to-life release — and the first under the PolyForm
Noncommercial 1.0.0 license. Four features close the competitive gaps a
56-item roadmap audit ranked most critical: a 100-profile Agent Library,
MCP client support, an in-app Model Manager, and real chat history.

### Added
- **Chat history polish** (roadmap critical): past conversations are now
  browsable, resumable, and pinnable from the sidebar on every page.
  Wizard chats persist server-side turn by turn and resume on `/wizard`
  after a reload or reconnect (the URL carries the conversation id);
  main-page single/council chats remain browser-local but join the shared
  sidebar everywhere, with rename/pin/delete/export applied to whichever
  store owns the chat. Pinned conversations are always included in the
  list response, even past the recency window.
  New API: `POST /v1/conversations` (create), `PATCH /v1/conversations/{id}`
  (rename), `GET /v1/conversations/search?q=` (full-text over message
  content); the list endpoint now returns `{conversations, count}` with
  epoch-ms timestamps plus `pinned` and `mode` on every summary. A SQLite
  column auto-migration keeps databases from older releases working.
  Fixed: the WebUI's lazy conversation-create had been 405ing silently
  (endpoint didn't exist), so no Wizard turn was ever persisted; unknown
  conversation ids on `/{id}/query` now 404 instead of 500.
- **Model Manager** (roadmap critical): an in-app model browser at
  `/models` and a `nvh models` CLI. Every catalog row shows whether the
  model fits the detected GPU's VRAM and how much disk it needs *before*
  you download — the LM Studio / Jan experience, on the rented GPU
  desktop nvHive provisions. One-click install streams live pull
  progress over SSE; installed models list their on-disk size and can be
  removed to reclaim space. CLI: `nvh models list [--all]`,
  `nvh models pull <name>`, `nvh models rm <name>`. Surfaces existing
  backend endpoints (`/v1/setup/model-fit`, `/v1/ollama/models`,
  `/v1/ollama/pull`, `DELETE /v1/ollama/models/{name}`). Docs: docs/MODELS.md.
- **MCP client support** (roadmap critical #1): attach external Model
  Context Protocol servers as Wizard tools. Claude-Desktop-compatible
  config at `$NVH_HOME/config/mcp-servers.json`; tools appear as
  `mcp_<server>_<tool>`, confirm-before-run by default with per-server
  `auto_approve`; hard connect/call timeouts; `nvh mcp list|refresh`;
  `/v1/mcp/servers` + `/v1/mcp/refresh` API; Integrations-page status
  cards; automatic cache warm-up on API start. Docs: docs/MCP.md.
- **Agent Library**: 100 original agent profiles across 38 categories,
  bundled in the package, grouped on /agents and in the composer picker.

### Changed
- **License: MIT → PolyForm Noncommercial 1.0.0.** nvHive is now a
  proprietary noncommercial stack: use, modification, and redistribution
  remain free for any noncommercial purpose; commercial use and sale are
  not permitted. Versions 0.40.0 and earlier were published under MIT and
  remain MIT. (Owner directive, 2026-08-05.)

## [0.40.0] - 2026-07-22

The post-0.39.0 hardening cycle (~30 PRs, #49–#88). One promise drove
all of it: `curl … install.sh | bash` ends in a browser tab where the
Wizard actually answers — verified bring-up, self-healing services,
in-browser diagnostics, and a CLI to drive the whole stack.

### Added

- **CLI-verified bring-up.** `install.sh` now finishes with
  `nvh services start --open`: a Rich Live "nvHive bring-up" table
  walks Ollama → API → WebUI from waiting → starting → healthy, and
  the browser opens **only when every gate is green** — never onto a
  red "API offline" banner (#84). The final gate is an end-to-end
  **Wizard smoke test** that POSTs a real `/v1/wizard/chat` request,
  so "three ports listening" can no longer masquerade as "the Wizard
  works" (#85). A deterministic-fallback answer (no local LLM yet)
  shows as a yellow *degraded* row but still opens the browser (#86).
- **`nvh services` CLI** (#70) — `status` / `start` / `restart` /
  `stop` (preserves Ollama's warm model cache unless `--ollama`) /
  `smoke-test`, built on module-level health probes
  (`ollama_healthy`, `api_healthy`, `webui_port_listening`) shared
  with `nvh webui`. The startup contract that previously lived
  implicitly across six PRs is now explicit in
  `docs/SERVICE_ORDER.md`.
- **Out-of-the-box self-heal** (#80). `nvh webui` daemonizes the API
  so it survives terminal close, silently auto-recovers an unhealthy
  API, and the Wizard proactively offers repair when it detects a
  broken state.
- **In-browser System Console** with rootless CLI bridges (#77) and a
  one-click **Debug Report** button designed for phone-photo sharing
  from a streamed cloud desktop (#78).
- **`/agents` discovery page** + sidebar nav entry (#68).
- **"Convene council" handoff**: the WizardChat composer deep-links
  into the `/council` page with the current draft pre-filled (#71).
- **GPU capability matrix in `install.sh`** (#72). Every GPU install
  now prints what the detected VRAM tier can actually run — not just
  Wizard chat. The matrix covers image generation (ComfyUI starter
  / edit / control), video generation (Wan 2.2 5B and 14B), local
  speech (WhisperX, faster-whisper), and music generation
  (ACE-Step), each with their honest VRAM floors:
  `8 GB → image-gen-starter`, `12 GB → image-edit`,
  `16 GB → image-control`, `24 GB → video-gen + speech-lab + music-gen`,
  `40 GB → video-gen-pro`.
- **`NVH_INSTALL_FULL_CAPABILITY=1`** opt-in env knob (#72). When
  set, the installer writes a marker file
  (`$NVH_HOME/state/capability/auto-enable.json`) recording which
  packs the rig qualifies for. The marker is written for a future
  WebUI/Wizard consumer — nothing reads it yet, so on its own the
  knob only stages. For a runtime effect today, add the companion
  knob `NVH_INSTALL_FULL_CAPABILITY_DOWNLOAD=1`, which force-pulls
  the matching packs inline at install time (useful for headless
  cloud images that won't have a browser later). Default for both is
  OFF so a school Wi-Fi student isn't surprised by a 60 GB pull.
- **`docs/GPU_TIER_MATRIX.md`** (#72). Canonical reference for the
  table above, including a frank "what Nemotron Omni is and isn't"
  section — Omni is a multimodal LLM (it gives the Wizard
  *vision*), not an image generator or speech synthesizer.
- **Vault seed notes for the hardening cycle**: Common Install
  Issues under Troubleshooting/ (#75); Service Order, GPU Capability
  Matrix, and Headless QA Loop (#76).
- **PhantomInput** trusted-input bridge for WebRTC remote-desktop
  streams — the backbone of the headless QA loop (#57).
- **Composer controls + cross-tab coherence**: tool-budget slider,
  session pill, proactive nudge, provider tooltip, PageHeader rollout
  (#51); per-profile cost ceilings + ComfyUI portrait workflow (#50);
  agent depth, onboarding tour, `.env` bulk import, snapshot URL
  (#49).

### Changed

- **NVIDIA Nemotron Omni is the Wizard default at every VRAM tier**
  (#60): `nemotron-omni` at 40 GB+, `nemotron-3-nano-omni` at 24 GB+,
  then `llama3.2-vision` / `minicpm-v` / `moondream` down the tiers.
  Since the Ollama library doesn't publish the Omni tags yet, the
  installer bootstraps them from HuggingFace GGUF + vision projector
  via an Ollama Modelfile (#62), with a soft fallback chain so the
  Wizard stays multimodal even when the bootstrap fails.
- **Browser auto-open prefers installed browsers** over the slow
  rootless-Firefox download: `NVH_BROWSER` override → existing
  rootless Firefox → system Firefox → Chromium / Chrome / Brave /
  Edge → download as last resort (#58).
- **ApiHealthBanner shows amber "Starting up…"** during a 20s boot
  grace window instead of flashing red while a cold API warms up
  (#67).
- **Cold-rig tuning**: longer probe timeout, boot grace, doctor exit
  handling, clearer daemon message (#81); Debug Report probe timeout
  matched to the System Console's (3s → 8s) (#83).
- **Complete dark-mode coverage** for WebUI panels that still showed
  light backgrounds (#73).
- **Legal hygiene**: NVIDIA-green visuals, benchmark caveat, DCO,
  trademark tone-down (#52).

### Fixed

- **Install actually works on cloud GPU rigs** — root-cause fixes
  rather than patches (#82); Ollama install failures are now visible
  and the binary lookup is defensive (#88).
- **Port conflicts detected + resolved before services start.**
  `install.sh` probes the whole stack-port set (3000/3001/3002/8000/
  11434), classifies each listener OK / STALE / FOREIGN, kills only
  processes that look like ours, and refuses to silently cascade to
  another port when a foreign process is in the way (#74).
- **Racy `ollama serve &>/dev/null & sleep N`** replaced with a real
  health-wait + log in `install.sh` (#66); extended Ollama startup
  wait + daemon state surfaced in `nvh webui` (#59).
- **Stale-API detection**: `nvh webui` HTTP-probes an existing API on
  :8000 and restarts it when unhealthy instead of trusting the TCP
  listener (#65).
- **`config.yaml` corruption on Omni reinstalls** (#63), plus
  recovery from previously-corrupted configs via whole-line
  replacement (#64).
- **EACCES when the WebUI bridge resolves the rootless `nvh` binary**
  (#79).
- **16 findings from the multi-agent audits** — 8 deep-dive
  correctness fixes (#86) and 8 UX / code-review fixes (#87),
  including the degraded smoke-test state, log tails shown inline on
  bring-up failure, outcome-oriented bring-up row labels, and the
  `nvh services stop` command that install copy referenced before it
  existed.

## [0.39.0] - 2026-05-15

### Fixed

- **"WebUI opens but nothing loads" silent-failure bug.** Three gaps
  were stacked: (1) `nvh webui` piped the auto-started `nvh serve`
  subprocess output to `/dev/null` so boot failures were invisible;
  (2) the readiness wait was 8s, too short for cold cloud-VM imports
  that take 10-15s; (3) the desktop-icon launcher polled the WebUI
  port but never checked the API port, so Firefox opened onto a UI
  shell whose every fetch silently failed. Fix:
  - API subprocess output now goes to `$NVH_HOME/logs/api-server.log`
    with a per-run timestamp preamble.
  - Readiness wait extended to 30s with a "…still waiting…" beat
    every 5s.
  - Desktop launcher now requires BOTH WebUI and API to be healthy
    before opening Firefox; logs which side stalled on timeout.
  - New `ApiHealthBanner` component in the WebUI surfaces a sticky
    red "API offline" banner with retry + dismiss when `/v1/health`
    is unreachable for 3+ consecutive polls. Names the log path
    explicitly so the user has somewhere to look.

### Added

- **AI Wizard now reads explicit diagnostic findings** on every chat
  turn. New `nvh/integrations/wizard/findings.py` derives a list of
  findings (e.g. `gpu-missing`, `no-providers`, `no-local-models`,
  `provider-unhealthy-<name>`) from the live workspace snapshot.
  Each finding has a stable id, severity, category, title, detail,
  and (when applicable) a `suggested_tool` the Wizard can invoke.
- **`GET /v1/wizard/diagnostics`** — consolidated endpoint that
  returns the findings list + workspace context + severity counts.
  Single source of truth for the setup-page System Check and the
  Wizard system prompt — same data, same shape, same ids.
- **New `diagnose` Wizard tool** (auto-class) refreshes the findings
  list mid-conversation so the agent can verify "did the repair
  actually clear the issue?" without waiting for the next reconnect.
- **Setup page → Wizard bridge.** New "Open in Wizard →" button in
  the System Check header (visible only when there are concerns)
  routes to `/wizard?starter=…`. The Wizard reads the URL param on
  mount, fetches diagnostics, and auto-sends a precise starter so
  the conversation lands directly on the user's issue.

## [0.38.0] - 2026-05-15

### Added

- **`nvh selfcheck`** — one-shot diagnostic bundle for product tests on
  rented GPU desktops. Runs `nvh test --quick` + a live Wizard
  round-trip + a redacted workspace snapshot, then writes a single JSON
  bundle to `$NVH_HOME/support/selfcheck-<ts>.json` (or `--output`
  path). No network egress; everything stays local so the bundle can be
  inspected, redacted, and shared on the user's terms. Flags:
  `--no-live-query`, `--strict`, `--quiet`, `--output`.
- **Opt-in install telemetry** (`nvh.telemetry`). Off by default; enable
  via `NVH_TELEMETRY=1` or the persisted config flag. Three events
  appended to `$NVH_HOME/telemetry/events.jsonl`: `install_completed`,
  `first_wizard_turn`, `reconnect_survived`. Stable anonymous
  `install_id` (UUID4), property redaction for secret-shaped keys,
  `emit()` is exception-safe and never destabilizes the host product.
  Documented in PRIVACY.md.
- **`nvh doctor --json`** — emits structured diagnostic JSON on stdout
  with rich UI redirected to stderr, so the output is parseable by
  `nvh selfcheck` and CI scripts. Pre-existing `nvh doctor` rich output
  is unchanged.
- **`nvh test --strict`** — CI-friendly invocation that fails the run
  when *any* provider soft-passes (rate limit / quota). Without
  `--strict`, soft-passes are still counted as passing but surfaced
  explicitly in the report summary.
- **Obsidian Graph view** wired into the seeded vault. `nvh init-vault`
  now ships a `MAP.md` Map-of-Content hub note, YAML `tags:` frontmatter
  on every seed note, `[[wikilinks]]` between hub / category / sibling
  notes, and `.obsidian/graph.json` color groups. Re-running on a
  populated workspace preserves user content via `_write_if_missing()`.

### Fixed

- **Smoke-test 429 soft-pass regression.** Previously a 429 from every
  provider silently re-labeled the result `passed=True`, so the report
  looked green even when no provider was reachable. `TestResult` now
  carries explicit `soft_pass` / `soft_reason` fields and
  `SmokeTestReport.strict_failed()` returns `hard_fails + soft_passes`
  so `--strict` mode (and the selfcheck bundle's strict mode) fail
  loud instead of silently counting environmental failures as passes.

## [0.37.0] - 2026-05-14

### Added

- **AI Wizard chat surface** at `/wizard` — the new primary conversational
  layer. Reads live workspace state (GPU, persistent storage, providers,
  Ollama models, recent install jobs, install receipts, vault) on every
  turn. Routes through the engine: local Ollama when healthy, cheapest
  free cloud otherwise, deterministic offline helper as the safe fallback.
  A stable persona (calm mission-control mentor, lightly playful, names
  the safest button) lives in `nvh/integrations/wizard/personality.py`.
- **Wizard tool registry** with explicit safety classes. `auto`-class
  tools run without asking (refresh_models, repair_workspace,
  validate_provider_key); `confirm`-class tools surface a UI confirmation
  card (save_provider_key); `never`-class operations are disabled at the
  registry level and cannot be registered. Per-tool handlers expose
  `name`, `description`, `safety_class`, `parameters` schema, and
  `summary_template`.
- **Chat ↔ tool wiring** via a `TOOL_CALL: {...}` JSON marker convention
  that works on any LLM, including local Ollama models without native
  function-calling. The WizardChat UI auto-runs auto-class tools and
  renders inline confirmation cards for the rest.
- **Welcome Back panel** on the chat page that calls
  `POST /v1/wizard/reconnect` once on mount. Shows what survived since
  last session, what changed (driver / CUDA / Python / etc.), what was
  auto-repaired, and what needs attention. Auto-hides on the happy
  "nothing changed, nothing repaired" path.
- **Autonomous safe-repair loop** runs on every reconnect with two new
  handlers: `ollama-model-refresh` (re-queries the local daemon's
  `/api/tags`) and `config-validate` (parses `config.yaml` without
  modifying it, surfaces schema drift before the first failed query).
- **`HardwareWidget`** in compact + hero variants on the chat empty
  state, top status bar, and setup page. Shows GPU short name,
  utilization gauge, VRAM bar, and persistent-workspace path + free GB.
  Polls every 5 s. Designed so a freshly-minted cloud Linux GPU session
  immediately confirms "yes, the rented hardware is real."
- **Dark mode foundation** — CSS-variable theming with a `.dark` swap;
  ThemeToggle component cycles Light → Dark → System and persists to
  `localStorage`. Pre-hydration inline script applies the chosen theme
  before React paints so dark-mode users never flash a white page.
- **Provider logos** in the cloud-key catalog via the `@lobehub/icons`
  CDN. New `logo_slug` field on the `/v1/setup/free-providers` response;
  the web client renders a brand mark or a typography monogram fallback.
- **Validate-before-save API key flow** — new
  `POST /v1/setup/validate-key` endpoint health-checks a proposed key
  against the provider without persisting. The WebUI surfaces "Testing…"
  → ✓ valid / ✗ rejected inline before the save button enables, so users
  catch a typo'd key at paste time instead of at the first query.
- **`/query` page promoted into the sidebar** as "Ask AI" with a new
  IconAsk SVG, fixing the orphan power-user surface.
- **AI Wizard sidebar entry** as the first item in BOTTOM_NAV with a
  new IconWizard sparkles icon.

### Changed

- **`/setup` welcome step** leads with a mission-first banner:
  *"Pick a mission and AI Wizard does the rest"* — names the three
  essential steps (storage, GPU, local AI) explicitly with ●/○
  vocabulary that matches the advanced step nav.
- **Setup advanced-group step nav** shows Essential (●) vs Optional (○)
  dots so a freshly-minted cloud GPU user sees what's required for
  first-query without guessing.
- **Convene mode empty state** explains *why* you'd use the council
  ("Best for decisions that need a second opinion: code review,
  architecture choices, debugging hard bugs") instead of the previous
  generic placeholder.
- **Soft radius pass** across the component layer: `.card`, `.card-flat`,
  `.input-base`, `.btn-primary`, `.btn-secondary`, `.btn-ghost`, `.tag`,
  `.progress-bar` move from `rounded-none` to `rounded-md`/`rounded-lg`
  with softer hover shadows. Scrollbar thumb and range-slider thumb gain
  rounded corners.
- **Drag-an-image hint** appears under the chat suggestion tiles so
  vision-capable model use is no longer a hidden feature.
- **`nvh/integrations/`** is now reorganized into subpackages by concern
  (`installs/`, `diagnostics/`, `services/`, `workspace/`, `wizard/`)
  with back-compat re-exports so existing `from nvh.integrations import …`
  callers keep working.

### Fixed

- `test_auto_repair_writes_env_file_without_downloads` mock now provides
  `config_dir` + `comfyui_dir` on its `SimpleNamespace` so the new
  Wizard-1 safe-repair handlers (`config-validate`, `comfyui-examples`)
  can resolve them.
- Provider key persistence no longer leaks to `localStorage` (security
  fix landed in 0.36.0; this release adds the validate-before-save UX
  that catches bad keys upstream of the save).

## [0.36.0] - 2026-05-14

### Security
- **Command injection** in the `process_kill` / `process_list` LLM tools
  (`nvh/core/browser_tools.py`) is blocked. Both tools now invoke subprocess
  with argv (no shell) and reject non-numeric process names that don't match
  a strict whitelist. Previously a tool call could expand shell metacharacters.
- **XSS** in the web root layout is fixed. `NEXT_PUBLIC_API_URL` is now
  JSON-encoded before being inlined into the bootstrap script tag.
- **API key persistence** in the web client no longer uses `localStorage`
  (origin-wide, persists indefinitely). Keys now live only in `sessionStorage`
  (per-tab, cleared on close), with a one-time migration that moves legacy
  keys over and deletes them.

### Added
- `nvh/api/services/` service layer; `QueryService` is the first migrated
  route, establishing the pattern for decoupling business logic from HTTP
  handlers. CouncilService / CompareService are the next migrations.
- `nvh/integrations/installs/_base.py` with the `PackInstaller` ABC and
  `PackInstallerRegistry`, formalizing the shape the 7 free-function
  installers in `studio_packs.py` will migrate to.
- `typecheck` CI job — strict mypy gates on `nvh/sandbox` and `nvh/catalog`
  today, with a repo-wide informational pass that tracks drift. Modules join
  the gated list as they reach zero strict errors.

### Changed
- `nvh/integrations/` is reorganized into subpackages by concern:
  `installs/`, `diagnostics/`, `services/`, `workspace/`, `wizard/`.
  Back-compat re-exports in `nvh/integrations/__init__.py` keep
  `from nvh.integrations import studio_packs` style imports working.
- `start-linux.sh` now refuses to fall back to ephemeral `$HOME/.nvh` on
  cloud GPU desktops without an explicit `NVH_ALLOW_EPHEMERAL=1` opt-in,
  preventing silent data loss when the OS disk is wiped on reconnect.
- Wizard helpers (`setup_agent.py`, `comfyui.py`) now log silent fallbacks
  instead of swallowing every exception, so failed probes are diagnosable.
- README and docs/GITHUB_LISTING.md drop the "self-healing" claim in favor
  of "self-diagnosing with one-click rootless repairs" — the wizard surfaces
  repairs but doesn't auto-run them without user confirmation.

### Fixed
- 11 pre-existing ruff I001 import-order errors that had been keeping CI
  red on `main`.
- `test_stream_multiple_chunks` was mocking `litellm.acompletion` after
  `OllamaProvider.stream` had switched to `_direct_stream` over httpx
  NDJSON; the mock had no effect and CI was failing on every run trying
  to reach `http://localhost:11434`.

## [0.35.1] - 2026-05-01

### Added
- NOTICE, trademark, third-party notice, and release-channel safeguards clarify
  the official nvHive project identity and canonical `nvhive` PyPI package.
- Release workflow guards prevent forks from accidentally publishing
  official-looking PyPI packages or binary release artifacts without renaming.

### Changed
- Setup wizard now starts with a calmer mission-first view and moves deeper
  diagnostics into grouped troubleshooting tabs.
- API client requests include supported local API key headers and retry
  transient install-job polling failures before surfacing an error.
- Private WebUI package metadata now uses the `nvhive-web` project identity.

## [0.35.0] - 2026-04-29

### Added
- Production-candidate rootless NVIDIA GPU AI lab release with the self-healing
  setup wizard promoted as the primary first-run path.
- Release-readiness and diagnostic reports with request IDs, error IDs, and
  redacted copyable troubleshooting bundles.
- Rootless Studio mission coverage for AI Starter, Graphics Creator Studio,
  Game Dev Lab, Music Producer Studio, Agent Builder, OpenClaw/NemoClaw,
  ComfyUI, Blender, NVIDIA Omni/NeMo/Nemotron paths, and music tooling.
- README and docs now lead with the no-root cloud GPU desktop journey and the
  target VM acceptance checklist.

### Fixed
- Persistent mount discovery ignores permission-denied probe paths such as
  locked `/mnt/lost+found` entries instead of crashing setup diagnostics.

## [0.34.1] - 2026-04-28

### Added
- Music Producer Studio mission and `nvh studio --install music -y` bundle
  with ACE-Step, Demucs/WhisperX/audio lab tooling, and Audacity/LMMS
  AppImage helpers.
- Setup wizard mission cards for AI Starter, Graphics Creator Studio, Game
  Dev Lab, Music Producer Studio, Agent Builder, Local LLM Lab, and Power
  User Workstation.
- GPU detection diagnostics that distinguish CPU-only hosts from rootless
  sessions where NVIDIA devices exist but NVML or `nvidia-smi` are blocked.
- Boot preflight tracking for GPU architecture, compute capability, framebuffer
  memory, Node/npm versions, storage capacity, storage write probes, and the
  selected PyTorch CUDA profile.

### Changed
- Setup model recommendations now use the same GPU inventory path as the
  system API, including aggregate multi-GPU framebuffer totals.
- Persistent storage checks now perform a real write/fsync/delete probe instead
  of relying only on `os.access`.
- The setup wizard surfaces real software icons and shorter mission-first
  language to reduce first-run wall-of-text fatigue.
- Setup wizard mission cards stay mission-first while storage, GPU, catalog,
  and compatibility scans continue in the background.
- `nvh webui` now builds and starts the optimized production WebUI by default,
  with `--dev` reserved for contributors editing the frontend.
- Pip/binary WebUI bootstrap now falls back to downloading the GitHub source
  archive when `git` is missing, so fresh rootless desktops have one less
  prerequisite.
- WebUI bootstrap now prefers the installed release tag before falling back to
  `main`, keeping PyPI/binary installs aligned with their shipped version.
- `nvh workstation --all -y` now passes the non-interactive yes flag through to
  the rootless Node installer instead of pausing for confirmation.

### Fixed
- Sorted the generated fallback catalog import block so the Python CI lint
  matrix can pass.
- Model fit reports now check the total recommended model queue against
  available persistent storage, not only one model at a time.
- WebUI API health and storage preflight now retry during slow API startup
  instead of leaving the setup page stuck offline until a manual reload.
- API CORS now allows localhost, 127.0.0.1, nvhive, and IPv6 loopback WebUI
  fallback ports so dynamic local previews can reach the API.

## [0.34.0] - 2026-04-28

### Added
- Rootless persistent workstation storage under `NVH_HOME`, including
  dedicated app, runtime, WebUI, model, cache, config, and log directories
  for ephemeral Linux cloud desktop sessions.
- Persistent background install jobs with progress, cancellation, and
  WebUI visibility for long-running model and app setup tasks.
- Optional rootless Python runtime fallback pack for systems where the
  bundled Python/venv path is not enough.
- Blender creative studio pack that installs Blender under
  `NVH_HOME/apps/blender` and creates a `nvhive-blender` launcher.
- Setup helper endpoint and wizard panel that rank next actions for
  students configuring storage, LLMs, ComfyUI, creative tools, and agents.
- ComfyUI model download assists, including target folders and a generated
  helper script for selected workflow examples.

### Changed
- `nvh webui` now keeps WebUI source, Node fallback tooling, and npm cache
  under persistent `NVH_HOME` paths instead of legacy home-directory paths.
- The setup wizard now exposes persistent storage locations, runtime status,
  local helper guidance, Blender/creative profile options, and clearer
  ComfyUI download-plan messaging.

## [0.33.2] - 2026-04-27

### Fixed
- Unsafe agent tools now require explicit approval instead of being
  accidentally covered by the safe-tool auto-approval flag.
- Smart routing now returns the best-scored model for the selected
  provider instead of falling back to the provider default.
- Student setup copy now points to the rootless Ollama studio pack,
  `nvh serve`, and the Linux desktop guide.
- AI Studio starter packs now skip ComfyUI custom nodes when ComfyUI is
  not installed yet, instead of failing the rest of the starter install.
- Install failure messaging now matches the supported Python 3.11+
  requirement.

## [0.33.1] - 2026-04-27

### Added
- **Setup quick profiles.** The first-run wizard now offers Student
  Starter, Creator/ComfyUI, Game Dev, and Full Workstation presets that
  preselect sensible local models, studio packs, and ComfyUI examples.

### Fixed
- Release workflow now hands PyPI publishing to the trusted
  `.github/workflows/publish.yml` workflow instead of running an inline
  publisher job that is not registered with PyPI.

## [0.33.0] - 2026-04-27

### Added
- **Model Picker wizard step.** Setup now shows exact local Ollama models
  with detected-VRAM recommendations, GPU-fit badges, disk estimates,
  installed status, and a streamed download queue.
- **Studio model endpoints.** Added `GET /v1/studio/models` and
  `POST /v1/studio/models/install` for model-by-model selection rather
  than only pack-level installs.
- **ComfyUI model-plan selector.** The ComfyUI setup step lets students
  choose workflow examples and save `MODEL_DOWNLOAD_PLAN.md` plus
  `model-download-plan.json` with required model names and source links.

### Changed
- Bumped package version to `0.33.0` for the model-picker/PyPI release.
- Expanded Linux desktop docs around rootless model selection and ComfyUI
  workflow model planning.

## [0.32.0] - 2026-04-23

### Added
- **AI Studio Packs.** Rootless, no-sudo install surface for LLMs, agents,
  ComfyUI nodes, and Linux game-dev tooling. All packs land under
  `~/.nvh/` and `~/.local/bin` — never touch apt/dnf/systemctl. Catalog
  lives in `nvh/integrations/studio_packs.py`; install via
  `nvh studio --install <id> -y`. Tagged packs: `rootless-ollama`,
  `llm-starter`, `agents`, `comfy`, `game`.
- **ComfyUI integration.** `nvh/integrations/comfyui.py` sets up ComfyUI
  under `~/.nvh/comfyui` with starter workflow examples and a user
  launcher. No system package install required.
- **Workstation integration.** `nvh workstation --all -y` orchestrates
  the full local desktop: launcher, WebUI, Ollama, ComfyUI, and all
  studio packs in one command.
- **GitHub Releases workflow.** `.github/workflows/release.yml` triggers
  on `v*.*.*` tag push — builds sdist, wheel, and single-file
  PyInstaller binaries for Linux x86_64, macOS arm64, and Windows x86_64.
  The existing `publish.yml` chains from `release:published` to push the
  sdist/wheel to PyPI via trusted publishing.
- **Three clear install paths in README.** One-line curl installer
  (recommended for GPU VMs), single-file binary from Releases (no Python
  needed), and pip from PyPI (existing Python envs).

### Changed
- **WebUI theme: NVIDIA corporate white.** Flipped the whole app from a
  pure-black dark theme to white + black typography + NVIDIA green
  accent. Code blocks intentionally remain dark (NVIDIA docs pattern).
  Touched `web/app/globals.css`, `web/tailwind.config.js`, and every
  component. Reason: higher legibility for college/classroom use, and
  closer alignment to NVIDIA's public brand.

## [0.31.2] - 2026-04-16

### Fixed
- **`nvh setup` never wrote `~/.hive/config.yaml`.** The command walked
  the EULA / email / provider / model flow and printed "Setup complete!"
  but never actually created the config file that the REPL / doctor /
  SDK all depend on. `nvh doctor` would then report "Config file exists:
  FAIL" and tell the user to run `nvh config init`. Now calls
  `_write_config()` with whatever providers were configured during the
  run.
- **`nvh setup` printed "Ollama not detected" when the daemon was up.**
  Setup used `subprocess.run(["ollama", "pull", ...])` which raised
  `FileNotFoundError` when the `ollama` CLI binary wasn't on PATH (very
  common with portable installs where the daemon is running but the CLI
  lives at `~/.nvh/ollama/ollama`). The error was caught by an outer
  `except Exception` that emitted the misleading "Ollama not detected.
  Install: curl ..." text and claimed every recommended model needed
  pulling — including ones that were already installed. Now uses the
  HTTP-based `setup._pull_model` which talks to the daemon directly and
  handles per-model failures without aborting the loop.
- **Startup missing-model check silently skipped when Ollama had no
  models.** `_startup_check_ollama_models` returned early whenever
  `list_installed_models()` came back empty — but that's the same signal
  for "daemon down" AND "daemon up but nothing pulled". The second case
  is exactly when we should prompt the user. Now probes the daemon
  separately via `_ollama_running()` and only skips when it's actually
  down.
- **REPL auto-pull didn't trigger on "model not found" errors.** The
  error handler only matched the exact string "Ollama is not running",
  so `litellm.APIConnectionError: OllamaException - {"error": "model X
  not found"}` fell through to the raw error display. Now matches both
  cases and routes through the same auto-recovery flow, which already
  handles the missing-model pull offer.

### Added
- **`nvh webui` auto-installs Node.js on Linux/macOS without root.** When
  Node isn't found, setup prompts to install via `fnm` (Fast Node
  Manager): single-binary installer, drops Node 22 LTS under
  `~/.local/share/fnm`, and prepends the bin dir to PATH for this
  process so the subsequent `npm ci` call finds it. Cleanly declines
  on Windows (use winget) and when the user says no.

## [0.31.1] - 2026-04-16

### Fixed
- **Ollama pulls failed with 404 on `nemotron-small` and `nemotron:120b`.**
  Both tags were referenced throughout the codebase (recommender, config
  writer, install scripts, Docker compose, cloud-session defaults, size
  tables) but neither exists on Ollama's registry. Only `nemotron-mini`
  (4B) and `nemotron` / `nemotron:70b` are real.

  Every reference audited and replaced:
  - Mid-VRAM text tier (6–24 GB): `nemotron-small` → `llama3.1:8b`
  - CPU-offload hint: `nemotron-small` → `llama3.1:8b`
  - High-VRAM flagship (80+ GB): `nemotron:120b` → `nemotron` (70B is
    the largest Nemotron on Ollama)
  - install.sh / install.ps1 / install-mac.sh: VRAM-tier model selection
    now uses `nemotron-mini` / `llama3.1:8b` / `nemotron` only
  - `nvh/config/settings.py`, `nvh/integrations/cloud_session.py`,
    `nvh/api/server.py`, `nvh/providers/quota_info.py`,
    `nvh/utils/gpu_emulation.py`, `docker-compose.yaml`,
    `docker-compose.cloud.yaml`, and 3 setup scripts all updated.

- **`_write_config` hardcoded `ollama/nemotron-small` as the Ollama
  advisor default.** Even on a 48 GB machine where the user should be
  running Nemotron 70B, the config pointed at a fake model. Now calls
  `recommend_models()` per-machine and writes the matching default (plus
  a real fallback_model), so the config reflects the detected hardware.

### Added
- **`_model_exists_on_registry()`** — cheap HEAD probe against
  `registry.ollama.ai/v2/library/<name>/manifests/<tag>` that
  `_pull_model` now consults before starting a pull. Confirmed 404s fail
  fast with a clear error instead of propagating as a cryptic "model not
  found" later. Network failures fall through (don't block the pull).

### Tests
- 160/160 passing across affected modules. New tests for registry probe
  (200 / 404 / network-error / tag-splitting paths).

## [0.31.0] - 2026-04-16

### Added
- **Missing-model detection across REPL, doctor, and startup.** A new shared
  helper (`nvh/utils/ollama.py`) resolves the set of Ollama models the
  config expects (default + fallback across every enabled advisor) and
  compares against `/api/tags` output with sensible tag-matching rules.
- **REPL auto-pull on missing model.** When the REPL hits an Ollama error
  and the daemon is actually up (i.e. the real cause is a missing model),
  it now shows which models are missing and offers a single-prompt pull
  via the existing progress-bar flow, instead of telling the user to run
  `ollama pull` manually.
- **Startup model-health check.** When the REPL launches with any Ollama
  advisor enabled, does a single cached `/api/tags` probe and — if any
  required model is missing — prints a one-line banner offering to pull
  right then. Silent on the happy path; skipped entirely for cloud-only
  configs.
- **`nvh doctor` required-models row.** New diagnostic that flags
  "configured but not pulled" models. With `--fix`, interactively pulls
  them all.

### Tests
- New `tests/test_ollama_utils.py` — 18 tests covering tag matching,
  missing-set computation, required-model resolution from config,
  installed-model listing with various HTTP error paths.
- 105/105 passing across affected modules.

## [0.30.1] - 2026-04-16

### Fixed
- **Ollama misdiagnosed "not running" errors** — `ollama_provider.py` was
  raising `ProviderUnavailableError` with "Ollama is not running" for any
  error containing the substring "connect" / "connection" / "refused".
  That matched unrelated failures like `HTTPConnectionPool` timeouts and
  model-not-found errors, sending users on a wild goose chase restarting a
  daemon that was fine. Now actually probes `/api/tags` before declaring
  the daemon down, so we only surface that error when it's real.
- **REPL auto-restart no longer lies** — when pre-check found Ollama
  already running, the REPL would print "Ollama is back up" and tell the
  user to retry, but retry hit the exact same error. Now detects the
  pre-check case and explains the likely real cause (missing model)
  instead of offering a bogus restart.

## [0.30.0] - 2026-04-16

### Added
- **Vision model tier in `recommend_models()`** — `nvh/utils/gpu.py` now returns
  a per-VRAM vision-capable Ollama model alongside the Nemotron + Gemma text
  pair. Tiers: moondream (4–12GB), minicpm-v (12–24GB), llama3.2-vision
  (24GB+). Turing (CC < 8.0) auto-swaps to minicpm-v since llama3.2-vision
  BF16 paths degrade without tensor cores.
- **`nvh openclaw --install` / `nvh nemoclaw --install`** — one-shot commands
  that pip-install the tool, run `register_openclaw()` / `register_nemoclaw()`,
  and smoke-test. Conda/micromamba/venv safe via `sys.executable -m pip`.
- **`nvh doctor --fix`** — when Ollama is enabled in config but daemon isn't
  reachable, interactively offer a restart using the hardened `_start_ollama`.
- **PATH check in `nvh doctor`** — new diagnostic row that detects
  conda/mamba/venv and suggests `micromamba activate <env>` rather than
  editing `.bashrc`.
- **Conda/micromamba aware `install.sh`** — detects active env via
  `$MAMBA_ROOT_PREFIX`/`$CONDA_PREFIX`/`$VIRTUAL_ENV` and offers to install
  into the active env instead of creating a fresh `~/nvh/venv`. Escape hatch:
  `NVH_FORCE_VENV=1` to keep old behavior.
- **REPL Ollama auto-restart** — the "Ollama is not running" error now
  prompts `Restart Ollama now? [Y/n]` and retries via `setup._start_ollama`.

### Fixed
- **Vision model pull ordering** — setup now pulls vision models first so the
  desktop-agent screenshot assist is usable in step 3 (API-key config) even
  if the large 70B text pull is still downloading.
- **Ollama daemon detachment** — `_start_ollama` now passes
  `stdin=subprocess.DEVNULL` and `close_fds=True` alongside
  `start_new_session=True`, so the daemon survives when the setup CLI or SSH
  session exits.
- **Config no longer unconditionally enables Ollama** — `_write_config` now
  re-probes Ollama reachability right before writing and only emits
  `enabled: true` when the daemon is actually up. Prevents the REPL error
  "Ollama is not running at http://localhost:11434" when the user skipped
  the Ollama install.
- **Spurious "env var is not set" warnings** — `_write_config` no longer
  emits `api_key: ${GROQ_API_KEY}` lines for providers the user didn't
  configure. The YAML loader was warning about every unset var on every
  `nvh` invocation.
- **PATH-not-found after setup** — setup now detects when `nvh` isn't on
  PATH and prints env-appropriate instructions (`micromamba activate pyenv`
  for conda users, `export PATH=...` for system installs). "Next steps"
  output falls back to the full path so the user can copy-paste something
  that works.

### Tests
- 134/134 passing across `test_setup.py`, `test_openclaw.py`,
  `test_nemoclaw.py`, `test_coverage_80_batch7.py`, `test_first_run.py`.
- 220/220 passing on the broader `test_cli_inprocess.py` + `test_cli_e2e.py`
  subset (5:56 runtime).
- New tests: vision tier detection (5), vision-pull ordering (5), config
  writer gating (4), PATH check (2), openclaw/nemoclaw pip helpers (4).

## [0.9.0] - 2026-04-09

### Added
- **Ollama + Triton provider tests** — closed the last 2 provider-coverage gaps
  (13 new tests, Ollama 0% → 73%, Triton 0% → 37%)
- **Live uvicorn integration harness** (`tests/test_live_api.py`) — spins up a
  real `uvicorn nvh.api.server:app` subprocess on an ephemeral port and runs
  smoke checks against lifespan hooks, OpenAPI schema, CORS preflight, and
  /v1/models. Catches startup/middleware bugs that the in-process TestClient
  can't see.
- CHANGELOG entries for 0.5.7 → 0.9.0 (this file had drifted since 0.5.1)

### Changed
- Coverage gate raised 28% → 30% (measured baseline is 31%)
- CI workflow `actions/setup-node` bumped v4 → v5 to clear the Node 20
  deprecation warning on the webui job

### Tests
- 450 → 469 passing, 0 failing
- Total coverage holds at 31% with the gate at 30% as a regression floor

## [0.8.0] - 2026-04-09

### Added
- **Parameterized provider contract tests** — one test file exercises all 20
  litellm-backed providers (120 test cases) against the same contract:
  construct, name, estimate_tokens, list_models, complete happy path, complete
  error wrapping, stream yields chunks + final usage. Adding a new provider
  only requires one line in `PROVIDER_SPECS`.
- **In-process Typer CliRunner tests** for `nvh/cli/main.py` — 39 tests that
  walk the full subcommand surface via `CliRunner`, so coverage actually
  moves (subprocess e2e tests don't contribute to pytest-cov).
- **API endpoint coverage pass** — 18 new smoke tests covering every
  documented endpoint that previously had zero tests: /metrics, /v1/system/*,
  /v1/conversations, /v1/locks*, /v1/sandbox/status, /v1/setup/*,
  /v1/agents/analyze, /v1/auth/me, /v1/webhooks, /v1/quota, /v1/context,
  /v1/analytics.
- **Codecov upload** wired into CI with PR-comment delta reporting
- **Coverage gate** ratcheted from 17% to 28%

### Tests
- 244 → 450 passing (+206)
- Coverage 17% → 30%
- nvh/api/server.py coverage 34% → 47%
- nvh/providers/* coverage 0% → 80%+ (20 providers)

## [0.7.0] - 2026-04-09

### Added
- **Windows and macOS added to the CI matrix** — the Python-3.11-on-Windows
  asyncio segfault hid undetected for months because CI was Linux-only
- **WebUI build + typecheck + lint in CI** (new `webui` job in ci.yml) — type
  errors and broken Next.js builds can no longer reach main
- **`pip-audit` dependency vulnerability scan** on every push
- **Wheel build + clean-venv smoke test** job gates releases
- **Dependabot** (`.github/dependabot.yml`) — weekly PRs for pip, npm, and
  github-actions ecosystems with grouped patch/minor updates
- **pytest-timeout** — 120s per-test timeout so hanging tests fail loudly
  with a clear error instead of wedging CI for 30+ minutes
- **Version consistency test** (`tests/test_version.py`) — asserts
  `nvh.__version__` == `pyproject.toml::project.version`
- **WebSocket observability hooks** in `/v1/ws/query` and `/v1/ws/council`:
  every streaming query now calls `rate_manager.record_success/failure` and
  `engine._log_query`, so WebSocket traffic shows up in analytics, budget,
  and circuit-breaker state (was a total blind spot before 0.7.0)
- **Council pre-synthesis budget check** — prevents member queries from
  collectively blowing the budget and then letting synthesis add another
  LLM call on top. Emits `error` event with `phase="synthesis_budget"` on
  cap exceeded.
- **Auth test coverage** (11 tests): missing/malformed/valid tokens for
  Bearer and X-Hive-API-Key, WebSocket auth rejection, register rate limiter
- **Streaming regression tests** (5 tests) locking down the 0.5.9/0.6.0
  synthesis rotation, terminal error events, and budget-check bypass fix
- **Concurrency stress tests** — 20 parallel `engine.query` calls verify no
  lost or duplicated provider dispatches under race conditions

### Fixed
- `test_cli_e2e.py::run_nvh` forces `stdin=subprocess.DEVNULL` so Linux CI
  runners don't inherit a pytest-owned pipe that wedges `sys.stdin.read()`
  in the pipe-detection path of `nvh/cli/main.py`
- WebSocket auth test no longer exercises the full stream path (hit an
  aiosqlite loop-binding deadlock on Linux; the auth contract was verifiable
  without touching the DB)
- Windows `0xC0000005` / exit 139 segfault-on-exit — patched
  `_ProactorBasePipeTransport.__del__` at CLI startup to swallow the GC race
  on httpx transport cleanup (cpython#81485)

## [0.6.0] - 2026-04-08

### Added
- **Live provider health polling** — shared `useProviderHealth` hook polls
  `/v1/advisors` every 30s across all webui pages (home, /query, /council,
  /providers, /setup). "Online/offline" indicators stay accurate throughout
  a session without manual refresh.
- **Home page Q&A layout** — submitted prompt pinned at the top of the
  results panel, synthesis renders above the member deliberations so the
  answer is the first thing you see
- **Health-aware model picker** on the home page — models are grouped into
  "Connected" and "Offline" optgroups sorted by provider latency, and the
  default selection picks the first healthy model (was defaulting to GPT-4o
  even when OpenAI was offline)
- **Pre-flight health gate** on single-query submit — warns inline if the
  selected model's provider is offline, offers to switch to the fastest
  healthy one
- **`/v1/models` live intersection with provider catalogs** — cross-references
  the static capability yaml against each provider's `list_models()` output
  with a 5-minute TTL cache, so deprecated models like the Groq 2 9B entry
  don't leak into the dropdown
- **Council member-resolution warning** — logs WARNING when explicitly-pinned
  advisors are unhealthy, so "why is my council silently failing?" stops
  being a debugging dead end

### Fixed
- Home page council synthesis "disappearing text" bug — stale-closure trap
  where `onComplete` captured the initial empty `synthesisContent`; tracked
  via ref so the final message keeps the streamed text
- Home page model picker defaulting to offline providers (GPT-4o picked
  even when OpenAI was rate-limited)
- WebUI scroll-into-view on synthesis start

## [0.5.9] - 2026-04-08

### Fixed
- **Streaming hangs: complete elimination**. Every streaming path (council
  synthesis, /v1/query SSE, /v1/proxy/chat/completions,
  /v1/proxy/messages) now has:
  - Per-chunk stall timeouts (45s for SSE, 60s per synthesis attempt)
  - Rotation through health-filtered candidates on failure
  - Always-emit-terminal-event contract (error event with `phase`, never
    a silent hang on the client)
- **Silent synthesis failures**: council streaming path used to catch
  exceptions into `failed_members["_synthesis"]` and never emit a terminal
  event, leaving the WebSocket client spinning forever. Now rotates through
  up to 3 health-filtered candidates with per-attempt timeouts, and emits
  a proper `error` event with `phase="synthesis"` when every candidate
  fails.
- **Health-aware provider selection**: `CouncilOrchestrator` now takes an
  optional `rate_manager` and exposes `_is_healthy()` + `_healthy_enabled()`
  helpers. `_synthesis_candidates()` builds a prioritized list (configured
  → healthy non-members → healthy members → unhealthy fallback) so broken
  advisors (GitHub auth error, Google quota exhausted) drop out of rotation
  automatically.
- **CORS default origins** widened to cover the hostnames `nvh webui`
  actually binds (`http://localhost`, `http://nvhive`, ports 80/3000-3002/
  8080) so the WebUI on port 80 can reach the API on 8000 without manual
  `HIVE_CORS_ORIGINS` setup.
- **Council WebUI stall watchdog** — 120s client-side timer resets on every
  WS event, kills the session with a visible error if the backend somehow
  still wedges. Defense in depth behind the server-side fixes.
- Advisor dropdown on `/query` page sorts by health + latency with
  Connected/Offline optgroups

## [0.5.8] - 2026-04-08

### Fixed
- `nvh serve` uvicorn entry-point string — was `council.api.server:app`,
  now correctly `nvh.api.server:app`

## [0.5.7] - 2026-04-08

### Added
- `nvh webui` auto-starts `nvh serve` if the API isn't already running
- `nvh webui --uninstall` and `--clean` for safe reinstall of the webui

## [0.5.1] - 2026-04-05

### Added
- `nvh why` — routing explainability (shows full scoring breakdown for last query)
- `nvh history` — recent query history with costs and timing
- Prometheus metrics endpoint (`/metrics`) — 7 metrics for Grafana dashboards
- Jupyter notebook integration (`%load_ext nvh.jupyter`) — magic commands
- Confidence-gated escalation (`--escalate`) — try free first, upgrade if uncertain
- Cross-model verification (`--verify`) — second model checks for errors
- TF-IDF task classifier (replaced regex keyword matching)
- Council synthesis retry with provider rotation and rate-limit staggering
- `nvh nvidia` GPU detection with automatic Nemotron model pull in setup
- Feature matrix table in README
- NemoClaw demo GIF, GPU detection GIF
- Throwdown mode diagram

### Fixed
- Engine now auto-loads API keys from keyring (setup saves, engine reads)
- Council synthesis reliability on free tiers (retry + backoff + rotation)
- Truthful OpenClaw positioning (complementary, not competitive)
- README reviewed by 10 AI personas, rewritten based on feedback
- All docs updated: provider count (23), test count (225)
- Removed "coming soon" on shipped features
- Fixed broken Nemotron link
- Fixed Mermaid diagram rendering on GitHub

## [0.5.0] - 2026-04-04

### Added
- **Adaptive learning loop** — routing gets smarter with every query via EMA-based score learning
- **Quality benchmark suite** (`nvh benchmark`) — 16 prompts, blind LLM judge, council vs single-model comparison
- **Anthropic API proxy** (`/v1/anthropic/messages`) — drop-in Claude API replacement, one URL change
- **Provider health dashboard** (`nvh health`) — resilience status, fallback chain, health scores
- **Council confidence scoring** — agreement analysis across member responses on every council call
- **OpenClaw migration** (`nvh migrate`) — auto-detect and import OpenClaw/Claw Code configs
- **Infrastructure SDK** — `nvh.complete()`, `nvh.route()`, `nvh.stream()`, `nvh.health()` for tool builders
- **NVIDIA dashboard** (`nvh nvidia`) — GPU hardware, inference stack, local models, --prefer-nvidia status
- **Routing stats** (`nvh routing-stats`) — learned vs static scores, per-provider per-task intelligence
- **Install scripts** — `curl -fsSL https://nvhive.dev/install | sh` with auto-migration
- **Claude Code channel plugin** — real-time events pushed into Claude Code sessions
- **Claude Code integration guide** — MCP server setup documentation

### Changed
- **MCP server hardened** — input validation, timeouts (120s/300s), typed error messages, thread-safe init
- **Provider timeouts** �� all 8 providers now have timeout on litellm.acompletion() calls (120s cloud, 300s Ollama, 15s health)
- **CLI error messages** — actionable messages for auth, rate limit, quota, token limit, provider down errors
- **Router error handling** — per-provider try-catch, skip reason tracking, graceful classification fallback
- **Engine fallback chain** — detailed per-provider failure log in error messages
- **Setup onboarding** — API key validation on paste, OLLAMA_BASE_URL support, post-setup guidance
- **Config validation** — Pydantic Field constraints on all numeric config values
- **Config loading** — error handling for corrupt YAML, validation failures, permissions
- **Env var interpolation** — unresolved ${VAR} warns + returns empty (was silent literal), nested ${VAR:-${OTHER}} resolves
- **litellm bumped to >=1.55** (was 1.40), **keyring bumped to >=26.0** (was 25.0)

### Fixed
- **Auth timing attack** — constant-time comparison prevents username enumeration
- **Password policy** — minimum 8 chars, username validation, role allowlist
- **Scopes mismatch** — auth.py and models.py default scopes aligned
- **API auth gaps** — 8 previously unauthenticated endpoints now require auth
- **Prompt length limits** — 500K char max on all API request models
- **Council streaming timeout** — was hanging indefinitely, now has timeout
- **Council task cleanup** — cancelled tasks now awaited to prevent resource leaks
- **Council label collision** — duplicate providers get unique labels
- **DB indexes** — added on conversation_messages and query_logs for query performance
- **DB integrity** — unique constraint on (conversation_id, sequence)
- **E501 line-length** — zero violations in all modified files

## [0.1.0] - 2026-03-31

### Added
- Initial release
- 22 LLM providers (25 free models)
- Smart routing with advisor profiles
- Auto-agent generation (22 personas, 12 cabinets)
- CLI: nvh ask/convene/poll/throwdown/quick/safe/bench
- Interactive REPL with /commands
- Web UI with NVIDIA theme
- GPU benchmarks (tokens/second)
- Python SDK
- Plugin system
- Hooks, tools, memory, workflows
- Docker deployment with Ollama
- Portable install (no root needed)
- Linux Desktop integration
- HIVE.md context injection
- File lock coordinator for multi-agent safety
- Security: auth, CORS, rate limiting, sanitization
