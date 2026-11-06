# Configuration

Every knob in one place: `config.yaml`, secrets, the `NVH_HOME` layout, the
environment variables nvHive reads, project context files, council cabinets,
the tool system and workflows.

## Where configuration lives

| Layer | Location | Notes |
|---|---|---|
| user config | `$NVH_HOME/config/config.yaml` | the storage layout's `config_dir` (`NVH_CONFIG`, legacy alias `HIVE_CONFIG_HOME`); `NVH_HOME` defaults to `~/.nvh`, so a plain pip install reads `~/.nvh/config/config.yaml` |
| secrets | `$NVH_HOME/config/.env` | `KEY=VALUE` lines, loaded by the CLI and the API at startup; shell variables win |
| project overlay | `.nvh.yaml` or `.nvh/config.yaml` | searched upward from the current directory, never above `$HOME` and never inside `NVH_HOME` itself; deep-merged over the user config. A project's pre-0.44 `.hive.yaml` / `.hive/config.yaml` is still read as a fallback |
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

### Atohi admission (disabled)

nvHive includes a native admission and cancellation boundary for shared compute
managed by [Atohi](https://github.com/thatcooperguy/atohi) (private repository).
It ships disabled:

```yaml
atohi:
  enabled: false
  managed_providers: []
```

When enabled, Ollama and Triton provider types, including configured aliases,
require admission. Add the exact registered names of other self-hosted GPU
endpoints (for example an OpenAI-compatible NIM endpoint) to `managed_providers`.
Local RAG embeddings also require admission. Hosted cloud providers are not
automatically treated as members of the home compute pool. This is top-level
configuration; profile overrides cannot replace it. Listing a provider's
models (`list_models`, the metadata probe behind `/v1/models` and
`nvh status --deep`) is not a model call: it passes through the admission
boundary un-admitted and is never paused, so a live model list is available
with or without a lease.

**No production native broker is installed. Leave this disabled.** Enabling it
currently stops engine initialization and council dispatch before any provider
call, and stops embedding requests before opening HTTP or pulling a model.
There is no endpoint, token, arbitrary import path, executable, or granted flag
that can turn this into a working lease. The code-only `AdmissionBroker` protocol
is an integration boundary exercised with hermetic test doubles.

Managed dispatch additionally requires a typed, in-process `ModelSession` from
that trusted broker. It binds one explicit provider, model and operation to a
job/allocation/generation identity and a process-local monotonic lease deadline.
Registry calls, RAG embeddings and managed vision use only its issued transport;
they never retarget a shared adapter, auto-pull, or fall back after a bound call
fails. Missing, mismatched, replaced, expired or revoked handles pause the call,
including revocation during cleanup before the final result. Registry default
models are captured with the engine policy; automatic model selection is refused
on this path. Disabled mode retains the ordinary providers. This is a local
application boundary, not a credential or implemented model-session issuer:
`job.running`, event dictionaries and controller release events provide no
model access. The future issuer must privately bind the consumed node/boot/epoch/
spec/launch authority and the owned model-server lifetime; closing its client
still does not establish physical GPU release.
Generic `run`/`stream` lifecycle primitives remain trusted extension plumbing;
they do not authorize new raw model callbacks. Managed model code must use the
session-bound dispatch path.

Revocation or uncertain ownership raises `ResourcePaused`, outside ordinary
provider errors. Chat does not fall back to cloud, embeddings do not retry or
auto-pull, and council/comparison sessions cancel and await their cooperating
client tasks before returning the pause. A council cannot treat a revoked member
as a quorum loss and continue to synthesis. CLI async commands stop with exit 1;
HTTP query, council, comparison, Wizard and RAG endpoints return 409 with code
`resource_paused`, status `paused` and `automatic_retry: false`. Streaming HTTP
and WebSocket query/council endpoints send a terminal error carrying the same
fields, without a completion event. Earlier streamed text remains partial.
No call is automatically replayed or resumed.

The boundary covers providers obtained through the application registry
(including local routing/synthesis helpers), local RAG embeddings, built-in
vision analysis and benchmark helpers. API and Wizard callers pass the owning
Engine's exact admission object through folder ingestion, vault indexing,
retrieval and automatic recall. Library callers with in-memory configuration
must pass the same `AtohiAdmission` using the helpers' trusted keyword-only
`admission` parameter. Omitting it at a standalone entrypoint loads normal file
configuration; request JSON and model tool arguments cannot supply authority.
Configuration changes require a controlled application restart.

Each engine and directly constructed council binds its own admission policy.
Reusing a registry with another configuration cannot enable or disable that
policy. Raw adapters and the model catalog remain shared, so later registrations
through the original registry or the engine stay visible. Bound policy changes
are rejected; create a new scoped registry or restart for a different policy.
Ownership remains watched through upstream cleanup and broker exit for ordinary
calls and streams. A result or final chunk is delivered only after that scope
closes without revocation or loss of ownership knowledge. Cleanup already handling
cancellation is drained without a second cancellation interrupting it. Trusted
engine-owned helpers use `engine.registry.admission`; an unconfigured registry
raises instead of supplying a missing policy that could fall back to disk.

In shared mode, opportunistic service warmup and synchronous setup token probes
are not attempted: they have no cancellable lease transport. A cached successful
probe cannot override the current shared policy. Setup reports distinguish this
pause from model readiness, and setup configuration writes preserve the existing
admission policy. Optional screenshot-assisted setup is likewise unavailable in
shared mode; manual setup remains available.

Tool registries bind built-in model calls per Engine without modifying a registry
used by another Engine. The API rebuilds its Wizard registry when its bound
policy changes. If a turn's registry is unavailable, tool execution cannot
silently rebuild it with disk policy. Revoked embedding batches do not replace
existing source chunks or continue to subsequent files. Compatible chat APIs
report resource pauses explicitly, including terminal streaming errors without
success markers.

This remains a disabled checkpoint, not complete shared-compute integration.
Arbitrary direct adapters, third-party plugins and external tools are not made
members of the compute pool by these changes. Paused council runs still need
durable accounting of completed and partial usage reconciled with the native
integration before shared workloads are enabled.

Before activation, a reviewed broker must implement Atohi's actual workload/job
contract and immutable workload, input and handler digests, authenticated node
ownership, durable operation identity, cancellation reconciliation and verified
physical GPU release. See Atohi's `schemas/` and node protocol documentation.
nvHive does not equate a closed HTTP connection, cancelled asyncio task, empty
client task list, or broker context exit with a released GPU. A provider that
ignores cancellation can still require process-level containment. Task cleanup
is cooperative and has no hard time bound; a qualified native worker still
needs an independent node watchdog. These tests
do not qualify multi-node inference, checkpoint recovery, model unloading,
exactly-once remote execution, or the 120-second reclaim deadline. A new user
request after a pause is a new operation, not permission to replay an ambiguous
earlier inference.

### Schema defaults

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
  # hourly_rate_usd: 2.10         # what this desktop costs you per hour; the WebUI's session pill multiplies it by the tab's age

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

`budget.hourly_rate_usd` is optional and never guessed: `nvh config set
budget.hourly_rate_usd 2.10` records what this desktop costs you per hour and
the WebUI's session cost pill shows that rate × the time since the tab opened
as its desktop term — an estimate from the rate you set, not a measurement;
nvHive does not know what you pay. The pill reads `GET
/v1/budget/status?since=<epoch ms>`, which since 0.46 adds `hourly_rate_usd`
(the configured rate as a string, `null` when unset), `daily_cost_known` (the
catalog's verdict on every model that answered today), `platform`
(`device_class`, `device_label`, `is_cloud`, `cloud_provider`, `is_owned`, from
the warmed platform facts) and, only with `since`, a `session` block:
`spend_usd` and `by_provider` sum the machine-wide ledger at or after that
instant (CLI, other tabs, proxy clients and scheduled runs all count),
`cost_known` is false when any model that answered since then — failed calls
included — has no catalog price, and `local_only` is true only when every
provider that answered was local by the catalog's verdict, never because the
sum is zero (an unpriced cloud model logs $0 too). Without `since` the block is
`null`; a `since` that is not epoch milliseconds is a 422 — negative,
non-integer, overflowing, or too small to be milliseconds (below 10^12, which
is 2001-09-09: a value in epoch *seconds* would otherwise read as a 1970
instant and sum the whole ledger as one "session").

Task types for `routing.rules` are the `TaskType` values in
`nvh/providers/base.py`: `code_generation`, `code_review`, `code_debug`,
`reasoning`, `math`, `creative_writing`, `summarization`, `translation`,
`conversation`, `question_answering`, `structured_extraction`, `multimodal`
and `long_context_analysis`. `nvh status --routing` shows how the last query
was classified and why it went where it went.

## Model catalog

The model catalog the router, `nvh model list` and `/v1/models` read is
assembled when the registry loads, from three sources in this precedence:

1. **An explicit override in `nvh/config/capabilities.yaml`.** A row may pin
   any LiteLLM-read fact (context window, max output, the tool / vision /
   JSON / system-prompt flags, prices, deprecation date) and must then carry
   a `source:` string saying where the numbers come from. Rows without
   overrides carry no numbers at all.
2. **LiteLLM's model table** (`litellm.model_cost`), with
   `nvh/config/model_overrides.json` registered into it at load for the
   provider default and fallback ids LiteLLM does not carry. Each payload has
   its own `source`; it is registered only while the id is absent from
   LiteLLM's table, so LiteLLM wins the moment a release adds the row.
   The table is the one bundled with the installed LiteLLM: importing `nvh`
   exports `LITELLM_LOCAL_MODEL_COST_MAP=True` unless the variable is already
   set, so no price map is fetched from GitHub at import and the catalog,
   the bill and the weekly model-currency report describe the same table
   (prices move with a LiteLLM release; the weekly job reports the drift).
   Export the variable as `False` before importing `nvh` to use the live map.
3. `ModelInfo` defaults.

`capabilities.yaml` owns the hand-maintained half: `provider`,
`display_name`, `capability_scores` (0–1 per task type),
`typical_latency_ms` and, for a model its provider has renamed,
`status: retired` plus `successor`. Price, context and tool flags are read
from LiteLLM every time the catalog loads and are never written into the
file, so a LiteLLM release cannot change what is committed. Two things are
derived rather than typed and need no `source:`: retirement
(`RETIRED_MODEL_RENAMES` in `nvh/cli/setup.py`) and `supports_vision` on
the `ollama/<tag>` rows (the VRAM tier table in `nvh/core/local_models.py`,
whose tags the rows must match one-for-one). A zero-cost provider's rows
(Ollama, LLM7, SiliconFlow) are `$0` by the spec flag alone and carry no
price in the file. `python scripts/gen_capabilities.py` rewrites the file in
canonical form and `--check` (run by `tests/test_capabilities_catalog.py`)
fails when the committed file is not canonical; the `# Generated against
litellm <version>` header line is informational and not compared. A row
that no source prices loads with `cost_known: false`: `nvh model list`
prints `cost unknown` instead of `$0`, the router scores it as neutral
rather than free, and the first request billed at the catalog rate or at
`$0` logs once per model per process. Every API envelope that carries
`cost_usd` carries `cost_known` beside it, and the WebUI's cost pill shows
`cost unknown` for `false`: a non-zero bill, a cache hit and a zero-cost
provider's `$0` are known, a priced model's `$0` only when the provider
metered the usage, and a council in which nothing was priced is unknown.
Hugging Face's rows are `cost unknown`
by design (`ProviderSpec.metered_upstream`): LiteLLM bills Inference
Providers at `$0` and the upstream provider's rate lands on the HF account.
A context window of `0` means unknown and is shown as `—`.

Retired rows stay in the file (the router and `nvh model list` still see
them, with `retired → <successor>` in the Status column) but are hidden
from `GET /v1/models` and the OpenAI-compatible model listing. `/v1/models`
also intersects each provider's catalog rows with the provider's live
`GET /models` list when a key is configured; a provider whose live list is
unknown (no key, no models endpoint, a probe error, an empty body, or a paused
Atohi lease) is passed through unfiltered, never hidden, and the response's
`warnings` names every configured default model the provider's live list
lacks. The live list is cached for five minutes per provider (thirty seconds
after a failure) and shared with the provider health probe, so health plus
listing is one request.

`nvh status --deep` (alias `nvh doctor`) makes **one network call per
configured cloud provider**: the `Default models` check reuses the
`GET /models` probe the health check just made and warns when a configured
(or spec) default model is not in the live list, with the fix text pointing
at `nvh model list` and the stanza's `default_model`. The `Advisors` health
check (`nvh status`, `--providers`, `--deep`) already made that call before;
`--deep` adds no second request per provider.

A weekly GitHub Actions workflow, `.github/workflows/model-currency.yml`
(Mondays, also `workflow_dispatch`), checks that the providers still serve
the catalog's and the specs' model ids and how far LiteLLM's table has moved
from the explicit overrides. It installs the latest LiteLLM, runs the
`network`-marked `tests/test_model_currency.py` with `NVH_NETWORK_TESTS=1`,
checks every VRAM-tier Ollama tag against the registry, prints an
informational drift report (each `source:` override beside today's LiteLLM
value, each `model_overrides.json` payload LiteLLM now carries, and each
active row that no source prices any more, which is how a LiteLLM release
that drops a row is seen without failing CI), and runs
`scripts/gen_capabilities.py --check`. The secret-free job needs no keys
(LLM7's anonymous models endpoint); the keyed job reads only `GROQ_API_KEY`
and `NVIDIA_API_KEY` (free, revocable keys), set on its test step alone so
the install steps never see them, and skips every step silently when either
repository secret is missing. Both jobs are
`continue-on-error`: the workflow informs, it never blocks a merge. See
[TESTING.md](TESTING.md#catalog-and-model-currency) for running the same
checks locally.

## Secrets

API keys are resolved in order: the stanza's `api_key` (usually `${VAR}`),
then `COUNCIL_<NAME>_API_KEY` and `<NAME>_API_KEY` in the environment, then
the provider's aliases, then the OS keyring — only when `NVH_USE_KEYRING=1`,
because headless boxes often have a slow or absent SecretService. Every
provider accepts the historical `HIVE_<NAME>_API_KEY`; the provider-native
aliases are `XAI_API_KEY` (grok), `GEMINI_API_KEY` (google), `CO_API_KEY`
(cohere), `TOGETHERAI_API_KEY` (together), `FIREWORKS_AI_API_KEY` (fireworks),
`PERPLEXITYAI_API_KEY` (perplexity), `HF_TOKEN` / `HUGGINGFACE_API_KEY`
(huggingface) and `NIM_API_KEY` (nvidia). `nvh setup`, `nvh advisor add` and the
Wizard write keys to the `.env` file under `$NVH_HOME/config`; `nvh advisor
remove` scrubs it and disables the stanza.
`.env.example` at the repo root is a template. Snapshots never bundle
`config.yaml` or `.env`.

## `NVH_HOME` layout

`NVH_HOME` (alias `NVHIVE_HOME`) is the only root. Unset, it defaults to
`~/.nvh`, which is fine on a laptop and wrong on an ephemeral VM — the
installers (`install.sh`, `install-mac.sh`, `install.ps1`) pick the persistent
volume and export it from your shell profile (or, on Windows, your user
environment) and from `$NVH_HOME/nvh-env.sh`. Each component directory has an
override so one large tree can be split across mounts; `nvh status --deep
--storage` prints the active layout with a real write probe and free-space
check. Every per-user file nvHive touches — config, keys, the state database,
schedules, workflows, plugins, the routing explanation — lives under one of
these directories: `storage_layout()` in `nvh/integrations/workspace/storage.py`
is the single path oracle and `tests/test_single_root.py` fails on any new
hard-coded home path. Two of these stores hold records that other tools may
import later: the vault's memory notes (the REPL's `/remember` notes among
them — ordinary notes under the `Wizard Memory` category tagged `#repl`;
`state/memory/memories.json` is only the pre-0.42 file the legacy migration
copies in once and the REPL turns into those notes once), and
`state/schedules.json` (scheduled tasks). Their record ids and on-disk
schema are documented below (`nvh-memory/1`, `nvh-schedules/2`), together
with the `wizard-meta` tail the Wizard chat appends to the assistant rows it
persists in `state/nvhive.db`.

| Directory | Override | Holds |
|---|---|---|
| `$NVH_HOME/` | `NVH_HOME` | `nvh-env.sh`, `venv/`, `repo/`, `uninstall.sh` |
| `bin/` | `NVH_BIN` | rootless Ollama, `nvhive-ai-studio`, `nvhive-openclaw` and other launchers |
| `models/`, `models/ollama/` | `NVH_MODELS`, `OLLAMA_MODELS` | local model weights |
| `cache/` | `NVH_CACHE` | `pip/`, `uv/`, `huggingface/`, `torch/`, `tmp/`, `catalog/`, `xdg/` (also exported as `PIP_CACHE_DIR`, `UV_CACHE_DIR`, `HF_HOME`, `HUGGINGFACE_HUB_CACHE`, `TORCH_HOME`, `TMPDIR`, `XDG_CACHE_HOME`); `huggingface/` is also where `nvh voice --download` puts the faster-whisper weights ([Voice](#voice-from-the-wizard-chat)) |
| `logs/` | `NVH_LOGS` | `install.log`, `api-server.log`, `nvhive.log`, service logs |
| `config/` | `NVH_CONFIG` (legacy alias `HIVE_CONFIG_HOME`, still honoured and still exported) | `config.yaml`, `.env`, `user.json`, `global_context.md`, `workflows/`, `mcp-servers.json` |
| `state/` | `NVH_STATE` | `nvhive.db` (SQLite: conversations, routing outcomes, costs), `schedules.json` (`nvh-schedules/2`; beside it a transient `schedules.json.<pid>-<token>.tmp` while a save is in flight and, after a parse failure, a quarantined `schedules.json.corrupt-<timestamp>`), `scheduler.lock` (the schedule runner's heartbeat lock: a JSON file `{pid, started_at, heartbeat, poll_s, runner}` created exclusively by the API lifespan or `nvh schedule start`, rewritten every poll and stale only once its heartbeat is older than three polls — see [Schedule records](#schedule-records-nvh-schedules2)), `last_query.json` (`nvh status --routing`), `memory/` (holds only the pre-0.42 `memories.json` the legacy migration copies in once; `nvh rag import-legacy --memories` or the REPL turns it into `Wizard Memory` notes once, `Source: import`), `legacy-migration.json`, capability marker, browser profiles |
| `plugins/` | `NVH_PLUGINS` | drop-in `.py` plugins (`nvh plugins`) |
| `runtimes/` | `NVH_RUNTIME_HOME` | micromamba and other runtime fallbacks |
| `apps/` | `NVH_APPS_HOME` | rootless Firefox, Obsidian, AppImages |
| `webui/` | `NVH_WEB_HOME` | WebUI source, build and the auto-installed Node |
| `studio/` | `NVH_STUDIO_HOME` | studio packs |
| `comfyui/` | `COMFYUI_HOME` | ComfyUI environment and nvHive examples |
| `projects/` | `NVH_PROJECTS` | agent workspaces |
| `outputs/` | `NVH_OUTPUTS` | generated images under `images/<scope>/` — `<scope>` is `wizard` for the chat's `generate_image` and `avatars` for profile portraits, each file `<UTC timestamp>-<8 hex>.png` (or `.jpg` / `.webp`), served only through the authenticated `GET /v1/outputs/images/{scope}/{filename}`; nothing prunes them in 0.46 — and other artefacts (`benchmark_results.md`) |
| `backups/` | `NVH_BACKUPS` | config backups |
| `support/` | `NVH_SUPPORT` | redacted support bundles from `nvh status --report` |
| `catalog/` | `NVH_CATALOG` | cached setup catalog |
| `vault/`, `rag/`, `jobs/`, `agent-profiles/` | — | Memory Vault notes (one Markdown file per record, below), the RAG store, background-job metadata and logs (install jobs and, since 0.46, the `schedule-run` jobs of each schedule's last 20 runs), custom agent profiles |

### Memory records (`nvh-memory/1`)

A vault memory note is one Markdown file under
`$NVH_HOME/vault/<Category>/<YYYY-MM-DDTHHMMSSZ0000>-<slug>[-<id8>].md`; the
file is the record and the folder tree is the store (Obsidian-readable,
RAG-indexable). `append_vault_memory` (`nvh/integrations/workspace/vault.py`,
behind `POST /v1/vault/memory`, the REPL's `/remember`, the Wizard's `remember`
tool, the legacy import and the privileged-change audit) writes, in this order: the `# <title>` heading, a
blank line, a header paragraph of `Key: value` lines — `Id:`, `Schema:
nvh-memory/1`, `Created:`, `Category:`, `Tags:` (`#a #b`, or `none`),
`Source:` — a blank line and the body. Readers ignore header keys they do not
know. The `-<id8>` suffix (the first eight hex characters of the id) is
appended only when that file name already exists, so two notes saved with the
same title in the same second no longer overwrite each other, and the file is
written to a temporary name and moved into place. The title and body pass
through `redact_secrets` (`nvh/core/agent_guardrails.py`) before the write — a
best-effort pattern pass; the result reports how many markers it inserted as
`redacted`, and a body that is nothing but a secret is refused with `nothing
to remember after redacting secrets`. Nothing is migrated in place: a pre-0.46
note keeps its name and body and reads as `schema: legacy` with a derived id
until `nvh vault upgrade --add-ids` inserts its `Id:` (the same derived id, so
links keep resolving), `Schema: nvh-memory/1` and `Source:` lines (`repl` for a
`#repl`-tagged note, else `api`) into the header paragraph — the body byte for
byte and the file's own line endings untouched, written through a temporary
name; `--dry-run` writes nothing and the pass is idempotent;
a seeded note, or any note without a header paragraph (or with a lone
`Key: value` line — a record's header carries at least two of the known
keys), reads as `doc`: `list_vault_notes` returns it by default beside the
records and drops it with `memory_only=True`. The table is generated from the
`MemoryRecord` dataclass by `scripts/gen_config_doc.py`.

Every record is listed by `GET /v1/vault/notes` (headers only, newest first;
filters `category`, `tag`, `q`, `memory_only` and `limit`, 1 to 500) and
`nvh vault list`, read in full by `GET /v1/vault/notes/{id}`, and removed by
`DELETE /v1/vault/notes/{id}` or `nvh vault forget <id>`, which also drop the
note's RAG chunks best-effort (`forgotten`, `chunks_removed`); the three routes
read the process home only — unlike `POST /v1/vault/memory` they take no
`home_dir` — and match ids against the notes' own `Id:` lines, never as paths.
`POST /v1/vault/memory` indexes the note it wrote into the RAG `vault`
collection best-effort and reports `indexed` beside `id`, `schema`, `source`
and `redacted`; the Wizard's `remember` tool does the same from a confirm card
([Questions, memories and images from the Wizard chat](#questions-memories-and-images-from-the-wizard-chat)).

<!-- BEGIN GENERATED: memory-record -->
| Field | Type | Meaning |
|---|---|---|
| `id` | `str` | Stable record id written as `Id:`: `uuid4().hex` (32 hex characters); a note without an `Id:` line derives `sha1(path relative to vault/)[:32]`, which is stable until the file moves. |
| `schema` | `str` | `nvh-memory/1` for a record written from 0.46 on, `legacy` for a pre-0.46 `append_vault_memory` note (a `Created:`/`Category:`/`Tags:` paragraph but no `Id:`/`Schema:` lines), `doc` for a seeded YAML-frontmatter note or any note without a meta paragraph, which is documentation rather than a record. |
| `title` | `str` | The first `# ` heading, always one line: internal newlines are collapsed to spaces on write (the API caps it at 120 characters). |
| `created` | `str` | ISO-8601 UTC timestamp with seconds (`+00:00`) from the `Created:` line; empty for a `doc` note. |
| `category` | `str` | The vault folder the note lives in, one of `VAULT_DIRS`, or empty for a note at the vault root (the seeded `README.md` and `MAP.md`); an unknown category falls back to `Wizard Memory` on write. |
| `tags` | `list[str]` | Lower-case slugs from the `Tags:` line (`#tag` tokens; `none` means no tags) or from a `doc` note's frontmatter `tags:` entry. |
| `source` | `str` | The writer of the record: `wizard`, `repl`, `web`, `api`, `import` or `audit`; `api` when a record has no `Source:` line, empty for a `doc` note. |
| `body` | `str` | Everything after the header paragraphs, stripped; empty in `list_vault_notes` results, which read headers only (and include `doc` notes unless `memory_only=True`). |
| `path` | `str` | Absolute path of the Markdown file, taken from the filesystem and never serialised into the note. |
<!-- END GENERATED: memory-record -->

### Schedule records (`nvh-schedules/2`)

`state/schedules.json` is one JSON object, `{"schema": "nvh-schedules/2",
"updated_at": "<ISO-8601>", "schedules": [record, ...]}`
(`nvh.core.scheduler.SCHEDULE_SCHEMA`). A pre-0.46 file — a bare JSON array
of records — still loads and is rewritten in the v2 shape on the next save,
never on load; a record's unknown keys round-trip under `extra`, and a missing
key takes the field's default: a record with no `id` reads with a derived
32-hex id (a hash of the record, the same on every load until the first save
writes it), one with no `interval_seconds` falls back to one day, and a
record missing its `prompt` or `interval_seconds` loads disabled, so a
half-written entry is listed but never due. A file that cannot be
parsed is moved aside once to `schedules.json.corrupt-<YYYYmmddHHMMSS>` with
one log line and the store starts empty, never silently zeroed. Saves write
a private `schedules.json.<pid>-<token>.tmp` and `os.replace` it over the file
(sorted keys), retrying briefly when Windows reports the file open in another
process; the temp file is removed on every failure. New ids are
`uuid4().hex`; legacy 8-character ids are kept verbatim. `last_status` is one
of `ok`, `error`, `skipped` or `deferred` (`RUN_STATUSES`), `last_error` is
cut to 300 characters (`MAX_ERROR_CHARS`), and `last_cost_usd` /
`last_cost_known` are `null` on a schedule that has never run. The table is
generated from the `ScheduledTask` dataclass.

Runs are jobs. While the API is up, its lifespan runs the due schedules
in-process (`nvh/integrations/services/schedule_runner.py`, polling every
`NVH_SCHEDULER_POLL_S` seconds, 60 by default; `NVH_SCHEDULER=0` keeps the
loop from starting, and `nvh serve --reload` sets that for its dev server,
whose worker is killed on every saved file), and `nvh schedule start` is the
same runner for a box without the API: with a live lock held it
prints ``the API server is running schedules; use `nvh schedule list` `` and
exits 0, while `--once` beside that live lock runs only the due `mode: do`
schedules (the server runs the other modes within its poll and never `do`),
and `nvh schedule run <id>` on an `ask` or `convene` schedule defers the same
way (Run now on the Schedules page, `POST /v1/schedules/{id}/run`, runs it in
the server's process). One runner per store: `state/scheduler.lock` is a JSON
file `{pid, started_at, heartbeat, poll_s, runner}` created exclusively and
rewritten every poll — and, from a side task, every `poll_s` while a run is
in flight, so a long unattended turn never lets the lock read stale — stale
and open to takeover only when its heartbeat is older than three times the
holder's `poll_s`; every beat re-reads the file first and a runner that finds
another pid in it stands down instead of overwriting the new holder's record;
a lock that exists but cannot be read counts as held, never stale (the doctor
row then reads `runner: unknown (lock unreadable)`); and the API's loop waits
for a lock another runner holds and takes over polling the moment it is
released, so a server started beside `nvh schedule start` is never left
without a scheduler. There is no pid probe, so `pid` and `runner` (`api` or
`cli`) are informational, shown by `GET /v1/schedules` (`runner`) and by the
doctor's `Scheduler` row, which reads `N task(s) · runner: api|cli|none` and
turns `warn`, naming the file, when a `schedules.json.corrupt-<timestamp>`
sibling exists. One run per schedule at a time: Run now on a schedule whose
run is still in flight answers 409 naming that job, the poll skips it, and
the poll leaves a due `mode: do` schedule alone for `nvh schedule start`
(failing it every interval would advance `next_run` and take the slot the
CLI runner needs). Inside the API process every write to the store — a
route's create, patch or delete and a run's `mark_run` — holds one lock
(`nvh.core.scheduler.STORE_LOCK`), so a Disable landing as a run finishes
loses neither record. Each run is one `schedule-run` job under `$NVH_HOME/jobs/`
(`request.{schedule_id, mode, advisor, profile, conversation_id}`; `GET
/v1/schedules/{id}/runs`, or `GET /v1/jobs?kind=schedule-run&schedule_id=`);
the runner keeps the last 20 runs per schedule (`SCHEDULE_RUN_HISTORY`) and
deletes older `.json` + `.jsonl` pairs right after `mark_run`, and the Setup
page asks `GET /v1/jobs?exclude_kind=schedule-run` so runs never crowd out
install jobs.

`mode: ask` is an unattended Wizard turn (`wizard_chat(..., unattended=True)`)
in the schedule's one conversation (`mode: wizard` in the sidebar): no question
card — `ask_user` is never offered to it — no approval token, and the tools it
may run are the profile's `tools_allowed` intersected with `SCHEDULE_AUTO_TOOLS`
(`nvh/integrations/wizard/chat.py`; the roster itself when the profile has no
whitelist). The roster is derived, not typed — the Wizard's core auto tools
minus the two that invalidate the local probe (`refresh_models`,
`repair_workspace`) plus the read-only lookups and dry runs — and today reads
`diagnose`, `playbook_list`, `playbook_plan`, `rag_ask`, `rag_ask_vault`,
`system_settings_get`, `system_settings_plan` and `web_search`, every member
auto-class (`GET /v1/schedules` lists it under `runner.tools`); any other auto
tool is refused with `not_allowed: true`, and a confirm or privileged request
for a tool the profile's own whitelist allows (any tool, for a profile without
one) is recorded on the run as `{name, arguments, privileged, unattended:
true}` — no plan, no token, every string argument through `redact_secrets` —
after which the run's `last_status` is `deferred`; a request for a tool
outside the whitelist is refused like any other call, so a strict-tools or
user-pinned profile keeps its whitelist on a timer exactly as with a human
present. The schedule's `advisor` is the turn's provider pin, applied like a
profile's own `provider` (an unavailable provider keeps the router's choice;
a `local-only` profile keeps its own pin). `mode: convene` convenes the
council (`advisor` names the one member when set; the synthesis is the
answer). `mode: do` is refused by the API runner — a
failed job whose message is
"mode 'do' runs tools unattended and cannot show an approval card; use mode ask (the Wizard runs safe tools on its own) or `nvh schedule start` in a terminal",
`last_status: error` — and runs only under `nvh schedule start`, which warns at
`nvh schedule add --mode do`.

`last_status` is `ok`, `deferred` (the turn asked for a confirm or privileged
tool; the request sits on the run record and is approved by asking again in
the chat), `skipped` (the budget's `hard_stop` tripped before the run; the job
completes with `outcome: skipped`) or `error` (`last_error`, cut to 300
characters — including a turn no model answered: when the Wizard fell back to
its offline helper because the provider was down or a key had expired, the run
is `error` with the `fallback_reason`, never an `ok` carrying the helper's
canned text), each with the run's cost and whether it is trusted; a run in
flight when the server restarts is marked `interrupted` with `Server restarted
or disconnected while this run was in flight; the schedule will run again at
its next interval.`, and one the server cancelled while stopping is `canceled`
with `Run canceled before it finished (the server stopped); the schedule will
run again at its next interval.` (the schedule itself records `error`, `run
canceled before it finished`).

<!-- BEGIN GENERATED: schedule-record -->
| Field | Type | Default | Meaning |
|---|---|---|---|
| `id` | `str` | — | Record id; new records get `uuid4().hex` (32 hex characters), legacy 8-character ids are kept verbatim. |
| `prompt` | `str` | — | The prompt each run sends. |
| `interval_seconds` | `int` | — | Seconds between runs; the CLI accepts `parse_interval` syntax such as `30m`, `1h` or `1d`. |
| `advisor` | `str` | — | Provider override for the run; empty means normal routing. |
| `mode` | `str` | — | `ask`, `convene` or `do`; `do` runs tools unattended and is accepted by the CLI runner only. |
| `last_run` | `str` | — | ISO-8601 UTC timestamp of the last run; empty before the first run. |
| `next_run` | `float` | — | Epoch seconds at which the schedule is next due. |
| `enabled` | `bool` | — | Whether the schedule can become due; disabled schedules never run. |
| `created_at` | `str` | — | ISO-8601 UTC timestamp of when the schedule was added. |
| `name` | `str` | `""` | Display label shown beside the prompt. |
| `profile` | `str` | `""` | Agent profile applied to each run; its `max_cost_usd_per_turn` caps the run and, on an unattended `ask` run, its tool whitelist is intersected with `SCHEDULE_AUTO_TOOLS` (`nvh/integrations/wizard/chat.py`), the read-only roster a scheduled turn may call. |
| `conversation_id` | `str` | `""` | The one conversation every run of this schedule appends to. |
| `updated_at` | `str` | `""` | ISO-8601 UTC timestamp of the last field change or recorded run; empty for records written before 0.46. |
| `last_status` | `str` | `""` | Outcome of the last run: `ok`, `error`, `skipped` or `deferred`; empty before the first run. |
| `last_error` | `str` | `""` | Error text of the last run, truncated to 300 characters; empty when the last run did not fail. |
| `last_job_id` | `str` | `""` | Id of the last run's job under `$NVH_HOME/jobs/`. |
| `last_cost_usd` | `float \| None` | `null` | Cost of the last run in USD; `null` means never run or no figure, and the UI shows nothing for it. |
| `last_cost_known` | `bool \| None` | `null` | Whether `last_cost_usd` is a trusted figure; `null` together with a `null` cost on a never-run schedule. |
| `run_count` | `int` | `0` | Number of runs recorded, whatever their status. |
| `extra` | `dict[str, Any]` | `{}` | Unknown keys, round-tripped. |
<!-- END GENERATED: schedule-record -->

### Wizard chat rows (`wizard-meta`)

Every assistant row the Wizard chat persists to the `conversation_messages`
table in `state/nvhive.db` ends with `\n\n<!-- wizard-meta: {json} -->`,
written only by `_persist_wizard_turn` (`nvh/integrations/wizard/chat.py`);
the row's own `input_tokens`, `output_tokens`, `cost_usd` and `latency_ms`
columns are filled from the turn's meter (user rows keep the defaults), so the
tail is no longer the only carrier of cost. The tail carries `v: 1`
(`WIZARD_META_VERSION`) and readers ignore keys they do not know
(`web/lib/wizardMeta.ts`). `source`, `v`, `used_profile`, `profile_reason`,
`iterations`, `tool_calls`, `tool_results` and `deferred_tool_calls` are on
every tail; `cost_usd`, `cost_known` and `latency_ms` only on turns a model
answered; `mode` and `fallback_reason` only on deterministic turns;
`streamed: true` only on turns served by the streaming endpoint. The table is
generated from `WIZARD_META_KEYS`.

<!-- BEGIN GENERATED: wizard-meta -->
| Key | Meaning |
|---|---|
| `source` | Always `"wizard"`: the Wizard chat is the only writer of this tail. |
| `v` | Schema version of this tail; currently `1`. |
| `used_profile` | Name of the agent profile that answered, or `null` for the general Wizard (a deterministic answer names one only when a pinned specialist itself declined). |
| `profile_reason` | Why the concierge chose or demoted that profile; `null` when the profile was pinned explicitly and no selection ran. |
| `mode` | Deterministic turns only: `"deterministic"` — no model answered (offline helper, local-only refusal of a pin, or a failed LLM path). |
| `fallback_reason` | Deterministic turns only: the LLM error text, `"engine not initialized"` or the local-only refusal reason. |
| `iterations` | LLM round-trips attempted in the turn (`0` when no model was called). |
| `tool_calls` | Confirm-class calls surfaced to the user as cards, without their `approval_token` and `approval_expires_at`; a confirm card whose tool has a planner carries its `plan` and the server-pinned `arguments.profile` (plus `arguments.profile_home` when the turn had a `home_dir`); on an unattended turn each carries `unattended: true`, no plan, redacted string arguments, and only a tool the profile's whitelist allows is recorded. |
| `tool_results` | Outcomes of the auto-class tools the server ran, plus whitelist refusals (`not_allowed: true`). |
| `deferred_tool_calls` | Calls the loop did not run, each with a `reason` (follow-up disabled, iteration cap, cost ceiling, question pending — a second `ask_user` in one turn, or any confirm or privileged call made in the same turn as a question). |
| `cost_usd` | LLM-answered turns only: the turn's completions summed, in USD (`0` when the provider reported no cost). |
| `cost_known` | LLM-answered turns only: whether `cost_usd` is a price (the catalog knows the model) or a placeholder the UI shows as "cost unknown". |
| `latency_ms` | LLM-answered turns only: wall-clock milliseconds summed over the turn's completions. |
| `streamed` | Streamed turns only: `true` when the streaming endpoint served the turn; absent otherwise. |
<!-- END GENERATED: wizard-meta -->

### Legacy homes (pre-0.44)

Before 0.44 files were spread over `~/.hive` (config, keys, plugins,
workflows, schedules, REPL memories, the pre-0.42 knowledge store),
`~/.council` (the state database) and `~/nvh` (the early installers' root).
The first `nvh` command after an upgrade — key loading, `nvh config init`, a
`load_config()` that finds no user config while a legacy one exists, or the
first database open — copies whatever it finds into the layout **once**:
legacy user files and database contents remain intact, nothing already under
`NVH_HOME` is overwritten, and `$NVH_STATE/legacy-migration.json` records what
moved. Two files are *merged* rather than skipped, because 0.43 kept both
copies live (`nvh setup` wrote `~/.hive/.env` and `~/.hive/config.yaml`, the
web wizard wrote the layout's): `KEY=VALUE` lines the layout `.env` lacks are
appended to it, and provider stanzas / sections the layout `config.yaml`
lacks are added (the layout copy wins every conflict; a `.pre-0.44-merge`
backup is left beside it). Symlinks are copied as links except for the state
database, which becomes an independent SQLite snapshot including committed WAL
data. The database is opened read-only; live WAL/SHM files are never copied.
SQLite may perform its normal WAL/read-lock bookkeeping while reading.
Backup checks a five-second deadline between SQLite steps and reports a timeout
for retry when the database remains busy.
The complete snapshot is published without replacing an existing file; a
filesystem without hard-link support or an existing destination sidecar causes
a reported failure and leaves the old database intact. A copy that fails
is reported and retried on the next command instead of being recorded as
done. If the state database import fails, opening the default database stops
instead of creating an empty replacement that would prevent a later retry.
Cooperating migrations serialize on a persistent SQLite lock under
`state/.legacy-migration-lock.sqlite3`, with a five-second lock-contention
timeout. Do not delete this lock file while migrations may be running. A lock
failure prevents default database initialization from creating an empty database.
This is not a deadline for arbitrary filesystem I/O. After acquiring the lock,
the importer rechecks the marker and every destination.

Other files, directories and links are copied into private staging beside their
destination and published atomically without replacing any existing entry,
including an empty directory or a dangling link. Windows uses no-replace rename;
Linux requires `renameat2(RENAME_NOREPLACE)` and macOS requires
`renamex_np(RENAME_EXCL)`. An unavailable operation/filesystem fails safely for
retry, without falling back to an overwriting rename. Only staging owned by the
current copy is cleaned up; an abrupt process exit may leave private staging
behind, which later migrations leave untouched. Process exit releases the lock;
the completed marker is published atomically while holding it. This does not
promise power-loss durability or synchronize unrelated applications/user edits
to existing `.env`/YAML files during their deliberate merge.

Stores a one-shot importer already consumed (`nvh rag import-legacy`,
the REPL memory import) are skipped. The old roots can be deleted once you
have checked the copy. `NVH_LEGACY_MIGRATION=0` turns the whole thing off.
`HIVE_DATA_DIR` is no longer read — export `NVH_STATE=<dir>/state` instead
(the migration warns while it is still set). `nvh snapshot` reads the
database from wherever `NVH_STATE` puts it.

`nvh status --deep` (alias `nvh doctor`) shows the import's state as the
**Legacy homes (pre-0.44)** row (`legacy_homes`, next to the legacy
knowledge-base row). The row is read-only: it reads
`$NVH_STATE/legacy-migration.json`, checks which old roots still exist and
lists what a run would still copy, and it never runs the import itself, so a
diagnostic cannot start a second migration. A recorded import passes and
reports how many items were copied, merged and skipped, naming the old roots
while they are still present so you know they can be removed once you have
checked the copy. Items the import would still copy warn with their count and
the command that runs it (`nvh config init`, or any command that loads keys).
An item the import could not copy when this command's own startup ran it
warns with the item and the reason; the import retries on the next command
and records itself only when every item copies. `NVH_LEGACY_MIGRATION=0`
shows as a skipped row that names the switch, and a still-set `HIVE_DATA_DIR`
warns with the `NVH_STATE` replacement. On a fresh install with no old roots
and no marker the row is absent.

## Environment variables

Storage overrides are in the table above. Everything else nvHive reads:

| Variable | Default | Effect |
|---|---|---|
| `HIVE_API_KEY` | unset | key for the API, accepted as `Authorization: Bearer <key>`, `X-Hive-API-Key: <key>` or `x-api-key: <key>` (the header the Anthropic SDKs send for `ANTHROPIC_API_KEY`); unset and no users = open/local mode. Required before `nvh serve --host 0.0.0.0` |
| `HIVE_CORS_ORIGINS` | WebUI defaults | comma-separated extra origins for the API |
| `HIVE_LOG_LEVEL`, `HIVE_LOG_FORMAT`, `HIVE_LOG_FILE` | `INFO`, `text`, `$NVH_HOME/logs/nvhive.log` | API logging; `json` for structured logs |
| `HIVE_PROFILE` | unset | config profile to apply when `--profile` is not given |
| `NVH_USE_KEYRING` | `0` | consult the OS keyring for keys |
| `NVH_API_PORT`, `NVH_WEB_PORT` | `8000`, `3000` | ports the service registry and WebUI expect |
| `NEXT_PUBLIC_API_URL` | `http://localhost:8000` | where the browser reaches the API (WebUI build/dev) |
| `NVH_DB_TIMEOUT` | `5` | SQLite busy timeout in seconds |
| `NVH_VERBOSE` | unset | verbose WebUI bootstrap output |
| `OLLAMA_BASE_URL` / `OLLAMA_HOST` | `http://127.0.0.1:11434` | where to find Ollama. Precedence: `OLLAMA_URL` (RAG embedder only) → `OLLAMA_BASE_URL` → `OLLAMA_HOST` → the default; a bare `host:port` gets `http://`, and `localhost` / `0.0.0.0` are rewritten to `127.0.0.1` |
| `NVH_OLLAMA_BIN` | `$(which ollama)` | Ollama binary for `nvh services` and `nvh models` |
| `NVH_DEFAULT_OLLAMA_MODEL` | from config | model preloaded after boot |
| `NVH_OLLAMA_PRELOAD` | `1` | `0` skips the post-boot warm-up |
| `NVH_OLLAMA_START_WAIT` | `10` | seconds the engine waits for a starting Ollama |
| `NVH_OLLAMA_HTTP_TIMEOUT` | `0.5` | seconds for the engine's Ollama liveness probe |
| `NVH_OLLAMA_NUM_CTX` | unset (detected) | `num_ctx` the Ollama provider sends with every request. Unset: for a loopback `OLLAMA_BASE_URL` with a visible GPU, the detected VRAM tier's value from the [tier table](MODELS.md#local-models-by-vram); with no visible GPU, or a daemon on another host (non-loopback `OLLAMA_BASE_URL`), nothing is sent and Ollama's own default applies. A positive integer overrides all of that (still capped at the model's own context length); `0` sends no `num_ctx` |
| `NVH_OLLAMA_VERSION`, `NVH_OLLAMA_URL`, `NVH_OLLAMA_DOWNLOAD_BASE` | latest, unset, `https://ollama.com/download` | pin or redirect the rootless Ollama download |
| `NVH_OLLAMA_BOOT_TIMEOUT`, `NVH_API_BOOT_TIMEOUT`, `NVH_WEBUI_BOOT_TIMEOUT` | `15`, `20`, `30` | health-gate windows for `nvh services start` ([MAINTAINERS.md](MAINTAINERS.md#service-order)) |
| `NVH_BROWSER` | unset | browser command for `nvh webui`, e.g. `firefox --new-window {url}` |
| `NVH_FIREFOX_AUTO_INSTALL` | `1` | `0` disables the rootless Firefox fallback install |
| `NVH_FIREFOX_PROFILE` | `$NVH_STATE/browser-profiles/desktop` | isolated Firefox profile directory |
| `NVH_SANDBOX_REQUIRE_DOCKER` | unset | `1`/`true`/`yes`: refuse `run_code`/`shell` instead of falling back to an unisolated subprocess (`nvh do --sandbox` sets it). Applies to the CLI/agent tools and to the Wizard's `shell` card, whose plan then reads "refused — Docker is unavailable and isolation is required" and executes nothing. The Wizard's `run_code` ignores this variable because it always requires Docker ([Tools](#tools)) |
| `NVH_SANDBOX` | unset | pre-0.42 spelling of `NVH_SANDBOX_REQUIRE_DOCKER`, honoured for one more release; truthy fails closed the same way |
| `NVH_ALLOW_PRIVILEGED` | unset (on) | `0`/`false`/`no`/`off` disables the Wizard's `privileged` tools (`system_settings_apply`, `apt_install`, `snap_install`, `service_enable`, `playbook_install`, `shell`): they stay listed with `enabled: false` and every call is refused naming this variable. Privileged tools always need a click on a red card showing the exact commands, use `sudo -n` only where the once-per-process probe found passwordless sudo, otherwise hand the command to a terminal, and record every apply that touched the host (complete, partial or failed) under the vault's `Decisions/`. The card carries an approval token bound to that exact call (15 minutes, single use) that the confirmed call must return; a confirmed call is also refused when the API runs in open mode (no `HIVE_API_KEY`) on a non-loopback bind, or arrives with a `Host`/`Origin` that is neither loopback nor in `HIVE_CORS_ORIGINS`. nvHive never prompts for, sees or stores a sudo password. Spark playbooks whose steps need sudo ([GETTING_STARTED.md](GETTING_STARTED.md#spark-playbooks)) obey it through `playbook_install`; the CLI path `nvh playbook install <id>` is not gated — it runs in your own terminal, where `sudo` prompts you directly. The privileged tier is on by default on every platform where sudo can be probed; `NVH_ALLOW_PRIVILEGED=0` turns it off, and off Linux (Windows and macOS) device changes are refused with a plain message (0.45). |
| `NVH_BOOT_PREFLIGHT`, `NVH_BOOT_AUTO_REPAIR` | `1`, `1` | run the boot preflight at API start; let it apply safe repairs |
| `NVH_SCHEDULER` | unset (on) | `0` (or `false`, `no`, `off`) keeps the API server's lifespan from starting the schedule runner, so `nvh schedule start` is the only runner; `nvh serve --reload` sets it to `0` for the dev server unless you set it yourself, and tests set it so no `TestClient` lifespan writes `state/scheduler.lock`; see [Schedule records](#schedule-records-nvh-schedules2) |
| `NVH_SCHEDULER_POLL_S` | `60` | seconds between the API runner's polls for due schedules; the lock is stale after three of them, so a shorter poll also means a faster takeover after an unclean restart. Anything that is not a positive finite number falls back to `60` with one warning |
| `NVH_TARGET_VM_VALIDATED` | unset | `1` after the target-VM checklist; gates `production-ready` |
| `NVH_API_URL` | `http://127.0.0.1:8000` | API server the `nvh status --smoke` / `--report` probes exercise (`nvh test --api URL` sets it); requests carry `Authorization: Bearer $HIVE_API_KEY` when that is set |
| `NVH_SYNC_LEARNING_LOAD`, `NVH_LEARNING_LOAD_TIMEOUT` | `0`, `3` | `1` loads learned routing scores before the engine answers (waiting up to the timeout in seconds) instead of in the background |
| `NVH_TELEMETRY` | unset | `1` enables the opt-in, local-only install-health log at `$NVH_HOME/telemetry/events.jsonl`; nothing is ever sent ([PRIVACY.md](../PRIVACY.md)) |
| `NVH_PLATFORM_WARMUP` | unset (on) | `0` skips the one-time platform probe the API runs in the background at startup (cloud-metadata lookup, `sudo -n -k true` for sudo-group members); the Wizard then classifies the machine from local DMI / os-release signals only. Never runs on the chat path either way |
| `NVH_CATALOG_URL` | GitHub raw URL | remote setup catalog; the bundled copy is the fallback |
| `NVH_RAG_EMBED_MODEL` | `nomic-embed-text` | Ollama embedding model for the RAG store |
| `NVH_RAG_AUTO_PULL` | `1` | `0` stops the embedder pulling a missing embedding model on first use |
| `NVH_WIZARD_PLUGIN_DIR` | `$NVH_HOME/plugins/` | directory of `.py` files exposing `register(reg)` that add Wizard tools — the one plugins directory, shared with provider plugins (only files that declare `register` are executed by the Wizard; `$NVH_HOME/wizard-tools/` is still read for one more release) |
| `NVH_LEGACY_MIGRATION` | unset (on) | `0` disables the one-shot copy of the pre-0.44 `~/.hive` / `~/.council` / `~/nvh` files into `$NVH_HOME` (see [Legacy homes](#legacy-homes-pre-044)); `nvh status --deep` then shows the `legacy_homes` row as skipped |
| `NVH_WIZARD_AUTOFOLD_VAULT` | `1` | `0` stops the Wizard folding a strongly matching vault note into its system prompt |
| `NVH_SEARXNG_URL`, `BRAVE_API_KEY` | unset | backends for the one `web_search` implementation (the Wizard tool and the agent tool): SearXNG wins, then Brave, then key-free DuckDuckGo |
| `HASS_URL` | unset | Home Assistant instance the Wizard's `home_assistant_*` tools talk to (`HOME_ASSISTANT_URL` is accepted too). Required whenever `HASS_TOKEN` is set — no address is ever guessed, so the token is never sent to whichever host answers an mDNS name. Prefer `https://`; plain `http://` is accepted only for loopback, RFC 1918 / IPv6-ULA addresses and `.local` names, and `home_assistant_status` then reports `insecure_transport: true` |
| `HASS_TOKEN` | unset | Home Assistant long-lived access token (profile → Security → Long-lived access tokens); unset = the tools explain how to connect instead of calling out (`HOME_ASSISTANT_TOKEN` is accepted too) |
| `NVH_HASS_ALLOW_ADMIN` | unset | `home_assistant_call` is limited to device-control domains by default (`light`, `switch`, `fan`, `cover`, `climate`, `media_player`, `scene`, `vacuum`, `humidifier`, `water_heater`, `lock`, `input_boolean`/`input_number`/`input_select`, `number`, `select`, `button`, `notify`). `1` also allows every other domain (`script.*`, `automation.*`, `update.*`, …); `all` additionally allows the host-reaching surface — `hassio.*`, `shell_command.*`, `python_script.*`, `homeassistant.restart`/`stop` — which `1` still refuses |
| `NVAPI_KEY` | unset | NVIDIA-hosted image generation (`generate_image` from the Wizard chat, profile portraits); the only variable the image path reads — `NVIDIA_API_KEY` / `NIM_API_KEY` are never consulted, so a NIM chat key cannot move a box onto the paid hosted path ahead of local ComfyUI |
| `NVH_NVIDIA_IMAGE_ENDPOINT`, `NVH_NVIDIA_IMAGE_MODEL` | NVIDIA's hosted SDXL Turbo endpoint, unset | endpoint and model hosted generation uses; the card's plan line names `NVH_NVIDIA_IMAGE_MODEL` or, unset, the endpoint's last path segment, and reports the hosted price as unknown (`cost_usd: null`, `cost_known: false`) |
| `NVH_COMFYUI_CHECKPOINT` | `sd_xl_base_1.0.safetensors` | checkpoint the local ComfyUI workflow renders with (`generate_image` and profile portraits); ComfyUI is reached at the host and port recorded in `comfyui/comfyui-service.json` when nvHive started it, not assumed at the default port |
| `NVH_VOICE_STT_MODEL` | `small.en` | faster-whisper model the local speech-to-text path loads (`pip install 'nvhive[voice]'`; `nvh voice --stt-model` overrides it per run; read when the request or command runs, not at import). The weights live under `$NVH_CACHE/huggingface` (`storage_layout().cache_dir / "huggingface"`, the layout's own `HF_HOME` spelling — the API process exports no `HF_HOME`) and are fetched only by `nvh voice --download` or the WebUI's **Download model** button, never on first use. The `voice` extra is outside `all`, and on Windows on Arm it installs nothing — its requirement carries `sys_platform != 'win32' or platform_machine != 'ARM64'` because no faster-whisper wheel exists there — so `nvh voice --status` and `GET /v1/voice/status` report `no faster-whisper wheel for this platform` and the Groq and browser paths remain ([Voice](#voice-from-the-wizard-chat)) |
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
`$NVH_HOME/config/global_context.md`; `.nvh/context/*.md` and `.nvh/rules/*.md`
add modular files (a project's pre-0.44 `.hive/` directories still work). Optional
frontmatter: `name`, `scope` (`all`, `convene`, `query`, `code`) and
`priority` (0–100, higher first). `HIVE.md.example` at the repo root shows the
shape; `GET /v1/context` lists what is loaded and `POST /v1/context/reload`
re-reads it.

## Council cabinets

`nvh convene --cabinet <name>` (and the chat page's council **Advanced**
drawer, `POST /v1/council`, the MCP `council` tool) picks a preset panel from
`nvh.core.agents.COUNCIL_PRESETS`. `nvh agent presets` lists them live; `nvh
agent analyze "question" -n 5` previews the panel `--auto-agents` would
generate instead. The table is generated from `COUNCIL_PRESETS` and
`PRESET_DESCRIPTIONS` (the summary the MCP `list_cabinets` tool shows) by
`scripts/gen_config_doc.py`.

<!-- BEGIN GENERATED: council-cabinets -->
| Cabinet | Members | Summary |
|---|---|---|
| `executive` | CTO, CEO / Business Strategist, CFO / Financial Analyst, Product Manager | CTO, CEO, CFO, Product Manager — strategic decisions |
| `engineering` | Software Architect, Senior Backend Engineer, DevOps/SRE Engineer, Security Engineer, QA/Test Engineer | Architect, Backend, DevOps/SRE, Security, QA — technical decisions |
| `security_review` | Software Architect, DevOps/SRE Engineer, Security Engineer, Legal/Compliance Advisor | Security analysts — vulnerability assessment |
| `code_review` | Software Architect, Senior Backend Engineer, QA/Test Engineer, Performance Engineer | Senior engineers — code quality review |
| `product` | CEO / Business Strategist, Product Manager, UX Designer, Engineering Manager | CEO, Product Manager, UX Designer, Engineering Manager — product decisions |
| `product_resilience` | DevOps/SRE Engineer, ML/AI Engineer, Product Manager, UX Designer, Underdog Student Advocate, QA/Test Engineer | Student advocate, PM, UX, SRE, QA, ML — onboarding and failure-mode review |
| `data` | Software Architect, Database Administrator, Data Engineer, ML/AI Engineer | Architect, DBA, Data Engineer, ML/AI Engineer — data analysis |
| `full_board` | CTO, Software Architect, Senior Backend Engineer, DevOps/SRE Engineer, Security Engineer, CEO / Business Strategist, CFO / Financial Analyst | CTO, Architect, Backend, DevOps/SRE, Security, CEO, CFO — the widest panel |
| `homework_help` | Patient Tutor, Devil's Advocate, Study Coach | Tutors across subjects — student help |
| `code_tutor` | Code Mentor, Bug Hunter, Best Practices Reviewer | Programming mentors — learn to code |
| `essay_review` | Writing Coach, Logic Checker, Style Editor | Writing coaches — improve essays |
| `study_group` | Socratic Questioner, ELI5 Explainer, Practice Problem Generator | Study partners — exam prep |
| `exam_prep` | Exam Coach, Flashcard Creator, Weak Spot Finder | Test prep specialists — practice questions |
<!-- END GENERATED: council-cabinets -->

Strategies: `weighted_consensus` (default; `--weights groq=0.5,google=0.5`),
`majority_vote`, `best_of`. `--members` picks advisors explicitly,
`--no-synthesize` shows the raw responses. `product_resilience` is the
skeptical panel for "what breaks for a beginner on a no-root GPU desktop".

`majority_vote` counts answers that match after normalizing letter case and
whitespace; it does not infer that differently worded answers mean the same
thing. More than half the responses must match to be labelled a majority.
Otherwise the result says plurality or tie; configured weights break ties.
The same rule applies to streaming councils. For long, differently worded
analyses, use `weighted_consensus` or `best_of`.

The MCP `throwdown` tool and both compatible APIs run two council rounds:
independent analysis, then critique with the first round's answers in context,
followed by a final answer using both rounds. A failed quorum stops the next
stage. Each completed round is recorded before the next budget check.
Council totals include reported agreement-check, synthesis and intermediate
round usage; latency runs through the final answer, including retries.
Failed streams retain any reported usage, but a provider that fails before
reporting usage may still incur a charge that nvHive cannot measure.

## Tools

Tools are functions the model may call while answering. They are opt-in:
`nvh do "task"` and `nvh agent run "task"` use them, the REPL enables them
with `/tools`, and the AI Wizard has its own registry
(`nvh.integrations.wizard.tools.WizardToolRegistry`, plus any
`mcp_<server>_<tool>` attached through
[INTEGRATIONS.md](INTEGRATIONS.md#nvhive-as-an-mcp-client)). Every tool
carries one of three safety classes (`SAFETY_CLASSES` in `nvh/core/tools.py`):
**auto** runs unattended, **confirm** asks first (`nvh do --auto` approves
the auto class only), and **privileged** shows a red card with the exact
commands and obeys the `NVH_ALLOW_PRIVILEGED` kill switch in
[Environment variables](#environment-variables). The Wizard's privileged and
bridged tools are described under
[Code and images from the Wizard chat](#code-and-images-from-the-wizard-chat),
its `ask_user`, `remember` and `generate_image` tools under
[Questions, memories and images from the Wizard chat](#questions-memories-and-images-from-the-wizard-chat).
The registry is `nvh.core.tools.ToolRegistry`; the built-ins by area,
confirm-class marked `*`:

| Area | Tools |
|---|---|
| Files | `read_file`, `list_files`, `search_files`, `find_files`, `disk_usage`, `write_file`\*, `move_file`\*, `delete_file`\* |
| Code and shell | `run_code`\*, `shell`\* — Docker-isolated when Docker is present, otherwise an audited subprocess fallback that the result and the tool output flag; `NVH_SANDBOX_REQUIRE_DOCKER=1` refuses the fallback |
| Web | `web_search`, `web_fetch`, `http_request`, `browser_navigate`, `download`\*, `browser_screenshot`\*, `browser_fill_form`\* |
| System | `system_info`, `list_processes`, `pip_list`, `get_env`, `open`, `open_terminal`, `notify`, `get_clipboard`, `set_clipboard`, `docker_ps`, `kill_process`\*, `pip_install`\*, `set_env`\*, `docker_run`\* |
| Vision and desktop | `screenshot`, `capture_screenshot`, `analyze_image`, `read_text_from_image`, `imagine`\*, `mouse_move`\*, `mouse_click`\*, `keyboard_type`\*, `keyboard_press`\*, `scroll`\* |

Since 0.46 `imagine` is confirm-class (it bills an image key): `nvh do` and
the REPL's `/do` ask before it runs (an `Allow? [y/N]` prompt), while the
agent loops that have no confirmation channel — a `mode: do` schedule under
`nvh schedule start` and `/v1/smart` — answer `Tool requires approval:
imagine` instead of generating (a pre-0.46 `do` schedule that drew a daily
image now records that line; move it to the Wizard's `generate_image`, whose
card is the confirmation). Any confirm-class tool that has a planner now shows
its dry run on the card as `plan`; the pinned arguments and the approval token
stay a privileged-only affair.

Guardrails in `nvh/core/agent_guardrails.py` run before every call and cannot
be bypassed by `--yes`: shell commands are checked against a deny list, file
paths must stay inside the workspace, writes have a size cap, secrets are
redacted from outputs and long outputs are truncated. The deny list is applied
to the argument each tool actually executes — `command` for `shell`, `code` for
`run_code`.

The AI Wizard's registry starts from the stock set below (`default_registry()`
in `nvh/integrations/wizard/tools.py`; the table is generated from it by
`scripts/gen_config_doc.py`, in the order `list_tools()` returns: by class,
then by name). At run time the registry also gains packaged plugins
(`nvh.wizard_tools` entry points), `.py` plugins from `NVH_WIZARD_PLUGIN_DIR`
and the cached tools of attached MCP servers (`mcp_<server>_<tool>`,
confirm-class unless the server's `auto_approve` list names them); those
depend on the machine and are not listed.

<!-- BEGIN GENERATED: wizard-tools -->
| Tool | Class | What it does |
|---|---|---|
| `analyze_image` | auto | Look at an image the user attached (or a screenshot) and describe it or answer a question about it — contents, UI elements, charts, error dialogs. |
| `diagnose` | auto | Refresh and return the current diagnostic findings (GPU, storage, providers, models, runtime). |
| `home_assistant_entities` | auto | List Home Assistant entities (lights, switches, sensors, climate, ...) with their current state, optionally filtered by domain and/or a name search. |
| `home_assistant_services` | auto | List the services Home Assistant exposes, per domain. |
| `home_assistant_state` | auto | Read one Home Assistant entity's current state and attributes by exact entity_id. |
| `home_assistant_status` | auto | Check the connection to the user's Home Assistant instance: whether HASS_URL/HASS_TOKEN are set, whether the API answers, and its version. |
| `playbook_list` | auto | List the DGX Spark playbooks nvHive can run (Ollama, Open WebUI, ComfyUI, vLLM, llama.cpp, LM Studio, Tailscale, VS Code, DGX Dashboard, CLI coding agents, OpenClaw, NemoClaw): which steps need sudo, the manual steps, time and disk estimates, a rootless studio-pack alternative where one exists, and whether the install receipt says it is installed. |
| `playbook_plan` | auto | Dry run of one Spark playbook: the exact commands in order, which run with sudo, the manual steps (browser logins, TUIs, foreground servers), how to verify, how to undo, estimates and warnings. |
| `rag_ask` | auto | Search the local RAG index for chunks relevant to a question and return them with source citations. |
| `rag_ask_vault` | auto | Search the nvHive Vault (user's own Markdown notes) for chunks relevant to a question. |
| `read_text_from_image` | auto | Read all visible text out of an image (OCR via the vision model) — terminal screenshots, error messages, photographed labels or documents. |
| `refresh_models` | auto | Re-query the local Ollama daemon for installed models so the picker stays fresh. |
| `repair_workspace` | auto | Run the idempotent rootless safe-repair pass: env file, catalog cache, ComfyUI examples, model list, config validation. |
| `system_settings_get` | auto | Read-only facts about this machine: hostname, DGX OS / distro version, kernel, NVIDIA driver and CUDA version, whether sudo is passwordless, docker group membership, the GNOME auto-suspend setting, ufw status, Tailscale state and unattended-upgrades. |
| `system_settings_plan` | auto | Dry run of a catalogue system setting: the exact commands it would run, whether sudo is needed, what changes and how to undo it. |
| `validate_provider_key` | auto | Validate an API key against its provider's health endpoint. |
| `web_search` | auto | Run a web search via the active backend (SearXNG, Brave, or DuckDuckGo) and return top hits with title, URL, and snippet. |
| `ask_user` | confirm | Ask the user one clarifying question; the answer arrives on the next turn. |
| `generate_image` | confirm | Generate an image from a text prompt and save it under the workspace's outputs/images folder. |
| `home_assistant_call` | confirm | Call a Home Assistant service, e.g. light.turn_on on light.kitchen with data {"brightness_pct": 40}. |
| `rag_ingest` | confirm | Walk a folder, chunk + embed every text/source file, and store under a RAG collection. |
| `remember` | confirm | Save a fact the user wants kept across chats as a note in their vault (Wizard Memory). |
| `run_code` | confirm | Execute a code snippet (python, javascript or bash) in the Docker sandbox: no network, read-only image, memory and time limits. |
| `save_provider_key` | confirm | Save a validated API key to the rootless workspace config so the engine can use it. |
| `apt_install` | privileged | Install Debian packages with `apt-get install -y --no-install-recommends` (sudo). |
| `playbook_install` | privileged | Install a Spark playbook on this machine (steps may use sudo; Linux only). |
| `service_enable` | privileged | Enable a systemd unit at boot and start it now: `systemctl enable --now <unit>` (sudo). |
| `shell` | privileged | Run one shell command in the agent workspace (NVH_PROJECTS, default $NVH_HOME/projects). |
| `snap_install` | privileged | Install snap packages with `snap install` (sudo). |
| `system_settings_apply` | privileged | Apply a catalogue system setting on this machine (usually with sudo). |
<!-- END GENERATED: wizard-tools -->

### Code and images from the Wizard chat

The AI Wizard bridges four of the tools above into its own registry
(`nvh.integrations.wizard.tools.WizardToolRegistry`, the single enforcement
point for the chat loop and `POST /v1/wizard/tools/execute`). Wizard tools carry
one of three safety classes: **auto** runs unattended, **confirm** shows a card
and waits for a click, **privileged** shows a red card with the exact commands,
carries a single-use approval token and is recorded under the vault's
`Decisions/` (see `NVH_ALLOW_PRIVILEGED` above, which disables the class
outright).

The privileged tier is Linux only. Off Linux (Windows, Windows on Arm, macOS)
`system_settings_apply`, `apt_install`, `snap_install`, `service_enable` and
any sudo step refuse before anything runs with the sentence `System changes
are not available on Windows yet` (`unsupported_host: true`, `applied:
false`, no vault note) and never prepend `sudo`; `system_settings_plan` still
renders and carries that sentence as its last note, and `playbook_install`
(like `nvh playbook plan` / `install`) refuses with its own sentence while
the catalogue stays listed. `system_settings_get` reports `platform.os`,
`sudo.host_probe_pending` (true while the privilege probe is still running,
so `can_sudo` / `in_sudo_group` are placeholders) and `system_changes:
{available, note}`. On Linux the tier is on wherever sudo can be probed;
`NVH_ALLOW_PRIVILEGED=0` turns it off.

Under native function calling (a provider that vouches for the model) the
Wizard's system prompt carries a short tool block — the safety-class rules,
tool names by class and the TOOL_RESULT / privileged-outcome instructions —
instead of the `TOOL_CALL:` text protocol and schema list; the channel is
decided once per turn and both channels are still read.

| Wizard tool | Class | Rules |
|---|---|---|
| `shell` | privileged | Parameters `command` (required), `cwd` (relative to the workspace or absolute inside it; default the workspace root, `NVH_PROJECTS`), `timeout_s` (1–300, default 60). The red card shows the command and the isolation it will get — `Docker sandbox (no network, read-only image, /workspace mounted)` when `docker info` answers within 5 s, otherwise `directly on this machine as <user>, no Docker isolation` with a "Not isolated" warning — plus the working directory and timeout. That isolation is **pinned into the approved call** (`arguments.isolation`, signed by the approval token): the confirmed run probes Docker again with the same 5 s bound and answers `refused: true` (`isolation_changed`) if the availability changed, so a card approved as a sandbox never runs on the host and one approved as "on this machine" never silently moves into a container; a `shell` call confirmed without a card's pin is refused too. The host fallback closes stdin and strips variables whose names contain `KEY`, `TOKEN`, `SECRET`, `PASSW`, `CREDENTIAL` or `PRIVATE` from the command's environment (Docker passes no host environment at all). Both deny lists run before anything spawns — `agent_guardrails.check_command` on the raw command, then the Wizard system-settings host-protection list on every simple command in it (`&&` / `;` / `\|` chains split, `env` / `nohup` / `nice` / `timeout` / `xargs` prefixes stripped, `sh -c '…'` and `eval …` payloads recursed), plus `find … -delete` under the same NVH_HOME rule as recursive `rm`, piping into a shell, `chmod … 777 /` and writes to block devices; `sudo` / `su` / `doas` / `pkexec` are refused outright (root belongs to `apt_install`, `service_enable`, `system_settings_apply`). A hit returns `BLOCKED: …` and runs nothing. The lists are a backstop for well-known destructive shapes, not a sandbox — the card the user reads is the gate. Relative `rm` / `find` targets resolve against the server's working directory, and under Docker `/workspace` paths are not host paths, so a recursive delete is accepted only for absolute paths strictly inside `NVH_HOME`. Every run (success or failure) is audited to `Decisions/` as `Privileged change: Shell command: <cmd>`. `NVH_SANDBOX_REQUIRE_DOCKER` makes the card say the run will be refused, and it is |
| `run_code` | confirm | Parameters `code` (required) and `language` (`python` default, `javascript`, `bash`). Docker is required from chat: without it the tool answers `refused: true` naming Docker, the `open-webui` / `vllm` playbooks that install it and the terminal, and executes nothing — never a subprocess. The CLI `nvh do` keeps its own fail-open default for the same core tool. `code` is checked against the shell deny list |
| `analyze_image` | auto | Parameters `image_path` (required) and `prompt` (optional). The path is resolved (symlinks followed; the resolved path is what gets read) and must sit under one of the allowlisted roots: `$NVH_HOME/rag/uploads` (including the Wizard attachment folder `rag/uploads/wizard/`), the workspace (`NVH_PROJECTS`, the same directory `shell` mounts), `$NVH_HOME/outputs/images` (`NVH_OUTPUTS`), where `generate_image` and the profile-portrait generator save, so a generated image can be looked at in the same chat, or — directly, no subfolders — the system temp dir where the core `screenshot` tool writes. It must also be an image: a basename on the guardrails' sensitive-file list (`.env`, `id_rsa`, `credentials.json` …) is refused, the extension must be png / jpg / jpeg / gif / webp / bmp, and an existing file must start with a matching image signature (a `.env` renamed `.png` is refused before a byte is encoded). Anything else is `refused: true` naming the rule and the allowed roots. Images over 20 MB are declined. **Cloud rule:** only an image the user attached in the chat (`rag/uploads/wizard/`) may fall back to a configured cloud vision API; a workspace file, a generated image, an ingested upload or a screenshot is analysed by a local Ollama vision model or, without one, refused (`local_only: true`) with the `ollama pull` hint — the model cannot ship a file off the machine on its own initiative. The answer keeps its `[Vision: <model>, N KB]` prefix and reports `model` and `provider` (`ollama` or `cloud`) |
| `read_text_from_image` | auto | OCR variant of `analyze_image`; same `image_path` parameter, allowlist, image and cloud rules, size cap and result fields |

Images enter the chat through the `attachments` field of `POST /v1/wizard/chat`
and `/v1/wizard/chat/stream` (`name`, base64 `content` — raw or a `data:` URL —
`mime_type`, `is_image`), or by dropping / pasting an image into the WebUI chat.
Only png, jpeg, gif, webp or bmp attachments are accepted, at most 6 per request
and 20 MB each (the vision tools' cap), and the bytes must carry that image
signature — the landed extension follows the bytes, not the declared type;
documents, other image types and mislabelled bytes are refused with HTTP 400
and nothing lands, documents belonging in the knowledge base via `POST
/v1/rag/upload-ingest`. Accepted images are written under the server's
`$NVH_HOME/rag/uploads/wizard/<conversation id>/<safe name>.<ext>` (a `home_dir`
on the request does not move them — the vision allowlist reads the process
home), the user turn gains one line, `Attached images (use analyze_image or
read_text_from_image on these paths): …`, so the model reaches them only through
the allowlisted tools above, and the response (`attachment_paths`, also on the
stream's `done` event) echoes the landed paths so a client can keep that line
in the history it sends on the follow-up turns.

### Questions, memories and images from the Wizard chat

Three more tools added in 0.46 belong to the Wizard registry alone — the core
registry behind `nvh do` and the REPL never sees them — and all three are
confirm-class: the card is the human check, and an unattended scheduled turn
(below) gets none of them. Each `tool_call` stream event now carries
`safety_class` (`unknown` for a tool that is not registered), so a client can
tell a card from a running tool, and a confirm-class tool with a planner shows
its dry run on the card as `plan`.

| Wizard tool | Class | Rules |
|---|---|---|
| `ask_user` | confirm | Parameters `question` (required, up to 500 characters), `choices` (up to six, 80 characters each) and `allow_free_text` (default true). The Wizard asks, **this turn ends**, and your answer starts the next one: the card shows the question with its choice chips and a free-text box; **Answer** posts your answer as the next user message, and the request history also carries `tool_results: [{name: "ask_user", ok: true, summary: <answer, cut at 300>}]`, rendered to the model as a `TOOL_RESULT ask_user` line so it knows which question was answered (the message is the authoritative answer, the entry closes the card); **Skip** records `ok: false, summary: "skipped by the user; not answered"`. Nothing runs on the server (`answered_in_chat: true`; the card carries no `plan` and no token). One question per turn: the first `ask_user` is the card, later ones land in `deferred_tool_calls` with `reason: question_pending`, an auto-class sibling in the same turn still runs but the loop stops there (`iterations: 1`), and a confirm or privileged sibling — an action the model made conditional on the unanswered question — is held in `deferred_tool_calls` with the same reason instead of becoming a card (no plan, no approval token on screen to contradict your answer; the model may ask for it again next turn). It is offered to every profile — `strict-tools` ones and a user-pinned `tools_allowed` included — whenever a human is present, because it executes nothing and a whitelist never has to name it, and never on an unattended scheduled turn. The prompt rule tells the model to prefer a question over guessing when a choice changes disk, model size or cost |
| `remember` | confirm | Parameters `title` (up to 120 characters), `body` (up to 2000) and `tags` (up to eight). The model composes the note and the card shows it before anything is written; **Run** saves it through `append_vault_memory` as an `nvh-memory/1` record under `$NVH_HOME/vault/Wizard Memory/` and nowhere else — a model-supplied `home_dir` or `category` is dropped, the process home is the only root — tagged `wizard` plus the model's tags, `Source: wizard`, then indexes it into the RAG `vault` collection best-effort (`indexed`). `redact_secrets` runs over the title and body first, but pattern redaction is best-effort: the card you read is the check. A body carrying the literal `approval_token` or the privileged terminal hint (`run this in a terminal; nvHive never asks for your password`) is refused — the privileged audit writer is the only writer allowed to describe a privileged action — and a body that is nothing but a secret is refused as `nothing to remember after redacting secrets`. Every record is visible and deletable on `/vault` (`GET /v1/vault/notes`, `DELETE /v1/vault/notes/{id}`) and with `nvh vault list` / `nvh vault forget <id>`. Among the library specialists only `daily-notes-coach` carries it (the concierge routes "remember this: …" there); the REPL's `<memory>` block keeps reading `#repl` notes only |
| `generate_image` | confirm | Parameters `prompt` (required), `width` / `height` (512, 768 or 1024; default 1024) and `backend` (`auto`, `local` or `nvidia`). The card carries a `plan` line computed before you click: `NVAPI_KEY` set → NVIDIA-hosted (the model is `NVH_NVIDIA_IMAGE_MODEL` or the endpoint's last path segment), the price reported as **cost unknown** and billed to your NVIDIA account; otherwise local ComfyUI at the host and port nvHive recorded when it started it ($0, uses GPU memory while it runs); otherwise the card says no backend is available (set `NVAPI_KEY`, or start ComfyUI from Setup → ComfyUI). A profile with a `max_cost_usd_per_turn` above zero (the field's `0` means "no limit", as for the LLM turn) never takes the paid path while the price is unknown: ComfyUI renders and the result says, for a $0.05 ceiling, `This profile has a $0.05/turn ceiling and the NVIDIA-hosted image price is not known, so nvHive used local ComfyUI`; with ComfyUI down the request is refused with that fact and `start ComfyUI (Setup → ComfyUI) or run without a specialist`, and `backend: nvidia` does not override the ceiling. The profile is pinned server-side: `profile` is not in the model-visible schema, the chat writes the turn's profile (`wizard` for the general Wizard) into the card's `arguments` — plus `profile_home`, the request's `home_dir`, when the chat resolved the profile there, so Run reads the same profile the chat did — the web echoes them back unchanged, and a call whose pin is missing, empty or unknown is refused with `this image request did not come from a Wizard card; re-run it from the chat` — never treated as "no ceiling" (someone editing the card on their own machine spends their own key, which the pin does not exist to prevent). The image lands under `$NVH_HOME/outputs/images/wizard/` as `<UTC timestamp>-<8 hex>.<ext>`, is served by the authenticated `GET /v1/outputs/images/wizard/<filename>`, can be looked at in the same chat with `analyze_image`, and the result's saved line ends `nvHive does not prune this folder` — nothing does in 0.46. DALL-E, Stability and Pollinations stay with `nvh imagine` |

**Unattended turns.** A scheduled `mode: ask` run (`wizard_chat(...,
unattended=True)`, [Schedule records](#schedule-records-nvh-schedules2)) is a
Wizard turn with no human present: `ask_user` is not offered, no approval token
is minted, stored or served, the tools it may run are the profile's
`tools_allowed` intersected with `SCHEDULE_AUTO_TOOLS`, and a confirm or
privileged request — `remember` and `generate_image` included — is only
recorded on the run as `{name, arguments, privileged, unattended: true}`
(string arguments redacted) when the profile's own whitelist allows that tool,
and refused otherwise; `mode: do` is CLI-only.

### Voice from the Wizard chat

The Wizard composer's hold-to-talk mic and **Read aloud** toggle
([WEBUI.md](WEBUI.md#pages)) ride on four routes under `/v1/voice`
(`nvh/api/routes/voice.py`), every one behind the same API credential as the
rest of `/v1`. The browser records (`MediaRecorder`; the WebUI has no Python
audio-capture path, which is also the only path on an RTX Spark) and posts
the clip to `POST /v1/voice/transcribe` (multipart `file` of type `audio/webm`,
`ogg`, `wav`, `mp4` or `mpeg`; anything else is 415, over 20 MB is 413 — the
route reads the request stream itself, so an oversized clip is refused as the
bytes arrive rather than after the whole body has been spooled).
Speech-to-text is local-first, in the order `nvh voice --stt auto` uses too:

1. **local** — faster-whisper inside the API process (CPU, int8, `small.en`
   by default: `NVH_VOICE_STT_MODEL` above) when the optional `voice` extra is
   installed (`pip install 'nvhive[voice]'`; outside `all`, and on Windows on
   Arm it installs nothing because no wheel exists) **and** the weights are
   under `$NVH_CACHE/huggingface`. $0; nothing leaves the machine. The weights
   are never fetched implicitly: `POST /v1/voice/download` (the status line's
   **Download model** button) starts the one explicit `voice-model-download`
   job — it shows on `/setup`'s job list like an install — and `nvh voice
   --download` is its terminal twin; until then the route answers 503 with
   the hint.
2. **groq** — Groq Whisper (`whisper-large-v3`) when a Groq key resolves
   (`COUNCIL_GROQ_API_KEY`, `GROQ_API_KEY`, `HIVE_GROQ_API_KEY`, then the
   keyring entry `nvh advisor add groq` writes). The clip leaves the box, and
   the pill reads **cost unknown** because no catalog row prices it.
3. otherwise the mic is disabled with the install hint, and
   `GET /v1/voice/status` says what is missing (`reason`: `install
   nvhive[voice]`, `model not downloaded` or `no faster-whisper wheel for
   this platform`).

Transcription runs in a worker thread behind a per-process semaphore, so the
API's one event loop keeps answering while the CPU works. Audio is never
stored: the clip lives in a temporary file for the request only and is
unlinked on every path, and the transcript is inserted into the composer,
never sent on its own. Text-to-speech in the WebUI is the browser's own
`speechSynthesis` (offline; the preference is the browser-local key
`nvh.voice.readAloud`, which **Clear browser data** on `/settings` drops);
`POST /v1/voice/speak` runs `edge-tts` when it is on PATH and answers 501
otherwise — there is no local TTS pack; the system voices it falls back to
read the text from stdin, never from an argument. The pre-0.46 openai-whisper
CLI stays reachable as `nvh voice --stt whisper-cli` from the CLI only: the
route accepts `provider` `auto`, `local` or `groq`, and its `model` must be a
faster-whisper size or a `Systran/` or `mobiuslabsgmbh/` repo id
(`--stt-model` may also name a converted model directory spelled as a path).

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

Workflows are discovered in `nvh/workflows/` (bundled),
`$NVH_HOME/config/workflows/` and `./.nvh/workflows/` (a project's pre-0.44
`./.hive/workflows/` is still read). Shell steps run through the same sandbox
as the `shell` tool.

Back to [README](../README.md)
