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
python -m mypy nvh/sandbox nvh/catalog --follow-imports=silent --ignore-missing-imports
```

`pyproject.toml` configures pytest: `asyncio_mode = "auto"` (async tests need
no marker), a 120 s per-test timeout enforced by `pytest-timeout` (declared in
`required_plugins`, so a missing plugin is a startup error rather than a
silent no-timeout run), and `filterwarnings` for three known-benign upstream
warnings. Tests run from any directory; paths are resolved from
`Path(__file__)`.

## Layout

`tests/` is flat: one `test_<subject>.py` per module or feature, no
subdirectories, no fixtures package beyond what a file needs. When a module is
deleted its test file goes with it; when a module is renamed the test file is
renamed too. A handful of files are guards rather than unit tests:

| File | Guards |
|---|---|
| `test_version.py` | `pyproject.toml` and `nvh.__version__` agree |
| `test_commands_doc_parity.py` | `docs/COMMANDS.md` matches the Typer registry byte for byte |
| `test_marketing_parity.py` | no hand-typed provider/model/free/cabinet/tool/persona/agent counts in README, docs or CLI strings unless they equal the derived value |
| `test_docs_links.py` | every relative link and image in README and `docs/` resolves; every doc has an inbound link |
| `test_mcp_cabinet_sync.py` | the MCP server's cabinet set is `COUNCIL_PRESETS` |
| `test_provider_defaults.py` | the settings template and the API server's copy of the provider defaults carry the same, current model IDs |
| `test_release_hardening.py` | `install.sh`, `nvh services` and the docs keep the bring-up contract |
| `test_cli_mcp_group.py` | no name is both a command and a command group (the bug that hid `nvh mcp` and `nvh agent`) |

## Kinds of tests

- **Unit** — the bulk: routing, council, agents, tools, storage, config.
  Providers are exercised through `MockProvider` (`nvh/providers/mock_provider.py`)
  or by patching `litellm`; nothing in the default run talks to a real
  provider.
- **API** — FastAPI's `TestClient` runs `nvh.api.server:app` in-process
  (`test_api.py`, `test_auth.py`, `test_chat_history.py`, ...).
- **Live server** — `test_live_api.py` spawns `uvicorn` on a free port and
  probes `/v1/health`, CORS and a WebSocket upgrade; this is the only place
  lifespan startup runs for real.
- **CLI** — `test_cli_inprocess.py` drives the Typer app with `CliRunner`;
  `test_cli_e2e.py` spawns the `nvh` console script (`encoding="utf-8",
  errors="replace"` — copy that on Windows).
- **MCP** — `test_mcp_client.py` runs a real stdio MCP server through the SDK
  pinned in the `dev` extra.
- **Installer** — `ci/integration-test-install.sh` runs `install.sh` in a
  clean Ubuntu container with a stubbed `nvidia-smi`. It downloads Ollama and
  a model, so it is not in the default matrix; run it before merging anything
  that touches `install.sh`.

## CI

`.github/workflows/ci.yml` runs on every push and pull request to `main`:

| Job | What |
|---|---|
| `test` | Ubuntu, Python 3.11 and 3.12: `ruff check` with the rule set above, `pytest tests/ -v`, then a coverage gate (`--cov-fail-under=49`) on 3.12 with upload to Codecov |
| `typecheck` | `mypy nvh/sandbox nvh/catalog` gates; a repo-wide mypy runs as an informational signal and grows the gated list as modules reach zero errors |
| `webui` | `npm ci`, `tsc --noEmit`, ESLint (findings warn, a crashed linter fails), `npm run build` on Node 22 |
| `security` | `pip-audit` (informational) |
| `build` | wheel + sdist, `twine check`, install into a clean venv and run `nvh version` / `nvh --help` |

Linux is the only OS in the matrix on purpose; Windows and macOS are
best-effort until the Linux journey is stable. The CLI presentation layer
(`nvh/cli/main.py`, `repl.py`, `completions.py`, `conversations.py`) is
excluded from the coverage metric and covered by the subprocess tests instead.

## Writing a test

- Isolate the workspace: point `NVH_HOME` (and `HIVE_CONFIG_HOME` when the
  test reads config) at `tmp_path` with `monkeypatch.setenv`, and call
  `nvh.providers.registry.reset_registry()` if you register providers.
- No network, no keys, no real Ollama in the default run. If a test must
  reach one, skip it unless an explicit opt-in variable is set.
- Prefer testing behaviour through the public entry point (`Engine`, the
  Typer app, the FastAPI app) over private helpers.
- Regenerate `docs/COMMANDS.md` when you add, rename or hide a command, and
  keep counts out of prose — the parity tests will tell you if you forgot.
- A test that reads a doc (`test_release_hardening.py`,
  `test_docs_links.py`) is a contract: update the doc and the test together.

## Manual smoke before a release

```bash
nvh status --smoke --strict            # offline workspace smoke test
nvh services start                     # Ollama → API → WebUI → Wizard answers
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
