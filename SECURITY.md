# Security

nvHive is a local-first tool: the API, the WebUI, the MCP server and the
sandbox all run on your machine, and the only remote parties are the AI
providers you route queries to ([PRIVACY.md](PRIVACY.md)). This document
describes the trust boundary as the code enforces it from the 0.44.1 line on,
names the module behind every rule so you can check it, and says how to report
a problem. Variables are documented in
[docs/CONFIGURATION.md](docs/CONFIGURATION.md#environment-variables); the tool
tiers in [docs/CONFIGURATION.md#tools](docs/CONFIGURATION.md#tools).

## Reporting a vulnerability

Please do not open a public issue with exploit details.

- Report through GitHub's private vulnerability reporting form on
  https://github.com/thatcooperguy/nvHive (Security tab, "Report a
  vulnerability") when the repository offers it. If the Security tab does not
  show the form, open an ordinary issue titled "Security contact request" with
  no technical details and the maintainer will arrange a private channel.
- Include the version (`nvh version`), the platform, what you ran, what you
  expected and what happened. A proof of concept helps; a working exploit is
  not needed.
- nvHive is maintained by one person. Fixes ship as patch releases and are
  listed under a `Security` heading in [CHANGELOG.md](CHANGELOG.md). Only the
  current release line is patched; older releases are not.

## Open mode and `HIVE_API_KEY`

The REST and WebSocket API (`nvh serve`, `nvh/api/server.py`) recognises two
credentials, checked in this order (`require_auth` in `nvh/api/deps.py`, which
`nvh/api/server.py` re-exports and every route depends on;
`_authenticate_websocket` in `nvh/api/server.py`):

1. per-user tokens created through `/v1/auth/*` when the users table has
   entries (`nvh/auth/auth.py`: passwords are stored as bcrypt hashes, API
   tokens are shown once and stored as SHA-256 hashes);
2. the single `HIVE_API_KEY` environment variable, accepted as
   `Authorization: Bearer <key>` or `X-Hive-API-Key: <key>`.

**Open mode** is what you have whenever `HIVE_API_KEY` is unset
(`_get_council_api_key` returns `None`): a request that presents no credential,
or one that is not a known user token, is admitted. Registering users adds a
credential the API accepts; it does not close the API. Only the key does
(`tests/test_api_server_endpoints.py` pins this).

Open mode is the default and is meant for one person on one machine with the
API bound to loopback. Set `HIVE_API_KEY` before the API becomes reachable by
anything other than your own browser on this host: `--host 0.0.0.0`, a port
forward, a reverse proxy, a shared or multi-user box. In open mode on loopback
every process on the machine, and every page open in your browser, can call
the non-privileged endpoints (CORS hides responses from pages outside the
allow-list; it does not stop the request). The privileged tier adds its own
network checks, described below, but the key is what closes the rest.

Two routes that answered without the key until 0.46 now go through
`require_auth` like the rest: `POST /v1/anthropic/messages` (and its SDK alias
`/v1/anthropic/v1/messages`) and `GET /v1/integrations/scan`, which returns
the config paths of the tools it detects. A keyed deployment that points an
Anthropic-compatible client at nvHive must now send the key as a bearer token
(`ANTHROPIC_AUTH_TOKEN=<key>`; the SDKs' `x-api-key` header is not a
credential nvHive reads). The OpenAI standard paths added in 0.46
(`/v1/chat/completions`, `/v1/completions`) are the same handler objects as
`/v1/proxy/...` and inherit its `require_auth`.

Three surfaces added in 0.46 are authenticated from the start. `GET
/v1/outputs/images/{scope}/{filename}` serves the images the Wizard
generated: `scope` and `filename` are regex-validated (one folder level, one
short image suffix), the resolved path must sit directly inside
`$NVH_HOME/outputs/images/<scope>`, and `..`, a foreign scope, an unknown
suffix, an escaping symlink or a missing file all answer 404, never 500;
responses carry `Cache-Control: private, max-age=300`. The `/v1/vault/notes`
routes (`GET`, `GET /{id}`, `DELETE /{id}`) read the process home only — no
`home_dir` field — and match ids against the notes' own `Id:` lines, never
as paths. The seven `/v1/schedules` routes take `require_auth` too. A keyed
deployment answers 401 to all of them without the key
(`tests/test_api.py`, `tests/test_vault_notes_route.py`,
`tests/test_schedule_runner.py`).

`GET /v1/health` stays open — monitors poll it — and since 0.46 reports
`bind_host` (the interface `run_server` bound, or `null` when the app was not
started by `run_server` — a bare `uvicorn nvh.api.server:app`, an embedding —
because the process then cannot vouch for the interface it listens on; a
client treats unknown as not-loopback), `auth_mode` (`open`, `key` or
`users`: the first tier `require_auth` applies) and `key_required` (whether
a token-less request is refused, i.e. `HIVE_API_KEY` is set). All three are
disclosed on purpose: anyone who can reach the port learns them from the
connection and from the 401 anyway, and `nvh launch` needs them to refuse an
off-loopback open-mode API, or to ask for a user token, before it points a
coding tool at the server. `users` without the key is still open mode, as
above — which is why `key_required` is there: `auth_mode` alone cannot tell
a keyed users deployment from an open one.

Streamed proxy completions (`/v1/proxy/chat/completions`,
`/v1/chat/completions` and the Anthropic Messages paths) check
`budget.hard_stop` before the first provider call and write their spend to
the query ledger when the stream ends (`mode: proxy`), as the non-streamed
calls always did (those go through `Engine.query` and carry `mode: simple`,
the mode `nvh ask` writes; the totals the budget reads sum both); until 0.46
a streaming client could spend past the budget unrecorded.

Provider API keys live in `$NVH_HOME/config/.env`, or in the OS keyring when
`NVH_USE_KEYRING=1` (`nvh/providers/registry.py`). They are sent only to the
provider they belong to.

## Loopback defaults

The API and the MCP server bind loopback unless you ask:

| Surface | Default bind | Exposing it |
|---|---|---|
| REST/WebSocket API: `nvh serve`, `run_server()` in `nvh/api/server.py` | `127.0.0.1:8000` | `nvh serve --host 0.0.0.0` with `HIVE_API_KEY` set first |
| MCP over HTTP: `nvh mcp -t streamable-http`, `nvh openclaw --start --http` (`MCP_HTTP_DEFAULT_HOST` in `nvh/cli/main.py`) | `127.0.0.1:8080` | `--host 0.0.0.0` |

Before 0.44.1 the MCP HTTP transport always bound `0.0.0.0`, and so did
`run_server()` when called from Python without a host (`nvh serve` already
defaulted to loopback); an OpenClaw or remote-client setup that relied on
either now needs the explicit `--host`.

The WebUI is the one local surface that does not follow this rule. `nvh webui`
starts the Next.js server (`next start`, or `next dev` under `--dev`) with
only `-p <port>`, and Next.js binds every interface by default (`-H 0.0.0.0`),
on the first free port of 80, 3000, 3001, 3002 and 8080 unless `--port` is
given; `nvh webui` has no `--host` flag yet. The pages hold no secrets and the
browser talks to the API on its own, but the Next.js server also proxies
`/api/*` to the loopback API (`web/next.config.js`), so on a shared network
another machine can reach an open-mode API through the WebUI's port. When that
matters, set `HIVE_API_KEY`, firewall the port, or start the WebUI by hand
with `npm run start -- -H 127.0.0.1 -p <port>` from the checkout under
`NVH_WEB_HOME`.

The MCP server (`nvh/mcp_server.py`) has no authentication of its own: the
default `stdio` transport is private to the client process that spawned it,
and the HTTP transport should only leave loopback behind something that
authenticates for it (a reverse proxy, an SSH tunnel, a VPN such as
Tailscale).

## CORS: `HIVE_CORS_ORIGINS` and `HIVE_CORS_ORIGIN_REGEX`

`nvh/api/server.py` installs `CORSMiddleware` with `allow_credentials=True`:

- `HIVE_CORS_ORIGINS` (comma-separated) **replaces** the default origin list
  `_DEFAULT_CORS_ORIGINS`: `localhost` and `127.0.0.1` on ports 3000-3002 and
  8080, bare `http://localhost` / `http://127.0.0.1`, and the `nvhive`
  hostname `nvh webui` may advertise.
- `HIVE_CORS_ORIGIN_REGEX` replaces the default regex
  `^http://(localhost|127\.0\.0\.1|nvhive|\[::1\])(:\d+)?$`, which lets the
  WebUI reach the API from whichever local port it fell back to.

Both lists are also what the privileged tier consults when it checks where an
approval came from, so widening them widens that check. Keep them to origins
you control.

## The privileged tier

Every tool carries a safety class (`nvh/core/tools.py`): `auto` runs
unprompted, `confirm` needs a click on a card, `privileged` needs a click on
a red card that shows the exact commands. The privileged tools are
`system_settings_apply`, `apt_install`, `snap_install`, `service_enable`,
`playbook_install` and the Wizard's `shell`.

- **Kill switch.** `NVH_ALLOW_PRIVILEGED=0` (or `false`, `no`, `off`;
  `PRIVILEGED_ENV` / `privileged_enabled()` in `nvh/core/tools.py`) disables
  the whole tier: the tools stay listed with `enabled: false` so the
  catalogue can explain them, and every call is refused naming the variable.
  The default is on.
- **Approval tokens.** The red card carries a token minted by
  `issue_approval`: an HMAC-SHA256 over the tool name and the exact
  arguments, valid for 15 minutes (`APPROVAL_TTL_S`), single use. The
  confirmed call must return it and `verify_approval` checks it against that
  exact call, so a changed argument, a replay or an expired card is refused
  (`approval_required`).
- **Where the click came from.** `POST /v1/wizard/tools/execute` runs
  `_privileged_network_refusal` (`nvh/api/server.py`) on every confirmed
  privileged call and refuses when the API is bound to a non-loopback address
  in open mode (no `HIVE_API_KEY`, registered users or not: anything on the
  network could approve for you; `_auth_open_mode`), when the
  `Host` header is neither loopback nor a host in the CORS allow-list (a
  DNS-rebound page talking to `127.0.0.1` under its own name), or when the
  `Origin` header is outside the CORS allow-list (a cross-site page).
  Refusals are HTTP 200 bodies with `ok: false, refused: true` and the
  reason.
- **No passwords.** `run_host_command`
  (`nvh/integrations/wizard/system_settings.py`) uses `sudo -n` only where
  the once-per-process probe found passwordless sudo; otherwise it answers
  `needs_terminal` with the exact command for you to run yourself. There is
  no password parameter anywhere and stdin is closed, so nothing can prompt.
  `nvh playbook install <id>` runs in your own terminal, where `sudo` prompts
  you directly; nvHive never sees, stores or passes a password.
- **Vault audit.** Every privileged apply that touched the host (complete,
  partial or failed) is recorded by `audit_privileged_change`
  (`nvh/core/tools.py`) as a note under the vault's `Decisions/` folder,
  titled `Privileged change: <summary>`, with the commands, exit codes and
  output (secrets redacted, cut at `AUDIT_OUTPUT_CHARS` = 4000 characters),
  tagged `privileged` and with the tool name. The vault is the one under
  `NVH_HOME`; nothing the model writes into the arguments can redirect the
  note.
- **Nothing unattended.** A scheduled run (`mode: ask` under the API's
  schedule runner, `nvh/integrations/services/schedule_runner.py`) is a
  Wizard turn with no human present: it runs only the profile's whitelist
  intersected with the read-only `SCHEDULE_AUTO_TOOLS` roster, never a
  confirm or privileged tool, and never mints, stores or serves an approval
  token — a request for a confirm or privileged tool the profile's own
  whitelist allows is recorded on the run as `{name, arguments, privileged,
  unattended: true}` with no plan and no token, its string arguments passed
  through `redact_secrets` (a key the model wrote into `save_provider_key`
  never reaches `jobs/<id>.jsonl` or `GET /v1/jobs/{id}/events`), and
  stripped through `_persistable_calls` before any job event is written
  (`tests/test_schedule_runner.py` greps every byte under `jobs/` for
  `approval_token` and for the key); a request outside the profile's
  whitelist is refused like any other call. It never asks a question either:
  `ask_user` is not offered to an unattended turn. The API runner refuses
  `mode: do` outright; only `nvh schedule start`, in your own terminal, runs
  it. The same rule holds with a human present: a confirm or privileged call
  the model makes in the same turn as an `ask_user` question is held in
  `deferred_tool_calls`, never surfaced as an approvable card beside an
  unanswered question.

## Deny lists

Two pattern lists run before anything spawns. They are enforced in code,
regardless of `--yes` or anything the model says, and a hit returns
`BLOCKED: <reason>` without running. They are a backstop for well-known
destructive shapes, not a sandbox: the card you read is the gate.

1. `nvh/core/agent_guardrails.py` (the agent, `nvh do`, the REPL and every
   shell-running tool):
   - `_BLOCKED_COMMANDS` via `check_command`: recursive deletion of `/` or a
     Windows drive, `format`, writes into `/etc` or `C:\Windows`,
     `chmod 777 /`, `chown` on root paths, `git push --force`,
     `git reset --hard`, `git clean -f`, killing init or `explorer.exe`,
     `shutdown` / `reboot`, piping `curl` or `wget` into a shell, bare
     `pip install` / `npm install`, `dd if=/dev/zero`, `yes |`, fork bombs,
     and `nvh playbook install` (a privilege escalation past the red card
     when run from the sandbox);
   - `check_path`: writes and shell commands stay inside the workspace, never
     in system directories;
   - `BLOCKED_FILES` via `check_file_read`: an exact-basename list, so
     `.env`, `.env.local`, `.env.production`, `.env.staging`,
     `credentials.json`, `service-account.json`, `id_rsa` / `id_ed25519` /
     `id_ecdsa`, `.npmrc`, `.pypirc` and `keyring` are never read into the
     model's context, while other dotenv spellings (`.env.development`,
     `.env.test`, a `.env.bak`) are not on the list and are readable, with
     `redact_secrets` as the only backstop;
   - `redact_secrets` replaces API keys, tokens, passwords and connection
     strings in tool output with `[REDACTED]` markers, and `truncate_output`
     caps it;
   - the same `redact_secrets` runs over every vault memory before it is
     written (`POST /v1/vault/memory`, the REPL's `/remember`, the Wizard's
     `remember` tool). Pattern redaction is best-effort, so for `remember`
     the confirm card showing the composed note is the check; the tool also
     refuses outright a body carrying the literal `approval_token` or the
     privileged terminal hint, writes only under the process home's
     `vault/Wizard Memory/` (a model-supplied `home_dir` or `category` is
     dropped), and every record it writes is listed and deletable through
     `/v1/vault/notes` and `nvh vault forget <id>`.
2. `nvh/integrations/wizard/system_settings.py` (the host-changing tools,
   applied to the rendered command after `check_command`):
   - `_DENIED` via `denied_reason`: user or password changes (only
     `usermod -aG docker <user>` is allowed), edits to `/etc/sudoers`,
     `/etc/shadow`, `/etc/passwd`, `/etc/group`, disk formatting or
     partitioning, raw writes to block devices, turning the firewall off or
     flushing its rules, NVIDIA driver removal, bare `apt upgrade` (it has
     stranded the DGX OS driver), shutdown, reboot and sleep, including the
     systemd power-state and rescue units behind them (`DENIED_UNITS`,
     `denied_unit`);
   - `DRIVER_PACKAGE_RE`: `apt_install` / `snap_install` refuse driver and
     kernel packages; `PACKAGE_RE` and the unit, hostname and username
     patterns validate every piece, and the model never supplies a command
     string, only names from a fixed catalogue.

The Wizard's `shell` tool (`nvh/integrations/wizard/sandbox_tools.py`) runs
both lists on every simple command in a chain and refuses `sudo`, `su` and
`doas` outright.

## Sandbox isolation

`nvh/sandbox/executor.py` runs tool code in Docker when `docker info`
answers: `--network none`, `--read-only` image with a 64 MB tmpfs `/tmp`,
`--user 1000:1000`, a memory limit (default 512 MB), `--cpus 1`,
`--pids-limit 64`, a time limit (default 30 s), and the agent workspace
(`NVH_PROJECTS`, default `$NVH_HOME/projects`) mounted at `/workspace`. The
subprocess fallback has none of that: the code runs as the nvHive user with
your files and your network. In both modes every spawned process gets stdin
closed and an environment scrubbed of variables whose names contain `KEY`,
`TOKEN`, `SECRET`, `PASSW`, `CREDENTIAL` or `PRIVATE` (`scrubbed_environment`),
so the server's API keys never reach sandboxed code. Set
`NVH_SANDBOX_REQUIRE_DOCKER=1` (legacy spelling `NVH_SANDBOX`) to fail closed
instead of falling back.

From the Wizard chat the rule is stricter (`nvh/integrations/wizard/sandbox_tools.py`):

- `run_code` always runs with `SandboxConfig(require_docker=True)`. Without
  Docker it returns an in-band refusal (`RUN_CODE_NEEDS_DOCKER_ERROR`) that
  names `docker` and the playbooks that install it, and executes nothing.
  That is why it may be a `confirm` tool rather than a `privileged` one.
- `shell` is `privileged`. Its red card states the isolation the run will get
  and pins it into the approved arguments; the confirmed run probes Docker
  again and refuses if the availability changed, so a card approved as a
  Docker sandbox can never run on the host, and one approved as "on this
  machine" never silently moves into a container.

## Telemetry

There is no analytics endpoint. The install-health logger (`nvh/telemetry.py`)
is off by default, writes only to `$NVH_HOME/telemetry/events.jsonl` when you
enable it, and never sends data over the network. See
[PRIVACY.md](PRIVACY.md#opt-in-install-telemetry).

## Dependencies

Dependabot checks the Python, npm and GitHub Actions dependencies weekly
(`.github/dependabot.yml`), and the CI `security` job installs the project
with its `dev` extra (the runtime dependencies plus `serve`, `nvidia`, `mcp`
and `rag`; `vision` and `browser` are not installed there) and runs
`pip-audit` over that resolved set on every push to `main`, every pull request
against it and on manual dispatch (`.github/workflows/ci.yml`; it reports
findings as warnings rather than failing the build).

With the MCP SDK 2.x line (what `pip install "nvhive[mcp]"` resolves today;
mcp 1.x does not require it) and with recent FastAPI releases in the `serve`
extra, `opentelemetry-api` arrives as an API-only package: nvHive installs no
OpenTelemetry SDK and no exporter, configures none, imports nothing from the
package, and the API package on its own records nothing and sends nothing, so
there is no telemetry egress. The MCP SDK 2.x line also adds `httpx2`,
`httpcore2`, `mcp-types` and `truststore`; `nvh status` prints the installed
SDK version when the package imports.

## Known limits

- The deny lists match patterns; a destructive command written in a shape they
  do not know is stopped only by the card you approve. Read the plan on every
  red card.
- The `generate_image` card's `profile` pin (and its `profile_home`) is echoed
  back by the client and is not signed: it exists to stop the *model* from
  choosing the paid path under a profile ceiling, not to stop the person at
  the keyboard. Someone who edits the card's arguments on their own machine
  spends their own `NVAPI_KEY`, and on a users-table deployment any
  authenticated user could already reach the same hosted path by chatting
  with the general Wizard; a per-user spend control would be a separate
  feature.
- Responses carry absolute paths under `$NVH_HOME` by design — `runner.lock_path`
  on `GET /v1/schedules`, `path` on `/v1/vault/notes` rows and on the
  `remember` card's result, the config paths `GET /v1/integrations/scan`
  returns — and with the default home that path names your user directory.
  They are behind `require_auth`; in open mode they go to anyone who can
  reach the port, one more reason to set `HIVE_API_KEY` off loopback.
- The subprocess fallback is not isolation. If you run untrusted code, install
  Docker or set `NVH_SANDBOX_REQUIRE_DOCKER=1`.
- Open mode trusts the machine. Anything that can reach loopback on this host
  can use the non-privileged API, and registering users does not change that;
  set `HIVE_API_KEY` when that is not acceptable.
- Model output is untrusted input. Prompt injection through fetched pages,
  files or tool results can steer the model toward a tool call; the tiers,
  the cards and the deny lists exist because of that, and none of them is a
  substitute for reviewing what you approve.
