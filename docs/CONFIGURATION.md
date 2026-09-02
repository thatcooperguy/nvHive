# Configuration

Every knob in one place: `config.yaml`, secrets, the `NVH_HOME` layout, the
environment variables nvHive reads, project context files, council cabinets,
the tool system and workflows.

## Where configuration lives

| Layer | Location | Notes |
|---|---|---|
| user config | `$HIVE_CONFIG_HOME/config.yaml` | the installer sets `HIVE_CONFIG_HOME=$NVH_HOME/config`; a plain pip install defaults to `~/.hive/config.yaml` |
| secrets | `$NVH_HOME/config/.env`, `~/.hive/.env` | `KEY=VALUE` lines, loaded by the CLI and the API at startup; shell variables win |
| project overlay | `.hive.yaml` or `.hive/config.yaml` | searched upward from the current directory, never above `$HOME` and never `~/.hive` itself; deep-merged over the user config |
| profile overlay | `profiles.<name>` in `config.yaml` | applied with `--profile <name>` or `HIVE_PROFILE=<name>` |

Values may reference the environment: `${GROQ_API_KEY}` or
`${TOGETHER_API_KEY:-${TOGETHERAI_API_KEY}}`. `advisors:` is accepted as an
alias for `providers:`.

```bash
nvh config init                    # write the default file (--force to overwrite)
nvh config get defaults.provider
nvh config set budget.daily_limit_usd 5.00
nvh config edit                    # $EDITOR
nvh config export                  # current config with keys masked
nvh config diff other.yaml         # compare
nvh config import other.yaml       # backs up the existing file first
nvh config migrate --dry-run       # rewrite retired model IDs and providers
```

`nvh config migrate` and `nvh advisor remove` write a `.yaml.bak` beside the
file before touching it.

## `config.yaml` reference

Defaults shown are the schema defaults (`nvh/config/settings.py`); `nvh config
init` writes a fuller template with every provider stanza disabled.

```yaml
version: "1"

defaults:
  mode: ask                 # ask | convene | poll | throwdown
  provider: ""              # empty = let the router choose
  model: ""
  output: text              # text | json | markdown | raw
  stream: true
  timeout: 30
  max_tokens: 4096
  temperature: 1.0
  system_prompt: "Always respond in English unless the user explicitly requests another language."
  show_metadata: true
  orchestration_mode: auto  # off | light | full | auto  (see MODELS.md)
  prefer_nvidia: false      # 1.3x routing bonus for Ollama, NIM, Triton

providers:                  # or `advisors:`
  groq:
    api_key: ${GROQ_API_KEY}
    default_model: groq/openai/gpt-oss-120b
    fallback_model: groq/openai/gpt-oss-20b
    enabled: true
    timeout: 60
    retry_attempts: 3
    retry_initial_delay: 1.0
    retry_multiplier: 2.0
    retry_max_delay: 30.0
  ollama:
    type: ollama            # bespoke adapters: ollama, triton, mock; anything else is OpenAI-compatible
    base_url: http://localhost:11434
    default_model: ollama/gemma3:4b
    enabled: true
  my-proxy:
    type: openai_compatible # any OpenAI-shaped endpoint
    base_url: https://llm.example.com/v1
    api_key: ${MY_PROXY_KEY}
    default_model: some-model

council:
  strategy: weighted_consensus   # weighted_consensus | majority_vote | best_of
  default_weights: {}            # advisor -> weight, normalised to 1.0
  synthesis_provider: ""         # empty = the local orchestrator or the best advisor
  fallback_order: []
  quorum: 2
  timeout: 60

routing:
  weights: { capability: 0.4, cost: 0.3, latency: 0.2, health: 0.1 }
  rules:                         # first match wins
    - match: { task_type: code_generation }
      provider: anthropic
    - match: { task_type: question_answering }
      provider: groq

budget:
  daily_limit_usd: 5
  monthly_limit_usd: 20
  alert_threshold: 0.80
  hard_stop: true                # false = warn and continue
  degrade_on_limit: true         # fall back to free/local advisors near the limit

cache:
  enabled: true
  ttl_seconds: 86400
  max_size: 1000
  cache_nonzero_temp: false

logging:
  level: INFO
  file: ""                       # empty = $NVH_HOME/logs/nvhive.log when NVH_HOME is set

profiles:
  cost_optimized:
    defaults: { provider: groq }
  quality:
    defaults: { mode: convene }
    council: { strategy: best_of }

webhooks:                        # events: query.complete, council.complete,
  - url: https://hooks.example.com/nvhive   # budget.threshold_reached, budget.exceeded,
    events: [council.complete, budget.exceeded]   # provider.circuit_open/closed, provider.error
    secret: ${NVHIVE_WEBHOOK_SECRET}
    enabled: true
    retry_count: 3
    timeout_seconds: 10
```

Task types for `routing.rules` are the `TaskType` values in
`nvh/providers/base.py`: `code_generation`, `code_review`, `code_debug`,
`reasoning`, `math`, `creative_writing`, `summarization`, `translation`,
`conversation`, `question_answering`, `structured_extraction`, `multimodal`
and `long_context_analysis`. `nvh status --routing` shows how the last query
was classified and why it went where it went.

## Secrets

API keys are resolved in order: the stanza's `api_key` (usually `${VAR}`),
then `COUNCIL_<NAME>_API_KEY` and `<NAME>_API_KEY` in the environment, then
the provider's own variables (LiteLLM's conventions, e.g. `XAI_API_KEY`), then
the OS keyring — only when `NVH_USE_KEYRING=1`, because headless boxes often
have a slow or absent SecretService. `nvh setup`, `nvh advisor add` and the
Wizard write keys to the `.env` file under `$NVH_HOME/config` (falling back to
`~/.hive/.env`); `nvh advisor remove` scrubs both and disables the stanza.
`.env.example` at the repo root is a template. Snapshots never bundle
`config.yaml` or `.env`.

## `NVH_HOME` layout

`NVH_HOME` (alias `NVHIVE_HOME`) is the only root. Unset, it defaults to
`~/.nvh`, which is fine on a laptop and wrong on an ephemeral VM — the
installer picks the persistent volume and exports it from your shell profile
and from `$NVH_HOME/nvh-env.sh`. Each component directory has an override so
one large tree can be split across mounts; `nvh status --deep --storage`
prints the active layout with a real write probe and free-space check.

| Directory | Override | Holds |
|---|---|---|
| `$NVH_HOME/` | `NVH_HOME` | `nvh-env.sh`, `venv/`, `repo/`, `uninstall.sh` |
| `bin/` | `NVH_BIN` | rootless Ollama, `nvhive-ai-studio`, `nvhive-openclaw` and other launchers |
| `models/`, `models/ollama/` | `NVH_MODELS`, `OLLAMA_MODELS` | local model weights |
| `cache/` | `NVH_CACHE` | `pip/`, `uv/`, `huggingface/`, `torch/`, `tmp/`, `catalog/`, `xdg/` (also exported as `PIP_CACHE_DIR`, `UV_CACHE_DIR`, `HF_HOME`, `HUGGINGFACE_HUB_CACHE`, `TORCH_HOME`, `TMPDIR`, `XDG_CACHE_HOME`) |
| `logs/` | `NVH_LOGS` | `install.log`, `api-server.log`, `nvhive.log`, service logs |
| `config/` | `HIVE_CONFIG_HOME` | `config.yaml`, `.env`, `mcp-servers.json` |
| `state/` | `NVH_STATE` | `nvhive.db` (SQLite: conversations, routing outcomes, costs), capability marker, browser profiles |
| `runtimes/` | `NVH_RUNTIME_HOME` | micromamba and other runtime fallbacks |
| `apps/` | `NVH_APPS_HOME` | rootless Firefox, Obsidian, AppImages |
| `webui/` | `NVH_WEB_HOME` | WebUI source, build and the auto-installed Node |
| `studio/` | `NVH_STUDIO_HOME` | studio packs |
| `comfyui/` | `COMFYUI_HOME` | ComfyUI environment and nvHive examples |
| `projects/` | `NVH_PROJECTS` | agent workspaces |
| `outputs/` | `NVH_OUTPUTS` | generated images and other artefacts |
| `backups/` | `NVH_BACKUPS` | config backups |
| `support/` | `NVH_SUPPORT` | redacted support bundles from `nvh status --report` |
| `catalog/` | `NVH_CATALOG` | cached setup catalog |
| `vault/`, `rag/`, `jobs/`, `agent-profiles/` | — | Memory Vault notes, the RAG store, background-job metadata and logs, custom agent profiles |

`HIVE_DATA_DIR` moves only the SQLite database; `nvh snapshot` reads it from
wherever it actually is.

## Environment variables

Storage overrides are in the table above. Everything else nvHive reads:

| Variable | Default | Effect |
|---|---|---|
| `HIVE_API_KEY` | unset | bearer token for the API; unset and no users = open/local mode. Required before `nvh serve --host 0.0.0.0` |
| `HIVE_CORS_ORIGINS` | WebUI defaults | comma-separated extra origins for the API |
| `HIVE_LOG_LEVEL`, `HIVE_LOG_FORMAT`, `HIVE_LOG_FILE` | `INFO`, `text`, `$NVH_HOME/logs/nvhive.log` | API logging; `json` for structured logs |
| `HIVE_PROFILE` | unset | config profile to apply when `--profile` is not given |
| `NVH_USE_KEYRING` | `0` | consult the OS keyring for keys |
| `NVH_API_PORT`, `NVH_WEB_PORT` | `8000`, `3000` | ports the service registry and WebUI expect |
| `NEXT_PUBLIC_API_URL` | `http://localhost:8000` | where the browser reaches the API (WebUI build/dev) |
| `NVH_DB_TIMEOUT` | `5` | SQLite busy timeout in seconds |
| `NVH_VERBOSE` | unset | verbose WebUI bootstrap output |
| `OLLAMA_BASE_URL` / `OLLAMA_HOST` | `http://localhost:11434` | where to find Ollama |
| `NVH_OLLAMA_BIN` | `$(which ollama)` | Ollama binary for `nvh services` and `nvh models` |
| `NVH_DEFAULT_OLLAMA_MODEL` | from config | model preloaded after boot |
| `NVH_OLLAMA_PRELOAD` | `1` | `0` skips the post-boot warm-up |
| `NVH_OLLAMA_START_WAIT` | `10` | seconds the engine waits for a starting Ollama |
| `NVH_OLLAMA_HTTP_TIMEOUT` | `0.5` | seconds for the engine's Ollama liveness probe |
| `NVH_OLLAMA_VERSION`, `NVH_OLLAMA_URL`, `NVH_OLLAMA_DOWNLOAD_BASE` | latest, unset, `https://ollama.com/download` | pin or redirect the rootless Ollama download |
| `NVH_OLLAMA_BOOT_TIMEOUT`, `NVH_API_BOOT_TIMEOUT`, `NVH_WEBUI_BOOT_TIMEOUT` | `15`, `20`, `30` | health-gate windows for `nvh services start` ([MAINTAINERS.md](MAINTAINERS.md#service-order)) |
| `NVH_BROWSER` | unset | browser command for `nvh webui`, e.g. `firefox --new-window {url}` |
| `NVH_FIREFOX_AUTO_INSTALL` | `1` | `0` disables the rootless Firefox fallback install |
| `NVH_FIREFOX_PROFILE` | `$NVH_STATE/browser-profiles/desktop` | isolated Firefox profile directory |
| `NVH_SANDBOX_REQUIRE_DOCKER` | unset | `1`/`true`/`yes`: refuse `run_code`/`shell` instead of falling back to an unisolated subprocess (`nvh do --sandbox` sets it) |
| `NVH_BOOT_PREFLIGHT`, `NVH_BOOT_AUTO_REPAIR` | `1`, `1` | run the boot preflight at API start; let it apply safe repairs |
| `NVH_TARGET_VM_VALIDATED` | unset | `1` after the target-VM checklist; gates `production-ready` |
| `NVH_CATALOG_URL` | GitHub raw URL | remote setup catalog; the bundled copy is the fallback |
| `SEARXNG_URL`, `BRAVE_SEARCH_KEY`, `GOOGLE_SEARCH_KEY` + `GOOGLE_CX` | unset | web-search backends for the `web_search` tool (DuckDuckGo needs nothing) |
| `NVAPI_KEY` | unset | NVIDIA-hosted image generation for the Wizard's portrait tool |
| `NVH_WEB_REF` | `v<version>` | git ref of the WebUI source `nvh webui` fetches |

Installer-only knobs (`install.sh` / `start-linux.sh`):

| Variable | Default | Effect |
|---|---|---|
| `NVH_MOUNT` | unset | preferred persistent mount when `NVH_HOME` is unset |
| `NVH_INSTALL_MODEL_DOWNLOAD` | `auto` | `0` skips the local model pull |
| `NVH_MODEL_DOWNLOAD_DELAY` | `10` | seconds before the background model pull starts |
| `NVH_INSTALL_LAUNCH` | `auto` | `0` installs without `nvh services start --open` |
| `NVH_INSTALL_FULL_CAPABILITY`, `NVH_INSTALL_FULL_CAPABILITY_DOWNLOAD` | unset | stage / also download image, video, speech and music packs by VRAM ([MODELS.md](MODELS.md#the-capability-matrix)) |
| `NVH_NO_OS_MOD` | `0` (`1` from `start-linux.sh`) | never touch files outside `NVH_HOME` and the shell profile |
| `NVH_PORT_CONFLICT_KILL_FOREIGN` | unset | `1` lets the installer stop foreign processes on 11434/8000/3000 |
| `NVH_USE_ACTIVE_ENV`, `NVH_FORCE_VENV` | `0`, unset | install into an already-active conda/venv; force a fresh `$NVH_HOME/venv` |
| `NVH_USE_BINARY` | `0` | `start-linux.sh` fetches the single-file binary instead of using Python |

## Project context: `HIVE.md`

A `HIVE.md` in the project root is injected into the system prompt of every
query run from that directory — for all advisors, local and cloud. Lower
priority sources are `~/HIVE.md` (user-level) and
`~/.hive/global_context.md`; `.hive/context/*.md` adds modular files. Optional
frontmatter: `name`, `scope` (`all`, `convene`, `query`, `code`) and
`priority` (0–100, higher first). `HIVE.md.example` at the repo root shows the
shape; `GET /v1/context` lists what is loaded and `POST /v1/context/reload`
re-reads it.

## Council cabinets

`nvh convene --cabinet <name>` (and the chat page's council **Advanced**
drawer, `POST /v1/council`, the MCP `council` tool) picks a preset panel from
`nvh.core.agents.COUNCIL_PRESETS`. `nvh agent presets` lists them live; `nvh
agent analyze "question" -n 5` previews the panel `--auto-agents` would
generate instead.

| Cabinet | Members |
|---|---|
| `executive` | CTO, CEO / Business Strategist, CFO / Financial Analyst, Product Manager |
| `engineering` | Software Architect, Senior Backend Engineer, DevOps/SRE Engineer, Security Engineer, QA/Test Engineer |
| `security_review` | Software Architect, DevOps/SRE Engineer, Security Engineer, Legal/Compliance Advisor |
| `code_review` | Software Architect, Senior Backend Engineer, QA/Test Engineer, Performance Engineer |
| `product` | CEO / Business Strategist, Product Manager, UX Designer, Engineering Manager |
| `product_resilience` | DevOps/SRE Engineer, ML/AI Engineer, Product Manager, UX Designer, Underdog Student Advocate, QA/Test Engineer |
| `data` | Software Architect, Database Administrator, Data Engineer, ML/AI Engineer |
| `full_board` | CTO, Software Architect, Senior Backend Engineer, DevOps/SRE Engineer, Security Engineer, CEO / Business Strategist, CFO / Financial Analyst |
| `homework_help` | Patient Tutor, Devil's Advocate, Study Coach |
| `code_tutor` | Code Mentor, Bug Hunter, Best Practices Reviewer |
| `essay_review` | Writing Coach, Logic Checker, Style Editor |
| `study_group` | Socratic Questioner, ELI5 Explainer, Practice Problem Generator |
| `exam_prep` | Exam Coach, Flashcard Creator, Weak Spot Finder |

Strategies: `weighted_consensus` (default; `--weights groq=0.5,google=0.5`),
`majority_vote`, `best_of`. `--members` picks advisors explicitly,
`--no-synthesize` shows the raw responses. `product_resilience` is the
skeptical panel for "what breaks for a beginner on a no-root GPU desktop".

## Tools

Tools are functions the model may call while answering. They are opt-in:
`nvh do "task"` and `nvh agent run "task"` use them, the REPL enables them
with `/tools`, and the AI Wizard has its own set (`rag_ask`, `rag_ask_vault`,
`rag_ingest`, `web_search`, `refresh_models`, `repair_workspace`,
`validate_provider_key`, `save_provider_key`) plus any `mcp_<server>_<tool>`
attached through [INTEGRATIONS.md](INTEGRATIONS.md#nvhive-as-an-mcp-client).
Every tool is either **safe** (runs unattended) or **confirm** (asks first;
`nvh do --auto` approves safe tools only). The registry is
`nvh.core.tools.ToolRegistry`; the built-ins by area, confirm-class marked `*`:

| Area | Tools |
|---|---|
| Files | `read_file`, `list_files`, `search_files`, `find_files`, `disk_usage`, `write_file`\*, `move_file`\*, `delete_file`\* |
| Code and shell | `run_code`\*, `shell`\* — Docker-isolated when Docker is present, otherwise an audited subprocess fallback that the result and the tool output flag; `NVH_SANDBOX_REQUIRE_DOCKER=1` refuses the fallback |
| Web | `web_search`, `web_fetch`, `http_request`, `browser_navigate`, `download`\*, `browser_screenshot`\*, `browser_fill_form`\* |
| System | `system_info`, `list_processes`, `pip_list`, `get_env`, `open`, `open_terminal`, `notify`, `get_clipboard`, `set_clipboard`, `docker_ps`, `kill_process`\*, `pip_install`\*, `set_env`\*, `docker_run`\* |
| Vision and desktop | `screenshot`, `capture_screenshot`, `analyze_image`, `read_text_from_image`, `imagine`, `mouse_move`\*, `mouse_click`\*, `keyboard_type`\*, `keyboard_press`\*, `scroll`\* |

Guardrails in `nvh/core/agent_guardrails.py` run before every call and cannot
be bypassed by `--yes`: shell commands are checked against a deny list, file
paths must stay inside the workspace, writes have a size cap, secrets are
redacted from outputs and long outputs are truncated.

## Workflows

A workflow is a YAML pipeline of `ask`, `convene`, `poll`, `safe` and `shell`
steps whose outputs feed later prompts through `{{variables}}`:

```yaml
name: Code Review Pipeline
description: Three-pass code review with different expert perspectives
variables: {}                       # defaults for {{...}} not passed on the CLI
steps:
  - name: security_scan
    action: ask
    prompt: "Analyze this code for security vulnerabilities:\n\n{{input}}"
    advisor: anthropic
    save_as: security
  - name: quality_review
    action: ask
    prompt: "Review this code for quality and best practices:\n\n{{input}}"
    advisor: openai
    save_as: quality
  - name: synthesis
    action: convene
    prompt: "Synthesize:\n\nSecurity: {{security}}\n\nQuality: {{quality}}"
    cabinet: code_review
    save_as: summary
    condition: security              # optional: run only if the variable is non-empty
```

```bash
nvh workflow list                        # bundled: code_review, debug, research
nvh workflow show code_review
nvh workflow run code_review --file main.py     # or --input "text"
```

Workflows are discovered in `nvh/workflows/` (bundled), `~/.hive/workflows/`
and `./.hive/workflows/`. Shell steps run through the same sandbox as the
`shell` tool.

Back to [README](../README.md)
