# Maintainers

The runbook for people who cut releases, keep the service bring-up contract
honest, decide when a build is "production-ready", and tend the repository
page. Contributors should start with [CONTRIBUTING.md](../CONTRIBUTING.md)
and [TESTING.md](TESTING.md).

## Releasing

The canonical project for the `nvhive` PyPI distribution is
`thatcooperguy/nvHive`; only its maintainers publish under that name. Forks
must use a distinct package name, metadata and release channel.

```bash
git checkout main && git pull --ff-only && git status     # clean and green
python -m pytest tests/ -q
# bump all three version files: pyproject.toml::project.version,
#   nvh/__init__.py::__version__ and web/lib/version.ts::NVHIVE_VERSION
#   (tests/test_version.py and tests/test_release_hardening.py::
#   test_webui_version_matches_package fail CI if any of them differ)
# add the CHANGELOG.md section
git commit -am "release: v0.42.0" && git push
git tag v0.42.0 && git push origin v0.42.0
```

The tag push (format `vX.Y.Z`, lowercase `v`) triggers
`.github/workflows/release.yml`: sdist + wheel, PyInstaller single-file
binaries for Linux x86_64, macOS arm64 and Windows x86_64, and a GitHub
Release with auto-generated notes. `publish.yml` runs on the same tag push
and on `release: published`, checks the artifacts with `twine check` and
uploads to PyPI through OIDC trusted publishing (environment `pypi`,
workflow file `publish.yml`, repository `thatcooperguy/nvHive`). It also
accepts `workflow_dispatch` as a recovery path:

```bash
gh workflow run publish.yml --ref v0.42.0 -f target=pypi
```

Expect 6–10 minutes for the GitHub Release (binary builds dominate) plus a
minute for PyPI.

### Before tagging

1. Tests green locally and on the last CI run of `main` (Linux, Python 3.11
   and 3.12 — a red matrix is a release blocker).
2. `cd web && npx tsc --noEmit && npm run build` passes; a broken WebUI
   breaks first run for every user.
3. `CHANGELOG.md` has the section; `docs/COMMANDS.md` is regenerated
   (`python scripts/gen_commands_doc.py`) if any command changed.
4. README install URLs, PyPI metadata, release asset names, `NOTICE.md`,
   `TRADEMARKS.md` and the trusted-publisher settings all point at
   `thatcooperguy/nvHive` and the `nvhive` package.
5. If `nvh/api/server.py::ALLOWED_ORIGINS` changed, `http://nvhive`,
   `http://localhost:3000` and port 80 are still present.
6. The readiness rule below: do not call a release production-ready unless
   the target-VM checklist passed.

### Verifying, yanking, rolling back

```bash
python -m venv /tmp/verify && /tmp/verify/bin/pip install nvhive==0.42.0
/tmp/verify/bin/nvh version && /tmp/verify/bin/nvh --help
```

A wrong version or an import failure means **yank** the release on PyPI
(`Options > Yank release` — hides it from new installs, keeps it for pinned
users) and fix forward with a patch release. Never delete or force-push a
released tag. Users on a broken release pin the previous one
(`pip install 'nvhive==0.41.1'`); WebUI-versus-backend drift is fixed by
`nvh webui --clean`.

### Workflow-scoped pushes

Edits under `.github/workflows/` need a token with the `workflow` scope or
GitHub rejects the push (`refusing to allow ... without 'workflow' scope`).
`gh auth refresh -s workflow` is the fastest way to get one.

## Service order

nvHive runs **three local services** and then verifies the result with an
**end-to-end Wizard smoke test**. The order and the health gates are the
contract; the same order is encoded in `nvh/cli/services.py`, `install.sh`
and `tests/test_release_hardening.py`. Change one, change all three.

```
[1] Ollama  :11434   no dependency          gate: GET /api/tags → 200 + {"models": [...]}
[2] API     :8000    needs Ollama           gate: GET /v1/health → status "success", data.engine_initialized true
[3] WebUI   :3000    needs the API          gate: TCP LISTEN on the port
[4] Smoke test       needs the whole stack  gate: POST /v1/wizard/chat returns a non-empty answer
                                                  mode "llm" = healthy · "deterministic*" = degraded
      → browser opens http://localhost:3000/setup
```

| Step | Default port | Health signal | Timeout knob |
|---|---|---|---|
| Ollama | 11434 | `/api/tags` 200 with a `models` array | `NVH_OLLAMA_BOOT_TIMEOUT` (15 s) |
| API | 8000 | `/v1/health` 200 with `engine_initialized: true` | `NVH_API_BOOT_TIMEOUT` (20 s) |
| WebUI | 3000 | port accepting TCP connections | `NVH_WEBUI_BOOT_TIMEOUT` (30 s) |
| Smoke test | — | non-empty `answer`; `mode: "llm"` fully healthy | 90 s in `start_pipeline`; `nvh services smoke-test --timeout` defaults to 45 s |

Why these signals: any 200 on `/api/tags` could be an unrelated process on
the port, so the body must carry `models`. `/v1/health` is used instead of
`/v1/ready` because readiness requires auth; `engine_initialized` is the same
flag the WebUI's `ApiHealthBanner` reads, so a green table matches what the
user sees. A TCP-only API check is insufficient — a stale `nvh serve` whose
engine failed to initialise keeps accepting connections and returns 500 on
`/v1/health`, which is exactly the failure PR #65 fixed. For the WebUI a full
HTTP GET would stream the homepage or 404 on an unbuilt route, so LISTEN is
the contract. Three listening ports still do not prove the Wizard can answer,
hence step 4; its 90 s budget covers cold imports on slow ephemeral disks.

The smoke test has three outcomes: `mode: "llm"` (healthy, browser opens),
`mode: "deterministic*"` (degraded — the Wizard answered from its fallback
because no local LLM is loaded; the browser still opens and the summary says
"Wizard running in fallback mode"), and empty/error/timeout (failed — the
browser does not open and the tail of `api-server.log` is printed).

`nvh webui` is the user-facing command: it installs Node deps, builds, starts
the API with the same stale-detection logic, health-gates Ollama and opens the
browser. `nvh services` is the surgical tool for troubleshooting and CI:
`nvh services` prints the live table; `start` boots all four steps with gates
and aborts on the first hard failure; `restart` SIGTERMs API + WebUI (Ollama
keeps its warm model), settles 1 s, then starts; `stop` stops WebUI then API
(`--ollama` to stop it too); `smoke-test` runs step 4 alone. Both commands
share the module-level helpers `ollama_healthy`, `api_healthy`,
`webui_port_listening`, `wizard_smoke_test`, `kill_stale_api` and
`start_pipeline`.

| Variable | Default | Effect |
|---|---|---|
| `NVH_OLLAMA_BOOT_TIMEOUT` | `15` | seconds to wait for `/api/tags` after spawning `ollama serve`; also honoured by `install.sh` |
| `NVH_API_BOOT_TIMEOUT` | `20` | seconds to wait for `/v1/health` to report `engine_initialized` |
| `NVH_WEBUI_BOOT_TIMEOUT` | `30` | seconds to wait for the WebUI port to LISTEN |
| `NVH_OLLAMA_BIN` | `$(which ollama)` | override the Ollama binary (rootless installs) |
| `NVH_OLLAMA_PRELOAD` | `1` | after Ollama is healthy, preload the default model so the first turn is not a cold load; `0` opts out |
| `NVH_DEFAULT_OLLAMA_MODEL` | unset | which model the preload warms (default: `config.yaml`'s default) |
| `NVH_INSTALL_LAUNCH` | `auto` | `install.sh` runs `nvh services start --open` at the end; `0` skips |

| Symptom | Likely cause | Recovery |
|---|---|---|
| WebUI shows "API offline" forever | stale `nvh serve` whose engine failed to init | `nvh services restart` |
| API healthy, WebUI absent | `npm` missing / WebUI never built | `nvh webui --install` |
| Ollama "unreachable (Connection refused)" | daemon never started or crashed on model load | `nvh services start` |
| `engine_not_initialized` after restart | bad `config.yaml` | `nvh setup` to repair, then restart |
| Ports up, Wizard silent | endpoint erroring or no model loaded | `nvh services smoke-test`, then `restart` |
| Smoke test `degraded` | no local LLM yet | finish the download from `/setup`, re-run `smoke-test` |

History, for readers wondering which fixes this consolidates: #58 browser
auto-open preferring installed browsers; #59 extended Ollama wait; #64
`install.sh` recovering a corrupted `config.yaml`; #65 HTTP-probing the API
instead of trusting TCP (`_api_healthy` + `_kill_stale_api`); #66 replacing
`ollama serve & sleep N` with a real health wait; #70 the `nvh services` CLI
and module-level helpers; #84 CLI-verified bring-up with the live table;
#85 the smoke test as step 4; #86/#87 the degraded state, inline log tails,
`nvh services stop` and outcome-oriented row labels.

## Production readiness

CI-clean is not production-ready. The bar is a real rootless NVIDIA Linux VM
with persistent block storage, because that is where drivers, CUDA, Python,
storage, display and model downloads meet.

`GET /v1/setup/production-readiness` returns `blocked` (a gate must be fixed),
`pilot-ready` (no blockers, but target-VM validation or warnings remain) or
`production-ready` (all gates pass and `NVH_TARGET_VM_VALIDATED=1` was set for
the final check). `GET /v1/ready` is the compact, auth-gated summary for
launchers and monitors; `/v1/health` stays minimal. Gates: persistent
`NVH_HOME` writable and explicitly configured; mount autopilot finds or
validates the block-backed home; a usable Python runtime (venv/pip or the
micromamba fallback); driver, CUDA and VRAM facts present; no blocked app
compatibility items; a stable boot-preflight baseline with no image drift;
smoke tests without failures; the recommended model queue fits storage;
install receipts healthy; every studio pack marked no-root; and the target-VM
acceptance run completed.

Target-VM acceptance, on a fresh no-root NVIDIA Linux cloud desktop:

1. Install from GitHub or PyPI into the user-owned persistent mount; confirm
   `NVH_HOME` is on the block-backed volume, not the OS disk or a read-only
   share, and that a real write probe under it succeeds with capacity to spare.
2. Launch the WebUI from the desktop launcher.
3. Install **AI Starter**; verify Ollama and the recommended model queue.
4. Install **Graphics Creator Studio**; launch ComfyUI with the starter
   examples.
5. Install **Game Dev Lab** and **Music Producer Studio**; verify the helper
   launchers without sudo.
6. Reboot or reconnect; confirm boot preflight reports a stable image.
7. `/v1/ready` is `pilot-ready` or `production-ready`, not `blocked`.
8. `export NVH_TARGET_VM_VALIDATED=1`, run `nvh webui`, open Advanced Details
   in the setup wizard and confirm Release Readiness is `production-ready`.

Release language: "beta" or "pilot-ready" before the checklist passes,
"production-ready" after. Do not publish a PyPI release as production-ready
while the report says otherwise.

Support bundles (`nvh status --report`, the dashboard's **Copy Error Report**,
`GET /v1/setup/diagnostics`) redact API keys, bearer tokens, GitHub tokens and
secret-shaped values, and replace the persistent home with `$NVH_HOME` and the
user home with `~`. They still name tools, packages and warning lines, so
read one before posting it publicly. Logs are under `$NVH_HOME/logs/`; every
API response carries an `X-Request-ID` that appears in them.

## Repository page

Short description: *Rootless NVIDIA AI lab for Linux GPU desktops: local LLMs,
ComfyUI, agents, creative tools, game-dev tools, music tools, and a guided
rootless setup wizard with one-click repairs.* Website:
`https://pypi.org/project/nvhive/`. Topics: `nvidia gpu llm local-ai comfyui
ollama agents rootless linux-desktop ai-workstation student-tools
generative-ai`.

Pinned release message before target-VM validation: "nvHive is pilot-ready.
CI is green and the rootless setup path is implemented, but production-ready
status waits for the real no-root NVIDIA Linux VM validation." After: "nvHive
is production-ready for the validated no-root NVIDIA Linux GPU desktop
profile."

## Documentation

- `docs/COMMANDS.md` is generated: `python scripts/gen_commands_doc.py`
  (`--check` in CI via `tests/test_commands_doc_parity.py`). Never hand-edit.
- No hand-typed inventory counts (providers, models, free tiers, cabinets,
  tools, personas, agents) in README, docs or CLI strings —
  `tests/test_marketing_parity.py` fails on any that disagree with the code.
- Every relative link and image in README and `docs/` must resolve, and every
  doc needs an inbound link — `tests/test_docs_links.py`.
- Screenshots are 1440×900 captures of the dashboard under
  `docs/screenshots/`; keep the set to what README and WEBUI.md actually
  embed.
- The audit and multi-release plan live in `docs/proposals/`; the
  user-facing summary is [ROADMAP.md](ROADMAP.md).

Back to [README](../README.md)
