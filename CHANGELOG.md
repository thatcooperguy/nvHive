# Changelog

## [Unreleased]

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
