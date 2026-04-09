# Changelog

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
