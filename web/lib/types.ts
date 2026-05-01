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

export interface CouncilResult {
  member_responses: Record<string, CompletionResponse>;
  failed_members: string[];
  strategy: string;
  total_cost_usd: string | null;
  total_latency_ms: number;
  quorum_met: boolean;
  agents_used: boolean;
  synthesis: CompletionResponse | null;
  members: CouncilMemberInfo[];
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

export interface AgentPersona {
  role: string;
  expertise: string;
  perspective: string;
  system_prompt: string;
  weight_boost: number;
}

export interface AgentPreset {
  name: string;
  description: string;
  roles: string[];
}

export interface AgentPresetsResult {
  presets: AgentPreset[];
}

export interface AgentAnalyzeResult {
  agents: AgentPersona[];
  count: number;
  prompt_preview: string;
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
}

export interface WsCouncilComplete {
  type: 'council_complete';
  total_cost: string;
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
  latency_ms: number;
  elapsedMs: number;  // live timer while streaming
  error?: string;
}

// ─── GPU / System ────────────────────────────────────────────────────────────

export interface GPUDevice {
  name: string;
  vram_mb: number;
  vram_gb: number;
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
  effective_for_llm_gb: number;
}

export interface GPUInfo {
  gpus: GPUDevice[];
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
  fits_hybrid: boolean;
  gpu_free_gb: number;
  ram_free_gb: number;
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

export interface SetupHelperReport {
  ready: boolean;
  summary: string;
  storage: StorageStatus;
  runtime: RuntimeStatus;
  comfyui: Record<string, unknown>;
  model_recommendation_count: number;
  actions: SetupAction[];
  issues?: SetupIssue[];
  issue_count?: number;
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
    can_read_jobs: boolean;
    can_read_receipts: boolean;
    can_refresh_catalog: boolean;
    description: string;
  };
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
  kind: 'wizard-mission' | 'comfyui-install' | 'studio-pack-install' | 'studio-model-install' | string;
  title: string;
  status: InstallJobStatus;
  message: string;
  progress: number;
  request: Record<string, unknown>;
  storage_home: string;
  created_at: string;
  updated_at: string;
  started_at: string | null;
  completed_at: string | null;
  event_count: number;
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
  url: string;
  install_root: string;
  app_dir: string;
  venv_python: string;
  examples_dir: string;
  examples_installed: boolean;
  manager_available: boolean;
  log_path: string;
  pid: number | null;
  examples: ComfyUIExample[];
  already_running?: boolean;
  started?: boolean;
  ready?: boolean;
  ready_timeout?: boolean;
  ready_wait_seconds?: number;
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

export type QueryMode = 'simple' | 'council' | 'compare';
export type ConnectionStatus = 'connected' | 'disconnected' | 'checking';

export interface RecentQuery {
  id: string;
  prompt: string;
  mode: QueryMode;
  provider?: string;
  timestamp: number;
  cost?: string;
  tokens?: number;
}

// ─── Chat UI types ────────────────────────────────────────────────────────────

export type ChatMode = 'single' | 'council' | 'compare';

export interface ChatMessage {
  id: string;
  role: 'user' | 'assistant' | 'error';
  content: string;
  provider?: string;
  model?: string;
  mode?: ChatMode;
  cost_usd?: string | null;
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
      latency_ms?: number;
    }>;
    synthesis?: string;
    total_cost?: string;
    member_order?: string[];
  };
  compare_data?: Record<string, {
    content: string;
    model: string;
    tokens?: number;
    cost_usd?: string | null;
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
  mode: ChatMode;
  message_count: number;
  created_at: number;
  updated_at: number;
  pinned?: boolean;
}

// ─── Setup / Free providers ───────────────────────────────────────────────────

export interface FreeProvider {
  id: string;
  name: string;
  signup_tier: 'none' | 'email' | 'account';
  free_tier_limits?: string;
  strengths?: string[];
  configured: boolean;
  env_key?: string;
  placeholder?: string;
  signup_url?: string;
}
