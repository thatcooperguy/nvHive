# Changelog

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
