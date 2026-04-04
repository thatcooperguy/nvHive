# Proposal: nvHive Claude Code Channels Integration

**Date:** April 4, 2026
**Status:** Draft
**Legal Risk:** Low (uses Anthropic's public, documented plugin API)

---

## Summary

Build an nvHive plugin for Claude Code Channels that lets Claude Code users access
multi-model routing, council consensus, and cost optimization directly within their
Claude Code sessions — whether triggered from the terminal, Telegram, Discord, or
iMessage.

This is **legally clean** — it uses Anthropic's documented, public plugin system and
MCP protocol. No leaked code involved.

---

## Why This Matters Now

1. **Channels just launched** (research preview) — early plugin ecosystem, low competition
2. **135K+ OpenClaw users** face new API costs — they need cheaper alternatives
3. **Enterprise orgs can allowlist custom plugins** — nvHive can be added to corporate
   plugin marketplaces
4. **nvHive already has an MCP server** — we're 70% of the way there

---

## What We'd Build

### Phase 1: nvHive MCP Tools Inside Claude Code (Already Works)

nvHive already registers as an MCP server:
```bash
claude mcp add nvhive -- uvx nvhive mcp
```

This gives Claude Code access to nvHive tools (ask, convene, route, etc.) during any
session. **No new code needed — just documentation and marketing.**

### Phase 2: nvHive Channel Plugin (New)

Build an nvHive channel plugin that pushes events INTO Claude Code sessions:

**Use cases:**
- **Cost alerts**: nvHive monitors API spend and pushes "You've spent $X today on Claude
  API — want me to route remaining queries through free providers?" into the session
- **Council results**: User kicks off a council deliberation via nvHive WebUI or CLI,
  and the consensus result gets pushed into Claude Code when ready
- **Provider health**: nvHive monitors provider status and alerts Claude Code when a
  provider goes down or comes back up
- **Batch results**: Long-running nvHive batch jobs push completion notifications into
  the active Claude Code session

**Technical approach:**
- Build as a Bun-based plugin following Anthropic's plugin spec
- Publish to a public plugin marketplace (or self-hosted for enterprise)
- Use nvHive's existing FastAPI backend as the event source
- Channel plugin bridges nvHive events → Claude Code session via MCP

### Phase 3: nvHive as a Routing Layer for Channels (Ambitious)

Position nvHive as the intelligence layer BETWEEN Claude Code Channels and LLM providers:

```
User (Telegram/Discord) → Claude Code Channel → nvHive MCP → Best Model for Task
                                                    ↓
                                              Groq (free, fast)
                                              DeepSeek (free, reasoning)
                                              Nemotron (local, private)
                                              Claude (complex tasks only)
```

**How it works:**
- User messages Claude Code via Telegram
- Claude Code receives the message and calls nvHive's MCP tools
- nvHive routes the query to the optimal model based on task type, cost, and privacy
- Result flows back through Claude Code → Telegram

**Value prop:** Claude Code becomes the orchestration UX. nvHive becomes the routing brain.
The user gets the best model for every query at the lowest cost.

---

## Technical Architecture

```
┌─────────────────────────────────────────────────────┐
│                   Claude Code Session               │
│                                                     │
│  ┌──────────┐   ┌──────────┐   ┌────────────────┐  │
│  │ Telegram  │   │ Discord  │   │   nvHive       │  │
│  │ Channel   │   │ Channel  │   │   Channel      │  │
│  │ Plugin    │   │ Plugin   │   │   Plugin       │  │
│  └────┬─────┘   └────┬─────┘   └───────┬────────┘  │
│       │              │                  │           │
│       └──────────────┴──────────────────┘           │
│                      │                              │
│              ┌───────▼────────┐                     │
│              │  Claude Code   │                     │
│              │  Tool Router   │                     │
│              └───────┬────────┘                     │
│                      │                              │
│              ┌───────▼────────┐                     │
│              │  nvHive MCP    │  ← Already exists   │
│              │  Server        │                     │
│              └───────┬────────┘                     │
└──────────────────────┼──────────────────────────────┘
                       │
          ┌────────────┼────────────────┐
          │            │                │
    ┌─────▼─────┐ ┌───▼────┐   ┌──────▼──────┐
    │ Free Tier  │ │ Mid    │   │ Premium     │
    │ Groq       │ │ DeepSk │   │ Claude      │
    │ GitHub     │ │ Gemini │   │ GPT-4o      │
    │ Nemotron   │ │ Grok   │   │ Opus        │
    └───────────┘ └────────┘   └─────────────┘
```

---

## Implementation Plan

| Phase | What | Effort | Prereq |
|-------|------|--------|--------|
| **1** | Document existing MCP integration for Claude Code users | 1 day | None |
| **2a** | Study Channels reference & plugin spec | 1 day | None |
| **2b** | Build nvHive channel plugin (Bun/TypeScript) | 3-5 days | 2a |
| **2c** | Test with fakechat, then Telegram/Discord | 2 days | 2b |
| **2d** | Publish to plugin marketplace | 1 day | 2c |
| **3** | Smart routing integration (nvHive decides which model) | 1 week | 2d |

**Total: ~2-3 weeks to full integration**

---

## What NOT to Do

### Do NOT use Claw Code / leaked source

- Claude Code's license is proprietary: "© Anthropic PBC. All rights reserved."
- Anthropic is actively filing DMCA takedowns (thousands of repos already hit)
- Claw Code's "clean-room" claim is legally untested
- Using ANY leaked code poisons nvHive for enterprise, investors, and acquirers
- **nvHive doesn't need it** — we have our own codebase and MCP integration

### What IS safe to use

- Anthropic's **public documentation** (Channels docs, MCP spec, plugin API)
- The **claude-plugins-official** repo (open-source reference plugins)
- **MCP protocol** (open standard, Apache 2.0 licensed)
- **Architectural concepts** discussed in public blog posts and articles
- Claude Code's **public CLI interface** and documented flags

---

## Competitive Positioning

| Feature | OpenClaw | Claw Code | nvHive + Claude Code |
|---------|----------|-----------|---------------------|
| Legal status | Targeted by Anthropic | Legal gray zone | Fully sanctioned |
| Model access | Claude only | Claude only | 63 models, 23 providers |
| Cost | Now requires API billing | Free (risky) | Smart routing, 25 free models |
| MCP integration | No | No | Native |
| Channel plugin | No | No | Yes (proposed) |
| Enterprise-ready | No | No | Yes |
| Council consensus | No | No | Yes |

---

## Bottom Line

**Don't steal the car. Build the better engine.**

nvHive's opportunity isn't in copying Claude Code — it's in becoming the routing brain
that makes Claude Code (and every other coding agent) smarter and cheaper. The Channels
plugin system is the door. Walk through it legally.
