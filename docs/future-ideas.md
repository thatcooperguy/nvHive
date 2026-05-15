# Future ideas & UX improvements (live log)

Captured during the Tier 1-6 build-out. Not all of these landed in this batch;
this file is the working list for the next round.

## UX / Onboarding
- Adaptive empty-state starters seeded from setup_helper_report + reconnect payload
- Inline numbered citations (Tavily-style [1][2] with hover) — replaces sources panel
- Live "session age" pill — "Session: 4h 12m on this ephemeral GPU"
- "Hand off to Wizard" button on chat errors (offline-safe)
- One-click model swap mid-conversation with cost diff
- Provider-key paste from clipboard with auto-detect (sk-, gsk-, …)
- OAuth device-code provider setup (where supported)
- Shareable workspace snapshot URL for forum helpers (local-only, signed)
- GPU-fit model recommender card on landing
- Persistent-mount visibility on /wizard route
- Global mount of WelcomeBackPanel (not just /)

## Wizard intelligence
- Receipt-aware proactive repair suggestions before user asks
- "Why this provider?" tooltip on auto-routed answers
- Tool-budget slider (max_iterations) in composer
- Council-aware citations when council members disagree
- Hierarchical agent memory (Mem0-style core/archival/recall)
- Hybrid BM25 + vector RAG with CrossEncoder reranking
- Spot-cost projection vs hourly desktop rate
- Multimodal cloud-or-local switch in one chat (drag PNG → vision model)

## Pipeline reliability
- Background vault re-index via inotify (Linux-only; polling diff fallback first)
- First-boot pre-warm during install (pull embed + recommended model in parallel)
- GPU-aware embed-model swap (bge-large on big VRAM)
- Sticky-prefix prompt caching across providers
- Response cache for identical requests
- MCP-as-a-tab — expose nvHive's tools over MCP to Claude Desktop / Cursor
- "Burn-the-VRAM" auto-warmup while cloud answers first question

## Distribution & extensibility
- server.py router split (nvh/api/routers/{wizard,rag,web_search,setup,auth}.py)
- Tool authoring SDK with sandbox (subprocess / wasm / Deno) — defer until external demand
- One credible internal benchmark (council vs single-model) — marketing oxygen
- Pipes/Functions/Filters extensibility (Open WebUI gap)

## Threats to watch
- Open WebUI shipping a "rootless cloud-desktop installer" with reconnect-survival
- Ollama bundling polished GUI + first-party web search (already in 2026)
