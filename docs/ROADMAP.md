# nvHive Roadmap

Generated 2026-08-05 from a 15-agent research pass: 7 web-trend scouts
(top GitHub skill/agent/MCP/local-AI listings), 4 competitor analysts
(Open WebUI, LibreChat, AnythingLLM, LM Studio, Jan, Pinokio, Harbor —
each gap verified against this repo's code), and the Agent Library
design pass. This is the build plan for bringing the stack to life.

## Where nvHive already wins

- One-curl rootless install on a rented GPU desktop with VRAM-tiered model selection — Open WebUI assumes you already have Docker/pip and a running Ollama; nvHive provisions the whole lab (install.sh, GPU tier matrix, Ollama health-wait, port-conflict detection) without root
- Smart multi-provider routing with learned quality: 23 providers, RoutingOutcome/LearnedScore/QualityBenchmarkLog tables drive cost- and quality-aware model choice — Open WebUI connects many APIs but makes the user pick the model every time
- Council mode: 23-persona multi-model deliberation with synthesis — Open WebUI's multi-model chat is side-by-side comparison with no deliberation or synthesized verdict
- Snapshot/reconnect survival built for ephemeral rented rigs (reconnect-resume card, pinned conversations surviving eviction, reconnect_survived telemetry event) — Open WebUI has no concept of a workspace that dies out from under you
- Self-healing AI Wizard that operates the workspace itself: repair_workspace, refresh_models, validate/save provider keys as chat tools — OWUI's assistant chats, it doesn't fix the deployment
- Studio packs: one-command ComfyUI, game-dev, and music environments on the same rig — outside OWUI's scope entirely
- Native MCP server plus Claude Code channel plugin (channel-plugin/nvhive-channel.ts): Claude Code/Cursor can route through nvHive's router and council — OWUI consumes MCP but doesn't offer itself as a tool server to coding agents
- Agentic coding loop with sandboxed run_code/shell (Docker isolation with subprocess fallback), guardrails, and cross-session agent memory (.nvhive/agent-memory.json)
- Per-message cost/token/latency accounting with per-agent-profile cost ceilings surfaced inline in chat — finer-grained cost control than OWUI's admin-level usage dashboard
- Rootless-everything design: cron-free scheduler, zero-dependency SQLite RAG, keyring-backed secrets — everything works as an unprivileged user on someone else's machine
- VS Code extension (vscode-nvhive/) shipping in-repo
- Markdown vault wired directly into RAG (rag_ask_vault, vault_bridge.py) so notes are queryable knowledge, not a separate notes silo

## Feature gaps (verified against the code)

### Critical — table stakes users expect on day one

| Feature | Who has it | Status | Effort |
|---|---|---|---|
| MCP client support (attach external MCP tool servers to chat/wizard) | Open WebUI (MCP + MCPO integration, plus OpenAPI tool servers) | missing | medium |
| Chat history sidebar (browse, resume, pin past conversations in the web UI) | Both — LibreChat and AnythingLLM have full conversation history UIs; it's table-stakes in every ChatGPT-class product | present-but-hidden | small |
| MCP client — connect external MCP servers as agent tools | Both — LibreChat has dynamic MCP server management from the UI with lazy tool loading; AnythingLLM is MCP-compatible for agent skills | missing | medium |
| In-app model browser/downloader UI (search catalog, one-click pull with size/VRAM-fit preview and live progress) | LM Studio (Hugging Face GGUF/MLX browser showing size, quant level, and estimated VRAM before download); Jan (in-app Hub with hardware-fit labels) | present-but-hidden | medium |
| Community/third-party app catalog with publishable install scripts | Pinokio (searchable directory of community-built AI apps/agents, hundreds of one-click scripts anyone can publish; 31k Windows installs in one week of v8.0.40); Harbor (100+ pre-wired services: 10+ frontends, 15+ backends, 90+ satellites) | missing | large |

### High — differentiators worth building next

| Feature | Who has it | Status | Effort |
|---|---|---|---|
| Code execution surfaced in chat (run Python/shell from the Wizard, show output) | Open WebUI (Pyodide in-browser + Open Terminal with real execution, file browser, live preview) | present-but-hidden | small |
| Persistent cross-chat memory in the web UI | Open WebUI (memory settings, auto context retention across conversations) | present-but-hidden | small |
| Image generation from chat | Open WebUI (DALL-E, Gemini, ComfyUI, AUTOMATIC1111 integrated into conversations) | present-but-hidden | small |
| Voice: STT input and TTS output in the web UI (hands-free mode) | Open WebUI (multi-provider STT/TTS, voice & video call mode) | present-but-hidden | medium |
| Hybrid RAG retrieval (BM25 + vector) with reranking | Open WebUI (hybrid search, rerank models, agentic retrieval, full-context injection mode) | partial | medium |
| Vision/image input in chat (plus OCR) | Both — LibreChat processes uploaded images and has OCR; AnythingLLM supports multimodal chat | missing | medium |
| In-chat image generation | LibreChat — GPT-Image-1, DALL-E, Stable Diffusion, Flux in chat; AnythingLLM has chart/image agent skills | present-but-hidden | small |
| Code interpreter in web chat (sandboxed execution surfaced in the wizard) | LibreChat — sandboxed Python/JS/Go/Rust execution API; AnythingLLM via agent skills | present-but-hidden | small |
| Message/conversation search | LibreChat — full-text message search via Meilisearch; AnythingLLM — searchable chat logs | missing | medium |
| Multi-user login UI + per-user data isolation | Both — LibreChat: OAuth2/SAML/LDAP, 2FA, moderation; AnythingLLM: three-tier RBAC (admin/manager/default) with workspace-scoped document access | partial | large |
| Artifacts (live rendering of generated HTML/React/Mermaid with an editor) | LibreChat — artifacts with Monaco code editor for React, HTML, Mermaid | missing | medium |
| Quantization variant picker (choose Q2–FP16 per model with size/quality tradeoff shown) | LM Studio (quant variants listed per model with file size + VRAM estimate); Jan (GGUF quant variants from HF) | missing | medium |
| Hardware/inference tuning controls (context length, GPU offload layers, flash attention, KV-cache quant, CPU threads) | LM Studio (per-model defaults for GPU offload, context size, flash attention); Jan (llama.cpp backend variants + GPU layers/context/threads settings) | missing | medium |
| MCP client/host support (attach external MCP tool servers to the Wizard/agents) | Jan (MCP integration for agentic capabilities is a headline README feature); LM Studio (acts as MCP host via mcp.json, connects local models to MCP tools) | missing | large |
| General model import (arbitrary GGUF file or any HF repo → local runtime) | LM Studio (import models feature); Jan (import local GGUF files) | partial | medium |
| Pack/app uninstall and disk reclamation | Pinokio (uninstall + disk management per app), Stability Matrix (remove packages), Harbor (docker compose down/rm reclaims everything) | missing | medium |
| Unified process management for installed apps (start/stop/logs per app in the UI) | Pinokio (start/stop any installed app from the browser UI with live logs), Harbor (harbor up/down/logs/exec/shell per service + Harbor App GUI with real-time status) | partial | medium |
| App update management (update-available detection, one-click update) | Stability Matrix (one-click per-package updates), Pinokio (app updates from the UI), Harbor (image pulls) | partial | medium |
| Standalone app-store style catalog page in the dashboard | Pinokio (Discover page is the product's front door), Harbor App (service grid), Stability Matrix (package browser) | present-but-hidden | small |

### Nice-to-have

| Feature | Who has it | Status | Effort |
|---|---|---|---|
| Document extraction breadth (docx/pptx/xlsx, OCR engines like Tika/Docling) | Open WebUI (8 pluggable extraction engines incl. Mistral OCR, Azure, Docling) | partial | medium |
| Chat organization: folders and tags (nvHive has pin only) | Open WebUI (folders, tags, pins, search across chats) | partial | small |
| Scheduled prompts / automations with a UI (and calendar view) | Open WebUI (automations, prompt scheduling, built-in calendar with AI scheduling) | present-but-hidden | small |
| Multi-user login UI, user groups, per-model access control | Open WebUI (granular RBAC, groups, per-model ACL, admin panel) | partial | medium |
| Enterprise auth: SSO/OIDC, LDAP/AD, SCIM provisioning | Open WebUI | missing | large |
| Team channels (shared real-time spaces, threads, reactions, @model tagging) | Open WebUI (Channels) | missing | large |
| Model arena / A-B evaluation with ELO leaderboard UI | Open WebUI (arena mode, ELO leaderboards) | partial | medium |
| Pluggable vector databases (Chroma, PGVector, Qdrant, Milvus, Pinecone...) | Open WebUI (9-13 vector DB integrations) | missing | medium |
| Web search provider breadth | Open WebUI (dozens of search providers incl. You.com, Tavily, Google PSE) | partial | small |
| PWA / installable mobile experience | Open WebUI (responsive PWA with offline shell) | missing | small |
| Internationalization (i18n) | Open WebUI (community-translated into many languages) | missing | medium |
| Cloud file integration (Google Drive, OneDrive/SharePoint) for RAG | Open WebUI | missing | medium |
| Scale-out deployment: Kubernetes/Helm, Postgres, S3/GCS storage, Redis multi-node, OpenTelemetry | Open WebUI | partial | large |
| In-UI Python tool editor and community tool/function marketplace | Open WebUI (built-in editor, pipelines framework, openwebui.com community hub) | partial | large |
| Persistent user memory injected into web chat | Both — LibreChat has user memory for cross-conversation context; AnythingLLM has memory/personalization | present-but-hidden | small |
| Voice I/O (STT/TTS) in the web UI | LibreChat — STT/TTS in chat; AnythingLLM — transcription models | present-but-hidden | medium |
| Shareable conversation links, export, forking, ChatGPT import | LibreChat — share links, conversation forking, temporary chats, import from ChatGPT | missing | medium |
| Embeddable chat widget for third-party sites | AnythingLLM — embeddable chat widgets | missing | medium |
| Pluggable vector databases for RAG | AnythingLLM — LanceDB, Chroma, Milvus, Pinecone, QDrant, Weaviate, AstraDB, Zilliz | missing | medium |
| Admin panel GUI (roles, users, access controls without CLI/YAML) | LibreChat — admin panel for user/role management, full config GUI on the 2026 roadmap; AnythingLLM — admin UI with logs and analytics | missing | large |
| Agent marketplace / sharing agent profiles | LibreChat — agent marketplace with granular ACL-based permissions | missing | large |
| Standard-path OpenAI endpoints (/v1/chat/completions and /v1/models at the conventional path) | LM Studio (localhost:1234/v1/...); Jan (localhost:1337/v1/...) | partial | small |
| TypeScript/JS SDK | LM Studio (lmstudio-js alongside its Python SDK) | partial | medium |
| Preset/config sharing hub (import, share, publish chat presets or agent configs) | LM Studio (config presets with import/sharing and publishing) | partial | medium |
| Speculative decoding | LM Studio (draft-model speculative decoding as a headline speed feature) | missing | large |
| Native cross-platform desktop app with fully-offline single-binary UX (incl. MLX on Apple Silicon) | LM Studio (Win/macOS/Linux desktop app, MLX + GGUF runtimes); Jan (Tauri desktop app, Win 10+/macOS 13.6+/Linux, NVIDIA/AMD/Intel Arc) | missing | large |
| In-app model browser for image-gen checkpoints/LoRAs (CivitAI/HuggingFace) | Stability Matrix (integrated CivitAI + HuggingFace browser with one-click download, preview images, and a shared-model dir across UIs) | partial | large |
| Side-by-side versions / multiple instances of the same app | Pinokio 8 (explicitly markets installing, switching between, and managing multiple copies/versions of one app), Stability Matrix (multiple package instances sharing models) | missing | large |
| Multiple local inference backends (llama.cpp, vLLM, TabbyAPI, mistral.rs, Aphrodite) | Harbor (15+ interchangeable inference backends pre-wired to its frontends) | partial | large |
| Tunnel/QR remote access to running services | Harbor (harbor tunnel exposes services to the internet; harbor qr for phone access) | missing | medium |
| Supply-chain security scanning of installed apps | Pinokio (Bluefairy security engine against software supply-chain attacks, shipped in v7.2, April 2026) | partial | medium |
| Windows/macOS desktop support | Pinokio (Win/Mac/Linux installers), Stability Matrix (Win/Linux/macOS), Harbor (anywhere Docker runs, incl. macOS Metal via Docker Model Runner/MLX) | partial | large |

## Trend adoptions (small/medium effort, from top GitHub listings)

Design-inspiration only — nvHive implements its own versions; nothing is copied.

### Small effort

- **anthropics/claude-plugins-official** — Two concrete borrows. (1) The immutable-slug + `renames` migration map is a bug nvHive will otherwise hit the first time a built-in profile is renamed — user copies under $NVH_HOME/agent-profiles/ would silently orphan. Add a renames table to the profile loader now, while there are only 6 built-ins. (2) The internal-vs-external split with an explicit trust disclaimer is the right governance shape for nvHive's studio packs: ships-with vs community-contributed must be visually and structurally distinct in the WebUI.
- **Token/context-optimization single skills (caveman, ponytail, i-have-adhd)** — The highest stars-per-line-of-code in the entire ecosystem, and disproportionately relevant to nvHive's actual constraint. nvHive users run local VRAM-tiered models on rented GPU desktops where every token is latency and every cloud fallback is metered spend. A brevity/verbosity control surfaced per-profile (nvHive already has temperature and max_tokens per profile — add an output-discipline dimension) is cheap to build and maps directly onto the rented-desktop persona the strategic consensus identified. Do not copy any prompt text; the pattern is the insight.
- **Curated 'awesome' distribution lists (ComposioHQ, VoltAgent, hesreallyhim, travisvn)** — Two things. (1) **Distribution**: these lists are where the rented-GPU-desktop user actually discovers tooling. Getting nvHive listed under 'Providers, Runtime & Integration Infrastructure' and 'Alternative Clients' in hesreallyhim's list is a zero-engineering-cost acquisition channel and is far closer to the 'ship the demo, stop shipping features' consensus than another feature. (2) **Taxonomy**: their section headers are a free, empirically-validated category vocabulary for nvHive's own profile browser — do not invent a taxonomy, reuse the one users already navigate. VoltAgent explicitly gates on 'community-adopted, proven in real-world usage — no skills you created 3 hours ago'.
- **Design/UI skill packs (nextlevelbuilder/ui-ux-pro-max-skill, nexu-io/open-design)** — Design-quality skills are among the very highest-starred in the whole ecosystem, which says the market's unmet need is output *taste*, not output *capability*. nvHive ships a Next.js dashboard and studio packs (ComfyUI, game-dev, music) — the creative surface is already there. A design-discipline skill attached to the studio packs is a high-signal, low-cost addition. The local-first desktop framing of open-design is also directly on-brand for a rootless workspace on a rented GPU desktop.
- **Cross-harness skill path convention (VoltAgent compatibility table)** — Cheapest interop win available. nvHive keeps profiles at $NVH_HOME/agent-profiles/ — invisible to every other agent on the same rented desktop, and vice versa. Adding a `$NVH_HOME/skills/` path plus optional symlinks into the harness-standard project/global locations means a skill installed once on the rig serves nvHive, Claude Code, and Cursor together. Given nvHive's users are provisioning fresh rented desktops repeatedly, being the tool that *sets up* the shared skills directory during install.sh is a genuine wedge rather than a checkbox.
- **x1xhlol/system-prompts-and-models-of-ai-tools** — Massive demand signal: people study how production agents structure identity, tool-use rules, guardrails, and output contracts. nvHive's 6 built-in profiles should be written to that production standard (identity block, tool whitelist rationale, refusal rules, output format) and shown transparently in the UI as a teaching feature. Never copy text (GPL + policy).
- **asgeirtj/system_prompts_leaks** — Confirms the same demand as x1xhlol at CC0 licensing: 'how do the pros write agent prompts' is a top-3 user interest. A 'view full effective system prompt' inspector in nvHive's agent profile UI (persona + tools + guardrails rendered as the model sees it) would serve this appetite natively.
- **hesreallyhim/awesome-claude-code** — Shows the winning meta-structure for an agent ecosystem index: skills / agents / commands / hooks / plugins as distinct resource types. nvHive's extension surface should name and separate these same primitives so community contributions have obvious slots.
- **ashishpatel26/500-AI-Agents-Projects** — Vertical demand map: healthcare, finance, legal, education, and cybersecurity dominate. nvHive could ship 3-5 vertical 'starter packs' (profile + RAG corpus slot + tool whitelist) targeting what renters actually build on GPU rigs.
- **linexjlin/GPTs** — Evidence that a store of user-authored personas generates its own gravity; the most-copied GPTs are tutors, writing fixers, logo/design makers, and code copilots — a shortlist for nvHive's next built-in profiles (written from scratch).
- **JushBJJ/Mr.-Ranedeer-AI-Tutor** — 29k stars for ONE persona proves depth beats breadth when the persona is configurable. nvHive profiles should expose declared knobs (dropdowns/sliders in the dashboard, variables in YAML) instead of asking users to edit prompt prose.
- **e2b-dev/awesome-ai-agents** — Its landscape taxonomy (coding, research, productivity, personal assistant, data/science, GUI/browser control, multi-agent) is a ready-made top-level nav for nvHive's agent gallery.
- **langgptai/LangGPT** — Validates schema-first persona authoring. nvHive's agent-profile YAML should formalize these sections as named fields (identity, skills, rules, workflow, greeting) so the Wizard can render, diff, and lint profiles — and so a profile-generator tool can fill the schema.
- **Microsoft MarkItDown** — Pattern for nvHive's rag_ingest + Markdown vault: a convert-anything-to-Markdown front stage so users can drop PDFs/docx/pptx into the vault and RAG index, not just .md files. Its popularity proves file-conversion-for-LLMs is the highest-demand capability in the ecosystem.
- **GitHub MCP Server (official)** — Hot capability confirmation: version control is a top-3 MCP category. Pattern: its toolset-grouping + read-only mode flags are a good model for nvHive's per-agent tool-whitelist YAML (group tools into named toolsets, gate destructive ones).

### Medium effort

- **anthropics/skills** — This is the canonical shape nvHive's agent-profiles should converge on. Today nvHive's profiles are a frozen dataclass in nvh/integrations/wizard/profiles.py with the entire system_prompt inlined and no bundled resources — a profile cannot ship a script, a reference doc, or a template. Restructure $NVH_HOME/agent-profiles/<name>/ to SKILL.md + scripts/ + references/ + assets/, keeping the current YAML fields (provider/model/temperature/tools_allowed/max_cost_usd_per_turn/avatar) under the spec's `metadata:` map so nvHive stays spec-valid while retaining its router-specific extras.
- **Agent Skills spec (agentskills/agentskills + agentskills.io)** — The single highest-leverage adoption. Emitting and consuming spec-valid SKILL.md turns nvHive's 6 built-in profiles from a proprietary silo into an ecosystem client — users can drop in any of the ~44k+ community skills and nvHive's council personas become portable artifacts. Note `allowed-tools` maps almost 1:1 onto nvHive's existing `tools_allowed` whitelist, and `compatibility` is the natural home for VRAM-tier requirements (e.g. `compatibility: Requires an 8GB+ VRAM tier and a local Ollama endpoint`). Wire `skills-ref validate` into CI next to the existing version-parity test.
- **Progressive disclosure (three-stage loading pattern)** — nvHive is the ecosystem member that needs this most, because it runs VRAM-tiered local models with small context windows — a 7B model on a low tier cannot afford the whole persona library resident. Today the Wizard has no notion of staged loading. Implement a profile/skill index that injects only name+description into the system prompt, then fetches the body via a tool call on match. This directly buys back context for RAG results on the exact tiers where context is scarcest.
- **obra/superpowers** — Two patterns. (1) Workflow skills beat knowledge skills — its SKILL.md bodies are imperative process rules ("NO PRODUCTION CODE WITHOUT A FAILING TEST FIRST") rather than reference material, and that is what makes small local models behave consistently. nvHive's council mode should get deliberation-methodology skills (verification-before-completion, dispatching-parallel-agents) rather than only its 23 personas. (2) `using-superpowers` is a meta-skill that teaches the agent to reach for its own skill library — nvHive's Wizard needs exactly this bootstrap skill or the profile library stays invisible to the model.
- **vercel-labs/skills (npx skills / skills.sh)** — nvHive already owns a one-curl install story; it should own a one-command *skill* story too. Ship `nvh skill add <source>` with the same resolver grammar (owner/repo, tree-path, local dir) writing into $NVH_HOME/agent-profiles/. Two details worth copying verbatim in spirit: the symlink-by-default/`--copy`-opt-out choice (keeps skills updatable in place, which matters for nvHive's snapshot/reconnect survival), and the non-interactive `-y` path so skill installs can ride inside install.sh unattended.
- **addyosmani/agent-skills** — The lifecycle-command-as-entry-point pattern is a better discovery UX than nvHive's current flat profile picker. A user does not know they want the 'Code Reviewer' profile; they know they want to ship. Map nvHive's existing tools (repair_workspace, validate_provider_key, rag_ingest, refresh_models) onto a small set of verbs in the WebUI Wizard, and let the verb select the profile rather than making the user pick a persona cold. Also note the explicit 'one approval, then autonomous, pauses on risk' contract — a clean model for nvHive's per-turn cost ceiling (max_cost_usd_per_turn) to plug into.
- **kepano/obsidian-skills** — The closest structural analog to a feature nvHive already ships. nvHive has a Markdown vault plus rag_ask_vault, but the vault is currently reachable only through RAG retrieval — the agent can *ask* the vault but cannot *operate* it. Adopt the pattern of vault-manipulation skills (create note, link notes, restructure, canvas/graph views over the vault) so the vault becomes a workspace rather than a corpus. The open-formats-first stance also matters: it is why these skills port across harnesses, and nvHive's vault is already Markdown, so the port cost is near zero.
- **K-Dense-AI/scientific-agent-skills** — Three transferable moves. (1) The **unified-lookup skill** — one skill fronting 78 databases beats 78 skills, which is exactly how nvHive should expose its 23 providers to the model rather than enumerating them. (2) **Version-aware package skills** — skills that pin and check library versions, directly applicable to nvHive's CUDA/driver/VRAM tier matrix where version drift is the top failure mode. (3) **CI that tests skills**, not just code — nvHive should gate profile changes on a skill-test workflow the same way it gates version parity.
- **Skill security scanning (snyk/agent-scan + VoltAgent security notice)** — If nvHive adds `nvh skill add <source>`, it inherits an arbitrary-instruction-execution surface pointed at a machine that already holds the user's 23 provider API keys in the vault. Non-negotiable design requirements before shipping any skill installer: (a) scan/quarantine on install with a diff shown to the user, (b) pin by commit SHA not branch — the 'maintainer mutates after listing' failure mode is the real one, (c) honour `allowed-tools` as an enforced deny-by-default whitelist wired into nvHive's existing tools_allowed rather than an advisory hint, and (d) never let an installed skill reach save_provider_key or validate_provider_key without an explicit user grant. VoltAgent's four quality criteria (third-person descriptions, progressive disclosure, no absolute paths, scoped tools — 'avoid blanket tools: ["*"]') are a ready-made lint rule set.
- **anthropics skill-creator (+ yusufkaraaslan/Skill_Seekers)** — nvHive's Wizard is already tool-using with rag_ingest and rag_ask_vault — it is one step from generating skills instead of only consuming them. Two moves: (1) a `create_profile`/`create_skill` Wizard tool so users author personas conversationally rather than hand-editing YAML, and (2) point rag_ingest's existing doc pipeline at skill *generation* — nvHive can already ingest a docs site, so emitting a spec-valid SKILL.md from that corpus is mostly plumbing. The eval-viewer is the part most people skip and shouldn't: without a way to measure whether a persona actually triggers, a 23-persona council is unfalsifiable.
- **f/prompts.chat (f.k.a. awesome-chatgpt-prompts)** — Two lessons: (1) the winning UX arc is README -> searchable card gallery with one-click copy/use -> self-hostable app; nvHive's dashboard should render agent profiles as a searchable card gallery, not a YAML directory. (2) The evergreen archetypes are translator, tutor, interviewer, coach, tool-simulator.
- **Shubhamsaboo/awesome-llm-apps** — Best-in-class gallery taxonomy: organize by capability tier (starter -> advanced -> team -> always-on), and always offer 'Local & Cloud' variants — that maps exactly to nvHive's Ollama-vs-23-provider router. Also validates 'agent team preset' as a first-class shippable unit alongside single profiles.
- **PlexPt/awesome-chatgpt-prompts-zh** — A localized fork alone earned 61k stars: persona packs are language/culture-sensitive. Low-cost win: make nvHive persona text a locale-swappable field rather than hardcoded English.
- **danielmiessler/Fabric** — The strongest non-persona pattern: a TASK library orthogonal to personas. nvHive should adopt (a) verb_noun naming convention and (b) pattern-per-markdown-file in $NVH_HOME/patterns/, runnable via CLI pipe and from the Wizard — perfect fit for a terminal-first GPU workspace.
- **wshobson/agents** — Two direct adoptions: (1) per-agent model TIERING — nvHive's router should let a profile declare a tier (e.g. 'local-VRAM', 'cheap-cloud', 'frontier') instead of a hardcoded model, mapped through the GPU tier matrix; (2) 'orchestrator' as a distinct profile type that composes other profiles — a generalization of council mode.

## Shipped from this plan

- **Agent Library (2026-08-05)** — 100 original agent profiles across 38
  categories, bundled in the package (`nvh/catalog/agent-library.json`),
  grouped on /agents and in the composer picker. Copy-and-edit into
  `$NVH_HOME/agent-profiles/` to customize.
