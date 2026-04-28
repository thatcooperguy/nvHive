import type {
  ApiEnvelope,
  QueryRequest,
  CouncilRequest,
  CompareRequest,
  CompletionResponse,
  CouncilResult,
  CompareResult,
  ProvidersListResult,
  ModelsListResult,
  BudgetStatus,
  CacheStats,
  AgentPresetsResult,
  AgentAnalyzeResult,
  ProviderHealth,
  HealthResult,
  StreamChunkPayload,
  StreamDonePayload,
  WsQueryMessage,
  WsCouncilMessage,
  WsCouncilStart,
  WsMemberStart,
  WsMemberChunk,
  WsMemberComplete,
  WsMemberFailed,
  WsSynthesisChunk,
  WsSynthesisComplete,
  WsCouncilComplete,
  GPUInfo,
  InstallJob,
  InstallJobEvent,
  InstallJobEventsResult,
  InstallJobsResult,
  RecommendationsResult,
  SystemInfo,
  StorageConfigureRequest,
  StorageStatus,
  ComfyUIExamplesResult,
  ComfyUIInstallEvent,
  ComfyUIInstallRequest,
  ComfyUIModelPlanResult,
  ComfyUIStatus,
  StudioPackInstallEvent,
  StudioPackInstallRequest,
  StudioPacksResult,
  StudioModelInstallEvent,
  StudioModelInstallRequest,
  StudioModelsResult,
} from './types';

function getApiBase(): string {
  // Runtime: check window for injected config (set by layout script tag)
  if (typeof window !== 'undefined') {
    const runtimeUrl = (window as any).__HIVE_API_URL__;
    if (runtimeUrl) return runtimeUrl;
  }
  // Fallback to build-time env var, then localhost
  return process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000';
}

const BASE_URL = getApiBase();

// ─── Low-level fetch helper ──────────────────────────────────────────────────

async function apiFetch<T>(
  path: string,
  options?: RequestInit
): Promise<T> {
  const url = `${BASE_URL}${path}`;
  const res = await fetch(url, {
    headers: {
      'Content-Type': 'application/json',
      ...options?.headers,
    },
    ...options,
  });

  if (!res.ok) {
    let detail = `HTTP ${res.status}`;
    try {
      const body = await res.json();
      detail = body?.detail ?? detail;
    } catch {
      // ignore
    }
    throw new Error(detail);
  }

  return res.json() as Promise<T>;
}

async function apiGet<T>(path: string): Promise<T> {
  const envelope = await apiFetch<ApiEnvelope<T>>(path);
  return envelope.data;
}

async function apiPost<T>(path: string, body: unknown): Promise<T> {
  const envelope = await apiFetch<ApiEnvelope<T>>(path, {
    method: 'POST',
    body: JSON.stringify(body),
  });
  return envelope.data;
}

// ─── Health ──────────────────────────────────────────────────────────────────

export async function checkHealth(): Promise<HealthResult> {
  return apiGet<HealthResult>('/v1/health');
}

// ─── System / GPU ─────────────────────────────────────────────────────────────

export async function getGPUInfo(): Promise<GPUInfo> {
  return apiGet<GPUInfo>('/v1/system/gpu');
}

export async function getRecommendations(): Promise<RecommendationsResult> {
  return apiGet<RecommendationsResult>('/v1/system/recommendations');
}

export async function getSystemInfo(): Promise<SystemInfo> {
  return apiGet<SystemInfo>('/v1/system/info');
}

export async function getStorageStatus(homeDir?: string): Promise<StorageStatus> {
  const qs = homeDir ? `?home_dir=${encodeURIComponent(homeDir)}` : '';
  return apiGet<StorageStatus>(`/v1/system/storage${qs}`);
}

export async function configureStorage(request: StorageConfigureRequest): Promise<StorageStatus> {
  return apiPost<StorageStatus>('/v1/system/storage', request);
}

// ─── Analytics ──────────────────────────────────────────────────────────────

// ComfyUI visual workflow setup

const TERMINAL_JOB_STATUSES = new Set(['complete', 'failed', 'canceled', 'interrupted']);

export async function getInstallJobs(options: {
  kind?: string;
  status?: string;
  limit?: number;
} = {}): Promise<InstallJobsResult> {
  const params = new URLSearchParams();
  if (options.kind) params.set('kind', options.kind);
  if (options.status) params.set('status_filter', options.status);
  if (options.limit) params.set('limit', String(options.limit));
  const qs = params.toString();
  return apiGet<InstallJobsResult>(`/v1/jobs${qs ? `?${qs}` : ''}`);
}

export async function getInstallJob(jobId: string): Promise<InstallJob> {
  return apiGet<InstallJob>(`/v1/jobs/${encodeURIComponent(jobId)}`);
}

export async function getInstallJobEvents(
  jobId: string,
  after = 0,
  limit = 200
): Promise<InstallJobEventsResult> {
  return apiGet<InstallJobEventsResult>(
    `/v1/jobs/${encodeURIComponent(jobId)}/events?after=${after}&limit=${limit}`
  );
}

export async function cancelInstallJob(jobId: string): Promise<InstallJob> {
  return apiPost<InstallJob>(`/v1/jobs/${encodeURIComponent(jobId)}/cancel`, {});
}

export async function startComfyUIInstallJob(request: ComfyUIInstallRequest): Promise<InstallJob> {
  return apiPost<InstallJob>('/v1/comfyui/install/job', request);
}

export async function startStudioPackInstallJob(request: StudioPackInstallRequest): Promise<InstallJob> {
  return apiPost<InstallJob>('/v1/studio/install/job', request);
}

export async function startStudioModelInstallJob(request: StudioModelInstallRequest): Promise<InstallJob> {
  return apiPost<InstallJob>('/v1/studio/models/install/job', request);
}

function payloadFromJobEvent<TEvent extends { event: string; status: string; message: string }>(
  event: InstallJobEvent
): TEvent {
  return {
    event: event.event,
    status: event.status,
    message: event.message,
    ...event.payload,
  } as TEvent;
}

export function watchInstallJob<TEvent extends { event: string; status: string; message: string }>(
  jobId: string,
  callbacks: {
    onEvent?: (event: TEvent, jobEvent: InstallJobEvent) => void;
    onStatus?: (job: InstallJob) => void;
    onComplete?: (event: TEvent, job: InstallJob) => void;
    onError?: (error: string, job?: InstallJob) => void;
  }
): () => void {
  let stopped = false;
  let after = 0;
  let timer: ReturnType<typeof setTimeout> | null = null;

  const poll = async () => {
    if (stopped) return;
    try {
      const [job, batch] = await Promise.all([
        getInstallJob(jobId),
        getInstallJobEvents(jobId, after),
      ]);
      callbacks.onStatus?.(job);

      let lastPayload: TEvent | null = null;
      for (const event of batch.events) {
        after = Math.max(after, event.sequence);
        const payload = payloadFromJobEvent<TEvent>(event);
        lastPayload = payload;
        callbacks.onEvent?.(payload, event);
      }

      if (TERMINAL_JOB_STATUSES.has(job.status)) {
        if (job.status === 'complete') {
          callbacks.onComplete?.(
            lastPayload ?? ({ event: 'complete', status: 'complete', message: job.message } as TEvent),
            job
          );
        } else {
          callbacks.onError?.(job.message || `Install job ${job.status}`, job);
        }
        return;
      }

      timer = setTimeout(poll, 1200);
    } catch (err) {
      if (!stopped) {
        callbacks.onError?.(err instanceof Error ? err.message : 'Install job polling failed');
      }
    }
  };

  void poll();

  return () => {
    stopped = true;
    if (timer) clearTimeout(timer);
  };
}

function runBackgroundInstall<TEvent extends { event: string; status: string; message: string }>(
  start: () => Promise<InstallJob>,
  callbacks: {
    onJob?: (job: InstallJob) => void;
    onStatus?: (job: InstallJob) => void;
    onEvent?: (event: TEvent) => void;
    onComplete?: (event: TEvent) => void;
    onError?: (error: string) => void;
  }
): () => void {
  let stopped = false;
  let stopWatch: (() => void) | null = null;

  (async () => {
    try {
      const job = await start();
      if (stopped) return;
      callbacks.onJob?.(job);
      callbacks.onStatus?.(job);
      stopWatch = watchInstallJob<TEvent>(job.id, {
        onEvent: event => callbacks.onEvent?.(event),
        onStatus: nextJob => callbacks.onStatus?.(nextJob),
        onComplete: event => callbacks.onComplete?.(event),
        onError: error => callbacks.onError?.(error),
      });
    } catch (err) {
      if (!stopped) {
        callbacks.onError?.(err instanceof Error ? err.message : 'Install job failed to start');
      }
    }
  })();

  return () => {
    stopped = true;
    stopWatch?.();
  };
}

export async function getComfyUIStatus(): Promise<ComfyUIStatus> {
  return apiGet<ComfyUIStatus>('/v1/comfyui/status');
}

export async function getComfyUIExamples(): Promise<ComfyUIExamplesResult> {
  return apiGet<ComfyUIExamplesResult>('/v1/comfyui/examples');
}

export async function startComfyUI(
  host = '127.0.0.1',
  port = 8188
): Promise<ComfyUIStatus> {
  return apiPost<ComfyUIStatus>('/v1/comfyui/start', { host, port });
}

export async function saveComfyUIModelPlan(example_ids: string[]): Promise<ComfyUIModelPlanResult> {
  return apiPost<ComfyUIModelPlanResult>('/v1/comfyui/model-plan', { example_ids });
}

export function installComfyUIStream(
  request: ComfyUIInstallRequest,
  callbacks: {
    onJob?: (job: InstallJob) => void;
    onStatus?: (job: InstallJob) => void;
    onEvent?: (event: ComfyUIInstallEvent) => void;
    onComplete?: (event: ComfyUIInstallEvent) => void;
    onError?: (error: string) => void;
  }
): () => void {
  return runBackgroundInstall<ComfyUIInstallEvent>(
    () => startComfyUIInstallJob(request),
    callbacks
  );
}

// Rootless AI Studio pack setup

export async function getStudioPacks(): Promise<StudioPacksResult> {
  return apiGet<StudioPacksResult>('/v1/studio/packs');
}

export function installStudioPacksStream(
  request: StudioPackInstallRequest,
  callbacks: {
    onJob?: (job: InstallJob) => void;
    onStatus?: (job: InstallJob) => void;
    onEvent?: (event: StudioPackInstallEvent) => void;
    onComplete?: (event: StudioPackInstallEvent) => void;
    onError?: (error: string) => void;
  }
): () => void {
  return runBackgroundInstall<StudioPackInstallEvent>(
    () => startStudioPackInstallJob(request),
    callbacks
  );
}

export async function getStudioModels(): Promise<StudioModelsResult> {
  return apiGet<StudioModelsResult>('/v1/studio/models');
}

export function installStudioModelsStream(
  request: StudioModelInstallRequest,
  callbacks: {
    onJob?: (job: InstallJob) => void;
    onStatus?: (job: InstallJob) => void;
    onEvent?: (event: StudioModelInstallEvent) => void;
    onComplete?: (event: StudioModelInstallEvent) => void;
    onError?: (error: string) => void;
  }
): () => void {
  return runBackgroundInstall<StudioModelInstallEvent>(
    () => startStudioModelInstallJob(request),
    callbacks
  );
}

export interface AnalyticsData {
  queries_today: number;
  queries_this_week: number;
  queries_this_month: number;
  cost_by_provider: Record<string, string>;
  queries_by_provider: Record<string, number>;
  latency_by_provider: Record<string, number>;
  most_used_models: Array<{ model: string; provider: string; count: number }>;
  free_queries: number;
  paid_queries: number;
  savings: {
    local_queries: number;
    cloud_queries: number;
    estimated_cloud_cost: string;
    total_savings: string;
    savings_pct: number;
  };
}

export async function getAnalytics(): Promise<AnalyticsData> {
  return apiGet<AnalyticsData>('/v1/analytics');
}

// ─── Query ───────────────────────────────────────────────────────────────────

export async function query(
  prompt: string,
  options: Omit<QueryRequest, 'prompt' | 'stream'> = {}
): Promise<CompletionResponse> {
  return apiPost<CompletionResponse>('/v1/query', {
    prompt,
    ...options,
    stream: false,
  });
}

// SSE streaming query — calls onChunk for each text delta, onDone when complete
export function queryStream(
  request: QueryRequest,
  onChunk: (payload: StreamChunkPayload) => void,
  onDone: (payload: StreamDonePayload) => void,
  onError: (error: string) => void
): () => void {
  let aborted = false;
  const controller = new AbortController();

  (async () => {
    try {
      const res = await fetch(`${BASE_URL}/v1/query`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ...request, stream: true }),
        signal: controller.signal,
      });

      if (!res.ok) {
        let detail = `HTTP ${res.status}`;
        try {
          const body = await res.json();
          detail = body?.detail ?? detail;
        } catch {
          // ignore
        }
        onError(detail);
        return;
      }

      const reader = res.body?.getReader();
      if (!reader) { onError('No response body'); return; }

      const decoder = new TextDecoder();
      let buffer = '';

      while (!aborted) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });

        const lines = buffer.split('\n');
        buffer = lines.pop() ?? '';

        let eventType = '';
        for (const line of lines) {
          if (line.startsWith('event: ')) {
            eventType = line.slice(7).trim();
          } else if (line.startsWith('data: ')) {
            const data = line.slice(6).trim();
            try {
              const parsed = JSON.parse(data);
              if (eventType === 'chunk') onChunk(parsed as StreamChunkPayload);
              else if (eventType === 'done') onDone(parsed as StreamDonePayload);
              else if (eventType === 'error') onError(parsed.error ?? 'Stream error');
            } catch {
              // ignore malformed JSON
            }
            eventType = '';
          }
        }
      }
    } catch (err) {
      if (!aborted) {
        onError(err instanceof Error ? err.message : 'Unknown streaming error');
      }
    }
  })();

  return () => {
    aborted = true;
    controller.abort();
  };
}

// ─── Council ─────────────────────────────────────────────────────────────────

export async function runCouncil(
  prompt: string,
  options: Omit<CouncilRequest, 'prompt'> = {}
): Promise<CouncilResult> {
  return apiPost<CouncilResult>('/v1/council', { prompt, ...options });
}

// ─── Compare ─────────────────────────────────────────────────────────────────

export async function compare(
  prompt: string,
  providers?: string[],
  options: Omit<CompareRequest, 'prompt' | 'providers'> = {}
): Promise<CompareResult> {
  return apiPost<CompareResult>('/v1/compare', { prompt, providers, ...options });
}

// ─── Advisors ────────────────────────────────────────────────────────────────

export async function getProviders(): Promise<ProvidersListResult> {
  return apiGet<ProvidersListResult>('/v1/advisors');
}

export async function testProvider(name: string): Promise<ProviderHealth> {
  return apiGet<ProviderHealth>(`/v1/advisors/${encodeURIComponent(name)}/health`);
}

// ─── Models ──────────────────────────────────────────────────────────────────

export async function getModels(provider?: string): Promise<ModelsListResult> {
  const qs = provider ? `?provider=${encodeURIComponent(provider)}` : '';
  return apiGet<ModelsListResult>(`/v1/models${qs}`);
}

// ─── Budget ──────────────────────────────────────────────────────────────────

export async function getBudgetStatus(): Promise<BudgetStatus> {
  return apiGet<BudgetStatus>('/v1/budget/status');
}

// ─── Cache ───────────────────────────────────────────────────────────────────

export async function getCacheStats(): Promise<CacheStats> {
  return apiGet<CacheStats>('/v1/cache/stats');
}

export async function clearCache(provider?: string): Promise<{ cleared: number; provider: string | null }> {
  const qs = provider ? `?provider=${encodeURIComponent(provider)}` : '';
  const envelope = await apiFetch<ApiEnvelope<{ cleared: number; provider: string | null }>>(
    `/v1/cache${qs}`,
    { method: 'DELETE' }
  );
  return envelope.data;
}

// ─── Agents ──────────────────────────────────────────────────────────────────

export async function getAgentPresets(): Promise<AgentPresetsResult> {
  return apiGet<AgentPresetsResult>('/v1/agents/presets');
}

export async function analyzeAgents(
  prompt: string,
  numAgents = 3,
  preset?: string
): Promise<AgentAnalyzeResult> {
  return apiPost<AgentAnalyzeResult>('/v1/agents/analyze', {
    prompt,
    num_agents: numAgents,
    preset,
  });
}

// ─── Conversations ────────────────────────────────────────────────────────────

export interface ConversationSummary {
  id: string;
  title: string;
  model?: string;
  provider?: string;
  mode: 'single' | 'council' | 'compare';
  message_count: number;
  created_at: number;
  updated_at: number;
  pinned?: boolean;
}

export interface ConversationMessage {
  id: string;
  role: 'user' | 'assistant' | 'system';
  content: string;
  provider?: string;
  model?: string;
  mode?: 'single' | 'council' | 'compare';
  cost_usd?: string | null;
  tokens?: number;
  latency_ms?: number;
  council_data?: {
    member_responses: Record<string, { content: string; provider: string; model: string; tokens: number; cost: string }>;
    synthesis?: string;
    total_cost?: string;
  };
  timestamp: number;
}

export interface ConversationDetail {
  id: string;
  title: string;
  messages: ConversationMessage[];
  created_at: number;
  updated_at: number;
}

export async function getConversations(): Promise<ConversationSummary[]> {
  try {
    const data = await apiGet<{ conversations: ConversationSummary[] }>('/v1/conversations');
    return data.conversations ?? [];
  } catch {
    // Endpoint might not exist yet — return empty array gracefully
    return [];
  }
}

export async function getConversation(id: string): Promise<ConversationDetail | null> {
  try {
    return await apiGet<ConversationDetail>(`/v1/conversations/${encodeURIComponent(id)}`);
  } catch {
    return null;
  }
}

export async function createConversation(title?: string): Promise<ConversationSummary | null> {
  try {
    return await apiPost<ConversationSummary>('/v1/conversations', { title: title ?? 'New Chat' });
  } catch {
    return null;
  }
}

export async function deleteConversation(id: string): Promise<void> {
  try {
    await apiFetch(`/v1/conversations/${encodeURIComponent(id)}`, { method: 'DELETE' });
  } catch {
    // Ignore if not supported
  }
}

export async function renameConversation(id: string, title: string): Promise<void> {
  try {
    await apiFetch(`/v1/conversations/${encodeURIComponent(id)}`, {
      method: 'PATCH',
      body: JSON.stringify({ title }),
    });
  } catch {
    // Ignore if not supported
  }
}

export async function sendMessage(
  conversationId: string | null,
  prompt: string,
  options: Omit<QueryRequest, 'prompt' | 'stream'> & { conversation_id?: string | null } = {}
): Promise<CompletionResponse> {
  return apiPost<CompletionResponse>('/v1/query', {
    prompt,
    ...options,
    conversation_id: conversationId ?? undefined,
    stream: false,
  });
}

// ─── Setup / Free-tier APIs ───────────────────────────────────────────────────

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

export interface FreeProvidersResult {
  providers: FreeProvider[];
}

export async function getFreeProviders(): Promise<FreeProvidersResult> {
  try {
    return await apiGet<FreeProvidersResult>('/v1/setup/free-providers');
  } catch {
    // Fallback if endpoint doesn't exist yet
    return { providers: [] };
  }
}

export async function saveProviderKey(provider_id: string, api_key: string): Promise<{ ok: boolean }> {
  return apiPost<{ ok: boolean }>('/v1/setup/save-key', { provider: provider_id, api_key });
}

// ─── WebSocket helpers ────────────────────────────────────────────────────────

/** Derive the WebSocket base URL from the HTTP base URL. */
function wsBaseUrl(): string {
  const http = BASE_URL.replace(/^http/, 'ws');
  return http;
}

/**
 * Stream a single-provider query over WebSocket.
 *
 * Returns the underlying WebSocket so the caller can close it early if needed.
 */
export function streamQuery(
  request: QueryRequest,
  callbacks: {
    onChunk?: (delta: string, accumulated: string) => void;
    onComplete?: (payload: Omit<WsQueryMessage & { type: 'complete' }, 'type'>) => void;
    onError?: (error: string) => void;
  }
): WebSocket {
  const ws = new WebSocket(`${wsBaseUrl()}/v1/ws/query`);

  ws.onopen = () => {
    ws.send(JSON.stringify({ type: 'query_request', ...request }));
  };

  ws.onmessage = (event) => {
    try {
      const msg = JSON.parse(event.data as string) as WsQueryMessage;
      if (msg.type === 'chunk') {
        callbacks.onChunk?.(msg.delta, msg.accumulated);
      } else if (msg.type === 'complete') {
        callbacks.onComplete?.(msg);
        ws.close();
      } else if (msg.type === 'error') {
        callbacks.onError?.(msg.error);
        ws.close();
      }
    } catch {
      // ignore malformed frames
    }
  };

  ws.onerror = () => {
    callbacks.onError?.('WebSocket connection error');
  };

  ws.onclose = (event) => {
    if (!event.wasClean && event.code !== 1000) {
      callbacks.onError?.(`WebSocket closed unexpectedly (code ${event.code})`);
    }
  };

  return ws;
}

/**
 * Stream a council session over WebSocket.
 *
 * Returns the underlying WebSocket so the caller can close it early if needed.
 */
export function streamCouncil(
  request: CouncilRequest,
  callbacks: {
    onStart?: (data: WsCouncilStart) => void;
    onMemberStart?: (member: string, persona: string) => void;
    onMemberChunk?: (member: string, delta: string, accumulated: string) => void;
    onMemberComplete?: (member: string, content: string, tokens: number, cost: string, latency: number) => void;
    onMemberFailed?: (member: string, error: string) => void;
    onSynthesisStart?: () => void;
    onSynthesisChunk?: (delta: string, accumulated: string) => void;
    onSynthesisComplete?: (content: string, tokens: number, cost: string) => void;
    onComplete?: (totalCost: string, totalLatency: number, quorumMet: boolean) => void;
    onError?: (error: string) => void;
  }
): WebSocket {
  const ws = new WebSocket(`${wsBaseUrl()}/v1/ws/council`);

  ws.onopen = () => {
    ws.send(JSON.stringify({ type: 'council_request', ...request }));
  };

  ws.onmessage = (event) => {
    try {
      const msg = JSON.parse(event.data as string) as WsCouncilMessage;

      switch (msg.type) {
        case 'council_start':
          callbacks.onStart?.(msg);
          break;
        case 'member_start':
          callbacks.onMemberStart?.(msg.member, msg.persona);
          break;
        case 'member_chunk':
          callbacks.onMemberChunk?.(msg.member, msg.delta, msg.accumulated);
          break;
        case 'member_complete':
          callbacks.onMemberComplete?.(msg.member, msg.content, msg.tokens, msg.cost, msg.latency_ms);
          break;
        case 'member_failed':
          callbacks.onMemberFailed?.(msg.member, msg.error);
          break;
        case 'synthesis_start':
          callbacks.onSynthesisStart?.();
          break;
        case 'synthesis_chunk':
          callbacks.onSynthesisChunk?.(msg.delta, msg.accumulated);
          break;
        case 'synthesis_complete':
          callbacks.onSynthesisComplete?.(msg.content, msg.tokens, msg.cost);
          break;
        case 'council_complete':
          callbacks.onComplete?.(msg.total_cost, msg.total_latency_ms, msg.quorum_met);
          ws.close();
          break;
        case 'error':
          callbacks.onError?.(msg.error);
          ws.close();
          break;
      }
    } catch {
      // ignore malformed frames
    }
  };

  ws.onerror = () => {
    callbacks.onError?.('WebSocket connection error');
  };

  ws.onclose = (event) => {
    if (!event.wasClean && event.code !== 1000) {
      callbacks.onError?.(`WebSocket closed unexpectedly (code ${event.code})`);
    }
  };

  return ws;
}
