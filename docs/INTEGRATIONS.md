# Integrations

Every programmatic way into nvHive: the Python SDK, the REST API, the OpenAI-
and Anthropic-compatible proxies, MCP in both directions, and the guided
setups for Claude Code, Cursor, OpenClaw, NemoClaw and VS Code.

| Surface | Start with | Use it for |
|---|---|---|
| Python SDK | `from nvh import ask` | scripts and apps in the same process |
| REST API | `nvh serve` | anything that speaks HTTP; the WebUI uses it |
| OpenAI-compatible proxy | `OPENAI_BASE_URL=http://127.0.0.1:8000/v1` (or `.../v1/proxy`) | any OpenAI SDK client, NemoClaw, OpenClaw |
| Anthropic-compatible proxy | `ANTHROPIC_BASE_URL=http://localhost:8000/v1/anthropic` | tools that speak the Messages API |
| MCP server | `nvh mcp` / `nvhive-mcp` | Claude Code, Cursor, Claude Desktop, OpenClaw agents |
| MCP client | `$NVH_HOME/config/mcp-servers.json` | give the Wizard external tools |
| `nvh integrate` | `nvh integrate --scan` | detect and configure installed AI tools in one go |
| `nvh launch` | `nvh launch --list` | start Claude Code with nvHive's MCP server registered, or print the snippet that points Codex, OpenCode, Continue or Cursor at the standard paths |

## Python SDK

```python
from nvh import ask, convene, poll, safe, quick

response = await ask("What is machine learning?")
response = await ask("Debug this code", advisor="anthropic")
result   = await convene("Should we use Rust?", cabinet="engineering")
results  = await poll("Write a sort function")
response = await safe("Analyse my salary data")       # Ollama only
```

Synchronous twins exist for each (`ask_sync`, `convene_sync`, `poll_sync`,
`safe_sync`, `quick_sync`). `nvh.complete(messages)` and `nvh.stream(messages)`
take OpenAI-shaped message lists — the full transcript is forwarded up to the
final user turn — and `nvh.route(prompt)` returns the routing decision without
calling a model. `nvh.health()` reports every advisor. The SDK reads the same
`config.yaml` and `.env` files as the CLI.

## REST API

```bash
nvh serve --port 8000          # 127.0.0.1 only; --host 0.0.0.0 needs HIVE_API_KEY
```

Interactive docs are at `http://localhost:8000/docs`. The routes you will
reach for first:

| Route | Purpose |
|---|---|
| `POST /v1/query` | one routed query (`provider`, `model`, `stream` optional) |
| `POST /v1/council` | council with `cabinet`, `auto_agents`, `strategy`, `weights` |
| `POST /v1/compare` | the same prompt to several providers side by side |
| `POST /v1/smart` | lets the local orchestrator choose the mode |
| `WS /v1/ws/query`, `WS /v1/ws/council` | streaming over WebSocket |
| `GET /v1/advisors`, `GET /v1/advisors/{name}/health` | configured providers and health |
| `GET /v1/models` | the capability catalog |
| `GET /v1/agents/presets`, `POST /v1/agents/analyze` | cabinets; preview an auto-generated panel |
| `GET /v1/budget/status`, `GET /v1/analytics` | spend and usage |
| `GET/POST /v1/conversations...` | server-side chat history and search |
| `POST /v1/rag/ingest`, `POST /v1/rag/ask` | the local RAG store |
| `POST /v1/wizard/chat`, `POST /v1/wizard/chat/stream` | the AI Wizard |
| `GET /v1/setup/*`, `GET /v1/jobs/*` | setup state and background jobs (`GET /v1/jobs` filters `kind`, `status_filter`, `limit` and, since 0.46, `exclude_kind` and `schedule_id`) |
| `GET`/`POST /v1/schedules`, `GET`/`PATCH`/`DELETE /v1/schedules/{id}`, `POST /v1/schedules/{id}/run`, `GET /v1/schedules/{id}/runs` | scheduled prompts and their runs (0.46; [CONFIGURATION.md](CONFIGURATION.md#schedule-records-nvh-schedules2)) |
| `POST /v1/vault/memory`, `GET /v1/vault/notes`, `GET`/`DELETE /v1/vault/notes/{id}` | the Memory Vault's records (0.46; [CONFIGURATION.md](CONFIGURATION.md#memory-records-nvh-memory1)) |
| `GET /v1/outputs/images/{scope}/{filename}` | images the Wizard generated (0.46, authenticated) |
| `GET /v1/health`, `GET /v1/ready` | liveness; readiness summary (auth-gated) |
| `POST /v1/workspace/snapshot/export` / `import` | the same bundle as `nvh snapshot` |

Authentication: with no `HIVE_API_KEY` and no user accounts the server runs
in open/local mode. Set `HIVE_API_KEY` for a single shared bearer token, or
create users and per-user tokens with `nvh auth create-user` / `nvh auth
create-token`. Every response carries an `X-Request-ID` that also appears in
the structured logs.

## OpenAI- and Anthropic-compatible proxies

```python
from openai import OpenAI
client = OpenAI(base_url="http://localhost:8000/v1", api_key="nvhive")
client.chat.completions.create(model="auto", messages=[{"role": "user", "content": "Hello"}])
```

`/v1/proxy/chat/completions`, `/v1/proxy/completions` and `/v1/proxy/models`
mirror the OpenAI paths, and since 0.46 the standard `/v1/chat/completions`
and `/v1/completions` reach the same handlers (see [Standard
paths](#standard-paths)); `/v1/anthropic/messages` mirrors Anthropic's
Messages API. The `model` field selects a routing mode:

| Model | Behaviour |
|---|---|
| `auto` (or `nvhive`) | smart routing to the best available provider |
| `safe` (or `local`) | Ollama only — nothing leaves the machine |
| `council` / `council:N` | N-member council with synthesis |
| `throwdown` | two-pass deep analysis with cross-critique |
| any real model ID | that model, with nvHive's fallback chain |

The header `x-nvhive-privacy: local-only` forces local inference for one
request regardless of model.

### Standard paths

Since 0.46 the base URLs the OpenAI and Anthropic SDKs expect resolve as the
SDKs build them:

| Set | Resolves to | Same handler as |
|---|---|---|
| `OPENAI_BASE_URL=http://127.0.0.1:8000/v1` | `POST /v1/chat/completions`, `POST /v1/completions` | `/v1/proxy/chat/completions`, `/v1/proxy/completions` |
| `ANTHROPIC_BASE_URL=http://127.0.0.1:8000/v1/anthropic` | `POST /v1/anthropic/v1/messages` (the SDKs append `/v1/messages`) | `/v1/anthropic/messages` |

`/v1/proxy/*` and `/v1/anthropic/messages` stay. `GET /v1/models` keeps the
nvHive envelope (the capability catalog, `{"status", "data"}`); the
OpenAI-shaped list is `GET /v1/proxy/models`, so a tool that validates its
provider against `/models` should keep `.../v1/proxy` as its base URL.
`OPENAI_BASE_URL` is the variable the OpenAI SDK reads; LangChain and LiteLLM
also accept the older `OPENAI_API_BASE` spelling.

Since 0.46 the shared chat handler behind `/v1/chat/completions` and
`/v1/proxy/chat/completions` forwards a multi-turn transcript as history
instead of flattening it: the last `user` message is the prompt, every
`system` message joins the system prompt, and every other turn — earlier user
and assistant turns, an assistant's `tool_calls`, `tool` results with their
`tool_call_id` and `name` — reaches the model in order, on the streamed and
the non-streamed path alike. The header `x-nvhive-history: flatten` restores
the pre-0.46 single `User:` / `Assistant:` string. Council (`council:N`) and
throwdown take one prompt and always see the transcript flattened, and so
does a transcript with no trailing user message (an agent loop posting tool
results back) — it is answered, not refused. `tools` and `tool_choice`
round-trip: a non-streamed answer carries `message.tool_calls` with
`finish_reason: "tool_calls"` (`content` is `null` when the model said
nothing else); a stream emits one `delta.tool_calls` chunk, then the finish
chunk with `finish_reason: "tool_calls"`, then `[DONE]`. `stream_options:
{"include_usage": true}` adds the usage-only chunk (`choices: []`) between the
finish chunk and `[DONE]`; `response_format` is accepted and not enforced.
`GET /v1/proxy/health` lists `standard_paths`, `tools` and `history` under
`features`.

Streamed completions on every proxy path honour `budget.hard_stop` — the
stream carries one `budget_exceeded` error event instead of tokens — and are
written to the query ledger like the non-streamed ones, so `nvh budget` and
`GET /v1/budget/status` count coding-tool traffic. The two rows differ in one
column: a streamed call is logged as `mode: proxy`, a non-streamed call on the
same route goes through `Engine.query` and is logged as `mode: simple` (the
mode `nvh ask` writes), so `/metrics` and `nvh history` show proxy streams
under their own label while the budget totals sum both. A streamed call
through an OpenAI-compatible provider asks for `stream_options.include_usage`
whenever LiteLLM's parameter table says the route takes it (derived from
`litellm.get_supported_openai_params`, never from a provider list), so its
ledger row carries the provider's prompt and completion counts; a route
LiteLLM says lacks it keeps the pre-0.46 estimate (0 input tokens, output
estimated from the text), and a caller's own `stream_options` is never
overridden.

With `HIVE_API_KEY` set, every proxy path needs the key as a bearer token —
the Anthropic proxy included since 0.46: `OPENAI_API_KEY=<key>` and
`ANTHROPIC_AUTH_TOKEN=<key>` (the Anthropic SDKs send `ANTHROPIC_API_KEY` as
`x-api-key`, which nvHive does not read). In open mode any value works.

### `nvh launch`

`nvh launch <tool>` points an installed coding tool at the standard paths
without you copying anything: it checks `GET /v1/health`, then either
launches the tool or prints the snippet to paste into the tool's config. Of
the five shipped targets only `claude` launches anything — it registers
nvHive's MCP server and runs `claude` with no nvHive variable in its
environment (inference stays with Anthropic) — while `codex`, `opencode`,
`continue` and `cursor` print their snippet and start nothing. A target of
kind `env` would hand the tool the base URL and key through its child
environment; no shipped row uses that kind yet. It never writes a
third-party config file and never reads or renders a provider secret: the
only key it passes is `HIVE_API_KEY` when set, else the documented
placeholder `nvhive` — which means a snippet, a snippet-kind launch and
`--inference-proxy` print your live `HIVE_API_KEY` in clear text when one is
set, so keep that output out of pasted logs, issues and recordings.
`nvh launch --list` shows the table below; `nvh launch <tool> --print`
renders the variables or snippet offline (no API needed) for every target;
arguments after the target (or after `--`) go to the tool. The targets live
in the bundled `nvh/data/launch_targets.json`, read through `nvh.data`.

| Target | Tool | How | Verified |
|---|---|---|---|
| `claude` | Claude Code | MCP: registers nvHive's MCP server (`claude mcp add nvhive -- nvhive-mcp`, the same step `nvh integrate` runs) and launches `claude`; inference stays with Anthropic | unverified |
| `codex` | Codex CLI | snippet: a `~/.codex/config.toml` `[model_providers.nvhive]` stanza with `base_url`, `env_key = "HIVE_API_KEY"` and `wire_api = "chat"`, plus `model_provider = "nvhive"` | unverified |
| `opencode` | OpenCode | snippet: an `opencode.json` provider entry with `baseURL` and the routing modes as model ids | unverified |
| `continue` | Continue | snippet: a `~/.continue/config.yaml` model with `provider: openai` and `apiBase` | unverified |
| `cursor` | Cursor | snippet: Settings → Models → Override OpenAI Base URL, the key field, and a custom model named `auto` | unverified |

Every row carries `verified_on`. The five targets ship with `null`, which
prints as "unverified against this tool's current version — `--print` shows
exactly what nvHive sets": a tool's config shape is an external fact the
repo cannot test, so the column stays honest until a maintainer records a
date after running the tool against that release. The `docs_url` column of
`--list` points at each tool's own documentation.

Refusals exit 2 with the fix in the message: no healthy API answers on
`--port` (default 8000) unless `--start-api` (`-y`) lets `nvh launch` start
`nvh serve` on loopback — on a free port only: a port held by something that
does not answer nvHive's health check is refused, never stopped (`nvh
services restart` is the tool for a stale nvHive API); the API is in open
mode (no `HIVE_API_KEY`) and its `bind_host` is not loopback — `null` counts
as not loopback, so an API that `nvh serve` did not start is refused too;
the API requires a key (`key_required`) and there is no `HIVE_API_KEY` in
this shell — the placeholder would only earn a 401, so export the server's
key first; or `auth_mode` is `users` and there is no `HIVE_API_KEY` here to
forward (put a user token from `nvh auth create-token`, or the API key, in
`HIVE_API_KEY`). The loopback test is the server's own (`localhost`,
`127.0.0.1`, `::1`, with any port or IPv6 brackets stripped). An unknown
target exits 1 and lists the names; after a launch the exit code is the
tool's own — the tool also owns Ctrl-C, which never ends the launched
session from the outside — and 127 when the tool is not on `PATH` (the
command is resolved the way the shell resolves it, so an npm `claude.cmd`
shim on Windows counts). `--print` makes none of these checks.

`claude`: nvHive's Anthropic-compatible proxy does not pass `tool_use`
through, which Claude Code needs, so `nvh launch claude` sets no
`ANTHROPIC_*` variable and inference stays with Anthropic; it registers the
MCP server and runs `claude` with any extra arguments. `nvh launch claude
--inference-proxy` prints the `ANTHROPIC_BASE_URL=http://127.0.0.1:8000/v1/anthropic`
/ `ANTHROPIC_AUTH_TOKEN` snippet anyway, marked unverified, for anyone who
wants to try it without tools. `codex`: Codex's default provider speaks the
Responses API, which nvHive does not serve, hence the `wire_api = "chat"`
stanza that keeps it on `/chat/completions`. A tool that validates its
provider against `GET /models` should use `http://127.0.0.1:8000/v1/proxy`
as the base URL instead (see [Standard paths](#standard-paths)).

## nvHive as an MCP server

Coding tools attach nvHive's router and council as tools:

```bash
pip install "nvhive[mcp]"
claude mcp add nvhive -- nvhive-mcp          # Claude Code (console script)
claude mcp add nvhive -- nvh mcp             # the same server via the CLI
nvh mcp -t streamable-http --port 8080       # HTTP transport on 127.0.0.1 (this machine)
nvh mcp -t streamable-http --host 0.0.0.0 --port 8080   # HTTP transport for remote clients
```

Tools exposed: `ask`, `ask_safe`, `council`, `throwdown`, `status`,
`list_advisors`, `list_cabinets`. The accepted cabinet names are derived from
`nvh.core.agents.COUNCIL_PRESETS` so the MCP surface cannot drift from the
CLI. The startup banner goes to stderr; stdout is the JSON-RPC channel.

| Client | Registration |
|---|---|
| Claude Code | `claude mcp add nvhive -- nvhive-mcp`, then `/mcp` inside Claude Code to check |
| Claude Desktop | add `{"nvhive": {"command": "nvhive-mcp"}}` under `mcpServers` in `claude_desktop_config.json` |
| Cursor | same JSON in Cursor's MCP settings |
| OpenClaw | `nvh openclaw --config` writes `openclaw.json` |
| NemoClaw agents | `nvh nemoclaw --mcp` prints the agent-config snippet |

Typical prompts once registered: "use the nvhive council tool with cabinet
`security_review` to audit this file", "use nvhive `ask_safe` to analyse this
credentials file locally". Simple questions route to free providers or the
local GPU and only hard ones hit premium models.

Troubleshooting: `pip install "nvhive[mcp]"` if the SDK is missing; `nvh setup`
if the server reports no advisors; `nvh mcp` by hand to see the banner;
`nvh status` prints the installed `mcp` version. The `mcp` dependency is
pinned `>=1.0,<3` and both SDK majors are supported — see [ROADMAP.md](ROADMAP.md).
The HTTP transport binds `127.0.0.1` unless you pass `--host`, so a remote
client needs `--host 0.0.0.0` and something that authenticates in front of it
([SECURITY.md](../SECURITY.md#loopback-defaults)).

## nvHive as an MCP client

External MCP tool servers plug into the AI Wizard, and their tools join its
toolset next to the built-ins.

1. `pip install "nvhive[mcp]"`.
2. Create `$NVH_HOME/config/mcp-servers.json`. The `mcpServers` shape is
   Claude Desktop's, so existing configs paste straight in:

```json
{
  "mcpServers": {
    "filesystem": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "/data"],
      "env": {},
      "auto_approve": ["read_file", "list_directory"],
      "enabled": true
    }
  }
}
```

3. `nvh mcp servers refresh` connects to each enabled server, lists its tools
   and rewrites the cache; the API server does the same at startup and the
   Developer Tools page has a **Refresh tools** button. `nvh mcp servers
   list` shows status, and `GET /v1/mcp/servers` / `POST /v1/mcp/refresh`
   expose both to UIs.

Each tool registers as `mcp_<server>_<tool>` (for example
`mcp_filesystem_read_file`). Because external servers run third-party code,
every MCP tool defaults to confirm-before-run — the WebUI shows a "Do this?"
card — and only names in a server's `auto_approve` list run unattended.
Calls use short-lived sessions with hard timeouts (20 s connect, 60 s call),
and a dead server records its error and contributes no tools rather than
breaking the Wizard.

| Field | Required | Meaning |
|---|---|---|
| `command` | yes | executable to spawn (stdio transport) |
| `args` | no | argument list |
| `env` | no | extra environment for the server process |
| `auto_approve` | no | tool names allowed to run without confirmation |
| `enabled` | no | `false` keeps the entry but detaches it |

## NemoClaw

nvHive is both an inference provider and an MCP tool server for
[NVIDIA NemoClaw](https://github.com/NVIDIA/NemoClaw) (OpenShell):

```bash
nvh nemoclaw --start                     # 1. start the nvHive proxy
openshell provider create \              # 2. register it
    --name nvhive --type openai \
    --credential OPENAI_API_KEY=nvhive \
    --config OPENAI_BASE_URL=http://host.openshell.internal:8000/v1/proxy
openshell inference set --provider nvhive --model auto   # 3. make it the default
nvh nemoclaw --test                      # verify connectivity
```

Agents then request the virtual models above (`auto`, `safe`, `council:N`,
`throwdown`) and can send `x-nvhive-privacy: local-only` to honour content
sensitivity. `nvh nemoclaw --mcp` prints the MCP setup so agents can also call
`council()` and `throwdown()` explicitly — inference routing picks one model
per call, the MCP tools fan out to many. `nvh nemoclaw --install` installs
NemoClaw itself when Docker is available without sudo.

## OpenClaw

nvHive sits between OpenClaw and the model providers so routine queries go to
free or local models and only the ones that need it reach a premium API:

```bash
pip install nvhive
nvh migrate --from openclaw     # import existing API keys (also: Claw Code, Claude Desktop)
nvh serve                       # then point OpenClaw at the proxy:
#   ANTHROPIC_BASE_URL=http://localhost:8000/v1/anthropic
#   OPENAI_BASE_URL=http://localhost:8000/v1/proxy
nvh openclaw --config           # or register nvHive as OpenClaw's MCP server
nvh status --providers          # what is enabled and healthy
```

nvHive does not replace OpenClaw's agent orchestration or tool management, does
not give you free Claude access, and needs at least one provider configured
(Ollama counts). `nvh savings` shows what the routing saved after a few days.

## Claude Code, Cursor, Claude Desktop

`nvh integrate --scan` detects installed NemoClaw, OpenClaw, Claude Code,
Cursor and Claude Desktop; `nvh integrate --auto -y` writes the MCP
registration for each without prompting. The manual commands are in the MCP
server table above. `nvh launch claude` does the Claude Code registration and
starts `claude` in one step; see [`nvh launch`](#nvh-launch).

## VS Code

`vscode-nvhive/` is a thin client for a running API server: **Ask nvHive**,
**Code Review** (staged diff), **Generate Tests**, **Explain Code** and **Ask
Council** send text to `/v1/query` or `/v1/council` and show the reply in an
"nvHive Agent" panel. It is not on the Marketplace; open the folder in VS
Code, `npm install && npm run compile`, and press F5. The server must be in
open/local mode because the extension sends no credentials.

Back to [README](../README.md)
