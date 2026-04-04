# nvHive + Claude Code Integration Guide

> Give Claude Code access to 63 models across 23 providers — with one command.

## Quick Start

### Register nvHive as an MCP Server

```bash
# Option 1: If nvHive is installed via pip
claude mcp add nvhive -- python -m nvh.mcp_server

# Option 2: If using uvx (no install needed)
claude mcp add nvhive -- uvx nvhive mcp

# Option 3: Direct entry point
claude mcp add nvhive -- nvhive-mcp
```

That's it. Claude Code now has access to nvHive's full toolkit.

## What Claude Code Gets

Once registered, Claude Code can use these tools in any session:

| Tool | What It Does |
|------|-------------|
| `ask` | Smart-routed query across 63 models — nvHive picks the best provider for the task |
| `ask_safe` | Local-only inference via Ollama — nothing leaves your machine |
| `council` | Multi-model consensus — 3+ LLMs debate and synthesize an answer |
| `throwdown` | Two-pass deep analysis with cross-critique between models |
| `status` | System health: providers, GPU, budget |
| `list_advisors` | Available providers and their status |
| `list_cabinets` | Expert persona presets (engineering, security_review, executive, etc.) |

## Why Use nvHive Inside Claude Code?

### 1. Cost Savings
Claude Code uses Anthropic models by default. With nvHive:
- Simple queries route to **free providers** (Groq, GitHub Models, LLM7, local Nemotron)
- Only complex tasks hit premium models
- Council mode: 3 free models often match a single premium model's quality

### 2. Multi-Model Verification
Ask Claude Code to verify its own answer using nvHive's council:
```
"Use the nvhive council tool to get a second opinion on this architecture decision"
```

### 3. Privacy-Sensitive Queries
Route sensitive code through local models only:
```
"Use nvhive ask_safe to analyze this credentials file locally"
```

### 4. Expert Panels
Convene a council of specialists:
```
"Use the nvhive council tool with cabinet 'security_review' to audit this code"
```

## Example Workflows

### Code Review with Multi-Model Consensus
```
You: "Use nvhive council with the code_review cabinet to review the changes in src/auth.py"
Claude Code → nvHive council → 3 expert personas review → synthesized feedback
```

### Cost-Optimized Research
```
You: "Use nvhive ask to research best practices for database indexing"
Claude Code → nvHive smart router → routes to free Groq/DeepSeek → instant answer at $0.00
```

### Deep Analysis
```
You: "Use nvhive throwdown to analyze the tradeoffs of microservices vs monolith for our app"
Claude Code → nvHive throwdown → 3 LLMs analyze → 3 LLMs cross-critique → synthesis
```

## Configuration

nvHive reads config from `~/.hive/config.yaml`. Key settings:

```yaml
# Provider API keys
providers:
  groq:
    api_key: "gsk_..."      # Free tier: 30 req/min
  github:
    api_key: "ghp_..."      # Free tier: 150 req/day
  openai:
    api_key: "sk-..."       # Pay per use

# Budget controls
budget:
  daily_limit_usd: 1.00     # Hard stop at $1/day
  warn_threshold: 0.80      # Alert at 80%

# Routing preferences
routing:
  prefer_free: true          # Prioritize free providers
  prefer_local: false        # Prioritize local Ollama models
  fallback_enabled: true     # Auto-fallback on provider failure
```

Run `nvh setup` for an interactive configuration wizard.

## Also Works With

nvHive's MCP server is compatible with any MCP client:

| Platform | Registration Command |
|----------|---------------------|
| **Claude Code** | `claude mcp add nvhive -- python -m nvh.mcp_server` |
| **Claude Desktop** | Add to `claude_desktop_config.json` |
| **Cursor** | Add to MCP settings |
| **OpenClaw** | Add to `openclaw.json` |

## nvHive Channel Plugin (Coming Soon)

nvHive is building a Claude Code Channel plugin that pushes real-time events
(cost alerts, council results, provider status) directly into your Claude Code
session. See [CLAUDE_CODE_CHANNELS_INTEGRATION.md](proposals/CLAUDE_CODE_CHANNELS_INTEGRATION.md)
for the roadmap.

## Troubleshooting

**"MCP SDK not installed"**
```bash
pip install "mcp[cli]"
```

**"No advisors enabled"**
```bash
nvh setup          # Interactive configuration
nvh test --quick   # Verify provider connectivity
```

**Server not responding**
```bash
# Test the MCP server directly
python -m nvh.mcp_server
# Or check Claude Code's MCP status
# In Claude Code, type: /mcp
```

## Links

- [nvHive on PyPI](https://pypi.org/project/nvhive/)
- [nvHive GitHub](https://github.com/thatcooperguy/nvhive)
- [MCP Protocol Spec](https://modelcontextprotocol.io)
- [Claude Code MCP Docs](https://code.claude.com/docs/en/mcp)
