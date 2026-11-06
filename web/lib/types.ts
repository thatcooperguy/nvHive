// ─── API Response Envelope ──────────────────────────────────────────────────

export interface ApiEnvelope<T> {
  status: 'success' | 'error';
  data: T;
}

// ─── Usage / Cost ────────────────────────────────────────────────────────────

export interface TokenUsage {
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
}

export interface CompletionResponse {
  content: string;
  model: string;
  provider: string;
  usage: TokenUsage;
  cost_usd: string | null;
  /** False = the catalog has no price for `model`: cost_usd is a placeholder
   * $0 the UI renders as "cost unknown" (lib/cost.ts). */
  cost_known?: boolean;
  latency_ms: number;
  finish_reason: string;
  cache_hit: boolean;
  fallback_from: string | null;
  metadata: Record<string, unknown>;
}

// ─── Query ───────────────────────────────────────────────────────────────────

export interface QueryRequest {
  prompt: string;
  provider?: string;
  model?: string;
  system_prompt?: string;
  temperature?: number;
  max_tokens?: number;
  stream?: boolean;
  attachments?: QueryAttachment[];
}

export interface QueryAttachment {
  name: string;
  content: string;
  mime_type?: string;
  is_image: boolean;
}

/** The image types a Wizard turn accepts; anything else is a document for RAG ingest. */
export type WizardImageMimeType = 'image/png' | 'image/jpeg' | 'image/webp' | 'image/gif';

/**
 * One image attached to a Wizard chat turn (`WizardChatRequest.attachments`,
 * at most 6 per request, 25 MB each). The same field names as
 * `QueryAttachment`, narrowed: `content` is RAW standard base64 with no
 * `data:` URL prefix (the `/v1/query` chat sends data URLs; the Wizard
 * server lands these bytes on disk under `NVH_HOME/rag/uploads/wizard/` and
 * decodes the field directly), `mime_type` is always one of the four accepted
 * types and `is_image` is always true — the server answers 400 to anything
 * else, so documents never travel this way (see lib/attachments.ts).
 */
export interface WizardChatAttachment {
  name: string;
  content: string;
  mime_type: WizardImageMimeType;
  is_image: true;
}

export interface StreamChunkPayload {
  delta: string;
  accumulated: string;
}

export interface StreamDonePayload {
  content: string;
  provider: string;
  model: string;
  usage?: TokenUsage;
  cost_usd?: string;
  /** Sent with cost_usd: false = no catalog price for the model (lib/cost.ts). */
  cost_known?: boolean;
  finish_reason?: string;
}

// ─── Council ─────────────────────────────────────────────────────────────────

export interface CouncilMemberInfo {
  provider: string;
  model: string;
  weight: number;
  persona: string | null;
}

export interface CouncilRequest {
  prompt: string;
  members?: string[];
  weights?: Record<string, number>;
  strategy?: string;
  auto_agents?: boolean;
  preset?: string;
  num_agents?: number;
  synthesize?: boolean;
  system_prompt?: string;
  temperature?: number;
  max_tokens?: number;
}

// ─── Compare ─────────────────────────────────────────────────────────────────

export interface CompareRequest {
  prompt: string;
  providers?: string[];
  system_prompt?: string;
  temperature?: number;
  max_tokens?: number;
}

export type CompareResult = Record<string, CompletionResponse>;

// ─── Providers ───────────────────────────────────────────────────────────────

export interface ProviderHealth {
  name: string;
  healthy: boolean;
  latency_ms: number | null;
  models_available: number;
  error: string | null;
}

export interface ProvidersListResult {
  providers: ProviderHealth[];
}

// ─── Models ──────────────────────────────────────────────────────────────────

export interface ModelInfo {
  model_id: string;
  provider: string;
  display_name: string;
  context_window: number;
  max_output_tokens: number;
  supports_streaming: boolean;
  supports_tools: boolean;
  supports_vision: boolean;
  supports_json_mode: boolean;
  input_cost_per_1m_tokens: string | null;
  output_cost_per_1m_tokens: string | null;
  typical_latency_ms: number | null;
  capability_scores: Record<string, number>;
  status: string;
}

export interface ModelsListResult {
  models: ModelInfo[];
  count: number;
}

// ─── Budget ──────────────────────────────────────────────────────────────────

export interface BudgetStatus {
  daily_spend: string;
  daily_limit: string;
  monthly_spend: string;
  monthly_limit: string;
  daily_queries: number;
  monthly_queries: number;
  by_provider: Record<string, string>;
  /** True when the ledger could not be read and the figures above are the
   * zero fallback (the server's, or `getBudgetStatus` standing in for a
   * failed fetch). Absent on servers that predate the field. */
  unavailable?: boolean;
  // ── 0.46 Wave C (design §7). All optional so an older server type-checks. ──
  /** `budget.hourly_rate_usd` as a decimal string; `null` when the owner has
   * not set one — the API never guesses a rate (`nvh config set
   * budget.hourly_rate_usd 2.10`). Absent (undefined) = not known, e.g. the
   * offline fallback. */
  hourly_rate_usd?: string | null;
  /** The catalog's verdict over every (model, provider) pair that answered
   * today: false = part of `daily_spend` is a placeholder $0. */
  daily_cost_known?: boolean;
  /** Only when the request carried `?since=<epoch ms>`; `null` otherwise. */
  session?: BudgetSession | null;
  /** Warmed platform facts; `null` when detection itself failed. */
  platform?: BudgetPlatform | null;
}

/**
 * `GET /v1/budget/status?since=<epoch ms>` → `session`: the machine-wide
 * ledger since one instant. CLI, every browser tab, proxy clients and
 * scheduled runs all land in the one ledger, so a pill must not present it
 * as "what this tab sent". `since` is epoch MILLISECONDS (`Date.now()`
 * scale); a value small enough to read as seconds is a 422.
 */
export interface BudgetSession {
  since: number;
  /** Decimal string: the sum of successful rows since `since`. */
  spend_usd: string;
  /** False = at least one (model, provider) pair since `since` has no
   * catalog price, so `spend_usd` is a floor (render it as `≥`). */
  cost_known: boolean;
  /** True only when EVERY pair since `since` is a local provider — a catalog
   * verdict, never inferred from a zero sum. */
  local_only: boolean;
  by_provider: Record<string, string>;
  /** The ledger could not be read: the figures above are the zero fallback. */
  unavailable: boolean;
}

/** The platform block the session cost pill reads (warmed platform facts). */
export interface BudgetPlatform {
  device_class: string;
  device_label: string;
  is_cloud: boolean;
  cloud_provider: string | null;
  /** An owned device class that is not cloud: no desktop-rate term unless
   * `hourly_rate_usd` is set (the rate always wins). */
  is_owned: boolean;
}

// ─── Cache ───────────────────────────────────────────────────────────────────

export interface CacheStats {
  hits: number;
  misses: number;
  size: number;
  max_size: number;
  hit_rate: number;
}

// ─── Agents ──────────────────────────────────────────────────────────────────

/** A council cabinet as the picker shows it; lib/cabinets.ts builds these from the wire dict. */
export interface AgentPreset {
  name: string;
  roles: string[];
  /** Optional blurb. The server's dict carries none today; the picker falls back to the roles. */
  description?: string;
}

/**
 * One value of the GET /v1/agents/presets dict. The server sends the role list
 * (nvh.core.agents.list_presets); presetsFromPayload also accepts an object
 * form with an optional description.
 */
export type AgentPresetWire = string[] | { roles?: string[]; description?: string };

/**
 * Wire shape of GET /v1/agents/presets: `{ presets: { <name>: [role, ...] } }`,
 * a dict keyed by preset name, not a list. Adapt it with
 * presetsFromPayload (lib/cabinets.ts) before rendering.
 */
export interface AgentPresetsResult {
  presets: Record<string, AgentPresetWire>;
}

// ─── Health ──────────────────────────────────────────────────────────────────

export interface HealthResult {
  status: string;
  engine_initialized: boolean;
  providers_enabled: number;
}

// ─── WebSocket — query streaming ─────────────────────────────────────────────

export interface WsQueryChunk {
  type: 'chunk';
  delta: string;
  accumulated: string;
}

export interface WsQueryComplete {
  type: 'complete';
  content: string;
  provider: string;
  model: string;
  usage?: TokenUsage;
  cost_usd?: string;
  /** Sent with cost_usd: false = no catalog price for the model (lib/cost.ts). */
  cost_known?: boolean;
  finish_reason?: string;
}

export interface WsError {
  type: 'error';
  error: string;
}

export type WsQueryMessage = WsQueryChunk | WsQueryComplete | WsError;

// ─── WebSocket — council streaming ───────────────────────────────────────────

export interface WsCouncilStart {
  type: 'council_start';
  session_id: string;
  members: CouncilMemberInfo[];
  agents: string[];
}

export interface WsMemberStart {
  type: 'member_start';
  member: string;    // e.g. "openai:Software Architect"
  provider: string;
  persona: string;
}

export interface WsMemberChunk {
  type: 'member_chunk';
  member: string;
  delta: string;
  accumulated: string;
}

export interface WsMemberComplete {
  type: 'member_complete';
  member: string;
  content: string;
  tokens: number;
  cost: string;
  /** The catalog's verdict on `cost` (0.46); undefined from an older server. */
  cost_known?: boolean;
  latency_ms: number;
}

export interface WsMemberFailed {
  type: 'member_failed';
  member: string;
  error: string;
}

export interface WsSynthesisStart {
  type: 'synthesis_start';
}

export interface WsSynthesisChunk {
  type: 'synthesis_chunk';
  delta: string;
  accumulated: string;
}

export interface WsSynthesisComplete {
  type: 'synthesis_complete';
  content: string;
  tokens: number;
  cost: string;
  cost_known?: boolean;
}

export interface WsCouncilComplete {
  type: 'council_complete';
  total_cost: string;
  /** True only when every member (and the synthesis) was priced; one unpriced model makes the total a floor. */
  cost_known?: boolean;
  total_latency_ms: number;
  quorum_met: boolean;
}

export type WsCouncilMessage =
  | WsCouncilStart
  | WsMemberStart
  | WsMemberChunk
  | WsMemberComplete
  | WsMemberFailed
  | WsSynthesisStart
  | WsSynthesisChunk
  | WsSynthesisComplete
  | WsCouncilComplete
  | WsError;

/** Per-member streaming state tracked in the UI */
export type MemberStreamStatus = 'waiting' | 'streaming' | 'complete' | 'failed';

export interface MemberStreamState {
  label: string;      // e.g. "openai:Software Architect"
  provider: string;
  persona: string;
  status: MemberStreamStatus;
  accumulated: string;
  tokens: number;
  cost: string;
  cost_known?: boolean;
  latency_ms: number;
  elapsedMs: number;  // live timer while streaming
  error?: string;
}

// ─── GPU / System ────────────────────────────────────────────────────────────

export interface GPUDevice {
  name: string;
  vram_mb: number;
  vram_gb: number;
  /** True when vram_* describe a CPU/GPU-shared pool (GB10 / DGX Spark), not dedicated VRAM. */
  unified_memory?: boolean;
  /**
   * True when the driver enumerated this GPU but reported no memory pool. vram_*
   * and memory_*_mb are then all 0 and mean "unknown", not "0 GB": render
   * "memory unreadable" and budget nothing against the row. Absent on payloads
   * that predate the flag — `isMemoryUnreadable()` falls back to vram_mb <= 0.
   */
  memory_unreadable?: boolean;
  memory_used_mb: number;
  memory_free_mb: number;
  memory_reserved_mb?: number;
  utilization_pct: number;
  driver_version: string;
  cuda_version: string;
  index: number;
  compute_capability?: [number, number];
  compute_capability_source?: string;
  architecture?: string;
  architecture_heuristic?: boolean;
}

export interface SystemRAM {
  total_gb: number;
  available_gb: number;
  /** RAM usable for CPU-offloaded layers. 0 on a unified pool — there is no second pool to spill into. */
  effective_for_llm_gb: number;
  /** True when the GPU shares this pool (GB10 / DGX Spark); there is no separate CPU-offload headroom. */
  unified_memory?: boolean;
}

/**
 * Detection verdict from /v1/system/gpu. 'ready' when at least one row has a
 * readable memory pool — models are sized against the first such row (see
 * `primaryGpu()` in lib/gpu.ts; never assume it is gpus[0]). A visible GPU whose
 * memory could not be read stays in `gpus` (0 GB, memory_unreadable) and is
 * named in `summary`; the status is 'blocked' only when *no* row is sized.
 */
export type GpuDetectionStatus = 'ready' | 'blocked' | 'unavailable' | 'not-detected';

export interface GPUInfo {
  /** Every visible row in driver order; unreadable rows are kept at 0 GB, so `gpus[0]` need not be the sized one. */
  gpus: GPUDevice[];
  /** Optional until every API build emits it top-level; mirrors detection.status. Read via `gpuStatusOf()`. */
  status?: GpuDetectionStatus;
  /**
   * One-line human digest, e.g. "1 GPU visible but its memory could not be read: …" or, for a
   * partial failure, "1 of 2 GPUs ready: …; memory unreadable: NVIDIA A100 (GPU 0)". Tooltip for
   * the unreadable tag and the GpuBlockedSummary line.
   */
  summary: string;
  total_vram_gb: number;
  detection?: {
    status: string;
    source: string;
    issues: Array<{ source: string; code: string; message: string; severity: string; detail: string }>;
    device_files_present: boolean;
    nvidia_smi: string;
  };
  system_ram: SystemRAM;
}

export interface ModelRecommendation {
  model: string;
  reason: string;
  vram_required_gb: number;
  tier: string;
  /** Platform caption (unified-memory bandwidth guidance on DGX Spark); "" elsewhere. */
  note?: string;
}

export interface OllamaOptimizations {
  flash_attention: boolean;
  num_parallel: number;
  recommended_ctx: number;
  recommended_quant: string;
  architecture: string;
  compute_capability: [number, number];
  notes: string[];
}

export interface OomCheckResult {
  safe: boolean;
  fits_gpu: boolean;
  /** Fits with CPU offload. Never true on a unified pool and may be omitted there — treat absence as false. */
  fits_hybrid?: boolean;
  gpu_free_gb: number;
  /** 0 on a unified pool — those bytes are already counted in gpu_free_gb. */
  ram_free_gb: number;
  /** True when the GPU shares the system RAM pool (GB10 / DGX Spark). */
  unified_memory?: boolean;
  recommendation: string;
}

export interface RecommendationsResult {
  recommendations: ModelRecommendation[];
  optimizations: OllamaOptimizations;
  oom_check: Record<string, OomCheckResult>;
}

export interface SystemInfo {
  version: string;
  gpu: GPUInfo;
  providers_online: number;
  providers_total: number;
  budget: Partial<BudgetStatus>;
  cache: Partial<CacheStats>;
  ollama_status: 'connected' | 'disconnected';
}

export interface StorageLayout {
  home: string;
  bin_dir: string;
  models_dir: string;
  ollama_models_dir: string;
  cache_dir: string;
  logs_dir: string;
  tmp_dir: string;
  runtime_dir: string;
  apps_dir: string;
  webui_dir: string;
  studio_dir: string;
  comfyui_dir: string;
  config_dir: string;
  projects_dir: string;
  outputs_dir: string;
  backups_dir: string;
  support_dir: string;
  state_dir: string;
  catalog_dir: string;
}

export interface StorageStatus {
  layout: StorageLayout;
  configured_by: string;
  exists: boolean;
  writable: boolean;
  write_probe_ok: boolean;
  write_probe_error: string;
  free_gb: number | null;
  total_gb: number | null;
  min_free_gb: number;
  ok: boolean;
  warnings: string[];
  env_file: string;
  export_lines: string[];
}

export interface StorageConfigureRequest {
  home_dir?: string;
  min_free_gb?: number;
  activate?: boolean;
}

export interface RuntimeStatus {
  python_executable: string;
  python_version: string;
  venv_available: boolean;
  pip_available: boolean;
  strategy: 'python-venv' | 'micromamba-fallback' | 'needs-runtime' | string;
  micromamba_installed: boolean;
  micromamba_binary: string;
  micromamba_root_prefix: string;
  notes: string[];
}

export interface RuntimeDoctorAction {
  id: string;
  label: string;
  description: string;
}

export interface RuntimeDoctorReport {
  checked_at: string;
  status: 'ready' | 'missing-runtime' | 'server-offline' | 'missing-models' | string;
  ready: boolean;
  summary: string;
  binary: string;
  binary_valid: boolean;
  binary_error: string;
  binary_probe?: Record<string, unknown>;
  local_candidate: string;
  server_running: boolean;
  installed_targets: string[];
  recommended_ids: string[];
  missing_recommended_ids: string[];
  missing_recommended_models: StudioModel[];
  detected_vram_gb: number;
  next_action: RuntimeDoctorAction | null;
  rootless: boolean;
}

export interface WorkspaceStateCheck {
  id: string;
  label: string;
  status: 'pass' | 'warn' | 'fail' | 'checking' | string;
  summary: string;
  detail: string;
  action_id: string | null;
}

export interface WorkspaceStateReport {
  schema_version: number;
  checked_at: string;
  phase: 'ready' | 'working' | 'needs-action' | 'blocked' | 'checking' | string;
  ready: boolean;
  summary: string;
  health_score: number;
  next_action: RuntimeDoctorAction | null;
  checks: WorkspaceStateCheck[];
  storage_home: string | null;
  free_gb: number | null;
  active_jobs: InstallJob[];
  recent_failures: InstallJob[];
  runtime: RuntimeStatus;
  local_ai: RuntimeDoctorReport;
  model_fit: {
    summary?: string;
    recommended_ids: string[];
    recommended_installed_count?: number;
    installed_model_count?: number;
    detected_vram_gb: number;
    storage_fits_queue?: boolean;
  };
  boot: {
    summary?: string;
    changed: boolean;
    needs_attention: boolean;
  };
  production: {
    status?: string;
    counts: Record<string, number>;
    next_actions: string[];
  };
  rootless: boolean;
}

export interface RootlessPolicyGate {
  id: string;
  title: string;
  status: 'pass' | 'warn' | 'blocked' | 'info' | string;
  summary: string;
  requires_admin: boolean;
  action_id: string | null;
}

export interface RootlessPolicyReport {
  schema_version: number;
  checked_at: string;
  status: 'ready' | 'warn' | 'blocked' | string;
  summary: string;
  no_root_required: boolean;
  allowed_write_roots: string[];
  blocked_operations: string[];
  preferred_runtimes: string[];
  storage: StorageStatus;
  runtime: RuntimeStatus;
  gates: RootlessPolicyGate[];
}

export interface WorkspacePassport {
  schema_version: number;
  workspace_id: string;
  created_at: string;
  updated_at: string;
  product: string;
  assistant: string;
  nvhive_version: string;
  storage_home: string;
  passport_path: string;
  legacy_passport_path?: string;
  rootless: {
    normal_setup_requires_admin: boolean;
    host_driver_requires_admin_if_broken: boolean;
    policy_status: string;
  };
  paths: StorageLayout & Record<string, string>;
  host_fingerprint: Record<string, unknown>;
  storage: StorageStatus;
  policy: RootlessPolicyReport;
  receipts: Record<string, unknown>;
  jobs: {
    ok: boolean;
    active_count: number;
    recent_count: number;
    active: InstallJob[];
    error?: unknown;
  };
  model_fit: Record<string, unknown>;
  compatibility: Record<string, unknown>;
}

export interface WizardPlanStep {
  id: string;
  title: string;
  status: 'pass' | 'warn' | 'ready' | 'blocked' | string;
  summary: string;
  action_id: string | null;
  requires_admin: boolean;
  risk: 'safe' | 'moderate' | 'high' | string;
}

export interface WizardPlanResult {
  schema_version: number;
  checked_at: string;
  profile: string;
  title: string;
  summary: string;
  rootless_safe: boolean;
  passport: {
    workspace_id: string;
    storage_home: string;
    policy_status: string;
    active_jobs: number;
  };
  steps: WizardPlanStep[];
}

export interface WizardMissionBuildRequest {
  profile: string;
  home_dir?: string;
  torch_profile?: string;
  force_update?: boolean;
  min_free_gb?: number;
}

export interface WizardMissionPlanResult {
  schema_version: number;
  profile: string;
  title: string;
  storage: StorageStatus;
  rootless_safe: boolean;
  needs_comfyui: boolean;
  torch_profile: string;
  pack_ids: string[];
  first_pack_ids: string[];
  comfy_node_pack_ids: string[];
  model_ids: string[];
  example_ids: string[];
  estimated_disk_gb: number;
  stages: Array<Record<string, unknown>>;
}

export interface WizardMissionInstallEvent {
  event: 'plan' | 'pack' | 'model' | 'step' | 'stage-complete' | 'log' | 'complete' | 'error' | string;
  status: 'running' | 'complete' | 'failed' | string;
  message: string;
  profile?: string;
  stage?: string;
  child_event?: string;
  child_status?: string;
  plan?: WizardMissionPlanResult;
  path?: string;
  pack_id?: string;
  model_id?: string;
  command?: string[];
}

export interface SupportSnapshotResult {
  schema_version: number;
  created_at: string;
  summary: string;
  path: string;
  passport: Record<string, unknown>;
  policy: RootlessPolicyReport;
  diagnostics: DiagnosticsReport;
  excludes: string[];
}

export interface SetupAction {
  id: string;
  title: string;
  priority: number;
  status: 'required' | 'recommended' | 'optional' | string;
  command: string;
  reason: string;
  can_run_without_root: boolean;
}

export interface SetupIssue {
  id: string;
  title: string;
  severity: 'required' | 'recommended' | 'optional' | string;
  reason: string;
  fix_action_id: string | null;
  affected_item: string | null;
  current_version: string | null;
  available_version: string | null;
}

export interface LocalChatSmokeReport {
  ready: boolean;
  status: string;
  summary: string;
  provider?: string;
  model?: string | null;
  output_chars?: number;
  latency_ms?: number | null;
  error?: string | null;
  next_action_id?: string | null;
  checked_at?: string | null;
  cached?: boolean;
  rootless?: boolean;
}

export interface SetupHelperReport {
  ready: boolean;
  summary: string;
  storage: StorageStatus;
  runtime: RuntimeStatus;
  comfyui: Record<string, unknown>;
  model_recommendation_count: number;
  local_chat?: LocalChatSmokeReport;
  actions: SetupAction[];
  issues?: SetupIssue[];
  issue_count?: number;
  core_issue_count?: number;
  optional_issue_count?: number;
  core_action_count?: number;
  optional_action_count?: number;
  receipts?: SetupReceiptsSummary;
  catalog?: SetupCatalogStatus;
  compatibility?: {
    summary?: string;
    issue_count: number;
    blocked_count: number;
    rootless_fixable_count: number;
    recommended_torch_profile?: string;
  };
  boot_preflight?: {
    summary?: string;
    checked_at?: string | null;
    changed: boolean;
    change_count: number;
    agent_helper?: BootAgentHelper;
  };
  assistant?: {
    mode: string;
    product?: string;
    can_read_jobs: boolean;
    can_read_receipts: boolean;
    can_refresh_catalog: boolean;
    can_use_local_state?: boolean;
    can_use_internet_when_available?: boolean;
    official_repo_url?: string;
    readme_url?: string;
    pypi_url?: string;
    grounding_sources?: string[];
    system_prompt?: string;
    description: string;
  };
}

export interface RootlessServiceStatus {
  id: string;
  name: string;
  category: string;
  installed: boolean;
  running: boolean;
  ready: boolean;
  status: string;
  summary: string;
  url?: string | null;
  host?: string | null;
  port?: number | null;
  install_path?: string | null;
  launcher?: string | null;
  log_path?: string | null;
  log_tail: string[];
  next_action_id?: string | null;
  next_action_label?: string | null;
  command?: string | null;
  rootless: boolean;
  metadata: Record<string, unknown>;
  checked_at: string;
}

export interface ServiceSnapshotInfo {
  enabled: boolean;
  path?: string | null;
  service_dir?: string | null;
  error?: string | null;
}

export interface ServicePortReport {
  conflict_count: number;
  conflicts: Array<{ port: number; services: Array<Record<string, unknown>> }>;
  occupied: Array<{ port: number; services: Array<Record<string, unknown>> }>;
  expected: number[];
}

export interface ServiceNextAction {
  service_id: string;
  service_name?: string | null;
  action_id: string;
  label: string;
  summary: string;
  rootless: boolean;
  command?: string | null;
}

export interface ServiceRegistryReport {
  checked_at: string;
  service_count: number;
  ready_count: number;
  running_count: number;
  rootless: boolean;
  services: RootlessServiceStatus[];
  snapshot?: ServiceSnapshotInfo;
}

export interface ServiceHealthReport extends ServiceRegistryReport {
  summary: string;
  status: 'ready' | 'warn' | 'blocked' | string;
  blocked_count: number;
  warning_count: number;
  issue_count: number;
  ports: ServicePortReport;
  next_actions: ServiceNextAction[];
  support_text: string;
}

export interface ServiceActionResult {
  ok: boolean;
  service_id: string;
  action_id: string;
  message: string;
  service?: RootlessServiceStatus;
  health?: ServiceHealthReport;
  support_text?: string;
  requires_job?: boolean;
  command?: string | null;
  result?: Record<string, unknown>;
}

export interface InstallReceiptHealth {
  install_path_exists: boolean;
  missing_launchers: string[];
  missing_files: string[];
  healthy: boolean;
}

export interface InstallReceipt {
  id: string;
  kind: string;
  item_id: string;
  title: string;
  status: string;
  installed_at: string;
  updated_at: string;
  install_path: string;
  version: string | null;
  source_urls: string[];
  launchers: string[];
  models: string[];
  files: string[];
  no_root: boolean;
  metadata: Record<string, unknown>;
  schema_version: number;
  health: InstallReceiptHealth;
}

export interface SetupReceiptsSummary {
  count: number;
  by_kind: Record<string, number>;
  unhealthy: number;
  root: string | null;
  receipts?: InstallReceipt[];
}

export interface SetupReceiptsResult {
  receipts: InstallReceipt[];
  count: number;
  summary: SetupReceiptsSummary;
}

export interface SetupCatalogStatus {
  source: string;
  url?: string;
  error?: string | null;
  schema_version?: number;
  updated_at?: string;
  profile_count?: number;
  pack_count?: number;
  model_count?: number;
  comfyui_example_count?: number;
}

export interface SetupCatalogResult {
  source: string;
  url: string;
  error: string | null;
  catalog: {
    schema_version: number;
    updated_at: string;
    channel?: string;
    profiles: Array<Record<string, unknown>>;
    packs: Array<Record<string, unknown>>;
    models: Array<Record<string, unknown>>;
    comfyui_examples: Array<Record<string, unknown>>;
  };
}

export interface CompatibilityRequirement {
  id: string;
  label: string;
  status: 'ok' | 'fixable' | 'warning' | 'blocked' | string;
  detail: string;
  fix_action_id: string | null;
  rootless_fix_available: boolean;
}

export interface AppCompatibility {
  id: string;
  title: string;
  category: string;
  status: 'ready' | 'fixable' | 'degraded' | 'blocked' | string;
  severity: 'info' | 'optional' | 'recommended' | 'required' | string;
  summary: string;
  recommended_action_id: string | null;
  rootless_fix_available: boolean;
  requirements: CompatibilityRequirement[];
  notes: string[];
}

export interface HostFact {
  id: string;
  label: string;
  value: string;
  status: string;
  severity: string;
  detail: string;
}

export interface CompatibilityReport {
  summary: string;
  ready: boolean;
  issue_count: number;
  blocked_count: number;
  rootless_fixable_count: number;
  recommended_torch_profile: string;
  host: Record<string, unknown>;
  facts: HostFact[];
  apps: AppCompatibility[];
}

export interface BootPreflightChange {
  id: string;
  label: string;
  before: string;
  after: string;
  severity: 'info' | 'optional' | 'recommended' | 'required' | string;
  detail: string;
}

export interface BootAgentHelper {
  offline_helper_ready: boolean;
  local_agent_ready: boolean;
  mode: string;
  recommended_action_id: string | null;
  summary: string;
  requirements: CompatibilityRequirement[];
}

export interface BootPreflightReport {
  schema_version: number;
  checked_at: string | null;
  state_file: string;
  first_run: boolean;
  changed: boolean;
  needs_attention: boolean;
  fingerprint_id: string | null;
  previous_fingerprint_id: string | null;
  previous_checked_at: string | null;
  summary: string;
  changes: BootPreflightChange[];
  agent_helper: BootAgentHelper;
  mount_autopilot?: MountAutopilotReport | null;
  auto_repair?: AutoRepairPlan | AutoRepairResult | null;
  smoke_tests?: SmokeTestReport | null;
  model_fit?: {
    summary?: string;
    detected_vram_gb?: number;
    recommended_ids?: string[];
    recommended_installed_count?: number;
    installed_model_count?: number;
  } | null;
  compatibility: CompatibilityReport | null;
  error?: string;
}

export interface MountCandidate {
  path: string;
  recommended_home: string;
  label: string;
  source: string;
  exists: boolean;
  writable: boolean;
  free_gb: number | null;
  total_gb: number | null;
  fs_type: string | null;
  device: string | null;
  mount_point: string | null;
  read_only: boolean;
  network_mount: boolean;
  os_mount: boolean;
  large_block_mount: boolean;
  score: number;
  warnings: string[];
  evidence: string[];
}

export interface MountAutopilotReport {
  summary: string;
  confidence: string;
  current: StorageStatus;
  recommended: MountCandidate | null;
  candidates: MountCandidate[];
}

export interface AutoRepairAction {
  id: string;
  title: string;
  status: string;
  summary: string;
  safe_to_auto_run: boolean;
  action_type: string;
  button_action_id: string;
}

export interface AutoRepairPlan {
  summary: string;
  auto_count: number;
  needs_user_count: number;
  actions: AutoRepairAction[];
}

export interface AutoRepairResult {
  summary: string;
  completed: Array<AutoRepairAction & { result?: string }>;
  skipped: Array<AutoRepairAction & { reason?: string }>;
  errors: Array<AutoRepairAction & { error?: string }>;
  plan: AutoRepairPlan;
}

export interface SmokeTestItem {
  id: string;
  title: string;
  status: 'pass' | 'warn' | 'fail' | 'skip' | string;
  summary: string;
  detail: string;
  action_id: string | null;
}

export interface SmokeTestReport {
  summary: string;
  ready: boolean;
  passed: number;
  warnings: number;
  failed: number;
  tests: SmokeTestItem[];
}

export interface ModelFitReport {
  summary: string;
  detected_vram_gb: number;
  free_gb: number | null;
  recommended_queue_disk_gb?: number;
  storage_fits_queue?: boolean;
  recommended_ids: string[];
  recommended_installed_count?: number;
  installed_model_count?: number;
  best_by_use_case: Record<string, Record<string, unknown>>;
  models: Array<Record<string, unknown>>;
  ollama_available: boolean;
  ollama_running: boolean;
}

export interface ProductionReadinessGate {
  id: string;
  title: string;
  status: 'pass' | 'warn' | 'blocked' | string;
  summary: string;
  detail: string;
  recommendation: string;
  source: 'local' | 'target-vm' | string;
}

export interface ProductionReadinessReport {
  checked_at: string;
  status: 'production-ready' | 'pilot-ready' | 'blocked' | string;
  summary: string;
  pilot_ready: boolean;
  production_ready: boolean;
  target_vm_validated: boolean;
  counts: {
    passed: number;
    warnings: number;
    blocked: number;
    total: number;
  };
  gates: ProductionReadinessGate[];
  next_actions: string[];
  target_vm_checklist: string[];
  inputs: Record<string, string | number | boolean | null | undefined>;
}

export interface DiagnosticsReport {
  report_id: string;
  checked_at: string;
  request_id?: string | null;
  summary: string;
  environment: Record<string, unknown>;
  paths: Record<string, string>;
  checks: Record<string, unknown>;
  logs: {
    included: boolean;
    files: string[];
    recent: Array<{
      path: string;
      lines: string[];
    }>;
  };
}

export interface MissionStage {
  id: string;
  title: string;
  status: 'pass' | 'warn' | 'fail' | string;
  summary: string;
  action_id: string | null;
}

export interface MissionControlReport {
  summary: string;
  ready: boolean;
  stages: MissionStage[];
  boot_preflight: BootPreflightReport;
  mount_autopilot: MountAutopilotReport;
  auto_repair: AutoRepairPlan;
  smoke_tests: SmokeTestReport;
  model_fit: ModelFitReport;
}

export interface SetupAssistantReply {
  question: string;
  answer: string;
  focus: string;
  commands: string[];
  product?: string;
  mode?: string;
  available_immediately?: boolean;
  requires_local_model?: boolean;
  official_repo_url?: string;
  readme_url?: string;
  grounding_sources?: string[];
  diagnostics_report_id?: string | null;
  debug_findings?: string[];
  log_highlights?: string[];
  troubleshooting?: Record<string, unknown> | null;
  official_urls?: string[];
  web_search_queries?: string[];
  observations: {
    ready: boolean;
    issue_count?: number;
    receipt_count: number;
    unhealthy_receipts: number;
    catalog_source?: string;
    recent_problem?: InstallJob | null;
  };
  actions: SetupAction[];
}

// ─── UI State helpers ────────────────────────────────────────────────────────

export type InstallJobStatus =
  | 'queued'
  | 'running'
  | 'complete'
  | 'failed'
  | 'canceled'
  | 'interrupted'
  | string;

export interface InstallJob {
  id: string;
  kind: 'wizard-mission' | 'comfyui-install' | 'studio-pack-install' | 'studio-model-install' | 'schedule-run' | 'voice-model-download' | string;
  title: string;
  status: InstallJobStatus;
  message: string;
  progress: number;
  /** Free-form per kind; a `schedule-run` job's is a `ScheduleRunJobRequest`. */
  request: Record<string, unknown>;
  storage_home: string;
  created_at: string;
  updated_at: string;
  started_at: string | null;
  completed_at: string | null;
  event_count: number;
  recent_events?: InstallJobEvent[];
  cancel_requested: boolean;
  events_path: string;
}

export interface InstallJobEvent {
  job_id: string;
  sequence: number;
  timestamp: string;
  event: string;
  status: string;
  message: string;
  payload: Record<string, unknown>;
}

export interface InstallJobsResult {
  jobs: InstallJob[];
  count: number;
}

export interface InstallJobEventsResult {
  events: InstallJobEvent[];
  count: number;
}

export interface ComfyUIExample {
  id: string;
  title: string;
  category: string;
  install_profile: string;
  recommended_vram_gb: number;
  why_trending: string;
  workflow_hint: string;
  source_url: string;
  models: string[];
  custom_nodes: string[];
  notes: string[];
}

export interface ComfyUIStatus {
  installed: boolean;
  running: boolean;
  ready?: boolean;
  service_status?: string;
  next_action?: 'install' | 'start' | 'open' | string;
  host?: string;
  port?: number;
  port_open?: boolean;
  port_conflict?: boolean;
  url: string;
  install_root: string;
  app_dir: string;
  venv_python: string;
  runtime_python?: string;
  runtime_strategy?: string;
  examples_dir: string;
  examples_installed: boolean;
  manager_available: boolean;
  log_path: string;
  log_tail?: string[];
  pid: number | null;
  runtime?: Record<string, unknown>;
  examples: ComfyUIExample[];
  already_running?: boolean;
  started?: boolean;
  ready_timeout?: boolean;
  ready_wait_seconds?: number;
  requested_port?: number;
  process_exit_code?: number;
}

export interface ComfyUIExamplesResult {
  examples: ComfyUIExample[];
  count: number;
  sources: string[];
}

export type ComfyUITorchProfile = 'nvidia-cu130' | 'nvidia-cu121' | 'cpu' | 'skip';

export interface ComfyUIInstallRequest {
  torch_profile?: ComfyUITorchProfile;
  force_update?: boolean;
}

export interface ComfyUIInstallEvent {
  event: 'plan' | 'step' | 'log' | 'complete' | 'error' | string;
  status: 'running' | 'complete' | 'failed' | string;
  message: string;
  command?: string[];
  install_root?: string;
  torch_profile?: string;
  examples_dir?: string;
  status_snapshot?: ComfyUIStatus;
}

export interface ComfyUIModelPlanModel {
  name: string;
  workflow_ids: string[];
  workflow_titles: string[];
  source_urls: string[];
  target_folder: string;
  requires_manual_download: boolean;
}

export interface ComfyUIModelPlanResult {
  examples: ComfyUIExample[];
  models: ComfyUIModelPlanModel[];
  custom_nodes: Array<{
    name: string;
    workflow_ids: string[];
    workflow_titles: string[];
  }>;
  model_count: number;
  custom_node_count: number;
  requires_manual_download: boolean;
  download_helper: string;
  message: string;
  plan_path: string;
}

export interface StudioPackStatus {
  id: string;
  installed: boolean;
  root: string;
  marker: string;
  details: Record<string, unknown>;
  installed_at: string | null;
}

export interface StudioComfyNode {
  name: string;
  repo_url: string;
}

export interface StudioPack {
  id: string;
  title: string;
  category: 'runtime' | 'llm' | 'agents' | 'comfyui' | 'game' | 'creative' | string;
  tagline: string;
  description: string;
  recommended_vram_gb: number;
  estimated_disk_gb: number;
  install_kind: string;
  no_root: boolean;
  models: string[];
  python_packages: string[];
  comfy_nodes: StudioComfyNode[];
  launchers: string[];
  source_urls: string[];
  notes: string[];
  status: StudioPackStatus;
}

export interface StudioPacksResult {
  packs: StudioPack[];
  bundles: Record<string, string[]>;
  root: string;
  count: number;
}

export interface StudioPackInstallRequest {
  pack_ids: string[];
  force_update?: boolean;
}

export interface StudioPackInstallEvent {
  event: 'plan' | 'pack' | 'step' | 'log' | 'complete' | 'error' | string;
  status: 'running' | 'complete' | 'failed' | string;
  message: string;
  pack_id?: string;
  pack_ids?: string[];
  command?: string[];
  estimated_disk_gb?: number;
  status_snapshot?: StudioPacksResult;
}

export interface StudioModel {
  id: string;
  title: string;
  provider: string;
  install_target: string;
  category: string;
  recommended_vram_gb: number;
  estimated_disk_gb: number;
  priority: number;
  capabilities: string[];
  why_recommended: string;
  source_url: string;
  license_note: string;
  recommended: boolean;
  fits_vram: boolean;
  installed: boolean;
  install_command: string;
}

export interface StudioModelsResult {
  models: StudioModel[];
  recommended_ids: string[];
  installed_targets: string[];
  detected_vram_gb: number;
  ollama_available: boolean;
  ollama_running: boolean;
  count: number;
}

export interface StudioModelInstallRequest {
  model_ids: string[];
  force_update?: boolean;
}

export interface StudioModelInstallEvent {
  event: 'plan' | 'model' | 'step' | 'log' | 'complete' | 'error' | string;
  status: 'running' | 'complete' | 'failed' | string;
  message: string;
  model_id?: string;
  model_ids?: string[];
  command?: string[];
  estimated_disk_gb?: number;
  status_snapshot?: StudioModelsResult;
}

export interface VaultConflict {
  title: string;
  summary: string;
}

export interface ObsidianStatus {
  installed: boolean;
  rootless_app: boolean;
  app_path: string | null;
  launcher_path: string;
  open_command: string;
  installable: boolean;
  reason: string;
  download_url: string;
  vault_dir: string;
  message?: string;
}

export interface VaultStatus {
  initialized: boolean;
  vault_dir: string;
  home_dir: string;
  markdown_files: number;
  /** Notes under `Wizard Memory/` (a directory count, 0.46); absent on older servers. */
  memory_files?: number;
  directories?: Record<string, string>;
  seeded_files?: Record<string, string>;
  conflicts: VaultConflict[];
  obsidian: ObsidianStatus;
}

export interface VaultMemoryRequest {
  title: string;
  body: string;
  category?: string;
  tags?: string[];
  /** The writer stamped as `Source:` (`web` from this UI; the server defaults
   * to `api`). At most sixteen characters. */
  source?: string;
  home_dir?: string;
}

export interface VaultMemoryResult {
  saved: boolean;
  path: string;
  category: string;
  title: string;
  vault_dir: string;
  // ── 0.46 record fields (`nvh-memory/1`); absent on older servers. ──
  /** Stable record id (32 hex chars) — the `/vault?id=<id>` deep link. */
  id?: string;
  schema?: string;
  created?: string;
  tags?: string[];
  source?: string;
  /** How many secret-looking spans were redacted before the write. */
  redacted?: number;
  /** Whether the note's RAG chunks are indexed yet (best effort, never fails the save). */
  indexed?: boolean;
}

// ─── Chat UI types ────────────────────────────────────────────────────────────

/** The modes of the one chat home (`/`, 0.47): the Wizard is the default. */
export type ChatMode = 'wizard' | 'single' | 'council' | 'compare';

/** Which surface a stored conversation belongs to. Superset of ChatMode:
 * every mode resumes on `/?c=<id>` (0.47); `/wizard?conversation=` redirects there for one release. */
export type ConversationMode = ChatMode | 'wizard';

export interface ChatMessage {
  id: string;
  role: 'user' | 'assistant' | 'error';
  content: string;
  provider?: string;
  model?: string;
  mode?: ChatMode;
  cost_usd?: string | null;
  /** From the API's `cost_known`; undefined on rows that predate it. */
  cost_known?: boolean;
  tokens?: number;
  latency_ms?: number;
  streaming?: boolean;
  council_data?: {
    member_responses: Record<string, {
      content: string;
      provider: string;
      model: string;
      tokens: number;
      cost: string;
      cost_known?: boolean;
      latency_ms?: number;
    }>;
    synthesis?: string;
    total_cost?: string;
    /** From `council_complete`: every member (and the synthesis) priced. */
    cost_known?: boolean;
    member_order?: string[];
  };
  compare_data?: Record<string, {
    content: string;
    model: string;
    tokens?: number;
    cost_usd?: string | null;
    cost_known?: boolean;
    latency_ms?: number;
    cache_hit?: boolean;
  }>;
  timestamp: number;
}

export interface ConversationSummary {
  id: string;
  title: string;
  model?: string;
  provider?: string;
  mode: ConversationMode;
  message_count: number;
  created_at: number;
  updated_at: number;
  pinned?: boolean;
}

// ─── Setup / Free providers ───────────────────────────────────────────────────

export interface FreeProvider {
  id: string;
  name: string;
  display_name?: string;
  signup_tier: 'none' | 'email' | 'account';
  daily_limit?: string;
  free_tier_limits?: string;
  strengths?: string[];
  configured: boolean;
  requires_signup?: boolean;
  requires_key?: boolean;
  env_var?: string;
  env_key?: string;
  placeholder?: string;
  signup_url?: string;
  key_url?: string;
  docs_url?: string;
  /** The provider's canonical free-tier or access note (nvh/providers/specs.py free_info). */
  free_info?: string;
  default_model?: string;
  fallback_model?: string;
  /** Logo slug for the @lobehub/icons CDN. Null when no brand mark is available. */
  logo_slug?: string | null;
}

// ─── Spark playbooks (nvh/integrations/installs/playbooks.py) ────────────────

/**
 * One row of the playbook catalogue, as the auto-class Wizard tool
 * `playbook_list` returns it (`playbooks.catalogue()`). Playbooks are NOT
 * studio packs: they may need sudo, they run only through the privileged tool
 * `playbook_install` (red approval card, approval token) or the CLI in the
 * user's own terminal, and `installed` comes from the install receipt. Every
 * id is an upstream folder name under
 * github.com/NVIDIA/dgx-spark-playbooks/tree/main/nvidia/<id>. Unknown fields
 * are kept, never rejected.
 */
export interface PlaybookCatalogueEntry {
  id: string;
  title: string;
  category: string;
  summary: string;
  /** Any step runs with sudo. Derived server-side from the steps. */
  requires_sudo: boolean;
  /** How many steps run with sudo (0 for a user-space playbook). */
  sudo_steps: number;
  /** Browser logins, TUIs, cabling, foreground servers — shown, never run. */
  manual_steps: number;
  estimated_minutes: number | null;
  estimated_disk_gb: number | null;
  /** A studio pack (rootless, no sudo) that covers the same tool, if any. */
  rootless_alternative: string | null;
  /** From the `playbook` receipt; false when no receipt exists. */
  installed: boolean;
  receipt_path: string | null;
  [key: string]: unknown;
}

/**
 * What a confirmed `playbook_install` answers: the run was started as a job
 * (kind `playbook-run`) and the card streams it from `/v1/jobs/<job_id>`.
 */
export interface PlaybookRunStart {
  ok?: boolean;
  job_id: string;
  playbook: string;
  steps_total: number;
  [key: string]: unknown;
}

/**
 * One `playbook-run` job event as the UI reads it (`InstallJobEvent.payload`
 * flattened over `event` / `status` / `message`). `step` is the 0-based index
 * into the compiled plan; the UI shows it 1-based. `command` is the rendered
 * argv (already `sudo`-prefixed where the step is privileged). A
 * `needs_terminal` event is a hand-off, not a failure: sudo wanted a password
 * so the runner stopped and `command` is the one line the user runs
 * themselves (`nvh playbook install <id>`).
 */
export interface PlaybookRunEvent {
  event: 'plan' | 'step' | 'log' | 'needs_terminal' | 'complete' | 'error' | string;
  status: string;
  message: string;
  playbook?: string;
  step?: number;
  steps_total?: number;
  title?: string;
  command?: string | string[];
  sudo?: boolean;
  skipped?: boolean;
  exit_code?: number | null;
  hint?: string;
  error?: string;
  [key: string]: unknown;
}

// ─── 0.46 Wave C — vault notes, schedules, Wizard card results ───────────────
// Append-only block (design §1–§3, §6). The routes are on main since Waves A/B;
// every shape here mirrors the Python record it names, nothing is re-derived.

/**
 * `schema` of a vault record: `nvh-memory/1` for a record written from 0.46
 * on, `legacy` for a pre-0.46 note (derived id), `doc` for a seeded or
 * frontmatter note that is documentation rather than a record — the
 * `memory_only` filter drops `doc` notes.
 */
export type VaultNoteSchema = 'nvh-memory/1' | 'legacy' | 'doc' | string;

/**
 * One vault note as `GET /v1/vault/notes` lists it — headers only: `body` is
 * present only on `GET /v1/vault/notes/{id}`. Mirrors `MemoryRecord`
 * (nvh/integrations/workspace/vault.py).
 */
export interface VaultNote {
  /** 32 hex chars: uuid4, or sha1(path relative to vault/)[:32] for a note without an `Id:` line. */
  id: string;
  schema: VaultNoteSchema;
  title: string;
  /** ISO-8601 UTC with seconds; '' for a `doc` note. */
  created: string;
  /** The vault folder (a `VaultStatus.directories` key); '' for a note at the vault root. */
  category: string;
  /** Lower-case slugs. */
  tags: string[];
  /** The writer: `wizard`, `repl`, `web`, `api`, `import` or `audit`; '' for a `doc` note. */
  source: string;
  /** Absolute path of the Markdown file. */
  path: string;
  /** Everything after the header paragraphs — single-note GET only. */
  body?: string;
}

/** Query string of `GET /v1/vault/notes`; every filter is optional. */
export interface VaultNotesQuery {
  /** A vault folder name, e.g. `Wizard Memory`. */
  category?: string;
  /** One tag slug (a leading `#` is tolerated). */
  tag?: string;
  /** Case-insensitive match over title, tags and opening lines. */
  q?: string;
  /** Records only (`nvh-memory/1` and `legacy`); drop seeded `doc` notes. */
  memory_only?: boolean;
  /** 1..500; the server defaults to 200. */
  limit?: number;
}

export interface VaultNotesResult {
  /** Newest first, headers only. */
  notes: VaultNote[];
  count: number;
  memory_only: boolean;
  limit: number;
}

/** `DELETE /v1/vault/notes/{id}`: the file is gone; `forgotten` says whether
 * the RAG chunk drop succeeded too (`chunks_removed` may be 0 for a note
 * that was never indexed). 404 for an unknown id, 403 outside the vault. */
export interface VaultNoteDeleteResult {
  deleted: boolean;
  id: string;
  path: string;
  forgotten: boolean;
  chunks_removed: number;
}

// ── Schedules (design §6) ──

/** The modes the API runner executes. `do` runs tools unattended and is
 * CLI-only: `POST /v1/schedules` answers 422 for it, so the web never sends it. */
export type ScheduleMode = 'ask' | 'convene';

/** `Schedule.last_status`: '' before the first run. */
export type ScheduleRunStatus = 'ok' | 'deferred' | 'skipped' | 'error' | '';

/** One schedule record — `ScheduledTask.to_dict()` (nvh/core/scheduler.py), every key. */
export interface Schedule {
  /** `uuid4().hex` for records from 0.46 on; legacy eight-character ids are kept. */
  id: string;
  prompt: string;
  interval_seconds: number;
  /** Provider override for the run; '' routes normally. Free text in the web — never a hand-typed provider list. */
  advisor: string;
  /** `do` appears only on a record the CLI wrote; the web creates `ask` or `convene`. */
  mode: ScheduleMode | 'do' | string;
  /** ISO-8601 UTC; '' before the first run. */
  last_run: string;
  /** Epoch SECONDS at which the schedule is next due (not ms — unlike `BudgetSession.since`). */
  next_run: number;
  enabled: boolean;
  /** ISO-8601 UTC. */
  created_at: string;
  /** Display label beside the prompt; '' when unnamed. */
  name: string;
  /** Agent profile name; '' lets the concierge choose. */
  profile: string;
  /** The one conversation every run appends to; '' until the first run creates it. */
  conversation_id: string;
  /** ISO-8601 UTC of the last field change or recorded run; '' for pre-0.46 records. */
  updated_at: string;
  last_status: ScheduleRunStatus;
  /** Error text of the last run (≤300 chars); '' when it did not fail. */
  last_error: string;
  /** Id of the last run's job under `$NVH_HOME/jobs/`; '' before the first run. */
  last_job_id: string;
  /** Cost of the last run; `null` = never run or no figure — the UI shows
   * nothing for it (not "cost unknown"). */
  last_cost_usd: number | null;
  /** Whether `last_cost_usd` is a trusted figure; `null` beside a `null` cost. */
  last_cost_known: boolean | null;
  run_count: number;
  /** Unknown keys, round-tripped by the store. */
  extra: Record<string, unknown>;
}

/** `GET /v1/schedules` → `runner`: what the heartbeat lock says plus the
 * unattended tool roster. `runner: ''` with `active: false` means no runner
 * holds the lock (schedules will not fire until the API or `nvh schedule
 * start` runs them). */
export interface ScheduleRunner {
  /** A lock exists and its heartbeat is fresh. */
  active: boolean;
  pid: number | null;
  lock_path: string;
  /** ISO-8601 UTC of the last heartbeat; `null` without a lock. */
  heartbeat: string | null;
  started_at: string | null;
  /** A lock exists but its heartbeat is older than the staleness window. */
  stale: boolean;
  /** A lock exists but could not be read (counted as active, nothing to show). */
  unreadable?: boolean;
  /** Who holds the lock; '' when no live lock is held. */
  runner: 'api' | 'cli' | '';
  poll_s: number | null;
  /** `SCHEDULE_AUTO_TOOLS`, sorted: the read-only roster an unattended `ask` run may call. */
  tools: string[];
  /** How many runs per schedule the store keeps (older job files are pruned). */
  history: number;
}

export interface SchedulesResult {
  schedules: Schedule[];
  count: number;
  runner: ScheduleRunner;
}

/** `POST /v1/schedules` body. `every` uses `parse_interval` syntax (`30m`,
 * `1h`, `1d`); an unparseable value, an unknown `profile` or `mode: do` is a
 * 422 whose `detail` is the reason in plain words. */
export interface ScheduleCreateRequest {
  /** 1..4000 characters. */
  prompt: string;
  every: string;
  /** ≤120 characters, whitespace-collapsed by the server. */
  name?: string;
  /** Provider override; ≤64 characters. */
  advisor?: string;
  /** Agent profile name; '' or `auto` → '' (concierge). */
  profile?: string;
  /** Defaults to `ask`. */
  mode?: ScheduleMode;
  /** Defaults to true. */
  enabled?: boolean;
}

/** `PATCH /v1/schedules/{id}` body: any subset. A wrong type is a 422. */
export interface SchedulePatchRequest {
  enabled?: boolean;
  every?: string;
  prompt?: string;
  name?: string;
  advisor?: string;
  profile?: string;
}

/** `DELETE /v1/schedules/{id}`. */
export interface ScheduleDeleteResult {
  deleted: boolean;
  id: string;
}

/** `POST /v1/schedules/{id}/run` → 202: the `schedule-run` job to watch
 * through `watchInstallJob<ScheduleRunEvent>(job_id, …)`. */
export interface ScheduleRunStarted {
  job_id: string;
  schedule_id: string;
}

/** The same route's 409 `detail` while a run of this schedule is still in
 * flight (the poll's or an earlier "Run now"): `job_id` names that run. */
export interface ScheduleRunConflict {
  message: string;
  job_id: string;
  schedule_id: string;
}

/** What `runSchedule` resolves to: exactly one of the two keys is set. */
export type ScheduleRunOutcome =
  | { started: ScheduleRunStarted; conflict?: undefined }
  | { conflict: ScheduleRunConflict; started?: undefined };

/** `GET /v1/schedules/{id}/runs?limit` — the schedule's `schedule-run` jobs,
 * newest first, each with `recent_events`. */
export interface ScheduleRunsResult {
  runs: InstallJob[];
  count: number;
  schedule_id: string;
}

/** `InstallJob.request` of a `schedule-run` job. */
export interface ScheduleRunJobRequest {
  schedule_id: string;
  mode: string;
  advisor: string;
  profile: string;
  conversation_id: string;
}

/** A confirm/privileged call an unattended run recorded and never ran
 * (no plan, no token), or an auto-class call the turn deferred. */
export interface ScheduleRunDeferredCall {
  name: string;
  arguments: Record<string, unknown>;
  privileged?: boolean;
  unattended?: boolean;
  reason?: string;
  [key: string]: unknown;
}

/**
 * One `schedule-run` job event as the UI reads it (`InstallJobEvent.payload`
 * flattened over `event` / `status` / `message`, the shape `watchInstallJob`
 * hands out). The final `complete` event carries `outcome` — `ok`,
 * `deferred` (a request awaits approval in the thread) or `skipped` (budget
 * exceeded: no `conversation_id`, `cost_usd`/`cost_known` null) — plus the
 * turn's answer, attribution and cost; an `error` event (`status: failed`)
 * carries only `message` (and `schedule_id`). `mode: do` is refused by the
 * API runner with exactly such an `error` event.
 */
export interface ScheduleRunEvent {
  event: 'step' | 'complete' | 'error' | string;
  status: string;
  message: string;
  outcome?: 'ok' | 'deferred' | 'skipped';
  schedule_id?: string;
  /** Absent on a `skipped` outcome and on `error`. The thread link is `/?c=<id>`. */
  conversation_id?: string;
  answer?: string;
  provider?: string;
  model?: string;
  cost_usd?: number | null;
  /** `null` on `skipped`; false = no catalog price for the model that answered. */
  cost_known?: boolean | null;
  latency_ms?: number;
  iterations?: number;
  used_profile?: string | null;
  mode?: string;
  deferred?: ScheduleRunDeferredCall[];
  [key: string]: unknown;
}

// ── Wizard card payloads (design §1–§3) ──

/** `arguments` of an `ask_user` confirm card. The question is answered in the
 * chat (your answer is the next user message plus a `tool_results` entry
 * `{name: 'ask_user', ok, summary}`), never through `/v1/wizard/tools/execute`. */
export interface WizardAskUserArguments {
  /** 1..500 characters. */
  question: string;
  /** Up to six chips, each ≤80 characters. */
  choices?: string[];
  /** Defaults to true: show a free-text box beside the chips. */
  allow_free_text?: boolean;
}

/** What a stale client gets for POSTing `ask_user` to execute — harmless. */
export interface WizardAskUserResult {
  answered_in_chat: true;
  question: string;
  note: string;
}

/** A confirmed `generate_image` run: the file is under
 * `$NVH_HOME/outputs/images/<scope>/` and nothing prunes it (the `note`
 * says so). `url` needs the API key headers — fetch it with `fetchApiBlob`
 * and render an object URL, never a bare `<img src>`. */
export interface WizardImageResult {
  ok: true;
  /** `/v1/outputs/images/<scope>/<file>` (auth-gated). */
  url: string;
  path: string;
  backend: 'nvidia' | 'comfyui' | string;
  model: string;
  width: number;
  height: number;
  /** `null` with `cost_known: false` for the NVIDIA-hosted backend (no catalog
   * price); `0` with `cost_known: true` for local ComfyUI. */
  cost_usd: number | null;
  cost_known: boolean;
  latency_ms: number;
  note: string;
}

/** `generate_image` refused or failed (profile pin missing, no backend, ComfyUI down). */
export interface WizardImageFailure {
  ok: false;
  error: string;
}

/** A confirmed `remember` run. There is no `ok` key: check `saved`. The
 * card links to `/vault?id=<id>`. */
export interface WizardRememberResult {
  saved: true;
  /** 32 hex chars. */
  id: string;
  path: string;
  title: string;
  category: string;
  tags: string[];
  /** Whether the note's RAG chunks are indexed yet (best effort). */
  indexed: boolean;
  /** How many secret-looking spans were redacted before the write. */
  redacted: number;
}

/** `remember` refused (missing title/body, a forbidden literal, a secret-only body). */
export interface WizardRememberFailure {
  saved: false;
  error: string;
}

// ─── 0.46 Wave D — voice ─────────────────────────────────────────────────────
// Append-only block (design §8). Every shape mirrors what nvh/core/voice.py
// `voice_status()` and the routes in nvh/api/routes/voice.py send; nothing is
// re-derived in the browser. The pure readers live in lib/voice.ts.

/** `voice_status()['stt']`: which speech-to-text paths this API process has. */
export interface VoiceSttStatus {
  /** `faster_whisper` imports (the `voice` extra is installed on a platform with wheels). */
  local_installed: boolean;
  /** The weights for `model` are under the cache dir — no download needed. */
  local_model_present: boolean;
  /** Both of the above: the local path answers `provider: auto`. */
  local_available: boolean;
  /** A Groq key is configured (env or keyring), so `groq` can answer. */
  groq_available: boolean;
  /** The faster-whisper model name (`small.en` by default; `NVH_VOICE_STT_MODEL`). */
  model: string;
  /** Why the local path is unavailable, e.g. "no faster-whisper wheel for this platform"; null when it is available. */
  reason: string | null;
  [key: string]: unknown;
}

/** `voice_status()['tts']` — server-side TTS is optional; the web speaks through `speechSynthesis`. */
export interface VoiceTtsStatus {
  edge_available: boolean;
  system_available: boolean;
  [key: string]: unknown;
}

/** `GET /v1/voice/status` → `data`. */
export interface VoiceStatus {
  stt: VoiceSttStatus;
  tts: VoiceTtsStatus;
  /** A `voice-model-download` job is in flight (when the route reports it). */
  downloading?: boolean;
  /** That job's id, when `downloading` — `download_job_id`, as `_status_payload` spells it. */
  download_job_id?: string | null;
  [key: string]: unknown;
}

/** `POST /v1/voice/transcribe` → `data`: one transcript with its provenance and cost verdict. */
export interface VoiceTranscript {
  text: string;
  /** `local` | `groq` | `whisper-cli` — whatever `speech_to_text` resolved `auto` to. */
  provider: string;
  model: string;
  latency_ms: number;
  /** 0 for the local path; null for Groq (no catalog row), paired with `cost_known: false`. */
  cost_usd: number | null;
  cost_known: boolean;
}

/** The same route's 503 `detail` while no STT path can answer (weights absent, nothing installed). */
export interface VoiceUnavailableDetail {
  downloading: boolean;
  hint: string;
  status?: VoiceStatus | null;
}

/** `POST /v1/voice/download` → `data` (202): the `voice-model-download` job to watch. */
export interface VoiceDownloadStarted {
  job_id: string;
}

/** The same route's 409 `detail` while a download is already in flight: `job_id` names that job. */
export interface VoiceDownloadConflict {
  message: string;
  job_id: string;
}

/** What `startVoiceDownload` resolves to: exactly one of the two keys is set. */
export type VoiceDownloadOutcome =
  | { started: VoiceDownloadStarted; conflict?: undefined }
  | { conflict: VoiceDownloadConflict; started?: undefined };

/** One `voice-model-download` job event as `watchInstallJob` flattens it (`InstallJob.kind` admits the kind). */
export interface VoiceDownloadEvent {
  event: string;
  status: string;
  message: string;
  /** The model being fetched, when the step names it. */
  model?: string;
  [key: string]: unknown;
}
