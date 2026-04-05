# Commands

Complete reference for all nvHive CLI commands.

## Essentials

| Command | Description |
|---------|-------------|
| `nvh "question"` | Smart default -- routes to the best available advisor |
| `nvh ask "question"` | Ask a specific advisor (use `-a provider`) |
| `nvh convene "question"` | Convene a council of AI-generated expert agents |
| `nvh poll "question"` | Ask every configured advisor, compare answers |
| `nvh throwdown "question"` | Two-pass deep analysis across all providers |
| `nvh quick "question"` | Fastest available model, minimal latency |
| `nvh safe "question"` | Local models only -- nothing leaves your machine |
| `nvh do "task"` | Detect action intent and execute (install, open, find) |

## Focus Modes

| Command | Description |
|---------|-------------|
| `nvh code "question"` | Code-optimized routing and prompts |
| `nvh write "question"` | Writing-optimized with style guidance |
| `nvh research "question"` | Multi-source research with citations |
| `nvh math "question"` | Math and reasoning, step-by-step |

## Tools

| Command | Description |
|---------|-------------|
| `nvh bench` | GPU benchmark -- measure tokens/second |
| `nvh benchmark` | Quality benchmark suite. Runs 16 prompts through different modes (single/council-free/council-premium/throwdown), scores with blind LLM judge. Flags: `--mode`, `--judge`, `--tasks`, `--export`, `--output` |
| `nvh scan` | Scan and index project files |
| `nvh learn "topic"` | Interactive learning sessions |
| `nvh clip` | Clipboard integration |
| `nvh voice` | Voice input/output |
| `nvh imagine "prompt"` | Image generation |
| `nvh screenshot` | Capture and analyze screenshots |
| `nvh git` | Git-aware operations |

## System

| Command | Description |
|---------|-------------|
| `nvh status` | Show configured providers, GPU, active model |
| `nvh health` | Provider health & resilience dashboard. Shows all providers' health scores, status (healthy/degraded/down), position in fallback chain, and resilience summary |
| `nvh routing-stats` | Routing intelligence dashboard. Shows learned vs static capability scores per provider/task, sample counts, and deltas. Flags: `--provider`, `--task`, `--reset` |
| `nvh nvidia` | NVIDIA AI infrastructure dashboard. Shows GPU hardware, inference stack status (Ollama/NIM/Triton), local models, `--prefer-nvidia` routing status |
| `nvh savings` | Track how much you have saved with free/local models |
| `nvh debug` | Debug mode with verbose output |
| `nvh doctor` | Diagnose configuration and connectivity |
| `nvh setup` | Interactive provider setup wizard |
| `nvh migrate` | Migrate from OpenClaw/Claw Code/Claude Desktop. Auto-detects configs, imports API keys. Flags: `--from`, `--dry-run` |
| `nvh keys` | Show all free API key signup links in one table |
| `nvh keys --open` | Open all free provider signup pages in browser |
| `nvh webui` | Launch web UI (auto hostname + smart port) |
| `nvh integrate` | Auto-detect and connect all AI platforms |
| `nvh integrate --auto` | Connect everything without prompting |
| `nvh update` | Check for and install updates |
| `nvh version` | Print version |
| `nvh serve` | Start the API server |
| `nvh serve --daemon` | Install as persistent background service |
| `nvh mcp` | Start MCP server (Claude Code, Cursor, OpenClaw) |
| `nvh openclaw` | OpenClaw integration setup guide |
| `nvh nemoclaw` | NemoClaw integration setup guide |

## Management

| Command | Description |
|---------|-------------|
| `nvh advisor` | Manage advisor profiles and routing weights |
| `nvh agent` | Manage auto-generated expert agents and cabinets |
| `nvh config` | View and edit configuration |
| `nvh conversation` | List, export, or resume conversations |
| `nvh budget` | Set and monitor spending limits |
| `nvh model` | List, pull, or remove models |
| `nvh template` | Manage prompt templates |
| `nvh workflow` | Run multi-step YAML pipelines |
| `nvh knowledge` | Manage knowledge base entries |
| `nvh schedule` | Schedule recurring queries |
| `nvh webhook` | Configure webhook integrations |
| `nvh auth` | Manage API keys and authentication |
| `nvh plugins` | Install and manage plugins |
| `nvh serve` | Start the OpenAI-compatible API server |
| `nvh repl` | Launch interactive REPL |
| `nvh completions` | Generate shell completions |

## Direct Advisor Access

Skip the router and talk directly to a provider:

```bash
nvh openai "question"       # Route to OpenAI
nvh groq "question"         # Route to Groq
nvh google "question"       # Route to Gemini
nvh ollama "question"       # Route to local Ollama
```

Works for all 23 providers. Run `nvh <provider>` with no question to launch that provider's setup.

---

Back to [README](../README.md)
