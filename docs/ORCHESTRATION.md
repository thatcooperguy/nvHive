# Local LLM Orchestration

The local Nemotron model doesn't just answer questions — it acts as an intelligent brain that orchestrates every cloud LLM call. All orchestration runs on your GPU for free.

## The Orchestrator's Role

When you ask a question, before any cloud API is called, the local model:

1. **Analyzes your query** — detects task type, complexity, privacy needs, and whether web access or code execution is required.
2. **Picks the best advisor** — goes beyond keyword matching to understand intent and route to the right cloud model.
3. **Rewrites your prompt** — optimizes wording for the target advisor's known strengths, reducing tokens and improving answer quality.
4. **Evaluates the response** — checks if the answer is complete and correct, and flags it for retry if not.
5. **Synthesizes locally** — when multiple advisors respond, merges their answers on your GPU instead of paying a cloud model to do it.
6. **Compresses conversation history** — summarizes long chats before sending context to cloud APIs, cutting token costs.

## Tiers

Orchestration scales automatically based on your GPU's available VRAM:

| Tier | VRAM Required | Features |
|------|--------------|----------|
| `off` | Any | Keyword routing, template agents (fallback mode) |
| `light` | 6 GB+ | Smart routing + prompt optimization |
| `full` | 20 GB+ | All features: routing, agents, eval, synthesis, compression |
| `auto` | — | Detects tier from available VRAM (default) |

With `auto` (the default), nvHive reads your GPU VRAM at startup and enables the highest tier your hardware supports. If no local model is available, the engine falls back gracefully to keyword-based routing — no errors, no configuration needed.

## Enabling and Disabling

```bash
# Show current orchestration mode
nvh config get defaults.orchestration_mode

# Disable orchestration (keyword routing only)
nvh config set defaults.orchestration_mode off

# Enable light mode (smart routing + prompt optimization)
nvh config set defaults.orchestration_mode light

# Enable full mode (all features)
nvh config set defaults.orchestration_mode full

# Auto-detect from VRAM (default)
nvh config set defaults.orchestration_mode auto
```

## Cost Impact

Every orchestration call runs on your local GPU — it costs nothing. The savings come indirectly:

- **Better routing** reduces expensive cloud calls by sending more queries to cheaper or local models.
- **Prompt optimization** sends fewer tokens to cloud APIs, directly reducing per-query cost.
- **Response evaluation** catches bad answers before you need to re-ask, avoiding retry costs.
- **Local synthesis** replaces cloud synthesis calls (the most expensive part of council mode) with free local inference.

---

Back to [README](../README.md)
