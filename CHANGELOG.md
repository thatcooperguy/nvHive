# Changelog

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
