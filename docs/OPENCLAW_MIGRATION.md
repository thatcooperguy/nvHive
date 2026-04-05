# OpenClaw Migration Guide

> Anthropic dropped OpenClaw support. This guide covers what changed,
> what still works, and how to keep your workflow running.

## What Happened

Anthropic ended support for OpenClaw, affecting ~135K developers who used it
for Claude Code integrations and third-party tool routing. If you were using
OpenClaw to access Claude or manage multi-tool workflows, that path is closed.

## What nvHive Can Do For You

nvHive is not a drop-in OpenClaw replacement. It's a different architecture
that solves the underlying problem: **your workflow depended on one provider.**

### If you used OpenClaw for Claude Code integrations

**Claude Code's MCP protocol still works.** Anthropic supports MCP as the
official extension mechanism. nvHive registers as an MCP server:

```bash
pip install "nvhive[mcp]"
claude mcp add nvhive -- python -m nvh.mcp_server
```

This gives Claude Code access to nvHive's tools:
- `ask` — route queries across 23 providers (picks the best for the task)
- `ask_safe` — local inference only, nothing leaves your machine
- `council` — 3+ models debate and synthesize a consensus answer
- `throwdown` — two-pass deep analysis with cross-critique
- `status` — provider health and budget

**This is not OpenClaw.** It's nvHive's own MCP server using Anthropic's
supported protocol. No dependency on OpenClaw.

### If you used OpenClaw for Anthropic API access

nvHive accepts Anthropic Messages API format. Point your existing SDK at
nvHive and it routes through any available provider:

```bash
# Start nvHive's API server
nvh serve

# Point your Anthropic SDK at nvHive
export ANTHROPIC_BASE_URL=http://localhost:8000/v1/anthropic
```

Your existing code works unchanged. nvHive handles routing.

**What this does:** Accepts the same request format as the Anthropic API.
Routes through the best available provider (could be Claude if you have an
API key, could be Groq/Google/local if you don't).

**What this doesn't do:** Give you free Claude access. If you need Claude
specifically, you need your own Anthropic API key. nvHive routes to it when
appropriate and fails over to alternatives when it can't.

### If you used OpenClaw to manage API keys

```bash
nvh migrate --from openclaw
```

This scans for OpenClaw configs (`~/.openclaw/`, `~/.config/openclaw/`) and
imports your API keys into nvHive's keyring. Also checks environment
variables and Claude Desktop configs.

---

## NemoClaw Users

**NemoClaw (NVIDIA's agent framework) used OpenClaw's infrastructure.** If you
were running NemoClaw agents with Claude as the inference provider via
OpenClaw, that path is affected.

**nvHive replaces that path directly.** Here's how:

### Architecture: Before vs After

**Before (with OpenClaw):**
```
NemoClaw Agent → OpenClaw → Anthropic Claude API
                    ↑
            (Anthropic dropped this)
```

**After (with nvHive):**
```
NemoClaw Agent → OpenShell Gateway → nvHive Proxy → 23 Providers
                                          ↓
                                    Smart Router
                                    ↓         ↓
                              Local GPU    Cloud APIs
                             (Nemotron)   (Claude, GPT-4,
                                           Groq, Google...)
```

**No OpenClaw dependency.** nvHive speaks OpenAI-compatible format through
its proxy, which NemoClaw already understands via OpenShell Gateway.

### Setup (3 commands)

```bash
# 1. Start nvHive proxy
nvh nemoclaw --start

# 2. Register with NemoClaw
openshell provider create \
    --name nvhive --type openai \
    --credential OPENAI_API_KEY=nvhive \
    --config OPENAI_BASE_URL=http://host.openshell.internal:8000/v1/proxy

# 3. Set as default inference provider
openshell inference set --provider nvhive --model auto
```

### What NemoClaw agents get through nvHive

| Feature | Via OpenClaw (broken) | Via nvHive |
|---------|----------------------|-----------|
| Claude access | Was direct | Via API key + routing |
| Multi-provider | Not available | 23 providers, automatic |
| Local inference | Not available | Ollama/Nemotron on your GPU |
| Council consensus | Not available | 3+ models collaborate |
| Failover | Not available | Automatic, transparent |
| Cost control | Not available | Budget limits, free routing |
| Privacy mode | Not available | `x-nvhive-privacy: local-only` header |

**NemoClaw agents get MORE through nvHive than they had through OpenClaw.**
The OpenClaw path gave you Claude. nvHive gives you Claude + 22 other
providers + local GPU inference + council consensus + automatic failover.

### Virtual Models for NemoClaw

NemoClaw agents can request any of these virtual models:

| Model | What It Does |
|-------|-------------|
| `auto` | Smart routing — best provider for the query |
| `safe` | Local only — nothing leaves your machine |
| `council` | 3-model consensus with synthesis |
| `council:5` | 5-model consensus |
| `throwdown` | Two-pass deep analysis with critique |

### MCP Tools for NemoClaw Agents

In addition to inference routing, NemoClaw agents can call nvHive tools
directly via MCP:

```json
{
  "mcpServers": {
    "nvhive": {
      "command": "nvhive-mcp"
    }
  }
}
```

Tools: `ask`, `ask_safe`, `council`, `throwdown`, `status`,
`list_advisors`, `list_cabinets`.

---

## What nvHive Cannot Do

Being honest about the limits:

- **We can't give you free Claude access.** OpenClaw had a deal with
  Anthropic. That's over. If you need Claude, you need your own API key.
- **We can't guarantee 100% OpenClaw compatibility.** Some OpenClaw
  integrations used Claude-specific features (tool use schemas, specific
  response formats) that may not translate perfectly through the proxy.
- **Council is not a Claude replacement.** It's a different approach.
  On some tasks, single Claude beats council. On others, council wins.
  Run `nvh benchmark` to see the data for yourself.
- **Free tiers have rate limits.** Groq: 30 RPM. Google: 15 RPM.
  For sustained production use, either run Ollama locally (unlimited)
  or add paid provider keys.

## What nvHive Gives You That OpenClaw Didn't

- **Provider independence.** Next time any provider changes terms, your
  workflow continues. nvHive routes around the problem automatically.
- **Adaptive routing.** Learns which providers work best for your tasks
  from actual measured quality. Gets smarter the more you use it.
- **Local-first.** Your NVIDIA GPU runs inference by default. Cloud is
  the fallback, not the default.
- **Resilience visibility.** `nvh health` shows your failover chain.
  You can see that your workflow survives any single provider going down.
- **Quality proof.** `nvh benchmark` lets you verify quality claims
  yourself. No trust required.

## Quick Migration Checklist

- [ ] `pip install nvhive`
- [ ] `nvh migrate --from openclaw` (imports API keys)
- [ ] `nvh setup` (add any missing providers)
- [ ] `nvh test --quick` (verify everything works)
- [ ] `nvh health` (check resilience)
- [ ] Update Claude Code: `claude mcp add nvhive -- python -m nvh.mcp_server`
- [ ] Update NemoClaw: `nvh nemoclaw --start` + register provider
- [ ] Run `nvh benchmark --mode council-free` to see quality data
