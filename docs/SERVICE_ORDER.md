# Service Order

nvHive runs **three local services**, then verifies the result with an
**end-to-end Wizard smoke test**. The steps have a strict dependency
order and explicit health gates between each one. This page is the
contract — the same order is encoded in `nvh/cli/services.py`,
`install.sh`, and the release-hardening tests. If you change anything
here, update those three.

## The pipeline

```
  [1] Ollama (:11434)           no dependency
                 |
                 |  /api/tags returns 200 + {"models":[...]}
                 v
  [2] API     (:8000)            depends on Ollama for engine init
                 |
                 |  /v1/health returns {status:"success",
                 |     data:{engine_initialized:true, ...}}
                 v
  [3] WebUI   (:3000)            depends on the API for data
                 |
                 |  TCP LISTEN on :3000
                 v
  [4] Wizard smoke test          depends on the whole stack
                 |
                 |  POST /v1/wizard/chat returns a non-empty answer
                 |  (mode "llm" = healthy; deterministic = degraded)
                 v
        Browser opens http://localhost:3000/setup
```

| # | Step       | Default port | Health endpoint        | Healthy signal                                                       | Timeout knob                  |
|---|------------|--------------|------------------------|----------------------------------------------------------------------|-------------------------------|
| 1 | Ollama     | 11434        | `/api/tags`            | HTTP 200 with a `models` array                                       | `NVH_OLLAMA_BOOT_TIMEOUT` (15s) |
| 2 | API        | 8000         | `/v1/health`           | HTTP 200 with `status:"success"` AND `data.engine_initialized:true`  | `NVH_API_BOOT_TIMEOUT` (20s)  |
| 3 | WebUI      | 3000         | TCP LISTEN             | Port accepting TCP connections                                        | `NVH_WEBUI_BOOT_TIMEOUT` (30s) |
| 4 | Smoke test | —            | `POST /v1/wizard/chat` | HTTP 200 with a non-empty `answer`; `mode:"llm"` = fully healthy     | 90s default (hardcoded in `start_pipeline`; `nvh services smoke-test --timeout` defaults to 45s) |

### Why these signals, not others

* **Ollama**: any 200 response would do, but checking that the body has
  a `models` array also catches the case where some unrelated service
  bound :11434 and returns 200 on the path by coincidence.
* **API**: `/v1/health` was deliberately picked over `/v1/ready` because
  the latter requires auth. `engine_initialized:true` is the same signal
  the WebUI's `ApiHealthBanner` uses, so a healthy `nvh services` table
  matches what the user sees in the browser. **TCP-LISTEN-only checks
  are insufficient here** — a stale `nvh serve` whose engine failed to
  initialize at startup will keep accepting connections, return HTTP 500
  on `/v1/health`, and silently block the WebUI's banner from clearing.
  This is the failure mode PR #65 fixed (see below).
* **WebUI**: a full HTTP GET on Next.js' `/` would stream the entire
  homepage (slow) or 404 on a route not yet built. The port being LISTEN
  is the de-facto contract.
* **Smoke test**: three ports listening is not the same as "the Wizard
  can answer the user". The last land-on-a-broken-state failure mode is:
  Ollama up, API up, WebUI up — but no model loaded, or the Wizard
  endpoint errors. The smoke test POSTs a real chat request
  (`{"question": "hi"}`) and reads the response's `mode` field. The 90s
  default exists because a cold first run on slow ephemeral disks can
  spend 30-60s warming imports before the first response; subsequent
  calls finish in under a second.

### The degraded state

The smoke test distinguishes three outcomes:

* `mode:"llm"` → **healthy** (green `✓ ready` row). The Wizard answered
  via an actual LLM call.
* `mode:"deterministic*"` → **degraded** (yellow `⚠ degraded` row, with
  the `fallback_reason`). The Wizard answered via the deterministic
  fallback — it works, but no local LLM is loaded. **The browser still
  opens** (the user can keep working while they sort out the LLM), and
  `nvh services start` prints "Services up, Wizard running in fallback
  mode" instead of "All services healthy".
* Empty answer, HTTP error, or timeout → **failed** (red row). The
  browser does NOT open; the tail of `api-server.log` is printed inline.

## How `nvh services` interacts with `nvh webui`

* **`nvh webui`** is the user-facing command for first-time setup and
  everyday use. It installs Node deps, builds the WebUI, auto-starts
  the API (with the same stale-detection logic as `nvh services`),
  health-gates Ollama, and opens the browser. **This is what the README
  tells new users to run.**
* **`nvh services`** is the surgical tool for **troubleshooting + CI**.
  It assumes everything is already installed and exposes five things:
  * `nvh services` / `nvh services status` — print the live status table.
  * `nvh services start` — boot all four steps in order with health
    gates, abort on the first hard failure, and (with the default
    `--open`) open the browser only when every gate is green.
  * `nvh services restart` — SIGTERM the API + WebUI (Ollama stays so
    its model cache survives), 1s settle, then `start`.
  * `nvh services stop` — stop the WebUI, then the API (reverse
    dependency order). Ollama is preserved by default so the warmed
    model stays in RAM; pass `--ollama` to stop it too.
  * `nvh services smoke-test` — run the end-to-end Wizard check on its
    own (`--timeout`, default 45s). Exits 0 on any non-empty answer
    (including fallback mode), 1 on hard failure.

Both commands call the same module-level helpers in `nvh/cli/services.py`
(`ollama_healthy`, `api_healthy`, `webui_port_listening`,
`wizard_smoke_test`, `kill_stale_api`, `start_pipeline`). The behavior
is shared — they differ only in what they expose to the user.

## Sample output

```
$ nvh services
Service  Port    Status       Health                                    Action
------   ----    ------       ------                                    ------
Ollama   11434   running      /api/tags 200 (3 models)                  leave
API      8000    unhealthy    /v1/health engine_not_initialized         restart
WebUI    3000    not running  no listener on 3000                       start
```

`nvh services start` renders a Rich Live table ("nvHive bring-up") that
updates in place as each step moves waiting → starting → ready. The row
labels lead with the outcome; the technical name stays parenthetical:

```
$ nvh services start
Starting service pipeline...
Logs: ~/nvhive/logs · Browser opens only when every service is verified healthy.

                            nvHive bring-up
 Service                   Port    Status       Detail
 Local AI brain (Ollama)   11434   ✓ ready      healthy after wait (3 models)
 nvHive backend (API)      8000    ✓ ready      healthy after wait (engine_initialized)
 Web dashboard (WebUI)     3000    ✓ ready      listening after wait (listening)
 End-to-end test           —       ✓ ready      ok: llm

Service  Port    Status   Health                              Action
------   ----    ------   ------                              ------
Ollama   11434   running  /api/tags 200 (3 models)            leave
API      8000    running  /v1/health 200 (engine_initialized) leave
WebUI    3000    running  listening                           leave

All services healthy. Browser → http://localhost:3000/setup
```

Status cells: `· waiting`, `⟳ starting`, `✓ ready`, `⚠ degraded`
(smoke test answered via the deterministic fallback — browser still
opens), `✗ failed`, `– skipped` (an earlier step failed). On failure
the command prints the failing step, the reason, and the last 25 lines
of the relevant service log inline.

## Environment knobs

| Variable                   | Default | Effect                                                                 |
|----------------------------|---------|------------------------------------------------------------------------|
| `NVH_OLLAMA_BOOT_TIMEOUT`  | `15`    | Seconds to wait for `/api/tags` after spawning `ollama serve`. Mirrors the knob honored by `install.sh`'s `start_ollama_with_health_wait`. |
| `NVH_API_BOOT_TIMEOUT`     | `20`    | Seconds to wait for `/v1/health` to report `engine_initialized:true`.   |
| `NVH_WEBUI_BOOT_TIMEOUT`   | `30`    | Seconds to wait for the WebUI port to start LISTENing.                 |
| `NVH_OLLAMA_BIN`           | `$(which ollama)` | Override the ollama binary location (used by rootless installs). |
| `NVH_OLLAMA_PRELOAD`       | `1`     | After Ollama is healthy, fire-and-forget preload of the default model so the first chat turn isn't a 30-60s cold load. Set `0` to opt out (unit tests, scripted use). |
| `NVH_DEFAULT_OLLAMA_MODEL` | unset   | Which model the preload warms; defaults to the `default_model` in `config.yaml`. |
| `NVH_INSTALL_LAUNCH`       | unset   | `install.sh` honors this to auto-run `nvh services start --open` after install (`0` skips the launch). |

The smoke-test timeout is not env-tunable: `start_pipeline` uses the
90s default, and the standalone `nvh services smoke-test` command takes
`--timeout` (default 45s).

## Pipeline failure modes and the recovery path

| Symptom                                            | Likely cause                                                | Recovery                          |
|----------------------------------------------------|-------------------------------------------------------------|-----------------------------------|
| WebUI shows red "API offline" banner forever       | Stale `nvh serve` whose engine failed to init at startup    | `nvh services restart`            |
| `nvh services` shows API healthy but WebUI absent  | `npm` not installed / WebUI never built                     | `nvh webui --install`             |
| Ollama "unreachable (Connection refused)"          | Daemon never started, or crashed during model load          | `nvh services start` (re-spawns)  |
| API "engine_not_initialized" even after restart    | Bad config (corrupted by an earlier failed Omni install)    | `nvh setup` to repair, then restart |
| All ports up but the Wizard is silent in the WebUI | Wizard endpoint erroring, or no model loaded                | `nvh services smoke-test`, then `nvh services restart` |
| Smoke test shows `⚠ degraded` (deterministic)      | No local LLM loaded yet — Wizard answers via fallback       | finish the model download from `/setup`; re-check with `nvh services smoke-test` |

## The PRs that built each piece

* **#58** — `nvh webui` browser auto-open: prefer pre-installed browsers
  over the slow rootless-Firefox download.
* **#59** — Extended Ollama startup wait + surfaced daemon state in
  `nvh webui`.
* **#60** — AI Wizard defaults to NVIDIA Nemotron Omni multimodal at
  every VRAM tier.
* **#64** — `install.sh` recovers from previously-corrupted `config.yaml`.
* **#65** — `nvh webui` HTTP-probes the existing API instead of trusting
  the TCP-only check; introduces `_api_healthy` + `_kill_stale_api`
  (later promoted to module level by #70).
* **#66** — `install.sh` replaces the racy `ollama serve & sleep N`
  pattern with a real health-wait + log.
* **#70** — the `nvh services` CLI + module-level health helpers + this
  doc: the consolidation step that made the order explicit and
  single-sourced.
* **#84** — CLI-verified bring-up: `install.sh` ends with
  `nvh services start --open`, the Rich Live table, browser only on
  green.
* **#85** — the 4th step: end-to-end Wizard smoke test before the
  browser opens.
* **#86 / #87** — audit fixes: the degraded (deterministic-fallback)
  state, inline log tails on failure, `nvh services stop`, and the
  outcome-oriented row labels.
