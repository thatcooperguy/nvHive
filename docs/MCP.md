# MCP — Model Context Protocol in nvHive

nvHive speaks MCP in both directions:

1. **nvHive as an MCP server** — coding tools (Claude Code, Cursor,
   OpenClaw) attach nvHive's routing/council as tools via the bundled
   `nvhive-mcp` command. See the Integrations page.
2. **nvHive as an MCP client** (this doc) — attach *external* MCP tool
   servers, and their tools appear in the AI Wizard's toolset. This is
   how you give the Wizard (and all 100+ agent profiles) real-world
   capabilities: filesystems, browsers, databases, GitHub, search, and
   anything else the MCP ecosystem ships.

## Setup

1. Install the MCP extra if you haven't:

```bash
pip install 'nvhive[mcp]'
```

2. Create `$NVH_HOME/config/mcp-servers.json`. The `mcpServers` shape is
   the same as Claude Desktop's, so existing configs paste straight in:

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

3. Connect and cache the servers' tools:

```bash
nvh mcp servers refresh
```

The API server also refreshes the cache automatically on startup, and the
Integrations page has a **Refresh tools** button.

4. Check status any time:

```bash
nvh mcp servers list
```

## How tools appear to the Wizard

Each MCP tool registers as `mcp_<server>_<tool>` (for example
`mcp_filesystem_read_file`) with the server name visible in its
description. The Wizard sees them in its tools block alongside the
built-in eight and calls them the same way.

## Safety model

External MCP servers run arbitrary third-party code, so:

- **Every MCP tool defaults to confirm-before-run** — the WebUI shows a
  "Do this?" card before execution.
- A per-server `auto_approve` list promotes named tools to auto-run.
  Reserve it for read-only operations on servers you trust.
- Tool calls run in short-lived sessions with hard timeouts (20s connect,
  60s call), so a wedged server can never hang the chat loop or leak
  processes.
- A dead or misconfigured server records its error in the status output
  and contributes no tools; it never breaks the Wizard.

## Field reference (`mcp-servers.json`)

| Field | Required | Meaning |
|---|---|---|
| `command` | yes | Executable to spawn (stdio transport) |
| `args` | no | Argument list |
| `env` | no | Extra environment variables for the server process |
| `auto_approve` | no | Tool names allowed to run without confirmation |
| `enabled` | no | Set `false` to keep the entry but detach it |

## Surfaces

| Surface | What it does |
|---|---|
| `nvh mcp servers list` | Config + cached tool status, per-tool safety class |
| `nvh mcp servers refresh` | Reconnect all servers, rewrite the tools cache |
| `GET /v1/mcp/servers` | Status JSON for UIs |
| `POST /v1/mcp/refresh` | Same as the CLI refresh |
| Integrations page | Server cards + Refresh tools button |
