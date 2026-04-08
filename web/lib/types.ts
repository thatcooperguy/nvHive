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
  utilization_pct: number;
  driver_version: string;
  cuda_version: string;
  index: number;
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

// ─── UI State helpers ────────────────────────────────────────────────────────

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
