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
  zero-dependency SQLite RAG, env-file secrets with an opt-in keyring. On
  owned hardware (DGX Spark today; RTX Spark once hardware is in hand) the
  helper can do more with approval: privileged actions are a separate tool
  class behind a red card, audited to the vault, never auto-approved.

## Plan by release

| Release | Theme | Contents |
|---|---|---|
| 0.41.1 | hotfix | retired default models on most cloud providers; `$0` cost accounting; unreachable `nvh mcp` / `nvh agent`; `nvh snapshot` archiving the wrong tree; GitHub Models removed |
| 0.42 | subtract | dead orchestration modules, Docker/compose, stale installers and demo assets; `~/.hive`-era modules folded into their `NVH_HOME` successors; provider adapters collapsed into `ProviderSpec` rows; CLI query modes into `nvh ask` flags and diagnostics into `nvh status`; `/query` and `/council` pages removed; docs 33 → 12 with parity and link tests |
| 0.43 | concierge | **merged 2026-09-03; first published in 0.44.0.** One Wizard with hidden specialists (`profile=auto`), the mascot, platform facts (device class, unified memory, sudo) in the prompt, Home Assistant tools, `nvh-linux-arm64` and arm64 CI, one registry-verified local-model table read by every ladder, installer and doc (`nemotron3:33b` MoE-first on unified memory, `num_ctx` passed to Ollama from the tier), `nvh ask --focus research` grounded on nvHive's own web search, Perplexity moved to the Agent API before its 2026-09-27 Chat Completions sunset ([proposal](proposals/SPARK_CONCIERGE_2026-09.md)) |
| 0.44 | refresh + privileged | **shipped 2026-10-31.** The first publication since 0.42.0, so it also carries the 0.43 row above. Refresh wave 1: provider facts live on the `ProviderSpec` rows and [PROVIDERS.md](PROVIDERS.md) is generated from them; `NVH_HOME` is the only root, with a one-shot legacy migration out of `~/.hive`, `~/.council` and `~/nvh`; one `Tool`/`ToolRegistry` with native function calling where the provider vouches for the model. The Spark concierge's **privileged tool class** (red approval cards showing the exact commands, single-use approval tokens, vault audit under `Decisions/`, `NVH_ALLOW_PRIVILEGED` kill switch); Spark playbooks as approved, audited runs, extracted from upstream READMEs and not yet exercised by maintainers on DGX Spark hardware; the Phase 3 bridge that brings the sandbox `run_code`/`shell` and the vision tools into the Wizard behind approval cards, with image attachments in the composer; council correctness, legacy-migration hardening and installer fixes (#138); a shared-compute admission boundary behind the `atohi:` stanza (`nvh/core/atohi.py`, #141, #142), disabled by default; no broker ships. The three refresh items this row once promised did not land here; they shipped in 0.45 |
| 0.44.1 | currency + loopback | **shipped 2026-11-01.** `mcp>=1.0,<3` with a dual-SDK import shim and a CI job that keeps the 1.x branch tested; LiteLLM verified at 1.106.6 with the floor unchanged; `nvh serve` and `nvh mcp` bind 127.0.0.1 by default (`--host` to expose); SECURITY.md; `ci.yml` gains `workflow_dispatch` |
| 0.45 | refresh completion | **shipped 2026-11-01.** The three refresh items re-scoped from 0.44: price, context and capability flags derived from LiteLLM at load (`nvh/providers/registry.py`; `LITELLM_LOCAL_MODEL_COST_MAP=True` is exported at import so the table is the bundled one, and a row nothing prices loads as "cost unknown" rather than free); a live `/v1/models` intersection with each provider's `GET /models`, plus a weekly, non-blocking `model-currency.yml` that also covers the cloud defaults, the Ollama tier tags and the playbook snapshot; a regenerated cloud catalog (`scripts/gen_capabilities.py --check`) with a `source:` line on every override and `nvh/config/model_overrides.json` for the rows LiteLLM lacks. With them, the rest of the refresh (#132): `ProviderSpec` absorbs the last hand-typed provider copies (the config template, the server's key and URL maps, the CLI advisor list, the setup page's provider cards) behind `tests/test_single_provider_table.py`, and the three installers write `config.yaml` from `generate_default_config()` instead of heredocs. Spark hygiene that needs no hardware: playbooks re-extracted from today's upstream with `scripts/check_playbook_upstream.py` against a committed snapshot, sha256-verified downloads and the VS Code manifest check, cancel between playbook steps, the privileged tier refusing in plain words on Windows and macOS, an ARM64-aware `install.ps1`, a `legacy_homes` row in `nvh status --deep`, derived agent cost, one native tool-instruction block under function calling with `Engine.query(exclude_providers=...)`, generated CONFIGURATION.md blocks (`scripts/gen_config_doc.py`), `nvh.data` loaders for the catalog, benchmark and workflow files, and a hermetic test suite. What was held out of this tag is listed, with the reason for each, in the 0.46 row; the `LOCAL_MODEL_TIERS` qwen3.6 review is an owner decision (Known follow-ups) |
| 0.46 | add (second re-scope) | **shipped 2026-11-02.** Seven of the eight add items planned for 0.44 and moved to 0.45 in September ship here, on their second re-scope (#133): a Wizard `ask_user` tool that pauses a turn with a question card; a `remember` tool with `/v1/vault/notes` and a Memory filter on `/vault`; `generate_image` (NVIDIA-hosted or local ComfyUI; the paid path only within the profile cost ceiling); the standard-path OpenAI endpoints and `nvh launch <tool>`; durable schedules under `NVH_HOME` with `/v1/schedules` and a `/schedules` page; session cost against the hourly desktop rate; hold-to-talk speech-to-text and Read aloud in the WebUI. The eighth, one chat surface, is re-scoped to 0.47: the Wizard thread was extracted to `web/components/chat/WizardThread.tsx` and resumes from `?conversation=` or `?c=`, but the shell merge (C1b) was held because O9 was not confirmed before the tag, so `/wizard` stays a page and the `/query` and `/council` redirects stay with it, re-dated to retire together with the one-release `/wizard` redirect that follows the merge (O14; its 0.47 date assumed a 0.46 merge and is the owner's to re-set). The design is [proposals/ADDS_0_46_DESIGN_2026-11.md](proposals/ADDS_0_46_DESIGN_2026-11.md): four gate-able waves with disjoint file ownership and fourteen owner decisions, each with a default that applied when unanswered, except O9, whose yes had to be explicit and was not given. Wave A (schemas and hygiene): every vault memory note is an `nvh-memory/1` record with a stable id and `state/schedules.json` an `nvh-schedules/2` store, both documented by generated blocks in [CONFIGURATION.md](CONFIGURATION.md#memory-records-nvh-memory1); `nvh/api/deps.py`; the standard `/v1/chat/completions`, `/v1/completions` and `/v1/anthropic/v1/messages` paths; `bind_host`, `auth_mode` and `key_required` on `/v1/health`; the Anthropic proxy and the integrations scan behind the key; streamed proxy and Wizard turns budget-checked and written to the ledger. Wave B (server features): the three Wizard tools with `nvh/integrations/imagegen.py` and the authenticated outputs route; schedules run inside the API process behind a heartbeat lock, with the seven `/v1/schedules` routes, `schedule-run` jobs and the `nvh vault` group; `budget.hourly_rate_usd` and the `GET /v1/budget/status?since=` session block; history and tools forwarded through the OpenAI-compatible handler. Wave C (the web surfaces): the question card, the plan line and inline image render on confirm cards, the `/vault` notes list with its Memory toggle, the session cost pill, the `/schedules` page, the `/integrations` standard-path snippet and the chat surface's move to `web/components/chat/`. Wave D (`nvh launch` and voice): `nvh launch <tool>` with five bundled targets (`claude` registers the MCP server and launches, the other four print their snippet; four client-side refusals read from `/v1/health`), the faster-whisper backend with the four `/v1/voice` routes, the hold-to-talk mic and Read aloud toggle in the composer, the `voice` extra outside `all` with its Windows-on-Arm marker, and the CI steps that audit and smoke them. O6 verdict: the informational arm64 CI step installed faster-whisper 1.2.1 with ctranslate2 4.8.4 and imported it on `ubuntu-24.04-arm` (Python 3.12), and every voice test passed on that runner once a colour-only `--help` assertion was fixed (the last `main` run before the tag), so `nvhive[voice]` ships as the local speech-to-text path: verified by import on the Linux x86_64 and arm64 CI runners (the DGX Spark target, not yet Spark hardware), installable everywhere but Windows on Arm, where the marker leaves it out, it installs nothing and the mic uses Groq Whisper when a key exists. The five items held out of the 2026-11-01 tag rather than slipping it ship here too, each with the gap 0.45 left: the web cost pill's three states (0.45 shipped the API field `cost_known` but the pill did not read it; now it is on every envelope that carries `cost_usd`, the council WebSocket and persisted rows included, and a priced `$0` reads `$0.0000`, an unpriced one `cost unknown`); process-group kill of an in-flight playbook command (0.45 stopped between steps only), with `killed`, `kill_failed` and `timed_out` told apart on the receipt and the vault note; the `--verify` cross-check honouring `exclude_providers` (only `--focus research` passed it in 0.45) and never letting a provider grade its own answer; `setup_catalog`, `profiles` and `setup_agent` reading bundled files through `nvh.data` (they were still on `importlib.resources` directly) behind `tests/test_bundled_data_access.py`; and the pre-0.44 fenced `tool_call` block and bare `{"tool": ..., "args": ...}` object dropped from the agent loop's parser (0.44 said one release and 0.45 still accepted them under `legacy=True`; `parse_tool_calls` has no `legacy` parameter). The `LOCAL_MODEL_TIERS` qwen3.6 review is an owner decision, not scheduled here (Known follow-ups). RTX Spark (Windows on Arm) work waits for hardware facts (#136 Phase C) |
| 0.47 | simplify | target to be set by the owner; the plan is [proposals/SIMPLIFY_0_47_DESIGN_2026-11.md](proposals/SIMPLIFY_0_47_DESIGN_2026-11.md) (2026-11-04: one chat home, five sidebar destinations plus a Settings group, a guided first run, a rewritten README, the 0.46 follow-ups that cause a dead end). The C1b shell merge, gated on O9 (unanswered at the 0.46 tag): `/` mounts `WizardThread`, `/wizard` becomes a one-release redirect, the thread links move to `/?c=`, and the `/query` and `/council` redirects retire together with it (O14); plus the Known follow-ups below that name 0.47 |

## Features

Status: **shipped** · **planned** (in a release above) · **re-scoped** (moved
to a later release; the notes say why) · **present-but-hidden** (exists in the
engine or CLI, not yet surfaced in the WebUI) · **open** (wanted, unscheduled)
· **declined** (see Non-goals).

| Feature | Status | Notes |
|---|---|---|
| Chat history sidebar — browse, resume, pin, search | shipped 0.41 | one server-side store, one-time localStorage import in 0.42 |
| In-app model browser with VRAM-fit preview and live progress | shipped 0.41 | the Model Manager ([MODELS.md](MODELS.md)) |
| MCP client — external tool servers in the Wizard | shipped 0.41 | `mcp-servers.json`, confirm-by-default, per-server auto-approve |
| Agent Library — bundled profiles grouped by category | shipped 0.41 | `nvh/catalog/agent-library.json`; copy into `$NVH_HOME/agent-profiles/` |
| Per-agent cost ceiling | shipped | enforced per iteration from reported usage in the Wizard, on the blocking and the streaming path; remaining gap: mid-stream cut-off and providers that report no usage |
| Provider-key paste with auto-detect; GPU-fit recommender card; reconnect panel on every page | shipped | |
| Code execution from the Wizard, surfaced in chat | shipped 0.44 | `shell` as a privileged Wizard tool (red card shows the command and the isolation it will get), `run_code` confirm-class and Docker-only; rules in [CONFIGURATION.md](CONFIGURATION.md#code-and-images-from-the-wizard-chat) |
| Persistent cross-chat memory in the WebUI | shipped 0.46 | the `remember` tool, `/v1/vault/notes` and `nvh vault list`/`forget`/`upgrade` landed in Wave B; the `/vault` notes list with its Memory toggle, Forget and `?id=` highlight and the `remember` card's "Open in vault" link landed in Wave C. The vault is already RAG-queryable. Since Wave A every note is an `nvh-memory/1` record with a stable `Id:`; the on-disk schema is the generated `memory-record` block in [CONFIGURATION.md](CONFIGURATION.md#memory-records-nvh-memory1), so a later state import is possible |
| Image generation from chat | shipped 0.46 | `generate_image` (a confirm card with a plan line), `nvh/integrations/imagegen.py` and the authenticated `GET /v1/outputs/images/{scope}/{filename}` landed in Wave B: NVIDIA-hosted when `NVAPI_KEY` is set, else local ComfyUI, and never the paid path under a profile cost ceiling while the price is unknown; `generate_portrait` is a shim over it. The card's `plan:` line and inline authenticated image render landed in Wave C |
| One chat surface (`/` and `/wizard` merged) | landed on main (0.47) | S2 of the simplify plan: `/` hosts the Wizard as its default mode with Ask, Convene and Poll beside it, `/wizard` redirects there (renaming `?conversation=` to `?c=`) for one release, every thread link is `/?c=`, and Enter sends in every mode. Before: two surfaces confuse first-time users; the Wizard thread was extracted in Wave C to `web/components/chat/WizardThread.tsx` (a `conversationParam`, resuming from `?conversation=` or `?c=`), and the shell merge (C1b in the design) is held for 0.47 because O9 was not confirmed before the 0.46 tag; `/wizard` stays a page and the thread links keep `/wizard?conversation=` until then, and the `/query` and `/council` redirects stay with it, re-dated to retire together with the one-release `/wizard` redirect that follows the merge (O14; its 0.47 date assumed a 0.46 merge and is the owner's to re-set) |
| Hidden specialists — the Wizard picks the agent per turn (`profile=auto`) | merged 2026-09-03, published in 0.44.0 | [Spark concierge](proposals/SPARK_CONCIERGE_2026-09.md) §3.1; the picker leaves the composer |
| Mascot — sprite-based avatar reacting to Wizard state | merged 2026-09-03, published in 0.44.0 | placeholder sprite ships; a real-person likeness needs sign-off ([MASCOT.md](MASCOT.md)) |
| Home Assistant operator (smart home via the HA REST API) | merged 2026-09-03, published in 0.44.0 | five Wizard tools, service calls behind a confirm card |
| Device settings and app installs with sudo (Spark playbooks) | shipped 0.44 | the `privileged` tool class, its red card and the `NVH_ALLOW_PRIVILEGED` kill switch are described in the [environment variables](CONFIGURATION.md#environment-variables) and [Tools](CONFIGURATION.md#tools) sections of CONFIGURATION.md; DGX OS only, and the playbooks are not yet exercised by maintainers on hardware; since 0.45 the tier refuses in plain words on Windows and macOS (`nvh/integrations/wizard/system_settings.py`), the scripts taken from the upstream repositories (ComfyUI's `setup.sh` and `launch.sh`, the vLLM launch script) and the VS Code `.deb` are sha256-verified while the ollama, Claude Code and LM Studio installers stay documented as `pipe-to-shell: unpinned`, and every playbook is tracked against upstream by `scripts/check_playbook_upstream.py`; Windows on Arm is unverified (#136 Phase C) |
| Standard-path OpenAI endpoints; `nvh launch <tool>` for Claude Code / Codex / opencode / Cursor / Continue | shipped 0.46 | `/v1/chat/completions`, `/v1/completions` and `/v1/anthropic/v1/messages` landed in Wave A beside `/v1/proxy/...`; history, `tools` and `stream_options.include_usage` through the handler in Wave B (`x-nvhive-history: flatten` opts out); `/integrations` shows the standard-path snippet since Wave C; `nvh launch <tool>` landed in Wave D with five bundled targets in `nvh/data/launch_targets.json`: `claude` registers nvHive's MCP server and launches (inference stays with Anthropic, O11), `codex` / `opencode` / `continue` / `cursor` print their snippet, `--print` works without an API, and the refusals (open mode off-loopback, a keyed API without `HIVE_API_KEY`, users mode without a token, a held port under `--start-api`) are read from `/v1/health`; every row ships `verified_on: null` and prints unverified until a maintainer runs the tool ([INTEGRATIONS.md](INTEGRATIONS.md#nvh-launch)) |
| Scheduled prompts with a UI | shipped 0.46 | the runner inside the API process, `schedule-run` jobs under `NVH_HOME`, the seven `/v1/schedules` routes and `nvh schedule enable`/`disable`/`run` landed in Wave B; the `/schedules` page landed in Wave C. Since Wave A the store is `nvh-schedules/2` with a stable id per record and `Scheduler.mark_run` recording each run's outcome; the on-disk schema is the generated `schedule-record` block in [CONFIGURATION.md](CONFIGURATION.md#schedule-records-nvh-schedules2), so a later state import is possible |
| Session cost vs hourly desktop rate | shipped 0.46 | the "am I wasting GPU money" pill; `budget.hourly_rate_usd` and the `GET /v1/budget/status?since=` session block landed in Wave B, the pill and the `/settings` Costs card in Wave C |
| Voice in the WebUI (push-to-talk STT, optional TTS) | shipped 0.46 | Wave D: a hold-to-talk mic in the Wizard composer (the transcript is inserted, never sent) and a Read aloud toggle through the browser's `speechSynthesis`; speech-to-text is local-first — faster-whisper through the `nvhive[voice]` extra after an explicit **Download model**, else Groq Whisper when a key exists (cost unknown), else the mic says what it needs; `nvh voice --stt auto|local|groq|whisper-cli`, `--status` and `--download` on the CLI; server TTS (`POST /v1/voice/speak`) is Edge TTS when on PATH, else 501, and there is no local TTS pack ([CONFIGURATION.md](CONFIGURATION.md#voice-from-the-wizard-chat)) |
| Quantisation variant picker; context length / GPU offload / KV-cache controls | open | `num_ctx` from the VRAM tier merged 2026-09-03, published in 0.44.0; the UI is unscheduled |
| Import an arbitrary GGUF or HF repo into Ollama | open | partial today via `nvh models pull <tag>` |
| Pack uninstall and disk reclamation; per-app start/stop/logs; update detection | open | receipts already carry uninstall and repair plans |
| Hybrid BM25 + vector RAG with reranking | open | SQLite store today; embedding-model swap by VRAM is the cheap first step |
| Vision input and OCR in chat | shipped 0.44 | `analyze_image` / `read_text_from_image` bridged into the Wizard behind a path allowlist; images attach in the chat composer |
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

- **mcp SDK 2.x.** Closed in 0.44.1. The pin is `mcp>=1.0,<3` (the `[cli]`
  extra is dropped; nvHive never shells out to `mcp dev`), and
  `nvh/mcp_server.py` plus `nvh/integrations/mcp_client.py` run on both SDK
  majors behind a dual-SDK shim: `mcp.server.mcpserver.MCPServer` is bound
  first and `mcp.server.fastmcp.FastMCP` second, constructor keywords are
  probed (2.x also receives `version=`), and the client reads snake_case
  then camelCase attributes. The `TypeError` that `nvh mcp -t
  streamable-http` raised on mcp 1.x (whose `run()` takes no host or port)
  is fixed in the same release. `nvh status` prints the active `mcp`
  version for support threads.
- **Shared-compute admission boundary** behind the `atohi:` stanza
  (`nvh/core/atohi.py`, #141, #142): disabled by default; no broker ships;
  see [CONFIGURATION.md](CONFIGURATION.md#atohi-admission-disabled).
- **Streaming cost ceiling.** Shipped: enforced per iteration from reported
  usage in the Wizard, on the blocking path (pinned by
  `tests/test_agent_cost_ceiling.py`) and the streaming path (pinned by
  `test_stream_done_reports_cost_from_final_chunk_and_enforces_ceiling` in
  `tests/test_wizard_profile_enforcement.py`). Remaining gap: mid-stream
  cut-off and providers that report no usage.
- **Left from Wave A** (the 0.46 schemas-and-hygiene wave, shipped in
  0.46.0). Closed in Wave B: streams through an OpenAI-compatible
  provider request `stream_options.include_usage` whenever LiteLLM's
  parameter table says the route takes it (routes it says lack it keep
  the estimate: `0` input tokens, output counted from the text); the
  `scheduler` doctor row and `nvh schedule list` name a quarantined
  `schedules.json`; writers inside the API process share one lock and the
  runner re-reads the store at every write. The `x-api-key` question (the Anthropic SDKs'
  header, so `ANTHROPIC_API_KEY=<key>` was a 401) is closed in 0.47:
  `require_auth` reads `x-api-key` as a second spelling of the same
  credential.
- **Left from Wave B** (the server-features wave, shipped in 0.46.0).
  A transcript posted to the OpenAI-compatible handler that ends in an
  assistant or tool message (an agent loop returning tool results) is
  was flattened to the pre-0.46 string rather than forwarded as history,
  because `Engine._build_messages` would append an empty user turn. Closed
  in 0.47: the engine, the stream generator and the handler forward it
  with an empty prompt (`if prompt or not history`), and only an empty
  transcript is a 400. A CLI `nvh schedule add` landing in the same
  instant as the API runner's write could lose one record: the
  in-process lock covered one process and the heartbeat lock covers
  runners only. Closed in 0.47: `Scheduler._save` holds `<store>.lock`
  across the read-modify-write and keeps any record another process saved
  meanwhile.
  The session block's `local_only` and `cost_known` were vacuously true for
  an idle tab (no model answered since `since`). Closed in 0.47: the block
  carries `pairs` (every request/response row in the window, failed ones
  included) and `cost_known` is `null` when nothing was counted; the pill
  reads `local only` only when `pairs` is above zero, so an idle tab shows
  the plain age and a tab whose only call was a failed local one reads
  `local only`. A `convene` run's per-member catalog verdict is still
  missing (its total is `$0` with `cost_known` from the window, not from
  the council). `_spend_by_provider_since` lives in
  `nvh/api/server.py` rather than the repository (a behaviour-neutral
  move), and the avatar route probes ComfyUI twice per request when it is
  installed but down. The MASCOT.md rows for the question card and for
  non-auto `tool_call` events landed with Wave C.
- **Left from Wave C** (the web-surfaces wave, shipped in 0.46.0).
  The `/vault` list filters client-side over the newest page of headers
  (the footer says so); re-querying the server with `memory_only` / `q`
  when the page is full is the next step if real vaults outgrow it.
  Cards are not rehydrated on reload (pre-existing), so a `remember`
  card's vault link and an image render live only on the live card. The
  question card's Answer is disabled while any turn streams rather than
  queued; the card is keyed by tool name because the server surfaces one
  `ask_user` per turn (`toolKey` is the one place to change). `starter`,
  `background` and `accent` are typed but not drawn on `/agents` (a 0.47
  S4 item); the thread's empty state no longer carries a hand-typed starter
  list — since 0.47 its starters are the profiles' `starter` field, and its
  first-run lines (the local model, three starters, where the rest lives)
  come from the pure `web/lib/firstRun.ts`. A configured hourly rate of exactly 0
  keeps the `desktop ≈ $0.0000` term (O4 read literally) and only the
  amber rule is suppressed. The `/schedules` Reconnect re-tails a run
  from its first event; `Schedule.mode` is typed loosely so a CLI-written
  `do` record still lists; thread links read `/wizard?conversation=`
  until the 0.47 shell merge (C1b, O9) moves them to `/?c=`.
- **Left from Wave D** (the launch-and-voice wave, shipped in 0.46.0).
  `claude` carries `verified_on: 2026-11-05` (Claude Code 2.1.257 accepts
  the registration command and lists the server); the other four print
  unverified until a maintainer runs the tool against its current version.
  Codex CLI 0.152.1 refuses `wire_api = "chat"` and speaks only the
  Responses API, which nvHive does not serve, so the `codex` snippet does
  not load on current Codex: a `/v1/responses` endpoint (a translation over
  the chat-completions handler) is a 0.47 S3 or 0.48 item. Detector
  rows for Codex, opencode and Continue were not added because `nvh
  integrate --auto` has no registration step to run for them. The route's
  `model` allow-list is faster-whisper's sizes plus the `Systran/` and
  `mobiuslabsgmbh/` owners, so a server whose `NVH_VOICE_STT_MODEL` names
  another owner keeps working but a client cannot request it over HTTP;
  `--stt-model <dir>` must be spelled as a path. `model_present` requires
  the CTranslate2 trio (weights, config, vocabulary) — reasoned from the
  upstream layout, since faster-whisper is not installed on the
  maintainer's box. The Space hold threshold (200 ms) and the recording
  path are verified by Node tests and tsc only; a hold and a tap on
  localhost is the owner's check. Cancelling a `voice-model-download` job
  stops the record and its stream but the worker inside huggingface_hub
  runs to completion; the partial snapshot is reused. The Groq keyring
  lookup was ungated by `NVH_USE_KEYRING`; closed in 0.47:
  `_keyring_groq_key` honours the same gate as the registry. The arm64 informational CI step decided O6 at pin
  time: `faster-whisper` 1.2.1 installed and imported on `ubuntu-24.04-arm`,
  so the extra is the local path there (recorded in the 0.46.0 notes).
  `POST /v1/voice/speak` is unused by the web (the browser reads aloud)
  and `edge-tts` stays an undeclared, opt-in subprocess; Piper is not in
  0.46.
- **Accepted dependency finding (0.46.0).** `pip-audit` lists thirteen
  advisories against `filelock` 3.32.x (fixed in 4.2.1); it arrives through
  `litellm`, which pins `filelock<4.0`, and nvHive imports none of its API.
  Accepted with the reason in SECURITY.md; the floor becomes
  `filelock>=4.2.1` the moment litellm allows 4.x (watch the weekly
  model-currency run and Dependabot for that litellm release).
- **Carried from 0.45.** The five items held out of the 2026-11-01 tag
  shipped in 0.46.0 and are listed in the 0.46 row above.
  Left from them: a `cost_known` column on `conversation_messages`, so a
  council-mode row keeps its verdict across a reload (today the verdict is
  recomputed on read from the stored model, which a council total lacks, so
  a `$0` council total shows its historical state after a reload); whether a
  `TOOL_CALL:` line quoted inside a foreign code fence should still fire
  (today it does; skipping fences would silence models that wrap the
  protocol line in one, so this is an owner call); and the POSIX kill
  integration test's first real run happened (ubuntu CI run 41351349212
  green, the arm64 job included); the root `sudo -n kill` escalation is
  still exercised through a fake process only.
- **`LOCAL_MODEL_TIERS` review for qwen3.6.** The table
  (`nvh/core/local_models.py`) is on qwen3 today; whether to move it is an
  owner decision, so the review is unscheduled rather than placed in 0.46.
- **Phase 4 (Spark binary, unified-memory probe)** waits on hardware facts
  (#136 Phase C); nothing is scheduled until an RTX Spark report arrives
  through the hardware-facts issue template.

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
  budget, `install.ps1` and `install-mac.sh` stay best-effort for Windows and
  macOS (`install.ps1` is ARM64-aware since 0.45: native arm64 Python first,
  Ollama's arm64 build, GPU acceleration on Windows on Arm unverified), and
  the LLM already answers in the user's language. One
  planned exception (owner decision 2026-09-02, #136): NVIDIA's
  unified-memory desktops. DGX Spark (Linux aarch64) became a supported
  target with the 0.43 concierge work, first published in 0.44.0, and RTX
  Spark (Windows on Arm) becomes one once
  hardware is in hand (#136 Phase C); both are the owned counterpart to the
  rented GPU desktop, not a new platform category.
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
  the Typer registry or the spec tables is generated and parity-tested
  (since 0.45: `nvh/providers/registry.py` reads price, context and flags
  from LiteLLM at load, `scripts/gen_capabilities.py --check` guards the
  catalog and `tests/test_single_provider_table.py` the provider copies).
- **New parallel implementations** (another memory store, agent dataclass,
  sandbox, tool protocol, chat store, or `~/.something` home) — every
  high-impact defect in the audit was a CLI/API/Wizard triple copy that had
  already drifted; new features plug into the single instance or are
  declined.

## Threats to watch

- Open WebUI shipping a rootless cloud-desktop installer with reconnect
  survival.
- Ollama bundling a polished GUI and first-party web search (verify: not
  checked against current Ollama releases).

Back to [README](../README.md)
