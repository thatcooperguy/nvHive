# Testing

How the suite is laid out, how CI runs it, and how to add a test that will
still be green on a rented GPU desktop with no keys and no network.

## Running

```bash
pip install -e ".[dev]"
python -m pytest tests/ -q                      # everything (a few minutes)
python -m pytest tests/test_council.py -q       # one subject
python -m pytest tests/ -k "snapshot and not live" -q
python -m pytest tests/ --cov=nvh --cov-report=term-missing
python -m ruff check nvh/ tests/ --ignore E501,E402,N806,E702,F841   # CI's exact rule set
python -m mypy nvh/sandbox nvh/catalog --strict --follow-imports=silent --ignore-missing-imports
```

`pyproject.toml` configures pytest: `asyncio_mode = "auto"` (async tests need
no marker), a 120 s per-test timeout enforced by `pytest-timeout` (declared in
`required_plugins`, so a missing plugin is a startup error rather than a
silent no-timeout run), and `filterwarnings` for three known-benign upstream
warnings. Tests run from any directory; paths are resolved from
`Path(__file__)`. Two markers are registered: `network` (opt-in live probes,
`NVH_NETWORK_TESTS=1`) and `slow` (subprocess-heavy CLI, live-uvicorn and stdio
MCP server tests; `python -m pytest tests/ -m "not slow" -q` is the quick local
run). Test processes use LiteLLM's bundled model-price map
(`LITELLM_LOCAL_MODEL_COST_MAP`, set by `nvh/__init__.py` and again in
`tests/conftest.py`) so no run depends on a GitHub fetch at import time.

## Layout

`tests/` is flat: one `test_<subject>.py` per module or feature, no
subdirectories. `tests/conftest.py` holds the shared `db` fixture (a fresh
SQLite database bound to the repository for one async test) and its
synchronous twin `sync_db` for `TestClient`-driven tests, plus three autouse
fixtures every test gets: a throwaway home (`NVH_HOME`, `Path.home()`,
`HOME` / `USERPROFILE` and the storage knobs all point away from the
developer's; a module that must see the real home sets `HERMETIC_HOME =
False` at module level), seeded platform facts (no `sudo -n` / `nvidia-smi`
probes) and a stubbed local-daemon probe (`HERMETIC_LOCAL_PROBE = False`
opts a module out). A module fixture must not reuse the name
`_hermetic_home`: it would shadow the conftest one and silently skip the
patching. Everything else lives in the file that needs it. When a module is deleted its test file goes
with it; when a module is renamed the test file is renamed too. A handful of
files are guards rather than unit tests:

| File | Guards |
|---|---|
| `test_version.py` | `pyproject.toml` and `nvh.__version__` agree |
| `test_commands_doc_parity.py` | `docs/COMMANDS.md` matches the Typer registry byte for byte |
| `test_marketing_parity.py` | no hand-typed provider/model/free/cabinet/tool/persona/agent counts in README, docs or CLI strings unless they equal the derived value |
| `test_docs_links.py` | every relative link and image in README and `docs/` resolves; every doc has an inbound link |
| `test_mcp_cabinet_sync.py` | the MCP server's cabinet set is `COUNCIL_PRESETS` |
| `test_provider_defaults.py` | the settings template and the API server's copy of the provider defaults carry the same, current model IDs |
| `test_single_provider_table.py` | no file under `nvh/`, `scripts/` or the WebUI setup page types a provider fact (display name, key variable, URL, model id) that `nvh/providers/specs.py` already holds |
| `test_bundled_data_access.py` | `nvh/data/__init__.py` is the only module under `nvh/` that touches `importlib.resources` (any spelling, the `importlib_resources` backport and `pkgutil.get_data` included); the setup catalog, Agent Library and Wizard prompt loaders return what the direct read returned |
| `test_capabilities_catalog.py` | `nvh/config/capabilities.yaml` is canonical (`scripts/gen_capabilities.py --check`), every hand-typed override row has a `source:`, every provider default and fallback has a price source on whatever LiteLLM is installed (Hugging Face excepted: metered upstream, loads as cost unknown), retired rows equal the rename table, and loading the shipped catalog opens no socket and attempts no HTTP request |
| `test_model_currency.py` | `network` tier: the providers still serve the catalog's and the specs' model ids; its offline drift report (each override beside today's LiteLLM value, each active row nothing prices any more) is pinned without the network |
| `test_release_hardening.py` | `install.sh`, `nvh services` and the docs keep the bring-up contract |
| `test_cli_mcp_group.py` | no name is both a command and a command group (the bug that hid `nvh mcp` and `nvh agent`) |
| `test_config_doc_parity.py` | the generated blocks of `docs/CONFIGURATION.md` match their sources byte for byte (`scripts/gen_config_doc.py --check`): `wizard-tools` from the stock Wizard registry, `council-cabinets` from `COUNCIL_PRESETS` / `PRESET_DESCRIPTIONS`, `memory-record` from `MemoryRecord` (`nvh-memory/1`), `schedule-record` from `ScheduledTask` (`nvh-schedules/2`) and `wizard-meta` from `WIZARD_META_KEYS`, one row per dataclass field or key in source order; plus the kill-switch row's privileged-tool list and the legacy-homes paragraph's doctor row |

## Kinds of tests

- **Unit** - the bulk: routing, council, agents, tools, storage, config.
  Providers are exercised through `MockProvider` (`nvh/providers/mock_provider.py`)
  or by patching `litellm`; nothing in the default run talks to a real
  provider.
- **API** - FastAPI's `TestClient` runs `nvh.api.server:app` in-process
  (`test_api.py`, `test_auth.py`, `test_chat_history.py`, ...).
- **Live server** - `test_live_api.py` spawns `uvicorn` on a free port and
  probes `/v1/health`, CORS and a WebSocket upgrade; this is the only place
  lifespan startup runs for real.
- **CLI** - `test_cli_inprocess.py` drives the Typer app with `CliRunner`;
  `test_cli_e2e.py` spawns the `nvh` console script (`encoding="utf-8",
  errors="replace"` - copy that on Windows).
- **MCP** - `test_mcp_client.py` runs a real stdio MCP server through the SDK
  pinned in the `dev` extra.
- **Installer** - `ci/integration-test-install.sh` installs the exact clean,
  committed checkout in a disposable Ubuntu container with no GPU access.
  The installer workflow runs `NVH_TEST_SKIP_MODEL=1 bash ci/integration-test-install.sh`
  for installer pull requests. This downloads the Ollama binary and Python
  dependencies, verifies the candidate package, workspace/config persistence,
  CLI, and local Ollama health, and checks that no models were downloaded.
  It does not test inference or model downloads. Run this gate before merging
  changes to `install.sh`; changes to model acquisition also require the
  explicit model-enabled invocation (`NVH_TEST_SKIP_MODEL=0`) on a suitable
  disposable host. That additional check records a pull attempt, not an
  inference-quality claim.
  The harness refuses dirty tracked files, shallow history, and the formerly
  ignored `NVH_TEST_DOCKERFILE` option. It builds from a Git bundle, excluding
  local untracked files, Git credentials/config, caches, and home directories.
  Its container-local Git redirect leaves the normal installer source URL
  unchanged. Windows persistence contracts run with
  `pwsh -File ci/test-windows-installer.ps1`; they execute extracted installer
  logic against fake user settings and native commands, including a mocked
  ARM64 case, with no installation or registry writes.

  Linux shell-path contracts (`python3 ci/test-linux-installer-env.py`) source
  generated environment/profile code and command shims with inert services.
  macOS contracts (`python3 ci/test-macos-installer.py`) run the installer with
  disposable homes and inert dependency commands, including later-shell
  readback in Bash and Zsh where available. These are control-flow/path checks,
  not actual Windows/macOS dependency installations or transactional rollback
  guarantees for a partially completed fresh install.

## CI

`.github/workflows/ci.yml` runs on every push and pull request to `main`:

| Job | What |
|---|---|
| `test` | Ubuntu, Python 3.11 and 3.12: `ruff check` with the rule set above, `pytest tests/ -v`, then a coverage gate (`--cov-fail-under=49`) on 3.12 with upload to Codecov |
| `typecheck` | `mypy nvh/sandbox nvh/catalog` gates; a repo-wide mypy runs as an informational signal and grows the gated list as modules reach zero errors |
| `webui` | `npm ci`, `tsc --noEmit`, ESLint (findings warn, a crashed linter fails), `npm run build` on Node 22 |
| `security` | `pip-audit` (informational) |
| `build` | wheel + sdist, `twine check`, install into a clean venv and run `nvh version` / `nvh --help` |
| `mcp-v1` | Python 3.12 with `mcp<2` pinned beside `.[dev]`: `tests/test_mcp_server.py`, `tests/test_mcp_client.py`, `tests/test_mcp_cabinet_sync.py` and `tests/test_cli_mcp_group.py`. The `mcp` extra resolves the 2.x SDK, so the `test` job only ever exercises that branch of the dual-SDK shim in `nvh/mcp_server.py` and `nvh/integrations/mcp_client.py`; this job keeps the 1.x branch (`mcp.server.fastmcp`) green in CI rather than only in a maintainer's venv (`tests/test_release_hardening.py` pins the job's pin and file list) |

Linux is the only OS in the matrix on purpose; Windows and macOS are
best-effort until the Linux journey is stable. The CLI presentation layer
(`nvh/cli/main.py`, `repl.py`, `completions.py`, `conversations.py`) is
excluded from the coverage metric and covered by the subprocess tests instead.

## Writing a test

- Isolate the workspace: point `NVH_HOME` (and `NVH_CONFIG`, legacy alias
  `HIVE_CONFIG_HOME`, when the test reads config) at `tmp_path` with
  `monkeypatch.setenv`, and call `nvh.providers.registry.reset_registry()`
  if you register providers.
- No network, no keys, no real Ollama in the default run. If a test must
  reach one, skip it unless an explicit opt-in variable is set.
- Prefer testing behaviour through the public entry point (`Engine`, the
  Typer app, the FastAPI app) over private helpers.
- Ollama attempts direct HTTP before its LiteLLM fallback. Mocking only
  `litellm.acompletion` can contact a running local model. The provider unit
  suite blocks unmocked HTTP and daemon probes; error tests mock both paths.
  Pin a fake provider when testing query behavior unrelated to connectivity,
  and mock host tools such as Docker in endpoint shape tests.
- Regenerate `docs/COMMANDS.md` when you add, rename or hide a command, and
  keep counts out of prose: the parity tests will tell you if you forgot.
  The same goes for the other generators: `scripts/gen_providers_doc.py`,
  `scripts/gen_models_doc.py` and `scripts/gen_capabilities.py` each take
  `--check` (exit 1 when the committed file is stale) and their parity tests
  run exactly that.
- A test that touches the model catalog resets every singleton it used:
  `nvh.providers.registry.reset_registry()` and `reset_catalog_state()`,
  `nvh.providers.openai_compatible._catalog.cache_clear()` and
  `_COST_FALLBACK_LOGGED.clear()`, and `nvh.api.server._LIVE_MODELS_CACHE.clear()`.
- A test that reads a doc (`test_release_hardening.py`,
  `test_docs_links.py`) is a contract: update the doc and the test together.

Run the focused admission regressions with:

```bash
pytest tests/test_atohi_admission.py \
  tests/test_atohi_awaitable_contract.py \
  tests/test_atohi_bound_model_dispatch.py \
  tests/test_atohi_lifecycle_isolation.py \
  tests/test_atohi_setup_probes.py \
  tests/test_atohi_rag_binding.py \
  tests/test_atohi_auxiliary_vision_benchmark.py \
  tests/test_atohi_proxy_pause.py -q
```

These tests use local provider, transport and broker doubles. They cover policy
isolation across reused registries and tools, late adapter registration,
revocation through transport cleanup and broker exit, setup/preload and RAG
bindings, vision/benchmark admission, terminal HTTP/SSE pause responses,
asyncio Awaitable and iterator contracts under an inert lease authority, and
registry/RAG dispatch over explicitly issued local test transports (Codex PRs
#141 and #142).
Setup tests also check that existing and migrated admission settings survive
configuration rewriting. Council and agent fixtures use real registries with
fake providers so policy binding follows the application path. No live model,
native broker, Spark allocation or physical GPU release is established by this
suite; paused-council partial-usage accounting remains a separate milestone.

## Catalog and model currency

The model catalog ([CONFIGURATION.md](CONFIGURATION.md#model-catalog)) has an
offline guard and an opt-in live tier. Both run one pytest process per file:

```bash
# capabilities.yaml is canonical and every default model has a price source
python -m pytest tests/test_capabilities_catalog.py -q
python scripts/gen_capabilities.py --check          # what the test runs; exit 1 when stale
python scripts/gen_capabilities.py                  # rewrite the file in canonical form

# the live tier: skipped unless opted in; keyed tests also skip per missing key
NVH_NETWORK_TESTS=1 python -m pytest tests/test_model_currency.py -q
NVH_NETWORK_TESTS=1 GROQ_API_KEY=... NVIDIA_API_KEY=... python -m pytest tests/test_model_currency.py -q -k keyed

# the override-drift report the weekly workflow prints (offline, never fails)
python scripts/gen_capabilities.py --drift
```

`tests/test_capabilities_catalog.py` passes on whatever LiteLLM `pip`
resolved, because the file holds no LiteLLM-read numbers; a LiteLLM release
can only move the informational drift report. `tests/test_model_currency.py`
without `NVH_NETWORK_TESTS=1` runs only its offline drift-report test. The
secret-free live tests need no key (LLM7's anonymous models endpoint); the
keyed tests read `GROQ_API_KEY` and `NVIDIA_API_KEY` only and print model
ids, never keys. `.github/workflows/model-currency.yml` runs all of this
weekly against the latest LiteLLM, plus every VRAM-tier Ollama tag against
the registry; it is non-blocking.

## Manual smoke before a release

```bash
nvh status --smoke --strict            # offline workspace smoke test
nvh services start                     # Ollama -> API -> WebUI -> Wizard answers
nvh status --report --live             # one live Wizard round-trip in the bundle
nvh ask "hello" --local                # local path
nvh convene "hello" --cabinet engineering
```

Then the target-VM checklist in [MAINTAINERS.md](MAINTAINERS.md#production-readiness).

## Local quirks

A developer box with a live Ollama, a legacy `~/.council` database or an old
Python 3.11 patch release can fail a few tests that pass in CI's clean
container. Run the failing file alone; if it passes with `NVH_HOME` pointed at
an empty directory, the environment, not the code, is the cause.

Back to [README](../README.md)
