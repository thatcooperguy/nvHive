# Service Order

nvHive runs **three local services**. They have a strict dependency order
and explicit health gates between each step. This page is the contract —
the same order is encoded in `nvh/cli/services.py`, `install.sh`, and the
release-hardening tests. If you change anything here, update those three.

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
        Browser opens http://localhost:3000/setup
```

| # | Service | Default port | Health endpoint | Healthy signal                                                  | Timeout knob                  |
|---|---------|--------------|-----------------|------------------------------------------------------------------|-------------------------------|
| 1 | Ollama  | 11434        | `/api/tags`     | HTTP 200 with a `models` array                                   | `NVH_OLLAMA_BOOT_TIMEOUT` (15s) |
| 2 | API     | 8000         | `/v1/health`    | HTTP 200 with `status:"success"` AND `data.engine_initialized:true` | `NVH_API_BOOT_TIMEOUT` (20s)  |
| 3 | WebUI   | 3000         | TCP LISTEN      | Port accepting TCP connections                                    | `NVH_WEBUI_BOOT_TIMEOUT` (30s) |

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

## How `nvh services` interacts with `nvh webui`

* **`nvh webui`** is the user-facing command for first-time setup and
  everyday use. It installs Node deps, builds the WebUI, auto-starts
  the API (with the same stale-detection logic as `nvh services`),
  health-gates Ollama, and opens the browser. **This is what the README
  tells new users to run.**
* **`nvh services`** is the surgical tool for **troubleshooting + CI**.
  It assumes everything is already installed and exposes three things:
  * `nvh services` / `nvh services status` — print the live status table.
  * `nvh services start` — boot all three in order, with health gates,
    abort on the first hard failure.
  * `nvh services restart` — SIGTERM the API + WebUI (Ollama stays so
    its model cache survives), 1s settle, then `start`.

Both commands call the same module-level helpers in `nvh/cli/services.py`
(`ollama_healthy`, `api_healthy`, `webui_port_listening`, `kill_stale_api`,
`start_pipeline`). The behavior is shared — they differ only in what
they expose to the user.

## Sample output

```
$ nvh services
Service  Port    Status       Health                                    Action
------   ----    ------       ------                                    ------
Ollama   11434   running      /api/tags 200 (3 models)                  leave
API      8000    unhealthy    /v1/health engine_not_initialized         restart
WebUI    3000    not running  no listener on 3000                       start
```

```
$ nvh services start
Starting service pipeline...
  ✓ Ollama: healthy after wait (3 models)
  ✓ API: healthy after wait (engine_initialized)
  ✓ WebUI: listening after wait (listening)

Service  Port    Status   Health                              Action
------   ----    ------   ------                              ------
Ollama   11434   running  /api/tags 200 (3 models)            leave
API      8000    running  /v1/health 200 (engine_initialized) leave
WebUI    3000    running  listening                           leave
```

## Environment knobs

| Variable                   | Default | Effect                                                                 |
|----------------------------|---------|------------------------------------------------------------------------|
| `NVH_OLLAMA_BOOT_TIMEOUT`  | `15`    | Seconds to wait for `/api/tags` after spawning `ollama serve`. Mirrors the knob honored by `install.sh`'s `start_ollama_with_health_wait`. |
| `NVH_API_BOOT_TIMEOUT`     | `20`    | Seconds to wait for `/v1/health` to report `engine_initialized:true`.   |
| `NVH_WEBUI_BOOT_TIMEOUT`   | `30`    | Seconds to wait for the WebUI port to start LISTENing.                 |
| `NVH_OLLAMA_BIN`           | `$(which ollama)` | Override the ollama binary location (used by rootless installs). |
| `NVH_INSTALL_LAUNCH`       | unset   | `install.sh` honors this to auto-run `nvh workstation` after install.   |

## Pipeline failure modes and the recovery path

| Symptom                                            | Likely cause                                                | Recovery                          |
|----------------------------------------------------|-------------------------------------------------------------|-----------------------------------|
| WebUI shows red "API offline" banner forever       | Stale `nvh serve` whose engine failed to init at startup    | `nvh services restart`            |
| `nvh services` shows API healthy but WebUI absent  | `npm` not installed / WebUI never built                     | `nvh webui --install`             |
| Ollama "unreachable (Connection refused)"          | Daemon never started, or crashed during model load          | `nvh services start` (re-spawns)  |
| API "engine_not_initialized" even after restart    | Bad config (corrupted by an earlier failed Omni install)    | `nvh setup` to repair, then restart |

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
  (later promoted to module level — this PR).
* **#66** — `install.sh` replaces the racy `ollama serve & sleep N`
  pattern with a real health-wait + log.

This PR (the `nvh services` CLI + module-level health helpers + this
doc) is the consolidation step: the order that lived implicitly across
those six PRs is now explicit, single-sourced, and exposed to the user
as one command.
