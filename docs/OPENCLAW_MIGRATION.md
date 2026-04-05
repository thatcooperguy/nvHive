# Using nvHive with OpenClaw

> nvHive works alongside OpenClaw to reduce your API costs by routing
> simple queries to free providers and reserving Claude for what needs it.

## What Changed

On April 4, 2026, Anthropic announced that Claude subscriptions (Pro, Max)
would no longer cover usage through third-party tools like OpenClaw. OpenClaw
still works — but now requires either pay-as-you-go API billing or a
separate Anthropic API key at full API rates.

This means OpenClaw users who were leveraging their flat-rate subscription
now face significantly higher costs for the same usage.

## How nvHive Helps

nvHive is a routing layer that sits between your tools and LLM providers.
It doesn't replace OpenClaw — it reduces how much you spend on Claude by
routing queries that don't need Claude to free or cheaper alternatives.

**The idea:** Not every query needs a $15/M-token model. A simple "what does
this error mean?" can go to Groq (free, 30 RPM) instead of Claude. nvHive
makes that routing decision automatically, based on task type and measured
provider quality.

### Setup

```bash
pip install nvhive

# Import your existing API keys from OpenClaw
nvh migrate --from openclaw

# Check what providers are available
nvh health

# Verify everything works
nvh test --quick
```

### Using nvHive as OpenClaw's Routing Backend

Point OpenClaw's inference at nvHive's proxy. nvHive accepts OpenAI and
Anthropic API formats and routes through the best available provider:

```bash
# Start nvHive's API server
nvh serve

# In your OpenClaw config, set the base URL:
# ANTHROPIC_BASE_URL=http://localhost:8000/v1/anthropic
# or
# OPENAI_BASE_URL=http://localhost:8000/v1/proxy
```

nvHive decides per-query:
- Simple queries → free providers (Groq, GitHub Models, LLM7) at $0
- Code questions → routes to whichever provider the learning loop has
  measured as best for code (could be Claude, could be Groq/Llama)
- Complex reasoning → premium provider when quality requires it
- Private/sensitive → local GPU via Ollama (no data leaves your machine)

### For NemoClaw Users

nvHive integrates with NemoClaw as both an inference provider and MCP
tool server. No OpenClaw dependency needed:

```bash
# Start nvHive proxy for NemoClaw
nvh nemoclaw --start

# Register with NemoClaw
openshell provider create \
    --name nvhive --type openai \
    --credential OPENAI_API_KEY=nvhive \
    --config OPENAI_BASE_URL=http://host.openshell.internal:8000/v1/proxy

# Set as default inference provider
openshell inference set --provider nvhive --model auto
```

NemoClaw agents get access to:
- 23 providers with automatic failover
- Local GPU inference (Ollama/Nemotron)
- Council consensus (3+ models collaborating)
- Privacy routing (`x-nvhive-privacy: local-only` header)
- Virtual models: `auto`, `safe`, `council`, `council:N`, `throwdown`

### For Claude Code Users

Claude Code's MCP protocol is Anthropic's supported extension mechanism.
nvHive registers as an MCP server:

```bash
pip install "nvhive[mcp]"
claude mcp add nvhive -- python -m nvh.mcp_server
```

This gives Claude Code access to nvHive tools: `ask`, `council`,
`throwdown`, `safe`, `status`, `list_advisors`, `list_cabinets`.

---

## What nvHive Does and Doesn't Do

**Does:**
- Route queries across 23 providers based on task type and quality
- Learn which providers work best for your specific queries over time
- Fail over automatically when a provider is down or rate-limited
- Run inference locally on your GPU for free, private queries
- Provide council consensus for higher-quality answers
- Track costs and enforce budget limits

**Doesn't:**
- Replace OpenClaw's agent orchestration or tool management
- Give you free Claude access (you need your own API key for Claude)
- Guarantee identical responses to what Claude would produce
- Work offline without at least one provider configured (Ollama for local)

## Cost Comparison

| Scenario | Direct Claude API | Through nvHive |
|----------|------------------|----------------|
| Simple Q&A | ~$0.003/query | $0 (free provider) |
| Code generation | ~$0.01/query | $0-0.01 (depends on complexity) |
| Complex reasoning | ~$0.05/query | ~$0.05 (routes to Claude when needed) |
| Council (3 models) | N/A | $0 (3 free providers) |

Actual savings depend on your query mix. Run `nvh savings` to see your
real numbers after a few days of usage.

## Quick Checklist

- [ ] `pip install nvhive`
- [ ] `nvh migrate --from openclaw` (imports your API keys)
- [ ] `nvh setup` (add any additional providers)
- [ ] `nvh health` (verify provider resilience)
- [ ] `nvh test --quick` (verify everything works)
- [ ] Point OpenClaw/NemoClaw at nvHive proxy
- [ ] Run `nvh benchmark --mode council-free` to see quality data
