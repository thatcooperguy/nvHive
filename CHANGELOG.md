# Changelog

## [Unreleased]

## [0.43.0] - 2026-09-03

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
