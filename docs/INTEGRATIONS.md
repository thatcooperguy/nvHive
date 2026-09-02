# Integrations

Every programmatic way into nvHive: the Python SDK, the REST API, the OpenAI-
and Anthropic-compatible proxies, MCP in both directions, and the guided
setups for Claude Code, Cursor, OpenClaw, NemoClaw and VS Code.

| Surface | Start with | Use it for |
|---|---|---|
| Python SDK | `from nvh import ask` | scripts and apps in the same process |
| REST API | `nvh serve` | anything that speaks HTTP; the WebUI uses it |
| OpenAI-compatible proxy | `base_url=http://localhost:8000/v1/proxy` | any OpenAI SDK client, NemoClaw, OpenClaw |
| Anthropic-compatible proxy | `ANTHROPIC_BASE_URL=http://localhost:8000/v1/anthropic` | tools that speak the Messages API |
| MCP server | `nvh mcp` / `nvhive-mcp` | Claude Code, Cursor, Claude Desktop, OpenClaw agents |
| MCP client | `$NVH_HOME/config/mcp-servers.json` | give the Wizard external tools |
| `nvh integrate` | `nvh integrate --scan` | detect and configure installed AI tools in one go |

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
| `GET /v1/setup/*`, `GET /v1/jobs/*` | setup state and background install jobs |
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
client = OpenAI(base_url="http://localhost:8000/v1/proxy", api_key="nvhive")
client.chat.completions.create(model="auto", messages=[{"role": "user", "content": "Hello"}])
```

`/v1/proxy/chat/completions`, `/v1/proxy/completions` and `/v1/proxy/models`
mirror the OpenAI paths; `/v1/anthropic/messages` mirrors Anthropic's
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

## nvHive as an MCP server

Coding tools attach nvHive's router and council as tools:

```bash
pip install "nvhive[mcp]"
claude mcp add nvhive -- nvhive-mcp          # Claude Code (console script)
claude mcp add nvhive -- nvh mcp             # the same server via the CLI
nvh mcp -t streamable-http --port 8080       # HTTP transport for remote clients
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

Troubleshooting: `pip install "mcp[cli]"` if the SDK is missing; `nvh setup`
if the server reports no advisors; `nvh mcp` by hand to see the banner. The
`mcp` dependency is pinned `>=1.0,<2` — see [ROADMAP.md](ROADMAP.md).

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
server table above.

## VS Code

`vscode-nvhive/` is a thin client for a running API server: **Ask nvHive**,
**Code Review** (staged diff), **Generate Tests**, **Explain Code** and **Ask
Council** send text to `/v1/query` or `/v1/council` and show the reply in an
"nvHive Agent" panel. It is not on the Marketplace; open the folder in VS
Code, `npm install && npm run compile`, and press F5. The server must be in
open/local mode because the extension sends no credentials.

Back to [README](../README.md)
