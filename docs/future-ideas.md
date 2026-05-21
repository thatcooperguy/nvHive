# Future ideas & UX improvements (live log)

The working list for the next-batch backlog. Items move *out* as they ship
and stay *in* until they do. Strikethroughs mark shipped or merged-as-done.

## Agent depth (new category — surfaced by the avatars batch)

- **Real ComfyUI portrait workflow.** Ship a default SDXL or FLUX workflow
  JSON, wire the `/prompt` + websocket-poll loop. The endpoint + UI exist
  in [#48](https://github.com/thatcooperguy/nvHive/pull/48); this is the
  missing piece to flip the "upload instead" hint into a working button.
- **NVIDIA-hosted multi-modal portrait gen** as a parallel path to ComfyUI.
  When `NVAPI_KEY` is set, route Generate Portrait through NVIDIA's image
  endpoint — zero install, ties directly to the rootless-NVIDIA wedge.
- **Per-agent conversation pinning.** Today `/pin` flags a chat for
  resume; pinning *the agent* (so a Researcher thread reopens with the
  Researcher profile pre-selected) is one boolean on the conversation
  record.
- **Agent marketplace.** Profiles are YAML; export to shareable URL +
  import from a URL. Community profiles with no server.
- **Agent skill chains.** Add `tool_chain_hint` on `AgentProfile` — a
  recipe string like "rag_ask_vault → web_search if no chunks → answer"
  that gets surfaced to the model as guidance, not enforcement.
- **Multi-agent council per turn.** Send the same question to 2-3 agents
  in parallel, render answers side-by-side, let the user pick or
  synthesize. Reuses the existing council infrastructure with a new UI.

## Visual identity

- **Avatar in chat bubbles.** Wire `AgentAvatar` into `MessageBlock` so
  every reply shows which agent answered.
- **Per-agent accent color** on message borders (extracted from the
  avatar SVG spec). Multi-agent threads become scannable.
- **Status pill on the avatar.** Green = local Ollama, blue = cloud
  provider, amber = router fallback. Immediate signal of where the
  answer came from.

## UX / Onboarding

- **First-run agent tour** — replace the empty Wizard state with six
  avatar cards (one per built-in) so users meet their agents before
  they type. Pick one → starter prompt customized to that agent.
- **OAuth device-code provider setup** (Groq, Google, GitHub support
  device codes) — eliminates the paste-a-key step entirely.
- **`.env` bulk import** on `/providers`. Drag a `.env` file (or paste
  many lines), we parse `OPENAI_API_KEY=…` style lines, validate each,
  save the valid ones.
- **Snapshot import from URL** — `https://example.com/my-snapshot.tar.gz`
  in a single field, server downloads + extracts. Cross-desktop handoff.
- **Merge `/` and `/wizard` or rename `/`.** Two chat surfaces confuses
  first-time users. Either redirect `/` → `/wizard` or rename `/` to
  "Quick Query".
- **Hardcoded hex colors → CSS vars** in page bodies. Dark mode toggle
  has a few cards stuck light because of literals like `text-[#0a0a0a]`.
- **`/providers` tab state in URL** — refreshing the page resets to
  Providers tab; move to query string.
- **Replace `window.confirm()`** with the existing `<ConfirmDialog>`
  pattern. One last 2003 prompt to kill.
- Adaptive empty-state starters seeded from setup_helper_report +
  reconnect payload (initial Tier 2 pass; needs more variants).
- Inline numbered citations (Tavily-style [1][2] with hover) — replaces
  the sources-panel UX with stitched-in references.
- Live "session age" pill — "Session: 4h 12m on this ephemeral GPU".
- "Hand off to Wizard" button on chat errors (offline-safe).
- One-click model swap mid-conversation with cost diff.
- ~~Provider-key paste from clipboard with auto-detect (sk-, gsk-, …)~~
  — shipped in [#48](https://github.com/thatcooperguy/nvHive/pull/48).
- Shareable workspace snapshot URL for forum helpers (local-only, signed).
- ~~GPU-fit model recommender card on landing~~ — shipped Tier 4.
- Persistent-mount visibility on /wizard route.
- ~~Global mount of WelcomeBackPanel (not just /)~~ — shipped Tier 2.

## Wizard intelligence

- Receipt-aware proactive repair suggestions before user asks.
- "Why this provider?" tooltip on auto-routed answers.
- ~~**Per-agent cost ceiling**~~ — shipped: non-streaming path enforces
  `max_cost_usd_per_turn` and aborts the follow-up loop early; UI shows a
  banner. **Follow-up: enforce on the streaming path too** once
  provider.stream emits per-chunk usage we can roll up (today the stream
  only surfaces the ceiling for display, not enforcement).
- Tool-budget slider (max_iterations) in composer.
- Council-aware citations when council members disagree.
- Hierarchical agent memory (Mem0-style core/archival/recall).
- Hybrid BM25 + vector RAG with CrossEncoder reranking.
- Spot-cost projection vs hourly desktop rate.
- Multimodal cloud-or-local switch in one chat (drag PNG → vision model).
- **Live agent leaderboard.** `/analytics` already tracks per-provider
  spend; pivot per-agent so users see which profile is most productive
  vs most expensive.

## Voice & multimodal

- **Voice replies via local TTS** (Piper, Coqui) — pipe streamed tokens
  through TTS so the Wizard can actually speak. The metered-desktop "I
  stepped away from the keyboard" moment.
- Voice-in via Whisper (revisit — was deferred as off-wedge during the
  two-pass review, but the TTS pairing changes the calculus).

## Pipeline reliability

- Background vault re-index via inotify (Linux-only; polling diff
  fallback first).
- First-boot pre-warm during install (pull embed + recommended model in
  parallel).
- GPU-aware embed-model swap (bge-large on big VRAM).
- Sticky-prefix prompt caching across providers.
- Response cache for identical requests.
- MCP-as-a-tab — expose nvHive's tools over MCP to Claude Desktop / Cursor.
- "Burn-the-VRAM" auto-warmup while cloud answers first question.

## Distribution & extensibility

- server.py router split (nvh/api/routers/{wizard,rag,web_search,setup,
  auth}.py).
- Tool authoring SDK with sandbox (subprocess / wasm / Deno) — defer
  until external demand.
- One credible internal benchmark (council vs single-model) — marketing
  oxygen.
- Pipes/Functions/Filters extensibility (Open WebUI gap).
- **PageHeader rollout** to /vault, /settings, /system, /analytics,
  /integrations, /providers, /council. (`web/components/PageHeader.tsx`
  exists; the Wizard page uses it. Mechanical migration for the rest.)

## Threats to watch

- Open WebUI shipping a "rootless cloud-desktop installer" with
  reconnect-survival.
- Ollama bundling polished GUI + first-party web search (already in 2026).
