# nvHive Simplification Plan — September 2026

Audit date 2026-09-01, against `main` at v0.41.0 (+ the unreleased fixes in
CHANGELOG `[Unreleased]`). Companion to [ROADMAP.md](../ROADMAP.md), which
ranks features against competitors; this document ranks what to delete,
fix, and consolidate before any of those features are built.

nvHive is two products stacked on top of each other — a `~/.hive`-era
"Council" core and the `NVH_HOME`-era "Wizard" layer — and every
user-visible problem the six lenses found (dead default models in 13 of 21
providers, a broken `nvh mcp`, a snapshot command that tars the wrong tree,
secrets the engine cannot read, four chat surfaces, 95 CLI names) is a
symptom of that duplication rather than a missing feature. The plan is:
ship a one-week 0.41.1 hotfix for the things that are broken today (`nvh
mcp` shadowed; Groq/Perplexity/Google/xAI/etc. defaults retired; GitHub
Models gone since 2026-07-30; llm7 — the only enabled-by-default provider —
pointing at an absent model; Perplexity Chat Completions sunsetting
2026-09-27), then 0.42 "subtract" (~15k LOC of code/scripts and ~3.5k doc
lines deleted with no feature loss), 0.43 "refresh" (one `ProviderSpec`
table, litellm-derived pricing, real `list_models`, one local-model tier
table with `num_ctx`, one tool registry, one council pipeline, `NVH_HOME` as
the only root), and 0.44 "add", where the Wizard finally gets to act —
`run_code`/`shell` with approvals, `remember`, `generate_image`, vision —
mostly by registering handlers that already exist. Say no to everything
enterprise, multi-tenant, marketplace, alt-platform, and to the GeForce NOW
Operator side project living in the product repo.

## How this was produced

Six independent lenses ran over the repo on 2026-09-01 — bit-rot,
duplication, model currency, UX, feature gaps (competitors), and repo
hygiene — and their findings were cross-checked against each other and
against the source before being merged here. Three classes of evidence:

- **Verified in-source.** Import graphs (which modules have zero importers),
  `difflib` similarity across provider adapters, `typer.main.get_command(app)`
  introspection of the CLI registry, `grep` counts, and line-referenced reads
  of every file named below. Stated as fact in this document.
- **Verified against official web pages, fetched 2026-09-01.** Groq's
  deprecations page (llama-3.3-70b-versatile and llama-3.1-8b-instant
  deprecated 2026-08-16; replacements `openai/gpt-oss-120b`,
  `openai/gpt-oss-20b`), Perplexity's model docs (`sonar`, `sonar-pro`,
  `sonar-reasoning-pro`, `sonar-deep-research`; Chat Completions supported
  until 2026-09-27), the GitHub changelog (GitHub Models retired 2026-07-30),
  Anthropic's API reference (`claude-fable-5-1`, `claude-opus-5`,
  `claude-sonnet-5`, `claude-sonnet-4-6`, `claude-haiku-4-5`; opus-4-6 is
  $5/$25), and ollama.com/library/nemotron3 (tag `nemotron3:33b`, 28GB, 128K
  ctx). Stated as fact, with the source named.
- **Unverified.** Replacement model IDs for OpenAI, Google, xAI, DeepSeek,
  Mistral, Cohere, Cerebras, SambaNova, Together, Fireworks, OpenRouter
  `:free`, AI21, NIM Nemotron, SiliconFlow and LLM7; proposed Ollama library
  additions (gemma4, qwen3.5, gpt-oss, qwen3-coder, nemotron-3-super);
  SiliconFlow's real-name-verification and DeepSeek-V2.5 discontinuation
  claims; every competitor release claim. Marked **unverified** wherever they
  appear; nothing in the sequencing depends on them.

Lens claims that were checked and found wrong are listed under
[Verification caveats](#verification-caveats) rather than silently dropped.

## Broken today (0.41.1)

Everything in this table is a defect a user hits on a fresh 0.41.0 install.
The hotfix ships before the 2026-09-27 Perplexity sunset.

| Symptom | Evidence | 0.41.1 fix |
|---|---|---|
| `nvh mcp` cannot start the MCP server | `get_command(app).commands['mcp']` is the TyperGroup at [main.py:13723](../../nvh/cli/main.py) (`list`/`refresh` only, no `invoke_without_command`); the server command at [main.py:6386](../../nvh/cli/main.py) is unreachable, so [COMMANDS.md:63](../COMMANDS.md) and [SDK_API.md:56](../SDK_API.md) (`claude mcp add nvhive nvh mcp`) are wrong | `mcp_app` gets `invoke_without_command=True`; external-server verbs move to `nvh mcp servers list\|refresh` (old names as hidden aliases); registry-collision test over `registered_commands` + `registered_groups`; fix both docs |
| Default/fallback model IDs are retired | Groq `llama-3.3-70b-versatile` + `llama-3.1-8b-instant` deprecated 2026-08-16; Perplexity `llama-3.1-sonar-large-128k-online` absent from Perplexity's model list; Google `gemini-2.0-flash`, xAI `grok-2`, Together/Fireworks/OpenRouter/SambaNova Llama-3.1-70B, Cerebras `llama3.1-70b`, AI21 `jamba-1.5-large`, Cohere `command-r-plus`, HF `Meta-Llama-3-8B-Instruct`, NVIDIA `meta/llama-3.1-70b-instruct`, OpenAI `gpt-4o`, llm7 `deepseek-r1-0528`; [settings.py:475](../../nvh/config/settings.py) makes llm7 the only `enabled: true` cloud provider | Swap IDs in all four copies (see [Update 1](#1-hotfix-swap-the-retired-defaultfallback-model-ids-in-all-four-copies)); pin only IDs verified today; ship `nvh config migrate` with a renames map; `nvh doctor` warns when a configured default is not in the catalog |
| GitHub Models provider still shipped | Service retired 2026-07-30 (github.blog changelog); [engine.py:433](../../nvh/core/engine.py) auto-enables it whenever `GITHUB_TOKEN` is set; [free_tier.py:51-56](../../nvh/core/free_tier.py) gives it priority 2; 23 files reference it | Delete [github_provider.py](../../nvh/providers/github_provider.py) and all 23 references; `nvh config migrate` prints the retirement notice (see [Update 2](#2-hotfix-remove-the-github-models-provider)) |
| `nvh snapshot save\|restore` tars the wrong tree | [core/snapshot.py:13-19](../../nvh/core/snapshot.py) bundles `.hive/config.yaml`, `.nvhive/agent-memory.json`, `.council/council.db`; the API/web/Wizard use [integrations/workspace/snapshot.py](../../nvh/integrations/workspace/snapshot.py) ([server.py:2339,2371,2392](../../nvh/api/server.py)) which bundles the `NVH_HOME` vault, RAG index, receipts and pinned conversations; [README.md:44](../../README.md) sells the CLI command as the reconnect-survival story | Point the CLI verbs at `export_snapshot`/`import_snapshot` |
| `nvh advisor remove` leaves the provider enabled; engine ignores keyring | [main.py:4396-4404](../../nvh/cli/main.py) deletes only the keyring copy; the engine reads keyring only if `NVH_USE_KEYRING` is truthy, default `'0'` ([engine.py:444](../../nvh/core/engine.py), [registry.py:127](../../nvh/providers/registry.py)) while keys are written to keyring at 7 sites | Remove the `.env`/`config.yaml` copies too; make `NVH_USE_KEYRING`'s default match what `nvh setup` writes, pending the 0.43 secrets module |
| VS Code extension status bar always reads "unreachable" | [extension.ts:48](../../vscode-nvhive/src/extension.ts) fetches `/health`; [server.py](../../nvh/api/server.py) defines only `/v1/health` (0 vs 1 occurrences) | `/health` → `/v1/health` (one line), or mark the extension deprecated |
| Perplexity Chat Completions sunsets 2026-09-27 | [perplexity_provider.py:77,127,198](../../nvh/providers/perplexity_provider.py) call `litellm.acompletion` (the chat-completions surface); Perplexity docs: "Sonar will be supported until September 27, 2026" | Hotfix pins `sonar-pro`/`sonar`; the litellm Agent-API route check lands inside the 0.42 window (see [Update 3](#3-perplexity-chat-completions-sunsets-2026-09-27)) |

## Simplify (0.42)

Thirteen deletions and collapses, none of which removes a feature a user can
reach today. Net target: ≈ −15k LOC of code/scripts, −3.5k doc lines,
−3.2 MB media. Items 9–12 have 0.43 tails where they turn into the single
tables the Update track depends on.

### 1. Delete the seven zero-importer orchestration modules, `/v1/locks`, and the `hooks` field

**Evidence.** Import audit: [autonomous.py](../../nvh/core/autonomous.py) (305
LOC), [rollback.py](../../nvh/core/rollback.py) (100), [hooks.py](../../nvh/core/hooks.py)
(117), [agent_report.py](../../nvh/core/agent_report.py) (103) have 0 importers
in `nvh/`; [agent_pr.py](../../nvh/core/agent_pr.py) (139) is imported only by
`autonomous.py`; [parallel_pipeline.py](../../nvh/core/parallel_pipeline.py)
(501) only by the importlib probe at [smoke_test.py:453](../../nvh/core/smoke_test.py);
[file_lock.py](../../nvh/core/file_lock.py) (455) only by the `/v1/locks`
routes at [server.py:4481-4520](../../nvh/api/server.py), which `web/` never
calls. [settings.py:186](../../nvh/config/settings.py) `hooks: list[dict]` is
read by `nvh doctor` ([main.py:9343](../../nvh/cli/main.py)) but no code
executes a hook. Dedicated tests: `tests/test_autonomous.py`,
`test_parallel_pipeline.py`, `test_rollback.py`, `test_file_lock.py` (~657
LOC). Contrary to the duplication lens, `iterative_loop.py`,
`recursive_agents.py`, `agent_protocol.py` and `agent_matching.py` are live
(the `--iterative` path in `cli/main.py`) and stay.

**Action.** `git rm nvh/core/{autonomous,agent_pr,parallel_pipeline,rollback,hooks,agent_report,file_lock}.py`
and the four test files; delete `server.py:4477-4520` + `ConflictCheckRequest`;
remove `hooks` from `CouncilConfig` and the doctor line; drop
`smoke_test.py:453`; also delete `agents.generate_agents_with_llm` and
`advisor_profiles.get_best_advisor_for_task` (0 callers each).

Effort S · Impact H · LOC removed ~2,400

### 2. Retire the Docker/compose family, the three stale installers, and the demo assets

**Evidence.** [README.md:7,49](../../README.md) says "No Docker" twice; no
workflow in `.github/workflows` builds the Dockerfile or runs `scripts/*.sh`.
Yet the repo ships [Dockerfile](../../Dockerfile) (91 lines, header "Council
API"), [web/Dockerfile](../../web/Dockerfile) (90, "Next.js 14" vs
`web/package.json` next 16.2.12), [docker-compose.yaml](../../docker-compose.yaml)
(230, services `hive-api`/`hive-web`), [docker-compose.cloud.yaml](../../docker-compose.cloud.yaml)
(241, mounts `./nginx` and `./caddy` — neither directory exists),
[scripts/setup.sh](../../scripts/setup.sh) (295), `scripts/cloud-setup.sh`
(568, placeholder `your-org/aiproject`), `scripts/portable-setup.sh` (355,
invokes a `council` binary that no longer exists), [scripts/install.sh](../../scripts/install.sh)
(419, a second pipx installer pointing at the unregistered nvhive.dev),
`scripts/ollama-setup.sh` (320). Demo assets `scripts/demo*.sh` +
`demo-setup.tape` + `generate_demo_gif.py` = 793 LOC (PIL is not a
dependency; the `.tape` still writes `~/.hive/config.yaml`).
[GETTING_STARTED.md:14-34](../GETTING_STARTED.md) leads with Docker and names
containers `nvhive-api` that compose does not define.

**Action.** Delete `Dockerfile`, `web/Dockerfile`, both compose files,
`.dockerignore`, `scripts/{setup,cloud-setup,portable-setup,install,ollama-setup}.sh`
and the demo assets (the GIFs are already committed). Fold
`ollama-setup.sh`'s VRAM-tier pull into `nvh models pull --recommended`
([studio_packs.py:1259-1301](../../nvh/integrations/installs/studio_packs.py)
already has `_detect_vram_gb`/`_recommended_model_ids`). Rewrite
GETTING_STARTED so Option A is the README one-liner and Option B is
`pip install nvhive`; rewrite [.env.example](../../.env.example) around
`NVH_HOME`. Keep [scripts/migrate.sh](../../scripts/migrate.sh) only after
dropping its nvhive.dev URLs.

Effort S · Impact H · LOC removed ~3,400 code + ~300 doc lines

### 3. Move the GeForce NOW Operator/PhantomInput toolkit out; fix-or-delete the two orphan sub-packages

**Evidence.** `tools/` = `cdp_session.py` 321, `gfn_input_bridge.py` 485,
`gfn_session.py` 298, `operator_chrome.py` 233,
[operator_mcp_server.py](../../tools/operator_mcp_server.py) 250,
`phantominput_host.py` 223, `operator_install.sh` 137,
[launch_bridge.command](../../tools/launch_bridge.command) 3, plus
`tools/phantominput-extension/` (665 LOC) with a committed extension key ID.
Zero imports from `nvh`; hardcoded personal paths at
`launch_bridge.command:2-3` and `operator_mcp_server.py:27`
(`/Users/ccooper/nvh/...`); the manifest requests `debugger` on
play.geforcenow.com; [operator-vision.md](../operator-vision.md) proposes a
SaaS/enterprise tier the PolyForm-NC license forbids. The product leaks it
via [vault.py:555-563](../../nvh/integrations/workspace/vault.py).
[vscode-nvhive/src/extension.ts:48](../../vscode-nvhive/src/extension.ts)
fetches `/health` but `server.py` only defines `/v1/health`, so the status
bar always reads "unreachable"; `nvhive.autoStart` is declared but never
read; its `package.json` says MIT. [channel-plugin/](../../channel-plugin)
(594 LOC TS, 1 commit, Bun-only, no lockfile/CI) duplicates
[mcp_server.py](../../nvh/mcp_server.py)'s seven tools and
[CLAUDE_CODE_INTEGRATION.md:121](../CLAUDE_CODE_INTEGRATION.md) still calls
it "Coming Soon".

**Action.** `git mv` the six `tools/*.py` files, `operator_install.sh`,
`launch_bridge.command`, `phantominput-extension/`, `docs/operator-vision.md`
and [docs/phantominput-roadmap.md](../phantominput-roadmap.md) to a separate
private repo; keep `tools/integration-test-install.sh` (rename the dir to
`ci/`). Remove the Operator paragraph from `vault.py`. vscode-nvhive: one PR —
`/health` → `/v1/health`, delete `autoStart` + the unsupported `intent`,
license → PolyForm-NC, add `npm run compile` to `ci.yml` — or delete it.
channel-plugin: delete and point Claude Code users at the `nvhive-mcp` entry
point ([pyproject.toml:130](../../pyproject.toml)), which already exposes the
same tools.

Effort S · Impact H · LOC removed ~3,000 (tools+docs) + ~620 (channel-plugin)

### 4. Delete the `~/.hive`-era core modules that have `NVH_HOME` successors — `nvh snapshot` first

**Evidence.** [core/snapshot.py](../../nvh/core/snapshot.py) (95 LOC) tars
`.hive/config.yaml`, `.nvhive/agent-memory.json`, `.council/council.db`
(lines 13-19) and is what `nvh snapshot save|restore` imports
([main.py:11875](../../nvh/cli/main.py)), while the API/web/Wizard use
[integrations/workspace/snapshot.py](../../nvh/integrations/workspace/snapshot.py)
([server.py:2339,2371,2392](../../nvh/api/server.py) — bundles the
`NVH_HOME` vault, RAG index, receipts, pinned conversations).
[README.md:44](../../README.md) sells this command as the reconnect-survival
story — the CLI snapshots the wrong tree. Same pattern:
[core/knowledge.py](../../nvh/core/knowledge.py) (225, JSON keyword search
under `~/.hive/knowledge`, CLI-only) vs `integrations/rag` (887,
SQLite+embeddings, Wizard/API); [core/memory.py](../../nvh/core/memory.py)
(172, `Path.home()/.hive/memory` at :48; REPL-only) vs the vault's
`append_vault_memory` ([vault.py:980](../../nvh/integrations/workspace/vault.py));
[core/scheduler.py](../../nvh/core/scheduler.py) (106,
`~/.hive/schedules.json` at :37, dies with the VM) vs
[services/jobs.py](../../nvh/integrations/services/jobs.py) (383, durable);
[core/smoke_test.py](../../nvh/core/smoke_test.py) (700) vs
[diagnostics/smoke_tests.py](../../nvh/integrations/diagnostics/smoke_tests.py)
(163); [core/templates.py](../../nvh/core/templates.py) (310,
`~/.council/templates`); [core/docker_sandbox.py](../../nvh/core/docker_sandbox.py)
(199, mounts the working dir read-write, 60s, no fail-closed) vs
[sandbox/executor.py](../../nvh/sandbox/executor.py) (281, `--read-only`,
`require_docker`) — both reachable from the same `shell` tool
([tools.py:238-243](../../nvh/core/tools.py)).

**Action.** Hotfix: point `nvh snapshot save|restore` at
`export_snapshot`/`import_snapshot`. 0.42: delete `knowledge.py` (→ `nvh rag`
over `integrations/rag` with `knowledge` as a hidden alias, one-shot
re-ingest in `nvh doctor`), `memory.py` (REPL `/remember` →
`append_vault_memory`, `/memories` → `ask_vault`), `scheduler.py` (→
`jobs.py` with a `recurrence` field), `smoke_test.py` (→ `nvh smoke --imports`
in diagnostics), `templates.py` (→ `prompt_template` on the AgentProfile
YAML), `docker_sandbox.py` (→ `SandboxConfig.mount_dir` +
`SandboxExecutor.run_shell`; move `nvh/sandbox/executor.py` to
`nvh/core/sandbox.py` with a re-export shim). Keep
[agent_memory.py](../../nvh/core/agent_memory.py) but move its file under
`$NVH_HOME/state/`. Update [COMMANDS.md:34,79](../COMMANDS.md).

Effort M · Impact H · LOC removed ~1,900 net (1,807 core + ~400 tests − ~300 glue)

### 5. Collapse 19 clone provider adapters into one table-driven `OpenAICompatibleProvider` + `ProviderSpec`

**Evidence.** `difflib` across `nvh/providers/`: 19 of 23 `*_provider.py`
files share 86-93% of their lines with
[openai_provider.py](../../nvh/providers/openai_provider.py) (together 91%,
groq 90%, anthropic 93%, nvidia 86%, llm7 79%); 4,432 clone LOC of which
3,917 are shared. Only ollama, triton and mock are bespoke.
[registry.py:81-105](../../nvh/providers/registry.py) already maps names to
`(module, class)` and `LazyProvider` constructs from kwargs, so class names
are not load-bearing. Per-provider facts live in 8 unsynchronised places:
each `__init__` `default_model`, [settings.py](../../nvh/config/settings.py)
`generate_default_config` (24 `default_model:` lines),
[server.py:4970-4988](../../nvh/api/server.py) `_PROVIDER_DEFAULT_CONFIG`,
[cli/setup.py:813-843](../../nvh/cli/setup.py) `advisor_defs`,
[advisor_profiles.py](../../nvh/core/advisor_profiles.py) (908 LOC),
[free_tier.py](../../nvh/core/free_tier.py) (251),
[quota_info.py](../../nvh/providers/quota_info.py) (208),
[proxy.py:53-85](../../nvh/api/proxy.py) `_MODEL_TO_PROVIDER`,
[PROVIDERS.md](../PROVIDERS.md) (still "Gemini 1.5", "Claude 3.5"). That is
why "update supported LLMs" currently means editing 8-9 files per provider
(at least nine — `KNOWN_ADVISORS` at [main.py:629](../../nvh/cli/main.py)
also carries defaults).

**Action.** 0.42: `nvh/providers/openai_compatible.py` with
`ProviderSpec{name, litellm_prefix, default_model, fallback_model, base_url,
env_keys, zero_cost, anonymous_key}` + `PROVIDER_SPECS` (23 rows); delete
the 19 adapter files, leaving 3-line compat shims for one release
([tests/test_providers_parametrized.py](../../tests/test_providers_parametrized.py)
imports by string). 0.43: extend the spec with routing weights, `cost_tier`,
free-tier limits, `model_prefixes`, `signup_url`, `sunset_date`; make
`ADVISOR_PROFILES`/`FREE_TIER_ADVISORS`/`QuotaInfo`/`KNOWN_ADVISORS`/`nvh
keys`/`/v1/setup/free-providers`/web `CLOUD_PROVIDERS`/`docs/PROVIDERS.md`
all derive from it; add `tests/test_provider_docs_parity.py`.

Effort M · Impact H · LOC removed ~3,700 (0.42) + ~1,200 (0.43)

### 6. CLI: fix `nvh mcp`, collapse 8 query-mode clones and 7 diagnostic verbs, derive reserved words, add did-you-mean

**Evidence.** In-process: `typer.main.get_command(app).commands['mcp']` is
the TyperGroup added at [main.py:13723](../../nvh/cli/main.py)
(`list`/`refresh` only, no `invoke_without_command`); the MCP-server command
at `main.py:6386` is unreachable, so [COMMANDS.md:63](../COMMANDS.md) and
[SDK_API.md:56](../SDK_API.md) are wrong. 95 top-level names, 61
`@app.command`, 21 auto-generated provider commands (`main.py:845`), 16
sub-groups. `code`/`write`/`research`/`math`/`quick`/`clip`/`pipe`/`safe`
(~830 LOC, `main.py:1205-3257`) each re-implement
`load_config→Engine→query→print`, differing only in a system prompt and a
preference list. `health`/`status`/`doctor`/`selfcheck`/`debug`/`test`/`services
status` (~1,700 LOC) overlap; only `status` has meaningful tests (65 refs vs
0-1). Dispatcher (`main.py:13514-13580`): unknown first word → prompt →
`_is_task_input()` → `do <prompt>` with `--auto` defaulting True
(`main.py:11143-11146`) → a metered LLM call or an auto-approved agent run;
`get_close_matches|did you mean` = 0 hits. `main.py` is 14,213 LOC.

**Action.** Hotfix: `mcp_app` gets `invoke_without_command=True`, starting
the server when no subcommand is given; external-server verbs move to `nvh
mcp servers list|refresh` (old names hidden aliases); registry-collision test
over `registered_commands` + `registered_groups`. 0.42: `ask --focus
code|write|math|research`, `--fast`, `--local`, `--clipboard`, stdin-aware
(kills `pipe`); `nvh status` with `--providers/--deep/--report/--smoke/--routing`
tiers over one checks registry shared with `/v1/setup/diagnostics`; the 21
provider commands become hidden aliases of `ask -p`; `known_commands` from
the Typer registry; a `difflib.get_close_matches` guard before treating argv
as a prompt; require an explicit `nvh do` for task-shaped bare prompts.
Regenerate `docs/COMMANDS.md` from `get_command(app)` in CI. The full
12-verb tree from the UX lens is a later release.

Effort M · Impact H · LOC removed ~2,200

### 7. Web: delete `/query` and `/council`, trim Preferences, stop persisting chats to localStorage

**Evidence.** Four chat surfaces: [web/app/page.tsx](../../web/app/page.tsx)
1,450 LOC (localStorage `council_chats_v2` at :39; imports only
`getConversation`/`getConversations` — never `createConversation`/`sendMessage`,
though [web/lib/api.ts](../../web/lib/api.ts) exports them),
[web/app/query/page.tsx](../../web/app/query/page.tsx) 555 (same api fns,
`council_recent_queries`), [web/app/council/page.tsx](../../web/app/council/page.tsx)
849 (reached only via a WizardChat deep link), `web/app/wizard` 41 +
[WizardChat.tsx](../../web/components/WizardChat.tsx) 1,498
(server-persisted). [web/app/settings/page.tsx](../../web/app/settings/page.tsx)
writes 14 settings to `council_settings` which no other file reads; real
budget limits live in `config.yaml`. The server already has
`/v1/conversations` CRUD + FTS search ([server.py:4611-4767](../../nvh/api/server.py)),
so ROADMAP's "message search — missing" is actually
unreachable-not-missing. CommandPalette's "Throwdown" mode is mapped
silently to council.

**Action.** 0.42: delete `web/app/query` and `web/app/council` (council
knobs become an "Advanced" drawer on the remaining council mode); cut
`settings/page.tsx` to Theme + Cache + Data; remove `council_recent_queries`;
on first load import `council_chats_v2` into `/v1/conversations` then delete
the key; delete [web/lib/localChats.ts](../../web/lib/localChats.ts) and the
[LayoutShell](../../web/components/LayoutShell.tsx) merge (48-107). Wire the
sidebar search box to `/v1/conversations/search`. The final merge of `/` into
`/wizard` lands in 0.44 together with vision ([Add 4](#4-one-chat-surface)).

Effort M · Impact H · LOC removed ~1,850

### 8. Docs: 33 files → ~12; delete the `council`-binary testing guide; fix CONTRIBUTING; generate every marketing number

**Evidence.** `docs/*.md` = 33 files / 6,184 lines; 24 have zero inbound
links. [TESTING_GUIDE.md](../TESTING_GUIDE.md) (1,725 lines) has 89
`council ` invocations for a binary and `compare`/`query` commands that do
not exist, a 125-line Docker section, and "Python 3.12 minimum" vs pyproject
`>=3.11`. [GETTING_STARTED.md](../GETTING_STARTED.md) (771) is Docker-first.
[CONTRIBUTING.md](../../CONTRIBUTING.md): registration in
`nvh/providers/__init__.py` (actual: `registry.py`), tests under
`tests/providers/` (does not exist), `docs/plugins.md` (does not exist),
"mypy --strict" (CI gates only `nvh/sandbox`, `nvh/catalog`). Marketing
numbers: "23 providers" in 16 files vs 21 real (registry has 23 incl.
mock+triton), "63 models" vs 73/70 non-mock, "25 free" vs 14
`FREE_TIER_ADVISORS`, "12 cabinets" vs 13 `COUNCIL_PRESETS`;
[TOOLS.md](../TOOLS.md) lists `open_app`/`open_url`/`npm_install` which do
not exist. [future-ideas.md](../future-ideas.md) lists ≥10 already-shipped
items; [ROADMAP.md](../ROADMAP.md) duplicates MCP/image-gen/code-exec/memory/voice
rows across competitor sections and says "23-persona"
([agents.py](../../nvh/core/agents.py) has 23 in `_PERSONA_POOL` but 38
`PersonaTemplate` rows).

**Action.** Target set: README, CHANGELOG, GETTING_STARTED (no Docker;
absorbs LINUX_DESKTOP/DEPLOY_NO_ROOT/STUDENTS/HARDWARE), MODELS (absorbs
GPU_DETECTION/GPU_TIER_MATRIX/ORCHESTRATION), PROVIDERS (generated), COMMANDS
(generated; absorbs COUNCIL/TOOLS/WORKFLOWS/CONFIGURATION — write the missing
env/layout tables), WEBUI, INTEGRATIONS
(MCP+SDK_API+CLAUDE_CODE_INTEGRATION+NEMOCLAW+OPENCLAW_MIGRATION),
ARCHITECTURE, MAINTAINERS, TESTING (~150 lines), ROADMAP (absorbs
future-ideas and proposals/; one table keyed by feature, plus a Non-goals
section). Delete [NVIDIA_DEVELOPER_BRIEF.md](../NVIDIA_DEVELOPER_BRIEF.md).
Add `tests/test_marketing_parity.py` (same shape as the version-parity test)
asserting the counts in README/docs/`mcp_server.py`/`main.py` equal
`len(registry−mock−triton)`, `len(catalog−mock)`, `len(FREE_TIER_ADVISORS)`,
`len(COUNCIL_PRESETS)`, `len(_PERSONA_POOL)`, `len(ToolRegistry)`; remove the
numbers from MCP tool descriptions. Add a lychee link-check step to
[ci.yml](../../.github/workflows/ci.yml). Delete the 6 unreferenced
screenshots + `docs/screenshots/frames/` (3.2 MB).

Effort M · Impact H · LOC removed ~3,500 doc lines + 3.2 MB media

### 9. `NVH_HOME` as the only root: one path oracle, one env helper, one secrets module

**Evidence.** Five homes in play: `~/.hive` via `HIVE_CONFIG_HOME`
([settings.py:202](../../nvh/config/settings.py); 44 hard-coded `.hive` refs
across 13 files incl. `main.py` `last_query.json`/`user.json`,
[plugins/manager.py:76](../../nvh/plugins/manager.py),
[workflows.py:103](../../nvh/core/workflows.py),
[context_files.py:92](../../nvh/core/context_files.py)); `NVH_HOME` →
`~/.nvh` ([storage.py:117-127](../../nvh/integrations/workspace/storage.py));
`~/nvh` no-dot ([main.py:7396 and :9318](../../nvh/cli/main.py));
`~/.council` ([templates.py:15](../../nvh/core/templates.py),
[mock_provider.py:63](../../nvh/providers/mock_provider.py) text);
`HIVE_DATA_DIR` ([repository.py:40](../../nvh/storage/repository.py)). Fresh
installs agree only because `install.sh` exports
`HIVE_CONFIG_HOME=$NVH_HOME/config`; `python -m nvh` without sourcing
`nvh-env.sh` splits config from state. 20 `HIVE_*`/`COUNCIL_*` env names
still read; env-booleans hand-parsed at 10 sites with 5 truthy sets
(`NVH_SANDBOX=on` fails, `NVH_RAG_AUTO_PULL=off` is truthy). Secrets: keyring
written at 7 sites (`main.py:831,3530,3583,3853,4386`; `setup.py:105`;
`server.py:5396`) plus `.env`, `config.yaml`, `os.environ`, localStorage,
`user.json` — but the engine reads keyring only if `NVH_USE_KEYRING` is
truthy, default `'0'` ([engine.py:444](../../nvh/core/engine.py),
[registry.py:127](../../nvh/providers/registry.py)); `nvh advisor remove`
deletes only the keyring copy (`main.py:4396-4404`) so the provider stays
enabled. [.env.example](../../.env.example) documents
`HIVE_DAILY_LIMIT`/`HIVE_MONTHLY_LIMIT`, which nothing reads.

**Action.** `settings.DEFAULT_CONFIG_DIR = storage_layout().config_dir`;
replace every `Path.home()/'.hive'/X` with `storage_layout()` paths
(mechanical, 44 edits); delete the `~/nvh` fallbacks and `HIVE_DATA_DIR`;
`nvh/utils/env.py` with `env_bool`/`env_str`/`env_path` + `aliases=` for
`HIVE_*`/`COUNCIL_*` legacy names warning once; `nvh/config/secrets.py`
(get/set/delete/list over `$NVH_HOME/config/secrets.env` 0600, keyring as an
opt-in mirror) used by the CLI, `/v1/setup/save-key` and a new `DELETE
/v1/providers/{id}/key`; `config.yaml` keeps only `${VAR}` refs; canonical
YAML key `providers:` with `advisors:` accepted for two releases; FastAPI
title "nvHive API", accept `X-NVH-API-Key` alongside `X-Hive-API-Key`; `nvh
doctor` migrates `~/.hive/config.yaml` and `~/.council/council.db` once.
Generate the env-var table in docs from one `ENV_VARS` list.

Effort M · Impact H · LOC removed ~400 net (hand-rolled parsers, duplicate key writers, dead env docs)

### 10. One `Tool` dataclass, one `ToolRegistry`, one text protocol, native function calling via LiteLLM

**Evidence.** [core/tools.py](../../nvh/core/tools.py) `ToolRegistry`
(`Tool.safe` bool, `handler(**args)->str`, fenced ```` ```tool_call ````
blocks parsed by regex at [agent_loop.py:184-208](../../nvh/core/agent_loop.py);
prompt duplicated at [agentic.py:296-300](../../nvh/core/agentic.py)) vs
[wizard/tools.py](../../nvh/integrations/wizard/tools.py)
`WizardToolRegistry` (`safety_class` auto|confirm|never,
`handler(dict)->dict`, `TOOL_CALL: {json}` lines parsed at
[wizard/chat.py:140-165](../../nvh/integrations/wizard/chat.py) with ~50
lines of stream filtering at 547-599). MCP tools register only into the
Wizard registry ([mcp_client.py:319-366](../../nvh/integrations/mcp_client.py)),
so the shipped "MCP client" is Wizard-only. The Wizard registry has exactly 9
tools (`diagnose`, `rag_ask`, `rag_ask_vault`, `rag_ingest`, `refresh_models`,
`repair_workspace`, `save_provider_key`, `validate_provider_key`,
`web_search`) — none execute. `web_search` is implemented twice with
incompatible env names (`tools.py:337-379` `BRAVE_SEARCH_KEY`/`SEARXNG_URL`
defaulting to the public searx.be vs
[web_search/client.py:18](../../nvh/integrations/web_search/client.py)
`NVH_SEARXNG_URL`/`BRAVE_API_KEY`). `Provider.complete`/`stream` forward
`**kwargs` to `litellm.acompletion` but no call site passes `tools=`;
`CompletionResponse` has no `tool_calls` field;
[capabilities.yaml](../../nvh/config/capabilities.yaml) already carries
`supports_tools`. A third plugin surface
([plugins/manager.py](../../nvh/plugins/manager.py), entry-point group
`nvhive.plugins`) is undeclared in pyproject.

**Action.** One `Tool(name, description, parameters JSON-schema, handler,
risk: auto|confirm|deny)` and one registry in `nvh/core/tools.py` with the
Wizard's three discovery sources (entry points, `$NVH_HOME/plugins`, MCP
cache); `WizardToolRegistry = ToolRegistry` alias for one release. Add
`tools=`/`tool_choice` to `Provider.complete`/`stream` and `tool_calls` to
`CompletionResponse`/`StreamChunk` via the `OpenAICompatibleProvider` base
and Ollama `/api/chat`; resolve capability per model from the catalog; keep
exactly one text-marker fallback (the `TOOL_CALL:` line form, which works on
small local models) with the fenced form accepted for one release. Delete
the `tools.py:323-409` `web_search` body in favour of a wrapper over
`integrations/web_search` (port the Google CSE branch; accept both env
spellings once; drop the searx.be default). Merge `plugins/manager.py`
discovery into the same code and declare the entry-point group.

Effort L · Impact H · LOC removed ~700 net (second registry, second parser, stream filter, duplicate web_search, plugin manager)

### 11. Council: one streaming pipeline; Engine wraps it once; CLI/REST/WS become behaviourally identical

**Evidence.** [council.py](../../nvh/core/council.py): `run_council`
(292-489, 198 lines) and `run_council_streaming` (490-923, 434 lines) copy
member resolution verbatim (317-354 vs 520-554); streaming re-inlines 44 of
`_weighted_synthesis`'s 74 lines including both prompt variants (707-760 vs
1015-1088). Drift already shipped: only non-streaming accepts multi-turn
`messages` (so the WebSocket council at [server.py:3468](../../nvh/api/server.py)
is single-turn), only non-streaming staggers same-provider free-tier members
by 2s (362-411), only streaming has budget-check + 3-attempt synth rotation
(689-704, 766-869). `server.py:3402-3500` re-does Engine's budget/log wrapper
by hand. `_majority_vote` (988-1013) says "For MVP: return the response from
the highest-weighted member" (line 997) yet is advertised in
[main.py:1680](../../nvh/cli/main.py), [settings.py:89](../../nvh/config/settings.py),
[mcp_server.py:169,348](../../nvh/mcp_server.py), [sdk.py:121](../../nvh/sdk.py)
and the vault template.

**Action.** Make `run_council_streaming` the implementation; `run_council` =
streaming with a no-op `on_event`; add `messages` to the streaming path; move
the 2s stagger into `_stream_member`; extract `_build_synthesis_prompt()`
used by both; keep budget-check + rotation as the only synthesis path. Add
`Engine.run_council_streaming(on_event)` and make `ws_council` call it.
Either implement `majority_vote` (normalised-similarity clustering, ~40
lines) or remove it from the five advertised surfaces. Also finish the two
other user-visible stubs: [agentic.py:683](../../nvh/core/agentic.py)
hardcodes `total_cost_usd=Decimal('0')`, printed as "Cost: $0.0000" by `nvh
agent`; `agentic.py:482-485` builds a 28-line `CODING_SYSTEM_PROMPT` and
discards it because `run_agent_loop` lacks a `system_prompt` param.

Effort M · Impact H · LOC removed ~350 (council + server wrapper)

### 12. One agent concept (`AgentProfile`) and one local-model preference table

**Evidence.** Four unrelated "agent" types feed two web pickers:
[agents.py](../../nvh/core/agents.py) `AgentPersona`/`PersonaTemplate` (599
LOC, 38 templates, 13 `COUNCIL_PRESETS`) → council presets picker;
[wizard/profiles.py](../../nvh/integrations/wizard/profiles.py) `AgentProfile`
(13 fields) + [agent-library.json](../../nvh/catalog/agent-library.json) (100
profiles) → `AgentProfilePicker`; [advisor_profiles.py](../../nvh/core/advisor_profiles.py)
`AdvisorProfile` is a per-vendor record that shares the word "profile" and
the `nvh advisor` group. Council members cannot use a library profile; the
Wizard cannot use a council persona. Routing exists four times
([router.py](../../nvh/core/router.py) `Router.route` — the one Engine uses;
[orchestrator.py](../../nvh/core/orchestrator.py) `smart_route`;
`agent_matching.match_agents_to_providers`;
`advisor_profiles.get_best_advisor_for_task` with 0 callers). "Which local
model should we prefer" tables exist four times:
[ollama_provider.py:27-69](../../nvh/providers/ollama_provider.py),
[local_chat.py:21-38](../../nvh/integrations/local_chat.py),
[cli/setup.py:627](../../nvh/cli/setup.py),
[workstation.py:71](../../nvh/integrations/installs/workstation.py) +
[studio_packs.py:1301](../../nvh/integrations/installs/studio_packs.py) +
[diagnostics/model_fit.py](../../nvh/integrations/diagnostics/model_fit.py);
the Ollama endpoint is resolved at 14 sites under 3 env names with 65
`:11434` literals.

**Action.** `AgentProfile` becomes the only agent type (+ optional
`triggers`, `council_weight_boost`); move the 23 pool personas + 15
preset-only personas into `agent-library.json` under category "Council" with
a `presets:` map; `agents.generate_agents`/`get_preset_agents` become thin
rankers over `list_profiles()` (function names kept so
council.py/server.py/cli/repl/iterative_loop keep importing). Rename `nvh
advisor` → `nvh providers` (hidden alias). Collapse the two web pickers into
`AgentProfilePicker` with a Council filter. Trim `orchestrator.py` to
prompt-optimisation + compression (its unique features) and have `router.py`
call it for the LLM-assisted tier. Add `nvh/utils/ollama.py::ollama_base_url()`
(`NVH_OLLAMA_URL` → `OLLAMA_BASE_URL` → `OLLAMA_HOST` → layout default),
`ollama_reachable()`, `installed_models()`; one `LOCAL_MODEL_TIERS` keyed by
VRAM tier × use-case consumed by setup.py, workstation.py, studio_packs.py,
local_chat.py, ollama_provider.py, gpu.py and the docs GPU table.

Effort M · Impact H · LOC removed ~1,200 (agents.py literals, orchestrator duplicates, three preference tables, endpoint copies)

### 13. Repo hygiene: dependency truth, test-file layout, package layout

**Evidence.** [pyproject.toml:38](../../pyproject.toml) `pydantic-settings>=2.0`
— 0 uses in `nvh/`; `packaging` imported at
[setup_agent.py:353](../../nvh/integrations/wizard/setup_agent.py) but
undeclared; `IPython` imported by [jupyter.py:22,53,83](../../nvh/jupyter.py)
with no extra; `dev` and `all` extras hand-copy the other extras;
`[tool.mypy] strict = true` while `ci.yml` gates only `nvh/sandbox` and
`nvh/catalog`. `tests/`: 24 coverage-campaign files
(`test_coverage_80_batch1-7`, `test_coverage_90_batch8-9`,
`test_final_push_a..l`, `test_coverage_boost`, `test_remaining_coverage`,
`test_coverage_deep`) = 8,711 LOC / 672 tests named for a coverage target,
not a subject; `rate_limiter` and `webhooks` have no dedicated test file but
appear in 6 and 4 campaign files. Single-file packages `nvh/prompts`,
`nvh/catalog`, `nvh/sandbox`, `nvh/workflows` (no `__init__`) each exist to
hold one data file; [integrations/catalog.py](../../nvh/integrations/catalog.py)
collides with the `nvh/catalog` package name.

**Action.** Remove `pydantic-settings`; add `packaging>=23`; add `jupyter =
['ipython>=8']` extra (or delete the undocumented 101-LOC `jupyter.py`);
self-referential extras (`all = ['nvhive[serve,nvidia,mcp,vision,browser,rag]']`);
drop the unenforced `strict = true` or grow the gated list. Fold campaign
tests into subject files (`test_rate_limiter.py`, `test_webhooks.py`,
`test_smoke_test.py`, merge `test_tools`/`test_tools_deep` etc.) preserving
`pytest --co -q | wc -l` (2,007) as the acceptance check; delete tests for
deleted modules first. Create `nvh/data/` (`read_text`/`read_json`/`read_yaml`
via `importlib.resources`) holding prompts, catalog JSON, workflows YAML,
`capabilities.yaml`; rename `integrations/catalog.py` → `setup_catalog.py`.
Extend the version-parity test to `web/`, `vscode-nvhive/`, `channel-plugin`
`package.json` or set them to `0.0.0-workspace`.

Effort M · Impact M · LOC removed ~400 net (tests consolidate, not shrink; layout is churn, not deletion)

## Update supported LLMs (0.43)

Items 1–3 are hotfix-sized and ship in 0.41.1; the rest need the
`ProviderSpec` table from [Simplify 5](#5-collapse-19-clone-provider-adapters-into-one-table-driven-openaicompatibleprovider--providerspec)
and land in 0.43.

### 1. HOTFIX: swap the retired default/fallback model IDs in all four copies

**Evidence.** Shipped defaults: google `gemini/gemini-2.0-flash` (both
default and fallback); groq `groq/llama-3.3-70b-versatile` +
`llama-3.1-8b-instant` (Groq deprecations page: both deprecated 2026-08-16,
replacements `openai/gpt-oss-120b` / `openai/gpt-oss-20b`); grok
`xai/grok-2`; deepseek `deepseek/deepseek-chat`; perplexity
`perplexity/llama-3.1-sonar-large-128k-online` (Perplexity docs list only
`sonar`/`sonar-pro`/`sonar-reasoning-pro`/`sonar-deep-research`; no
`llama-3.1-sonar-*` anywhere); together/fireworks/openrouter/sambanova
Llama-3.1-70B; cerebras `cerebras/llama3.1-70b`; ai21 `jamba-1.5-large`;
cohere `command-r-plus`; huggingface `Meta-Llama-3-8B-Instruct`; nvidia
`meta/llama-3.1-70b-instruct`; openai `gpt-4o`; anthropic
`claude-sonnet-4-6`; llm7 `deepseek-r1-0528` — and
[settings.py:475](../../nvh/config/settings.py) makes llm7 the only `enabled:
true` cloud provider by default. The same IDs are copied in `settings.py`
`generate_default_config` (24 `default_model` lines),
[server.py](../../nvh/api/server.py) `_PROVIDER_DEFAULT_CONFIG`,
[cli/setup.py](../../nvh/cli/setup.py) `advisor_defs`,
[main.py:1484](../../nvh/cli/main.py) `o3-mini` / `:1486` `deepseek-reasoner`,
and web copy ([setup/page.tsx:148-152](../../web/app/setup/page.tsx) "GPT-4o",
"Grok 2"). Anthropic lineup per the API reference: `claude-fable-5-1`,
`claude-opus-5`, `claude-sonnet-5`, `claude-sonnet-4-6`, `claude-haiku-4-5`
(no date suffix; 200K, $1/$5); `claude-opus-4-6` is $5/$25, so
[capabilities.yaml](../../nvh/config/capabilities.yaml)'s 15/75 is 3x over
and the router's cost score penalises it wrongly.

**Action.** One PR touching the four copies (the `ProviderSpec` table does
not exist yet). Pin only IDs verified against the provider today: groq
`openai/gpt-oss-120b` / `openai/gpt-oss-20b`; perplexity
`perplexity/sonar-pro` / `perplexity/sonar`; anthropic `claude-sonnet-5`
default (the cost-aware router default for a metered user; opus-5 and
fable-5-1 in the catalog) / `claude-haiku-4-5` fallback. For openai, google,
xai, deepseek, mistral, cohere, together, fireworks, openrouter, cerebras,
sambanova, ai21, nvidia, siliconflow, llm7: the candidate IDs are
**unverified** — call each provider's `GET /models` (llm7's is keyless) at
implementation time and pin what it returns; drop huggingface if no
Inference-Providers route resolves. Ship `nvh config migrate`, which rewrites
known-dead IDs in the user's `config.yaml` via a renames map, and make `nvh
doctor` warn when a configured default is not in the catalog.

Effort S · Impact H

### 2. HOTFIX: remove the GitHub Models provider

**Evidence.** github.blog changelog: "the playground, model catalog,
inference API, and BYOK endpoints will no longer be available" after July
30, 2026, for all customers. In-repo: 23 files reference GitHub
Models/`github_provider`/`models.inference.ai.azure.com`;
[engine.py:433](../../nvh/core/engine.py) auto-enables it whenever
`GITHUB_TOKEN` is set (common on dev boxes);
[free_tier.py:51-56](../../nvh/core/free_tier.py) gives it `priority=2`,
i.e. chosen before groq/google;
[advisor_profiles.py:665-696](../../nvh/core/advisor_profiles.py) markets
"frontier models completely free"; `capabilities.yaml:1738-1820` has three
entries; `docs/PROVIDERS.md`, `GETTING_STARTED.md`, `HARDWARE.md` still list
it.

**Action.** Delete [github_provider.py](../../nvh/providers/github_provider.py)
and all 23 references (`registry.py`, `engine.py:433`, `free_tier.py`,
`advisor_profiles.py`, `quota_info.py`, `settings.py`, `server.py`,
`cli/main.py:726,3316,7306`, docs); `nvh config migrate` prints "github
provider retired 2026-07-30 — removed from config". Provider count becomes
20 real (+ triton). Fill the zero-cost slot with OpenRouter `:free` models,
which litellm already prices.

Effort S · Impact H

### 3. Perplexity Chat Completions sunsets 2026-09-27

**Evidence.** docs.perplexity.ai/getting-started/models: "Sonar Chat
Completions is now Agent API. Sonar will be supported until September 27,
2026." [perplexity_provider.py](../../nvh/providers/perplexity_provider.py)
calls `litellm.acompletion` (the chat-completions surface) at lines 77, 127,
198; [advisor_profiles.py:90,127](../../nvh/core/advisor_profiles.py)
positions Perplexity as the web-search advisor. This is the second
provider-level EOL this year after GitHub Models, and nothing in the code
can express it. 26 days out at audit time; an ID swap alone is not enough.

**Action.** The hotfix pins `sonar-pro`/`sonar`. Before 09-27 (inside the
0.42 window): verify litellm's Perplexity route follows the Agent API on the
pinned litellm version, or route web-grounded queries to the
`integrations/web_search` backends + any model. Add `sunset_date` to
`ProviderSpec` so `nvh doctor`, `/v1/providers` and the Integrations page
warn ahead of provider EOLs.

Effort S · Impact M

### 4. Derive price, context, capability flags and deprecation from litellm at load

**Evidence.** [pyproject.toml:36](../../pyproject.toml) `litellm>=1.55`
(Dec-2024 floor). litellm is used only for `acompletion` and
`completion_cost` ([openai_provider.py:108-118](../../nvh/providers/openai_provider.py));
nothing calls `get_model_info`/`model_cost`/`supports_function_calling`/`deprecation_date`.
`_calc_cost` catches every exception → `Decimal('0')`, so retired or lagging
IDs are billed at $0 and `budget.hard_stop` cannot trigger.
[capabilities.yaml](../../nvh/config/capabilities.yaml) (header "Last
updated: 2026-03-31", 73 entries) hand-types pricing that is already wrong
(opus-4-6 15/75 vs $5/$25; mistral-large, gemini-2.5-flash, deepseek-chat
per the lens). [router.py:834-848](../../nvh/core/router.py) scores
cost/latency straight from those fields.

**Action.** Bump the floor to a current litellm and add a weekly dependency
bump. In `registry.load_capabilities()`, fill input/output cost, max tokens,
`supports_vision`/`function_calling` and a new `deprecation_date` from
`litellm.get_model_info()`, with YAML values acting only as overrides;
shrink `capabilities.yaml` to
provider/display_name/capability_scores/typical_latency_ms. Ship
`nvh/config/model_overrides.json` in litellm's schema fed to
`litellm.register_model()` for gap IDs (Ollama sizes/ctx, NIM, OpenRouter
`:free`, any ID litellm lags on — check `claude-fable-5-1`), and let the
remote `nvhive-catalog.json` carry the same block (`SCHEMA_VERSION` 2) so
pricing fixes ship between releases. `_calc_cost` logs once per model and
the chat cost pill shows "cost unknown" instead of $0. Hide entries with a
past `deprecation_date` in `/v1/models`, `nvh models`, `nvh doctor`.

Effort M · Impact H

### 5. Make the `/v1/models` "live intersect" real; add a CI model-currency test

**Evidence.** [server.py:2933-3001](../../nvh/api/server.py) comments that
it intersects the catalog against each provider's `list_models()` precisely
because the YAML drifts — but `list_models()` is static in every cloud
adapter (returns `[default, fallback]` built from constructor constants,
e.g. [groq_provider.py:199-202](../../nvh/providers/groq_provider.py),
anthropic:180-183, google:165-166), so the intersect collapses each provider
to one (dead) ID and never filters anything. Only ollama (`/api/tags`) and
mock are live. Groq, xAI, DeepSeek, Mistral, Together, Fireworks,
OpenRouter, Cerebras, SambaNova, SiliconFlow, NVIDIA, Perplexity, OpenAI,
llm7 (keyless) all expose `GET /v1/models`; Anthropic has `GET /v1/models`;
Google has `ListModels`.

**Action.** One `openai_compatible_list_models(base_url, api_key)` in
`OpenAICompatibleProvider` (httpx, 5s timeout, reuse the 5-min cache at
`server.py:2939-2972`); Anthropic/Google list calls; static lists only for
triton/mock. `nvh doctor` reports "default model X not served by provider
Y". `tests/test_model_currency.py` (network-marked, weekly schedule) asserts
every default/fallback and catalog ID is present in `litellm.model_cost`
with no past `deprecation_date` and, where a key is available, in the
provider's live list. Extend [proxy.py:53-85](../../nvh/api/proxy.py)
`_MODEL_TO_PROVIDER` (still gpt-4o/o1/claude-3/gemini-1.5/mixtral) with
current prefixes and fall back to `litellm.get_llm_provider()`.

Effort M · Impact H

### 6. Regenerate the cloud catalog (~55 entries) with each provider's current cheap/fast tier

**Evidence.** Router weights are capability 0.4 / cost 0.3 / latency 0.2
([settings.py:487-492](../../nvh/config/settings.py)) but the catalog has no
current cheap tier for most providers: all 5 Groq entries are dead (mixtral
shut 2025-03-20, gemma2 2025-10-08, specdec 2025-04-14, both llama
2026-08-16 — Groq page), OpenRouter's single entry is a paid Llama 3.1 70B
with no `:free` variants, Google tops out at 2.5, OpenAI has zero
GPT-5-generation rows (only gpt-4o/4.1/o3/o3-mini), Anthropic lacks
opus-5/sonnet-5/fable-5-1. `capabilities.yaml` keys also mismatch what
providers send: `nvidia/meta-llama-3.1-70b-instruct` vs the nvidia_provider
default `meta/llama-3.1-70b-instruct`; `siliconflow/Qwen2.5-7B-Instruct` vs
`Qwen/Qwen2.5-7B-Instruct` — so router picks can never resolve to the
provider's accepted string; NIM has no Nemotron entry despite
`docs/NVIDIA_DEVELOPER_BRIEF` pitching it.

**Action.** Replace the ~50 cloud rows with ~55 current ones using the
model-currency lens's list as candidates, each verified against the
provider's `/models` before commit (verified today: groq
`openai/gpt-oss-120b`, `openai/gpt-oss-20b`; perplexity `sonar`,
`sonar-pro`, `sonar-reasoning-pro`, `sonar-deep-research`; anthropic
`claude-fable-5-1`, `claude-opus-5`, `claude-sonnet-5`, `claude-sonnet-4-6`,
`claude-haiku-4-5`). Hand-curate only `capability_scores`. Normalise every
key to the exact litellm string the adapter sends and add a unit test that
each provider's default/fallback is a catalog key. Add NIM Nemotron 3
entries once the exact IDs are confirmed on build.nvidia.com. Soften
SiliconFlow's "permanently free" copy ([quota_info.py:111](../../nvh/providers/quota_info.py),
`advisor_profiles.py:739,762`, `free_tier.py:104`, `main.py:747`) — the lens
reports real-name verification is now required (**unverified**; verify
before editing copy).

Effort M · Impact H

### 7. Local models: `nemotron3:33b` replaces the phantom tags; one VRAM tier table

**Evidence.** ollama.com/library/nemotron3: "NVIDIA Nemotron 3 Nano Omni …
unifies video, audio, image, and text", tag `nemotron3:33b` (28GB, 128K ctx,
image input, tools). [install.sh](../../install.sh) has 13 references to
`nemotron-omni`/`nemotron-3-nano-omni`, including the comment at :1299-1300
"ollama pull nemotron-omni 404s" and ~100 LOC of
`_nvwizard_hf_gguf_source`/`bootstrap_omni_via_hf` (:1314-1407) that exist
only to work around the wrong tag;
[studio_packs.py](../../nvh/integrations/installs/studio_packs.py) has 6 refs
(two `StudioModel`s for the same phantom), `capabilities.yaml` 2
(byte-identical scores). [gpu.py](../../nvh/utils/gpu.py) `recommend_models()`
is mid-migration — `model='qwen3:8b'` with reason text "Gemma 4 26B"
(:586-590), `model='llama3.2-vision'` reason "Gemma 4 26B" (:620-624),
`model='minicpm-v'` reason "llama3.1:8b" — and
[cli/setup.py:848-856](../../nvh/cli/setup.py) writes these into the user's
`config.yaml`. The Ollama default differs by surface:
[settings.py:355](../../nvh/config/settings.py) `gemma3:4b`,
[web/app/settings/page.tsx:223](../../web/app/settings/page.tsx)
`nemotron-mini`, `install.sh` the `nemotron-omni` chain. Superseded rows
still carried: gemma2, codellama, phi4, qwen2.5, llava:7b, minicpm-v,
moondream (ctx 4096).

**Action.** Tags: `nemotron3:33b` for the 32-40GB+ tier, `nemotron3:33b-q8`
for 48GB+; delete `install.sh:1314-1407` + call sites, the `-omni`
corruption shim (:931, :955), the duplicate yaml row; rename `StudioModel`
ids. Build one `LOCAL_MODEL_TIERS` table (VRAM tier × chat/code/vision/embed)
that `install.sh` (via a generated shell snippet or `nvh models recommend
--json`), gpu.py, cli/setup.py, workstation.py, studio_packs.py,
local_chat.py, ollama_provider.py and [MODELS.md](../MODELS.md) all read;
generate `reason` text from the table so model and reason cannot disagree.
The lens's proposed additions (gemma4, qwen3.5, gpt-oss, qwen3-coder,
nemotron-3-super) are plausible but **unverified** — gate every tag in that
table on the existing `_model_exists_on_registry()` HEAD probe (CHANGELOG
0.31.1) and run that probe in CI so a phantom tag can never ship again.
Retire the gemma2/codellama/phi4/qwen2.5/llava/minicpm-v rows; keep
moondream only as the CPU fallback.

Effort M · Impact H

### 8. Pass `num_ctx` to Ollama, derived from the VRAM tier

**Evidence.** `grep -rn num_ctx nvh install.sh web` = 0 hits.
[ollama_provider.py:505-508 and :597-600](../../nvh/providers/ollama_provider.py)
send only `temperature` + `num_predict`, so the Wizard system prompt + the
100-profile addon ([chat.py:232-234](../../nvh/integrations/wizard/chat.py))
+ RAG chunks run at the daemon's default window and are silently truncated
on every tier. This is the minimal, on-thesis slice of
[ROADMAP.md:52](../ROADMAP.md) "Hardware/inference tuning controls" and
bounds every RAG/profile/memory feature in the Add track.

**Action.** Add `num_ctx` to both Ollama option dicts, read from
`LOCAL_MODEL_TIERS` (e.g. 8k <12GB, 16k 24GB, 32k 40GB+) with a config
override; surface the effective window in `nvh status` and the chat cost
pill so users see why a long paste was cut.

Effort S · Impact H

## Add (0.44)

The Wizard can act — mostly by registering what already exists. Every item
lists what it builds on; none introduces a second implementation of
anything in the Simplify track.

### 1. The Wizard can act: `run_code` and `shell` (confirm-class) plus `ask_user`, rendered as approval cards

**Why.** Every competitor turned its chat into an agent that executes and
asks (the feature-gaps lens cites Open WebUI 0.11.1 tool approvals +
`ask_user`, LibreChat 0.8.8 stateful code interpreter, LM Studio Bionic —
release claims **unverified**, but the gap is verifiable in-repo: the
Wizard registry has 9 tools and none execute). [TOOLS.md](../TOOLS.md)
advertises `run_code`/`shell` without saying they are CLI-only.
[ROADMAP.md](../ROADMAP.md) lines 40 and 47 both mark this
present-but-hidden/small.

**Builds on.** [tools.py:213-243](../../nvh/core/tools.py) `run_code`/`shell`
→ [sandbox/executor.py](../../nvh/sandbox/executor.py) `SandboxExecutor`
(isolation badge, `require_docker` fail-closed), guardrails in
`agent_guardrails.py`, `/v1/sandbox/execute` + `/status` at
[server.py:4408/4449](../../nvh/api/server.py), `tests/test_sandbox_isolation.py`;
the existing confirm-class button flow in WizardChat;
`AgentProfile.tools_allowed` ([profiles.py:46](../../nvh/integrations/wizard/profiles.py))
so only coder-class profiles get them. Requires the single `ToolRegistry`
from [Simplify 10](#10-one-tool-dataclass-one-toolregistry-one-text-protocol-native-function-calling-via-litellm)
so this is one registration, not a Wizard-only copy.

Effort S · Impact H

### 2. `remember` Wizard tool + Memory filter on `/vault` + recent-memory titles in the Wizard prompt

**Why.** ROADMAP lines 41 and 78 mark cross-chat memory present-but-hidden;
it is hidden because memory is three unrelated stores (`core/memory.py`
under `~/.hive`, `.nvhive/agent-memory.json`, vault "Wizard Memory" notes).
The vault is the one that lives on the persistent mount and survives a VM
image swap — say so in the UI copy. This is also where the `memory.py`
deletion lands (REPL `/remember` → vault).

**Builds on.** [vault.py:980-1018](../../nvh/integrations/workspace/vault.py)
`append_vault_memory`, `POST /v1/vault/memory` ([server.py:1262](../../nvh/api/server.py)),
`rag_ask_vault` already a Wizard tool ([wizard/tools.py:538](../../nvh/integrations/wizard/tools.py)),
[web/app/vault/page.tsx](../../web/app/vault/page.tsx) (251 LOC). ~120 LOC
total, zero new dependencies: a `build_memory_context(question)` in
`wizard/chat.py` that `engine.query` also calls, so CLI/API/Wizard share
recall.

Effort S · Impact H

### 3. `generate_image` Wizard tool (rename `generate_portrait`; NVIDIA-hosted or local ComfyUI; inline `<img>`)

**Why.** ROADMAP lines 42 and 46 list image generation from chat as
present-but-hidden/small; the full pipeline exists and is exposed only as an
avatar endpoint. [comfyui.py:983](../../nvh/integrations/installs/comfyui.py)'s
docstring still says "Local ComfyUI — not wired yet" while :998 wires it.

**Builds on.** `comfyui.py:969-1008` `generate_portrait` →
`_generate_portrait_nvidia` (1163-1224, `NVAPI_KEY`) or
`_generate_portrait_comfyui` (1078-1160); sole caller `server.py:1853`
avatar endpoint used by [CreateAgentModal.tsx:113](../../web/components/CreateAgentModal.tsx);
six `/v1/comfyui/*` routes; `tests/test_comfyui_integration.py`. Move to
`nvh/integrations/imagegen.py` with a shim import; confirm-class with a VRAM
warning (SDXL on a 24GB rig evicts the chat model); deep-link to
`/setup#comfyui` when ComfyUI is not running.

Effort S · Impact M

### 4. One chat surface

Merge `/` into `/wizard`, give the Wizard vision attachments, server-side
store only, search wired.

**Why.** Four chat UIs with two persistence stores is the largest UX debt;
[ROADMAP.md:45](../ROADMAP.md) calls vision "missing" when it is wired on
`/` (`QueryRequest.attachments`, [server.py:537-560](../../nvh/api/server.py))
but absent from `WizardChatRequest` (0 `attachments` in the model), so the
surface whose default model is chosen for multimodality at every VRAM tier
cannot see a screenshot. Conversation search exists server-side
(`/v1/conversations/search`) and is unreachable from 3 of 4 chat modes.

**Builds on.** The 0.42 deletion of `/query` and `/council`;
[WizardChat.tsx](../../web/components/WizardChat.tsx) (1,498 LOC,
server-persisted); [ChatInput.tsx:115-146](../../web/components/ChatInput.tsx)
attachment UI (fix :169, which also stuffs "User uploaded image: name" into
the prompt text); `server.py:2725-2753` `prefer_vision` routing block as the
template; `/v1/conversations` CRUD + FTS (`server.py:4611-4767`); ROADMAP
"folders/tags" and "message search" become single-store features. Redirect
`/` → `/wizard`; delete `page.tsx`'s localStorage store (−1,450 LOC).

Effort L · Impact H

### 5. Standard-path OpenAI endpoints + `nvh launch <tool>` for Claude Code / Codex / opencode / Cursor / Continue

**Why.** The rented-GPU developer is exactly the person who wants a coding
agent pointed at the local model with cloud fallback.
[ROADMAP.md:85](../ROADMAP.md) lists standard-path endpoints as
partial/small. The feature-gaps lens reports Ollama shipped `ollama launch
claude-desktop` in August (**unverified**; the in-repo gap stands
regardless).

**Builds on.** The OpenAI-compatible proxy at [server.py:5495](../../nvh/api/server.py)
`/v1/proxy/chat/completions`, :5728 `/completions`, :5799 `/models`, :5817
`/health` — ~30 LOC of route aliases at `/v1/chat/completions`,
`/v1/completions`, `/v1/models` (openai-shape); [mcp_server.py](../../nvh/mcp_server.py)
(548 LOC) for the MCP entry; the Integrations page's tool-config detection
(`page.tsx:198`). `nvh launch` writes `OPENAI_BASE_URL`/`ANTHROPIC_BASE_URL`
or an `mcp.json` entry and prints the one-liner. Skip an
Anthropic-messages-shape adapter unless asked.

Effort M · Impact M

### 6. Scheduler as durable jobs under `NVH_HOME` with `/v1/schedules` and a small UI, run inside the API lifespan

**Why.** [ROADMAP.md:66](../ROADMAP.md) marks scheduled prompts
present-but-hidden/small, but the storage location is a correctness bug on
the target platform: [scheduler.py:37](../../nvh/core/scheduler.py) writes
`~/.hive/schedules.json` — outside `NVH_HOME` and outside the snapshot, so
schedules die with the VM image; `nvh schedule start` is a foreground loop;
there are 0 API routes, 0 web refs, 0 tests.

**Builds on.** [services/jobs.py](../../nvh/integrations/services/jobs.py)
(383 LOC durable job runner used by 6 modules) gains a `recurrence` field;
the poll loop runs as an asyncio task in the API lifespan so `nvh webui`'s
daemonized API is the scheduler (no second daemon); schedules can target a
workflow YAML or an agent profile with the existing `max_cost_usd_per_turn`
ceiling. Deletes `core/scheduler.py`. Keep interval syntax — no cron
expressions or calendar view.

Effort M · Impact M

### 7. Session cost vs hourly desktop rate in the SessionAgePill

**Why.** The only feature in the whole audit that no competitor can copy,
because none of them assume a metered rig: "Session 4h12m · GPU ≈ $3.37 ·
cloud $0.42", with a warning when cloud spend/hour exceeds the GPU rate (the
signal to route local). [future-ideas.md:86](../future-ideas.md) lists it;
`grep -rniE 'hourly|per_hour|desktop_rate'` finds nothing built.

**Builds on.** [SessionAgePill.tsx](../../web/components/SessionAgePill.tsx)
mounted globally ([LayoutShell.tsx:215](../../web/components/LayoutShell.tsx)),
per-message cost accounting, `/v1/budget` and `/v1/analytics`,
`BudgetWidget.tsx`; one `hourly_rate_usd` setting (the settings page's only
surviving real control after the trim).

Effort S · Impact M

### 8. Voice in the WebUI: push-to-talk STT, optional TTS replies

**Why.** ROADMAP lines 43/79, present-but-hidden/medium; unlike the other
adds this needs new endpoints, so it ships last. On-thesis when the local
faster-whisper path from the speech-lab pack is preferred over Groq.

**Builds on.** [voice.py](../../nvh/core/voice.py) (199 LOC: Groq
whisper-large-v3 or local `whisper`, edge-tts/system TTS, record/play) + CLI
`nvh voice` ([main.py:11945-12023](../../nvh/cli/main.py)); needs `POST
/v1/voice/transcribe` and `/v1/voice/speak`, a MediaRecorder mic button in
ChatInput, browser `speechSynthesis` first (zero server work) with edge-tts
as the higher-quality option. Zero tests today — add them.

Effort M · Impact M

## Cut — non-goals

Declined, not deferred. Mirrored as the `## Non-goals` section in
[ROADMAP.md](../ROADMAP.md).

1. **Enterprise and multi-tenant** — SSO/OIDC/LDAP/SCIM, team channels,
   multi-user login/RBAC/admin panel, embeddable widget,
   Kubernetes/Helm/Postgres/S3/Redis/OpenTelemetry, pluggable vector DBs
   (ROADMAP.md lines 49, 67, 68, 69, 71, 76, 81, 82, 83). PolyForm
   Noncommercial (CHANGELOG 0.41.0) means no enterprise buyer can legally
   deploy; the product runs on one renter's VM with single-API-key auth, and
   ROADMAP:20 lists "zero-dependency SQLite RAG" and "rootless-everything"
   as wins that Postgres/Redis/vector-DB plugins would undo. Five large +
   three medium rows for a solo maintainer. The operating envelope is:
   single-user, single-VM, SQLite, API-key auth, noncommercial.
2. **Runtime/platform sprawl** — speculative decoding, multiple inference
   backends (llama.cpp/vLLM/TabbyAPI/mistral.rs/Aphrodite), native desktop
   app incl. MLX, Windows/macOS desktop parity, side-by-side app versions,
   i18n and locale-swappable persona text (ROADMAP.md lines 74, 88, 89, 91,
   92, 95, 133). nvHive is a router over Ollama + NIM; speculative decoding
   is Ollama's job; every extra backend competes for the same VRAM budget
   `model_manager.py` already juggles; the thesis is a rented Linux GPU
   desktop. Freeze [install.ps1](../../install.ps1) (315 LOC) and
   [install-mac.sh](../../install-mac.sh) (272 LOC) as "best-effort,
   contributions welcome" rather than growing them. i18n across 100
   profiles and a 5,859-LOC setup page is a permanent tax while the LLM
   already answers in the user's language.
3. **Marketplace/community surfaces** — community app catalog with
   publishable scripts, agent marketplace, preset hub, in-UI Python tool
   editor, vertical packs (healthcare/finance/legal), Fabric-style patterns
   dir, the full SKILL.md restructure — deferred (ROADMAP.md lines 34, 77,
   84, 87, 111, 121-122, 134). ROADMAP:129 itself says any installer of
   third-party instructions must scan, quarantine, pin SHAs and hard-deny
   `save_provider_key` on a machine holding up to 20 provider keys —
   moderation a solo maintainer cannot carry. Extensibility already exists
   three ways (`$NVH_HOME/wizard-tools` plugin dir, entry points, the
   shipped MCP client). Keep only `nvh agents export/import <url>` for a
   single YAML profile and the `renames` migration map (ROADMAP:103).
   Revisit SKILL.md when a concrete `nvh skill add` request exists; until
   then add `compatibility:`/`allowed-tools` fields to the YAML so a later
   migration is mechanical.
4. **Operator/PhantomInput commercialisation and the April strategy
   proposals** — [operator-vision.md](../operator-vision.md)
   pricing/enterprise/Chrome-Web-Store/SSO sections,
   [phantominput-roadmap.md](../phantominput-roadmap.md) Phase 4 "premium
   add-on", `docs/proposals/*.md`. operator-vision.md:151-157 plans a
   per-session-minute SaaS and an enterprise tier the license forbids, and
   admits (:168-171) the audience is different from nvHive's. The Channels
   proposal's deliverable already shipped (channel-plugin) and the Claude
   Code alignment proposal is built on a March news cycle. Keep PhantomInput
   strictly as the internal QA harness in its own repo; archive the
   proposals with a "superseded" note.
5. **Docker/compose as a supported deployment path, and a second curl
   installer** ([scripts/install.sh](../../scripts/install.sh) via the
   unregistered nvhive.dev). README says "No root, no Docker" twice; nothing
   in CI builds the images; the compose files reference directories that do
   not exist. One installer (`install.sh` → `NVH_HOME` venv) and one
   fallback (`pip install nvhive`). If a container is ever demanded, the
   only acceptable re-add is a single ~40-line API-only Dockerfile built in
   CI — never compose.
6. **A second runtime for the Claude Code integration**
   ([channel-plugin/](../../channel-plugin) on Bun) alongside the Python MCP
   server. Same seven tools as `nvh/mcp_server.py`, 1 commit, no
   lockfile/tests/CI, an MIT license field contradicting PolyForm-NC, and
   docs call it "Coming Soon". One integration surface: `nvhive-mcp`.
7. **Hand-typed model pricing, context windows, capability flags, marketing
   counts, default-model tables and command docs anywhere in the repo.**
   Every one of these drifted within five months (catalog dated 2026-03-31;
   "23/63/25/12/22" vs 21/70/14/13/23; 4 copies of default IDs; 42
   undocumented CLI names). Rule for 0.43 onward: if a number or ID can be
   derived (litellm DB, provider `/models`, the Typer registry,
   `PROVIDER_SPECS`, `LOCAL_MODEL_TIERS`), it is generated and
   parity-tested; a PR that hand-edits it is rejected.
8. **Any new parallel implementation** — a fourth memory store, a fifth
   agent dataclass, a third sandbox, a third tool protocol, a second chat
   store, a new `~/.something` home. Every high-impact finding across all
   six lenses is the same defect: a mechanism built once for the CLI, again
   for the API, and again for the Wizard, with the copies already drifted.
   After 0.43 there is exactly one of each: `ToolRegistry`, `AgentProfile`,
   vault memory, `SandboxExecutor`, `TOOL_CALL` protocol,
   `/v1/conversations`, `NVH_HOME`. New features must plug into those or be
   declined. Also not in the next three releases: model arena/ELO UI,
   artifacts editor, PWA, shareable links, cloud-file RAG,
   document-extraction breadth, supply-chain scanning, tunnel/QR (ROADMAP
   lines 50, 64, 70, 73, 75, 80, 93, 94).

## Sequencing

### 0.41.1 — hotfix, this week (before the 09-27 Perplexity sunset)

Stop shipping things that are broken today.

**Status (2026-09-01): shipped as 0.41.1** — see CHANGELOG. The replacement
model IDs listed as unverified in the caveats below were verified at
implementation time against LiteLLM 1.99.0's model DB and each provider's
official docs; 17 of 21 providers changed defaults, mistral and siliconflow
were already current. Two extra defects surfaced while implementing and are
also fixed: cost accounting was returning `$0` for *every* provider (the
`litellm.completion_cost` call used kwargs current LiteLLM rejects and the
bare `except` swallowed it), and `nvh agent "task"` was unreachable for the
same group-shadowing reason as `nvh mcp` (now `nvh agent run "task"`).

- Fix `nvh mcp` shadowing (`invoke_without_command` + `nvh mcp servers …`;
  registry-collision test) and correct docs/COMMANDS.md:63,
  docs/SDK_API.md:56
- Swap retired default/fallback IDs in all four copies (verified today:
  groq gpt-oss-120b/20b, perplexity sonar-pro/sonar, anthropic
  sonnet-5/haiku-4-5; verify the rest against each provider's `/models`
  before pinning) and ship `nvh config migrate` with a renames map
- Delete the GitHub Models provider (retired 2026-07-30) from all 23 files,
  including engine.py:433 `GITHUB_TOKEN` auto-enable and free_tier priority 2
- Point `nvh snapshot save|restore` at integrations/workspace/snapshot.py so
  the README's reconnect-survival promise is true from the CLI
- Fix the `nvh advisor remove` half-delete (remove `.env`/`config.yaml`
  copies too) and make `NVH_USE_KEYRING`'s default match what `nvh setup`
  writes, pending the 0.43 secrets module
- Fix vscode-nvhive `/health` → `/v1/health` (one line) or mark the
  extension deprecated

### 0.42 — "subtract" (≈ −15k LOC code/scripts, −3.5k doc lines, −3.2 MB media)

Delete the second product; no new behaviour.

- Dead orchestration set + tests + `/v1/locks` + `hooks` field + two
  zero-caller helpers (−2,400)
- Docker/compose family, five stale scripts, demo assets; GETTING_STARTED
  rewritten without Docker; `.env.example` on `NVH_HOME` (−3,400 + docs)
- `tools/` Operator/PhantomInput + two vision docs to their own repo;
  channel-plugin deleted; vault.py Operator paragraph removed (−3,600)
- Legacy `~/.hive` core modules → `NVH_HOME` successors: knowledge→rag,
  memory→vault, scheduler→jobs, smoke_test→diagnostics, templates→profile
  YAML, docker_sandbox→`SandboxExecutor.run_shell` (−1,900 net)
- Provider adapters → `OpenAICompatibleProvider` + `ProviderSpec` table with
  one-release compat shims (−3,700)
- CLI: `ask --focus/--fast/--local/--clipboard`, `nvh status` tiers,
  provider commands hidden, registry-derived reserved words, did-you-mean,
  explicit `nvh do` for task-shaped bare prompts; COMMANDS.md generated
  (−2,200)
- Web: delete `/query` and `/council`, trim Preferences, one-time
  localStorage→`/v1/conversations` import, delete `localChats.ts` +
  LayoutShell merge, sidebar search wired (−1,850)
- Docs 33→12 with marketing-parity test and link-check; CONTRIBUTING
  corrected; unreferenced media removed
- Dependency truth (pydantic-settings out, packaging in, jupyter extra,
  self-referential extras); tests for deleted modules removed
- Perplexity: verify litellm's Agent-API route before 09-27; add
  `sunset_date` to `ProviderSpec`

### 0.43 — "refresh"

One table for every fact; current models everywhere.

- `ProviderSpec` absorbs advisor_profiles/free_tier/quota_info/proxy
  map/settings YAML/server dict/setup.py/`KNOWN_ADVISORS`/web
  `CLOUD_PROVIDERS`; docs/PROVIDERS.md generated (−1,200)
- litellm-derived price/context/capabilities/`deprecation_date`;
  `model_overrides.json` via `register_model` (also in the remote
  nvhive-catalog.json v2); `_calc_cost` never silently $0; deprecated
  entries hidden
- Real `list_models()` for all OpenAI-compatible providers +
  Anthropic/Google; `/v1/models` intersect becomes truthful;
  tests/test_model_currency.py on a weekly schedule; proxy prefix map falls
  back to `litellm.get_llm_provider`
- Cloud catalog regenerated (~55 entries, keys normalised to the exact
  strings adapters send, NIM Nemotron rows, SiliconFlow copy softened after
  verification)
- `LOCAL_MODEL_TIERS`: `nemotron3:33b` replaces the phantom tags, HF-GGUF
  bootstrap deleted, gpu.py/setup.py/install.sh/studio_packs/local_chat/
  ollama_provider read one table, registry HEAD probe in CI, superseded
  Ollama rows retired
- `num_ctx` from the VRAM tier; `ollama_base_url()` replaces 14 env reads
  and 65 literals
- `NVH_HOME` the only root: `storage_layout()` everywhere, env helper with
  legacy aliases, `nvh/config/secrets.py`, `providers:` canonical key,
  "nvHive API" title + `X-NVH-API-Key`, doctor migration of `~/.hive` and
  `~/.council`
- One `Tool`/`ToolRegistry` with native `tools=` through the
  OpenAI-compatible base + Ollama `/api/chat`, one `TOOL_CALL` text
  fallback, web_search deduplicated, plugin discovery merged — MCP tools now
  reach `nvh do` and the REPL
- Council single streaming pipeline + Engine wrapper; `majority_vote`
  implemented or removed; `nvh agent` real cost; coding system prompt
  actually passed
- `AgentProfile` as the only agent type (council personas in
  agent-library.json, one picker); orchestrator trimmed to
  prompt-optimisation + compression; `nvh advisor` → `nvh providers`
- Coverage-campaign tests folded into subject files (test count preserved);
  `nvh/data` package

### 0.44 — "add"

The Wizard can act — mostly by registering what already exists.

- `run_code` + `shell` (confirm-class, isolation badge) + `ask_user` with
  approval cards; docs/TOOLS.md gains a CLI-only/Wizard column
- `remember` tool + Memory filter on `/vault` + `build_memory_context()`
  shared by CLI/API/Wizard
- `generate_image` tool (NVIDIA-hosted or ComfyUI) rendered inline
- One chat surface: `/` merged into `/wizard` with vision attachments,
  server-side store only, conversation search and pins in one sidebar
  (−1,450 more LOC)
- Standard-path `/v1/chat/completions` + `/v1/models` aliases and `nvh
  launch claude-code|codex|opencode|cursor|continue`
- Scheduler as durable jobs under `NVH_HOME` with `/v1/schedules` and a list
  UI, running in the API lifespan
- Session cost vs hourly desktop rate in SessionAgePill
- Voice: `/v1/voice/transcribe` + `/speak`, mic button, read-aloud toggle
  (local faster-whisper preferred)

## Verification caveats

Lens claims that were checked and corrected, or could not be checked. None
of the sequencing above depends on an unverified claim.

- **Duplication lens: "delete `iterative_loop.py`, `recursive_agents.py` and
  `agent_matching.py`" — wrong.** Importers: `iterative_loop` ←
  `cli/main.py` (`--iterative`); `recursive_agents` and `agent_protocol` ←
  `iterative_loop`; `agent_matching` ← `cli/main.py`, `iterative_loop`,
  `recursive_agents`. Only `autonomous`, `agent_pr`, `parallel_pipeline`,
  `rollback`, `hooks`, `agent_report`, `file_lock` are dead.
- **Model-currency lens: specific replacement IDs — unverified, not pinned.**
  OpenAI (gpt-5.6-sol/terra/luna), Google (gemini-3.7-flash,
  3.5-flash-lite), xAI (grok-4.6/4.3), DeepSeek (v4-flash/pro), Mistral,
  Cohere (command-a-plus-05-2026), Cerebras, SambaNova, Together, Fireworks,
  OpenRouter `:free`, AI21, NIM Nemotron, SiliconFlow and LLM7 are
  candidates to confirm against each provider's `GET /models` at
  implementation. Verified today only: Groq `openai/gpt-oss-120b` and
  `openai/gpt-oss-20b` (Groq deprecations page), Perplexity
  `sonar`/`sonar-pro`/`sonar-reasoning-pro`/`sonar-deep-research`
  (Perplexity docs), Anthropic
  `claude-fable-5-1`/`claude-opus-5`/`claude-sonnet-5`/`claude-sonnet-4-6`/`claude-haiku-4-5`
  (API reference), and the `nemotron3:33b` Ollama tag.
- **Model-currency lens: `claude-haiku-4-5-20251001` as the Anthropic
  fallback and a litellm `deprecation_date` of 2026-10-15 for it.** The
  current API model ID is `claude-haiku-4-5` (no date suffix); the
  deprecation-date claim is unverified. The lens's Anthropic pricing
  corrections are confirmed (opus-4-6 $5/$25, 1M context on sonnet-4-6).
- **Model-currency and feature-gaps lenses: Ollama library additions gemma4
  / qwen3.5 / qwen3.6 / gpt-oss / qwen3-coder / Muse Glimmer / "Nemotron 3.5
  Lightning" with sizes — unverified.** The feature-gaps lens's suggestion
  to use a Lightning tag for the 24GB tier conflicts with the verified fact
  that the Nemotron 3 Nano Omni tag is `nemotron3:33b` (28GB). No local tag
  enters `LOCAL_MODEL_TIERS` without passing the registry HEAD probe.
- **Model-currency lens: SiliconFlow "now requires real-name verification
  (since 2026-05-15)" and "DeepSeek-V2.5 discontinued 2026-04-22" —
  unverified.** Verify before rewriting the free-tier copy.
- **Feature-gaps lens: all competitor release claims** (Open WebUI
  0.11.0/0.11.1, LM Studio Bionic, LibreChat 0.8.8-rc1, AnythingLLM 1.16.1,
  Jan 0.8.4, Pinokio 8.x, Ollama funding/launch features) — not
  independently verified; no item in this plan depends on them, the in-repo
  gaps stand on their own.
- **Duplication lens: "per-provider facts live in 8 tables" — confirmed but
  undercounted.** There are at least nine (`KNOWN_ADVISORS` in
  cli/main.py:629 and cli/setup.py `advisor_defs` also carry defaults).
- **Bit-rot lens: "`docker_sandbox.py` is live — keep".** Factually right
  (imported by tools.py and smoke_test.py), but the duplication lens's
  fold-into-`SandboxExecutor` recommendation is adopted because the two
  sandboxes give the same `shell` tool different isolation policies.
- **UX lens: "`nvh webui` opens `/setup` on every launch (main.py:9068)".**
  The literal exists at 9068, but it was not confirmed to be unconditional;
  treat as a check, not a bug. The UX lens's target 12-verb CLI tree is a
  direction, not a 0.42 deliverable; only the mechanical collapses are
  scheduled.
