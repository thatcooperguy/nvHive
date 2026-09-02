import type {
  ApiEnvelope,
  QueryRequest,
  CouncilRequest,
  CompareRequest,
  CompletionResponse,
  CompareResult,
  ProvidersListResult,
  ModelsListResult,
  BudgetStatus,
  CacheStats,
  AgentPresetsResult,
  ProviderHealth,
  HealthResult,
  StreamChunkPayload,
  StreamDonePayload,
  WsQueryMessage,
  WsCouncilMessage,
  WsCouncilStart,
  ConversationSummary,
  ConversationMode,
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
  RuntimeStatus,
  RuntimeDoctorReport,
  WorkspaceStateReport,
  RootlessPolicyReport,
  SetupAssistantReply,
  SetupCatalogResult,
  ServiceActionResult,
  ServiceHealthReport,
  ServiceRegistryReport,
  SetupHelperReport,
  SetupReceiptsResult,
  CompatibilityReport,
  BootPreflightReport,
  MissionControlReport,
  ProductionReadinessReport,
  DiagnosticsReport,
  AutoRepairResult,
  MountAutopilotReport,
  WorkspacePassport,
  WizardMissionBuildRequest,
  WizardMissionInstallEvent,
  WizardMissionPlanResult,
  WizardPlanResult,
  SupportSnapshotResult,
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
  VaultMemoryRequest,
  VaultMemoryResult,
  VaultStatus,
} from './types';

function normalizeApiBase(value?: string | null): string | null {
  const trimmed = value?.trim();
  if (!trimmed) return null;
  return trimmed.replace(/\/+$/, '');
}

function getSameHostApiBase(): string | null {
  if (typeof window === 'undefined') return null;
  try {
    const current = new URL(window.location.href);
    return `${current.protocol}//${current.hostname}:8000`;
  } catch {
    return null;
  }
}

function rememberApiBase(base: string): void {
  if (typeof window === 'undefined') return;
  (window as any).__HIVE_API_URL__ = base;
}

function getApiBases(): string[] {
  const runtimeUrl = typeof window !== 'undefined'
    ? normalizeApiBase((window as any).__HIVE_API_URL__)
    : null;
  const envUrl = normalizeApiBase(process.env.NEXT_PUBLIC_API_URL);
  const sameHostUrl = normalizeApiBase(getSameHostApiBase());
  const candidates = [
    runtimeUrl,
    envUrl,
    sameHostUrl,
    'http://localhost:8000',
    'http://127.0.0.1:8000',
  ];
  const seen = new Set<string>();
  return candidates.filter((candidate): candidate is string => {
    if (!candidate || seen.has(candidate)) return false;
    seen.add(candidate);
    return true;
  });
}

function getApiBase(): string {
  return getApiBases()[0] ?? 'http://localhost:8000';
}

// Legacy localStorage keys that previously held the API key. These are
// migrated into sessionStorage (and then cleared) the first time we see them
// so existing users keep working while we stop persisting secrets across tabs.
const LEGACY_LOCAL_STORAGE_KEYS = ['nvh_api_key', 'HIVE_API_KEY', 'hive_api_key'];

function migrateLegacyApiKey(): string | null {
  if (typeof window === 'undefined') return null;
  try {
    for (const legacyKey of LEGACY_LOCAL_STORAGE_KEYS) {
      const value = window.localStorage.getItem(legacyKey);
      if (value?.trim()) {
        window.sessionStorage.setItem('nvh_api_key', value);
        for (const k of LEGACY_LOCAL_STORAGE_KEYS) {
          window.localStorage.removeItem(k);
        }
        return value;
      }
    }
  } catch {
    // Storage blocked — fall through.
  }
  return null;
}

function getApiAuthHeaders(): Record<string, string> {
  if (typeof window === 'undefined') return {};

  try {
    const runtimeKey = (window as any).__HIVE_API_KEY__;
    if (typeof runtimeKey === 'string' && runtimeKey.trim()) {
      const key = runtimeKey.trim();
      return { Authorization: `Bearer ${key}`, 'X-Hive-API-Key': key };
    }

    const params = new URLSearchParams(window.location.search);
    const queryKey = params.get('hive_api_key') ?? params.get('api_key') ?? params.get('token');
    if (queryKey?.trim()) {
      const key = queryKey.trim();
      // Per-tab persistence only — never cross-tab/origin-wide via localStorage.
      window.sessionStorage.setItem('nvh_api_key', key);
      return { Authorization: `Bearer ${key}`, 'X-Hive-API-Key': key };
    }

    const storedKey =
      window.sessionStorage.getItem('nvh_api_key') ??
      migrateLegacyApiKey();

    if (storedKey?.trim()) {
      const key = storedKey.trim();
      return { Authorization: `Bearer ${key}`, 'X-Hive-API-Key': key };
    }
  } catch {
    // Storage can be blocked in hardened browser profiles; open/local mode still works.
  }

  return {};
}

// ─── Low-level fetch helper ──────────────────────────────────────────────────

async function apiFetch<T>(
  path: string,
  options?: RequestInit
): Promise<T> {
  const bases = getApiBases();
  let lastNetworkError: unknown;

  for (const base of bases) {
    const url = `${base}${path}`;
    let res: Response;
    try {
      res = await fetch(url, {
        ...options,
        headers: {
          'Content-Type': 'application/json',
          ...getApiAuthHeaders(),
          ...options?.headers,
        },
      });
    } catch (err) {
      lastNetworkError = err;
      continue;
    }

    rememberApiBase(base);

    if (!res.ok) {
      let detail = `HTTP ${res.status}`;
      const requestId = res.headers.get('x-request-id') ?? undefined;
      const errorId = res.headers.get('x-error-id') ?? undefined;
      try {
        const body = await res.json();
        const bodyDetail = body?.error?.message ?? body?.detail ?? detail;
        detail = typeof bodyDetail === 'string' ? bodyDetail : JSON.stringify(bodyDetail);
      } catch {
        // ignore
      }
      const suffix = [
        requestId ? `request ${requestId}` : null,
        errorId ? `error ${errorId}` : null,
      ].filter(Boolean).join(', ');
      const error = new Error(suffix ? `${detail} (${suffix})` : detail) as Error & {
        statusCode?: number;
        requestId?: string;
        errorId?: string;
      };
      error.statusCode = res.status;
      error.requestId = requestId;
      error.errorId = errorId;
      throw error;
    }

    return res.json() as Promise<T>;
  }

  throw lastNetworkError instanceof Error
    ? lastNetworkError
    : new Error(`Hive API is offline. Tried ${bases.join(', ')}`);
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

export async function getMountAutopilot(minFreeGb = 200): Promise<MountAutopilotReport> {
  return apiGet<MountAutopilotReport>(`/v1/system/mount-autopilot?min_free_gb=${encodeURIComponent(String(minFreeGb))}`);
}

export async function activateMountAutopilot(homeDir?: string, minFreeGb = 200): Promise<{
  summary: string;
  storage: StorageStatus;
  mount_autopilot: MountAutopilotReport;
}> {
  return apiPost('/v1/system/mount-autopilot/activate', {
    home_dir: homeDir,
    min_free_gb: minFreeGb,
  });
}

export async function getRuntimeStatus(): Promise<RuntimeStatus> {
  return apiGet<RuntimeStatus>('/v1/system/runtime');
}

export async function getRuntimeDoctor(homeDir?: string): Promise<RuntimeDoctorReport> {
  const qs = homeDir ? `?home_dir=${encodeURIComponent(homeDir)}` : '';
  return apiGet<RuntimeDoctorReport>(`/v1/setup/runtime-doctor${qs}`);
}

export async function getWorkspaceState(homeDir?: string): Promise<WorkspaceStateReport> {
  const qs = homeDir ? `?home_dir=${encodeURIComponent(homeDir)}` : '';
  return apiGet<WorkspaceStateReport>(`/v1/setup/workspace-state${qs}`);
}

export async function getWizardPassport(homeDir?: string, create = true): Promise<WorkspacePassport> {
  const params = new URLSearchParams();
  params.set('create', create ? 'true' : 'false');
  if (homeDir) params.set('home_dir', homeDir);
  return apiGet<WorkspacePassport>(`/v1/wizard/passport?${params.toString()}`);
}

export async function getWizardRootlessPolicy(homeDir?: string): Promise<RootlessPolicyReport> {
  const qs = homeDir ? `?home_dir=${encodeURIComponent(homeDir)}` : '';
  return apiGet<RootlessPolicyReport>(`/v1/wizard/rootless-policy${qs}`);
}

export async function getWizardPlan(profile = 'student', homeDir?: string): Promise<WizardPlanResult> {
  const params = new URLSearchParams();
  params.set('profile', profile);
  if (homeDir) params.set('home_dir', homeDir);
  return apiGet<WizardPlanResult>(`/v1/wizard/plan?${params.toString()}`);
}

export async function getWizardMissionPlan(
  profile = 'student',
  torchProfile = 'nvidia-cu130',
  minFreeGb = 200,
  homeDir?: string
): Promise<WizardMissionPlanResult> {
  const params = new URLSearchParams();
  params.set('profile', profile);
  params.set('torch_profile', torchProfile);
  params.set('min_free_gb', String(minFreeGb));
  if (homeDir) params.set('home_dir', homeDir);
  return apiGet<WizardMissionPlanResult>(`/v1/wizard/mission-plan?${params.toString()}`);
}

export async function startWizardMissionJob(request: WizardMissionBuildRequest): Promise<InstallJob> {
  return apiPost<InstallJob>('/v1/wizard/mission/job', request);
}

export async function createSupportSnapshot(
  homeDir?: string,
  includeLogs = true
): Promise<SupportSnapshotResult> {
  return apiPost<SupportSnapshotResult>('/v1/wizard/support-snapshot', {
    home_dir: homeDir,
    include_logs: includeLogs,
  });
}

export async function getSetupHelper(homeDir?: string): Promise<SetupHelperReport> {
  const qs = homeDir ? `?home_dir=${encodeURIComponent(homeDir)}` : '';
  return apiGet<SetupHelperReport>(`/v1/setup/helper${qs}`);
}

// ─── Analytics ──────────────────────────────────────────────────────────────

// ComfyUI visual workflow setup

export async function askSetupAssistant(
  question: string,
  homeDir?: string
): Promise<SetupAssistantReply> {
  return apiPost<SetupAssistantReply>('/v1/setup/assistant', {
    question,
    home_dir: homeDir,
  });
}

export async function getSetupCatalog(refresh = false): Promise<SetupCatalogResult> {
  return apiGet<SetupCatalogResult>(`/v1/setup/catalog${refresh ? '?refresh=true' : ''}`);
}

export async function getSetupCompatibility(homeDir?: string): Promise<CompatibilityReport> {
  const qs = homeDir ? `?home_dir=${encodeURIComponent(homeDir)}` : '';
  return apiGet<CompatibilityReport>(`/v1/setup/compatibility${qs}`);
}

export async function getSetupBootPreflight(homeDir?: string, recheck = false): Promise<BootPreflightReport> {
  const params = new URLSearchParams();
  if (homeDir) params.set('home_dir', homeDir);
  if (recheck) params.set('recheck', 'true');
  const qs = params.toString();
  return apiGet<BootPreflightReport>(`/v1/setup/boot-preflight${qs ? `?${qs}` : ''}`);
}

export async function getSetupMissionControl(homeDir?: string): Promise<MissionControlReport> {
  const qs = homeDir ? `?home_dir=${encodeURIComponent(homeDir)}` : '';
  return apiGet<MissionControlReport>(`/v1/setup/mission-control${qs}`);
}

export async function getSetupProductionReadiness(
  homeDir?: string,
  targetVmValidated?: boolean
): Promise<ProductionReadinessReport> {
  const params = new URLSearchParams();
  if (homeDir) params.set('home_dir', homeDir);
  if (typeof targetVmValidated === 'boolean') {
    params.set('target_vm_validated', targetVmValidated ? 'true' : 'false');
  }
  const qs = params.toString();
  return apiGet<ProductionReadinessReport>(`/v1/setup/production-readiness${qs ? `?${qs}` : ''}`);
}

export async function getSetupDiagnostics(
  homeDir?: string,
  includeLogs = true
): Promise<DiagnosticsReport> {
  const params = new URLSearchParams();
  if (homeDir) params.set('home_dir', homeDir);
  params.set('include_logs', includeLogs ? 'true' : 'false');
  const qs = params.toString();
  return apiGet<DiagnosticsReport>(`/v1/setup/diagnostics${qs ? `?${qs}` : ''}`);
}

export async function repairSetupWorkspace(homeDir?: string): Promise<AutoRepairResult> {
  return apiPost<AutoRepairResult>('/v1/setup/repair-workspace', {
    home_dir: homeDir,
  });
}

export async function getSetupReceipts(options: {
  kind?: string;
  status?: string;
  limit?: number;
} = {}): Promise<SetupReceiptsResult> {
  const params = new URLSearchParams();
  if (options.kind) params.set('kind', options.kind);
  if (options.status) params.set('status_filter', options.status);
  if (options.limit) params.set('limit', String(options.limit));
  const qs = params.toString();
  return apiGet<SetupReceiptsResult>(`/v1/setup/receipts${qs ? `?${qs}` : ''}`);
}

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

export async function getSetupServices(homeDir?: string): Promise<ServiceRegistryReport> {
  const params = new URLSearchParams();
  if (homeDir) params.set('home_dir', homeDir);
  const qs = params.toString();
  return apiGet<ServiceRegistryReport>(`/v1/setup/services${qs ? `?${qs}` : ''}`);
}

export async function getSetupServiceHealth(homeDir?: string): Promise<ServiceHealthReport> {
  const params = new URLSearchParams();
  if (homeDir) params.set('home_dir', homeDir);
  const qs = params.toString();
  return apiGet<ServiceHealthReport>(`/v1/setup/service-health${qs ? `?${qs}` : ''}`);
}

export async function runSetupServiceAction(request: {
  service_id: string;
  action_id: string;
  home_dir?: string;
}): Promise<ServiceActionResult> {
  return apiPost<ServiceActionResult>('/v1/setup/service-action', request);
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

export function installWizardMissionStream(
  request: WizardMissionBuildRequest,
  callbacks: {
    onJob?: (job: InstallJob) => void;
    onStatus?: (job: InstallJob) => void;
    onEvent?: (event: WizardMissionInstallEvent) => void;
    onComplete?: (event: WizardMissionInstallEvent) => void;
    onError?: (error: string) => void;
  }
): () => void {
  return runBackgroundInstall<WizardMissionInstallEvent>(
    () => startWizardMissionJob(request),
    callbacks
  );
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
  let pollFailures = 0;

  const poll = async () => {
    if (stopped) return;
    try {
      const [job, batch] = await Promise.all([
        getInstallJob(jobId),
        getInstallJobEvents(jobId, after),
      ]);
      pollFailures = 0;
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
        pollFailures += 1;
        if (pollFailures <= 6) {
          timer = setTimeout(poll, Math.min(6000, 1000 * pollFailures));
          return;
        }
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

export async function getVaultStatus(homeDir?: string): Promise<VaultStatus> {
  const qs = homeDir ? `?home_dir=${encodeURIComponent(homeDir)}` : '';
  return apiGet<VaultStatus>(`/v1/vault/status${qs}`);
}

export async function initVault(homeDir?: string): Promise<VaultStatus> {
  return apiPost<VaultStatus>('/v1/vault/init', { home_dir: homeDir });
}

export async function saveVaultMemory(request: VaultMemoryRequest): Promise<VaultMemoryResult> {
  return apiPost<VaultMemoryResult>('/v1/vault/memory', request);
}

export async function installObsidian(homeDir?: string, forceUpdate = false): Promise<VaultStatus['obsidian']> {
  return apiPost<VaultStatus['obsidian']>('/v1/vault/obsidian/install', {
    home_dir: homeDir,
    force_update: forceUpdate,
  });
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
      const res = await fetch(`${getApiBase()}/v1/query`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...getApiAuthHeaders() },
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
  try {
    const raw = await apiGet<any>('/v1/budget/status');
    return {
      daily_spend: String(raw?.daily_spend ?? '0'),
      daily_limit: String(raw?.daily_limit ?? '0'),
      monthly_spend: String(raw?.monthly_spend ?? '0'),
      monthly_limit: String(raw?.monthly_limit ?? '0'),
      daily_queries: Number(raw?.daily_queries ?? 0),
      monthly_queries: Number(raw?.monthly_queries ?? 0),
      by_provider: raw?.by_provider ?? {},
    };
  } catch {
    return {
      daily_spend: '0',
      daily_limit: '0',
      monthly_spend: '0',
      monthly_limit: '0',
      daily_queries: 0,
      monthly_queries: 0,
      by_provider: {},
    };
  }
}

// ─── Cache ───────────────────────────────────────────────────────────────────

export async function getCacheStats(): Promise<CacheStats> {
  const raw = await apiGet<any>('/v1/cache/stats');
  const source = raw?.stats ?? raw?.cache ?? raw ?? {};
  const hits = Number(source.hits ?? source.cache_hits ?? 0);
  const misses = Number(source.misses ?? source.cache_misses ?? 0);
  const entries = Number(source.entries ?? source.size ?? source.count ?? 0);
  const maxSize = Number(source.max_size ?? source.max_entries ?? source.capacity ?? 1000);
  const attempts = hits + misses;
  const hitRate = Number.isFinite(Number(source.hit_rate))
    ? Number(source.hit_rate)
    : (attempts > 0 ? hits / attempts : 0);
  return {
    hits,
    misses,
    size: Number(source.size ?? entries),
    max_size: maxSize,
    hit_rate: hitRate,
  };
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

// ─── Conversations ────────────────────────────────────────────────────────────

// Canonical summary type lives in types.ts (the Sidebar imports it from
// there); re-export so existing `from '@/lib/api'` imports keep working.
export type { ConversationSummary } from './types';

export interface ConversationMessage {
  id: string;
  role: 'user' | 'assistant' | 'system';
  content: string;
  provider?: string;
  model?: string;
  cost_usd?: string | null;
  tokens?: number;
  latency_ms?: number;
  timestamp: number;
}

export interface ConversationDetail {
  id: string;
  title: string;
  mode?: ConversationMode;
  pinned?: boolean;
  messages: ConversationMessage[];
  created_at: number;
  updated_at: number;
}

export async function getConversations(limit = 100): Promise<ConversationSummary[]> {
  try {
    // The server always appends pinned conversations even past this window.
    const data = await apiGet<{ conversations: ConversationSummary[] }>(
      `/v1/conversations?limit=${limit}`
    );
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

export interface CreateConversationOptions {
  pinned?: boolean;
  /** Seed turns stored in the same transaction as the conversation. */
  messages?: AppendMessageInput[];
}

/**
 * Create a server-side conversation. Pass an empty title to let the backend
 * auto-title it from the first user message; pass `messages` to import a
 * whole thread in one request (it lands entirely or not at all).
 */
export async function createConversation(
  title = '',
  mode: ConversationMode | '' = '',
  options: CreateConversationOptions = {}
): Promise<ConversationSummary | null> {
  try {
    return await apiPost<ConversationSummary>('/v1/conversations', {
      title,
      mode,
      pinned: options.pinned ?? false,
      messages: (options.messages ?? []).map(toWireMessage),
    });
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

export async function pinConversation(id: string, pinned = true): Promise<boolean> {
  try {
    await apiPost(`/v1/conversations/${encodeURIComponent(id)}/pin`, { pinned });
    return true;
  } catch {
    return false;
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

export interface AppendMessageInput {
  role: 'user' | 'assistant';
  content: string;
  provider?: string;
  model?: string;
  tokens?: number;
  cost_usd?: string | null;
  latency_ms?: number;
}

function toWireMessage(message: AppendMessageInput) {
  // The server rejects non-finite or negative costs with 422; a stray bad
  // value (old localStorage rows) must not block the whole turn or thread.
  const cost = Number(message.cost_usd);
  return {
    role: message.role,
    content: message.content,
    provider: message.provider ?? '',
    model: message.model ?? '',
    tokens: message.tokens ?? 0,
    cost_usd: message.cost_usd && Number.isFinite(cost) && cost >= 0 ? message.cost_usd : '0',
    latency_ms: message.latency_ms ?? 0,
  };
}

/**
 * Persist one turn to a server-side conversation. Best-effort: the chat
 * keeps working in-memory when the API is offline, so failures are
 * reported as `false` rather than thrown.
 */
export async function appendConversationMessage(
  conversationId: string,
  message: AppendMessageInput
): Promise<boolean> {
  try {
    await apiPost(
      `/v1/conversations/${encodeURIComponent(conversationId)}/messages`,
      toWireMessage(message)
    );
    return true;
  } catch {
    return false;
  }
}

export type ConversationSearchHit = ConversationSummary & { snippet: string };

/** Full-text search over message content (GET /v1/conversations/search). */
export async function searchConversations(q: string, limit = 20): Promise<ConversationSearchHit[]> {
  const query = q.trim();
  if (!query) return [];
  try {
    const data = await apiGet<{ results: ConversationSearchHit[] }>(
      `/v1/conversations/search?q=${encodeURIComponent(query)}&limit=${limit}`
    );
    return data.results ?? [];
  } catch {
    return [];
  }
}

/** Fired whenever a conversation is created/renamed/pinned/deleted so every
 * mounted sidebar can refetch the server list without a navigation. */
export const CONVERSATIONS_CHANGED_EVENT = 'nvh:conversations-changed';

export function announceConversationsChanged(): void {
  if (typeof window !== 'undefined') {
    window.dispatchEvent(new CustomEvent(CONVERSATIONS_CHANGED_EVENT));
  }
}

// ─── Setup / Free-tier APIs ───────────────────────────────────────────────────

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
  /** Logo slug for the @lobehub/icons CDN. Null when no brand mark is available. */
  logo_slug?: string | null;
}

export interface ValidateKeyResult {
  valid: boolean;
  error?: string;
  latency_ms?: number;
  model_count?: number;
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

/** Ping the provider with the proposed key. Does not save. */
export async function validateProviderKey(
  provider_id: string,
  api_key: string,
): Promise<ValidateKeyResult> {
  return apiPost<ValidateKeyResult>('/v1/setup/validate-key', { provider: provider_id, api_key });
}

// ─── Wizard reconnect ────────────────────────────────────────────────────────

export interface WizardReconnectFact {
  label: string;
  value: string;
}

export interface WizardReconnectChange {
  fact: string;
  label: string;
  previous: string;
  current: string;
  severity: string;
}

export interface WizardReconnectAction {
  id: string;
  title: string;
  summary: string;
  button_action_id?: string;
}

export interface WizardReconnectResult {
  summary: string;
  first_run: boolean;
  changed: boolean;
  needs_attention: WizardReconnectAction[];
  auto_repaired: WizardReconnectAction[];
  what_changed: WizardReconnectChange[];
  what_survived: WizardReconnectFact[];
  elapsed_ms: number;
  preflight?: unknown;
}

/**
 * Runs the Wizard reconnect routine: boot preflight, safe auto-repair,
 * and a shaped "Welcome back" summary. Cheap enough to call once per
 * page mount; the underlying state is cached under NVH_HOME.
 */
export async function wizardReconnect(homeDir?: string): Promise<WizardReconnectResult> {
  return apiPost<WizardReconnectResult>('/v1/wizard/reconnect', { home_dir: homeDir });
}

// ─── AI Wizard diagnostics ───────────────────────────────────────────────────

export interface WizardFinding {
  id: string;
  severity: 'info' | 'warn' | 'error';
  category: 'gpu' | 'storage' | 'providers' | 'models' | 'runtime' | 'workspace';
  title: string;
  detail: string;
  suggested_tool: string | null;
  suggested_tool_args: Record<string, unknown>;
}

export interface WizardDiagnosticsPayload {
  findings: WizardFinding[];
  context: Record<string, unknown>;
  counts: {
    total: number;
    error: number;
    warn: number;
    info: number;
  };
}

/**
 * Consolidated Wizard diagnostic findings + live workspace context.
 *
 * This is the single source of truth for the setup page's System Check
 * surface AND the Wizard's prompt context — same data, two surfaces. Each
 * finding has a stable `id` the setup page can link to as
 * `/wizard?issue=<id>` for a guided fix.
 */
export async function wizardDiagnostics(homeDir?: string): Promise<WizardDiagnosticsPayload> {
  const qs = homeDir ? `?home_dir=${encodeURIComponent(homeDir)}` : '';
  return apiGet<WizardDiagnosticsPayload>(`/v1/wizard/diagnostics${qs}`);
}

// ─── AI Wizard chat ──────────────────────────────────────────────────────────

export interface WizardChatTurn {
  role: 'user' | 'assistant';
  content: string;
  /** Assistant turns only: the specialist that produced the reply (`null` =
   * the general Wizard). Carries continuity between turns so the concierge
   * can stay with a specialist on a weak follow-up ("and then?") without the
   * user pinning anything. Omit when unknown (rows persisted before attribution). */
  used_profile?: string | null;
}

export interface WizardChatToolCall {
  name: string;
  arguments: Record<string, unknown>;
}

/**
 * An auto-class call the server chose NOT to run this turn — `max_iterations`
 * was 1 or the profile's cost ceiling fired. Informational only: the UI lists
 * it as "not run: <reason>" and must never execute it (the user asked for a
 * shallow / cheap turn). Confirm-class calls travel separately in
 * `tool_calls` / `confirm_required`.
 */
export interface WizardDeferredToolCall {
  name: string;
  arguments: Record<string, unknown>;
  reason?: string | null;
}

export interface WizardChatToolResult {
  name: string;
  arguments: Record<string, unknown>;
  result: {
    ok?: boolean;
    error?: string;
    result?: unknown;
    safety_class?: string;
    /** Whitelist refusal: the active profile forbids this tool. It never ran,
     * so it must not count as a used tool or contribute sources. */
    not_allowed?: boolean;
    [key: string]: unknown;
  };
}

/** True for a trace entry the server refused (profile whitelist) — never executed. */
export function isRefusedToolResult(entry: WizardChatToolResult): boolean {
  return entry.result?.not_allowed === true;
}

export interface WizardChatResult {
  answer: string;
  mode: 'llm' | 'deterministic';
  used_provider?: string | null;
  used_model?: string | null;
  fallback_reason?: string;
  fallback_from?: string | null;
  context?: Record<string, unknown>;
  tool_calls?: WizardChatToolCall[];
  tool_results?: WizardChatToolResult[];
  iterations?: number;
  // Aggregated across the full follow-up loop, not just the last iteration.
  cost_usd?: number;
  latency_ms?: number;
  input_tokens?: number;
  output_tokens?: number;
  // Profile-level cost ceiling diagnostics. cost_ceiling_hit=true means the
  // follow-up loop was aborted because the running cost crossed the active
  // profile's max_cost_usd_per_turn. cost_ceiling_usd echoes the limit for
  // display ("Stopped at $0.05 — profile budget").
  cost_ceiling_hit?: boolean;
  cost_ceiling_usd?: number | null;
  /** Hidden specialist the concierge picked for this turn; null = the general Wizard. */
  used_profile?: string | null;
  profile_reason?: string | null;
  deferred_tool_calls?: WizardDeferredToolCall[];
}

/**
 * Send a turn to the AI Wizard chat. Grounded in live workspace state
 * (GPU, storage, providers, jobs, receipts, vault). Routes through the
 * engine when available; falls back to the deterministic setup helper.
 */
export async function wizardChat(
  question: string,
  options: { history?: WizardChatTurn[]; homeDir?: string; conversationId?: string } = {},
): Promise<WizardChatResult> {
  return apiPost<WizardChatResult>('/v1/wizard/chat', {
    question,
    history: normalizeWizardHistory(options.history),
    home_dir: options.homeDir,
    conversation_id: options.conversationId,
  });
}

/**
 * The history the Wizard endpoints accept: `role` + `content` on every turn,
 * and `used_profile` ONLY on assistant turns that know it (`null` = the
 * general Wizard answered). A user turn never carries attribution and an
 * assistant turn with unknown attribution omits the key rather than sending
 * `undefined`, so the concierge's continuity tier sees a clean signal.
 */
export function normalizeWizardHistory(history: WizardChatTurn[] | undefined): WizardChatTurn[] {
  return (history ?? []).map(turn => {
    const base: WizardChatTurn = { role: turn.role, content: turn.content };
    if (turn.role === 'assistant' && turn.used_profile !== undefined) {
      base.used_profile = turn.used_profile;
    }
    return base;
  });
}

/** The `profile` the chat endpoints receive: `auto` for any empty / unset /
 * auto selection, otherwise the pinned name verbatim. */
export function wizardProfileParam(profile: string | null | undefined): string {
  return isAutoProfile(profile) ? AUTO_PROFILE : (profile as string).trim();
}

// ─── Agent profiles ─────────────────────────────────────────────────────────

/**
 * Sentinel the composer sends when the user has not pinned a profile: the
 * concierge picks a hidden specialist per turn (docs/proposals/
 * SPARK_CONCIERGE_2026-09.md §3.1). The API treats `null`, `""` and `"auto"`
 * as auto; `"wizard"` is an explicit pin of the general persona.
 */
export const AUTO_PROFILE = 'auto';
/** The general Wizard persona — what an auto turn is attributed to when the
 * concierge picked no specialist (`used_profile: null`). */
export const GENERAL_PROFILE = 'wizard';

export function isAutoProfile(name: string | null | undefined): boolean {
  return !name || name.trim().toLowerCase() === AUTO_PROFILE;
}

/**
 * The one `fallback_reason` that is an answer rather than a failure: a
 * local-only specialist (explicit pin) declined because no local provider was
 * up. Mirrors `LOCAL_ONLY_FALLBACK_REASON` in nvh/integrations/wizard/chat.py.
 * Every other reason the server emits (the LLM exception text, "engine not
 * initialized") means the offline helper answered because the model path
 * failed — worth telling the user.
 */
export const WIZARD_LOCAL_ONLY_FALLBACK_REASON = 'profile_local_only_provider_unavailable';

/** True when a Wizard fallback (stream `error` event or non-stream result) is
 * a specialist's deliberate refusal, not a model-path failure. */
export function isWizardDeliberateRefusal(
  envelope: { fallback_reason?: string | null } | null | undefined,
): boolean {
  return envelope?.fallback_reason === WIZARD_LOCAL_ONLY_FALLBACK_REASON;
}

export interface AgentProfileSchema {
  name: string;
  title: string;
  description: string;
  system_prompt: string;
  provider: string;
  model: string;
  temperature: number | null;
  max_tokens: number | null;
  tools_allowed: string[] | null;
  built_in: boolean;
  tags: string[];
  avatar: string;
  // Per-turn cost ceiling in USD. Set on profiles (e.g. Researcher) that
  // route to potentially-expensive cloud models — surfaced on the /agents
  // discovery cards so users can see the guard before they pick.
  max_cost_usd_per_turn?: number | null;
  // Agent Library grouping label ("Coding", "Music Production", …).
  // Empty/absent for the six core built-ins and ungrouped user profiles.
  category?: string;
}

/** Build a renderable URL given a profile's avatar field. Returns null when
 * the profile has no avatar set — caller renders the initial fallback. */
export function profileAvatarUrl(
  profile: { avatar?: string } | null | undefined,
): string | null {
  if (!profile?.avatar) return null;
  if (profile.avatar.startsWith('http://') || profile.avatar.startsWith('https://')) {
    return profile.avatar;
  }
  const base = getApiBases()[0] ?? '';
  return `${base}${profile.avatar}`;
}

export async function listAgentProfiles(homeDir?: string): Promise<{ profiles: AgentProfileSchema[] }> {
  const qs = homeDir ? `?home_dir=${encodeURIComponent(homeDir)}` : '';
  return apiGet<{ profiles: AgentProfileSchema[] }>(`/v1/wizard/profiles${qs}`);
}

export async function saveAgentProfile(
  profile: Omit<AgentProfileSchema, 'built_in' | 'avatar'> & {
    avatar?: string;
    home_dir?: string;
  },
): Promise<{ saved: boolean; path: string; name: string }> {
  return apiPost('/v1/wizard/profiles', profile);
}

export async function deleteAgentProfile(name: string, homeDir?: string): Promise<void> {
  const qs = homeDir ? `?home_dir=${encodeURIComponent(homeDir)}` : '';
  await apiFetch(`/v1/wizard/profiles/${encodeURIComponent(name)}${qs}`, { method: 'DELETE' });
}

export async function uploadAgentAvatar(
  name: string,
  file: File,
  homeDir?: string,
): Promise<{ ok: boolean; url: string }> {
  const bases = getApiBases();
  const fd = new FormData();
  fd.append('file', file, file.name);
  const qs = homeDir ? `?home_dir=${encodeURIComponent(homeDir)}` : '';
  let lastError: unknown;
  for (const base of bases) {
    try {
      const res = await fetch(
        `${base}/v1/wizard/profiles/${encodeURIComponent(name)}/avatar/upload${qs}`,
        { method: 'POST', headers: { ...getApiAuthHeaders() }, body: fd },
      );
      rememberApiBase(base);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const env = await res.json() as ApiEnvelope<{ ok: boolean; url: string }>;
      return env.data;
    } catch (err) {
      lastError = err;
    }
  }
  throw lastError instanceof Error ? lastError : new Error('avatar upload failed');
}

// ─── RAG upload-and-ingest (multipart) ──────────────────────────────────────

export interface RagUploadIngestResult {
  ok: boolean;
  collection?: string;
  files_ingested?: number;
  chunks?: number;
  uploaded_files?: number;
  upload_dir?: string;
  pdfs_skipped_missing_pypdf?: number;
  hint?: string;
  error?: string;
}

/**
 * Upload one or more files via multipart and ingest them into a RAG
 * collection. The server lands the files under ``NVH_HOME/rag/uploads/``
 * (so they survive reconnect) and points the standard ingest walker at
 * the directory.
 */
export async function uploadAndIngest(
  files: File[],
  options: { collection?: string; homeDir?: string } = {},
): Promise<RagUploadIngestResult> {
  const bases = getApiBases();
  const fd = new FormData();
  for (const f of files) fd.append('files', f, f.name);

  const qs = new URLSearchParams();
  if (options.collection) qs.set('collection', options.collection);
  if (options.homeDir) qs.set('home_dir', options.homeDir);

  let lastError: unknown;
  for (const base of bases) {
    try {
      const url =
        `${base}/v1/rag/upload-ingest` + (qs.toString() ? `?${qs}` : '');
      const res = await fetch(url, {
        method: 'POST',
        headers: { ...getApiAuthHeaders() },
        body: fd,
      });
      rememberApiBase(base);
      if (!res.ok) {
        const detail = `HTTP ${res.status}`;
        throw new Error(detail);
      }
      const env = (await res.json()) as ApiEnvelope<RagUploadIngestResult>;
      return env.data;
    } catch (err) {
      lastError = err;
    }
  }
  throw lastError instanceof Error
    ? lastError
    : new Error('upload-ingest: API offline');
}

// ─── AI Wizard chat — streaming variant ─────────────────────────────────────

export type WizardStreamEvent =
  | { type: 'iteration'; n: number }
  | { type: 'token'; text: string }
  | { type: 'tool_call'; name: string; arguments: Record<string, unknown> }
  | { type: 'tool_result'; name: string; result: WizardChatToolResult['result'] }
  /** Confirm-class calls awaiting the user. Emitted once, immediately before `done`. */
  | { type: 'confirm_required'; tool_calls: WizardChatToolCall[] }
  | {
      type: 'done';
      answer: string;
      used_provider: string | null;
      used_model: string | null;
      routing_reason?: string | null;
      /** Same confirm-class list as `confirm_required`; non-empty means cards are still pending. */
      tool_calls: WizardChatToolCall[];
      tool_results: WizardChatToolResult[];
      iterations: number;
      cost_usd?: number;
      latency_ms?: number;
      fallback_from?: string | null;
      cost_ceiling_hit?: boolean;
      cost_ceiling_usd?: number | null;
      /** Hidden specialist the concierge picked for this turn; null = the general Wizard. */
      used_profile?: string | null;
      profile_reason?: string | null;
      /** Auto-class calls skipped server-side (depth 1 / cost ceiling). Display only. */
      deferred_tool_calls?: WizardDeferredToolCall[];
    }
  | {
      type: 'error';
      error: string;
      /** Deterministic text that saved the turn (offline helper or a specialist's refusal). */
      fallback?: string;
      /** Why no LLM answered. The server sets this on EVERY error event that
       * ended in a deterministic answer, so its presence says nothing about
       * severity — its value does: `WIZARD_LOCAL_ONLY_FALLBACK_REASON` is a
       * specialist deliberately declining (no banner); anything else is the
       * LLM exception text or "engine not initialized", i.e. the offline
       * helper stood in for a failed model path (banner). See
       * `isWizardDeliberateRefusal`. */
      fallback_reason?: string | null;
      /** Attribution when a specialist itself declined; null = the general Wizard. */
      used_profile?: string | null;
      profile_reason?: string | null;
    };

interface WizardChatStreamOptions {
  history?: WizardChatTurn[];
  homeDir?: string;
  conversationId?: string;
  profile?: string;
  maxIterations?: number;
  signal?: AbortSignal;
}

/**
 * Stream a Wizard turn. Uses POST + ReadableStream rather than EventSource
 * because EventSource doesn't support custom auth headers, and our auth
 * lives in Authorization / X-Hive-API-Key.
 *
 * Yields one event per SSE message. The caller drives token-by-token UI
 * rendering off `token` events and renders tool-call cards off `tool_call` /
 * `tool_result` / `confirm_required`. Final state lands in `done`; errors
 * land in `error` (with an optional `fallback` string when the deterministic
 * helper saved the turn).
 */
export async function* wizardChatStream(
  question: string,
  options: WizardChatStreamOptions = {},
): AsyncGenerator<WizardStreamEvent> {
  const bases = getApiBases();
  let lastNetworkError: unknown;

  for (const base of bases) {
    let resp: Response;
    try {
      resp = await fetch(`${base}/v1/wizard/chat/stream`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          Accept: 'text/event-stream',
          ...getApiAuthHeaders(),
        },
        body: JSON.stringify({
          question,
          history: normalizeWizardHistory(options.history),
          home_dir: options.homeDir,
          conversation_id: options.conversationId,
          // Same package ships both sides: "auto" is the literal the server
          // treats as "let the concierge pick"; empty / unset map to it too.
          profile: wizardProfileParam(options.profile),
          max_iterations: options.maxIterations,
        }),
        signal: options.signal,
      });
    } catch (err) {
      lastNetworkError = err;
      continue;
    }

    rememberApiBase(base);

    if (!resp.ok || !resp.body) {
      let detail = `HTTP ${resp.status}`;
      try {
        const body = await resp.json();
        const bodyDetail = body?.error?.message ?? body?.detail ?? detail;
        detail = typeof bodyDetail === 'string' ? bodyDetail : JSON.stringify(bodyDetail);
      } catch {
        // ignore
      }
      yield { type: 'error', error: detail };
      return;
    }

    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buf = '';
    try {
      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        buf += decoder.decode(value, { stream: true });

        // SSE frames are separated by a blank line. Split, parse the JSON
        // payload of each `data: …` line, yield. The producer guarantees
        // one event per frame and no comments.
        let idx: number;
        while ((idx = buf.indexOf('\n\n')) !== -1) {
          const frame = buf.slice(0, idx);
          buf = buf.slice(idx + 2);
          for (const line of frame.split('\n')) {
            if (!line.startsWith('data:')) continue;
            const payload = line.slice(5).trim();
            if (!payload) continue;
            try {
              yield JSON.parse(payload) as WizardStreamEvent;
            } catch {
              // Malformed payload — drop and keep streaming.
            }
          }
        }
      }
    } finally {
      try {
        reader.releaseLock();
      } catch {
        // ignore
      }
    }
    return;
  }

  throw lastNetworkError instanceof Error
    ? lastNetworkError
    : new Error(`Hive API is offline. Tried ${bases.join(', ')}`);
}

/**
 * Get the live workspace snapshot the Wizard sees. Useful for the
 * "what does the Wizard know right now?" debug surface.
 */
export async function getWizardContext(homeDir?: string): Promise<Record<string, unknown>> {
  const qs = homeDir ? `?home_dir=${encodeURIComponent(homeDir)}` : '';
  return apiGet<Record<string, unknown>>(`/v1/wizard/context${qs}`);
}

// ─── AI Wizard tool registry ─────────────────────────────────────────────────

export type WizardToolSafetyClass = 'auto' | 'confirm';

export interface WizardToolSchema {
  name: string;
  description: string;
  safety_class: WizardToolSafetyClass;
  parameters: Record<string, unknown>;
  summary_template: string;
}

export interface WizardToolListResult {
  tools: WizardToolSchema[];
  auto_count: number;
  confirm_count: number;
}

export interface WizardToolExecuteResult {
  ok: boolean;
  result?: Record<string, unknown>;
  error?: string;
  needs_confirmation?: boolean;
  tool?: WizardToolSchema | string;
  arguments?: Record<string, unknown>;
  summary?: string;
  safety_class?: WizardToolSafetyClass;
}

/** Fetch the Wizard tool catalog with safety classes. */
export async function listWizardTools(): Promise<WizardToolListResult> {
  return apiGet<WizardToolListResult>('/v1/wizard/tools');
}

/**
 * Execute a Wizard tool. For confirm-class tools, omit/pass false for
 * `confirmed` first to surface the structured confirmation card, then re-call
 * with `confirmed: true` once the user clicks the confirmation button.
 */
export async function executeWizardTool(
  name: string,
  args: { arguments?: Record<string, unknown>; confirmed?: boolean } = {},
): Promise<WizardToolExecuteResult> {
  return apiPost<WizardToolExecuteResult>('/v1/wizard/tools/execute', {
    name,
    arguments: args.arguments ?? {},
    confirmed: args.confirmed ?? false,
  });
}

// ─── WebSocket helpers ────────────────────────────────────────────────────────

/** Derive the WebSocket base URL from the HTTP base URL. */
function wsBaseUrl(): string {
  const http = getApiBase().replace(/^http/, 'ws');
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

// ─── Model Manager (roadmap: in-app model browser/downloader) ────────────────

export interface ModelFitEntry {
  /** Catalog id, e.g. "gemma3-4b". */
  id: string;
  /** Human title, e.g. "Gemma 3 4B". */
  title: string;
  /** Ollama pull target, e.g. "gemma3:4b" — this is what we install. */
  install_target: string;
  provider?: string;
  category?: string;
  use_case?: string;
  use_case_label?: string;
  capabilities?: string[];
  installed?: boolean;
  recommended?: boolean;
  fits_vram?: boolean;
  estimated_disk_gb?: number | null;
  recommended_vram_gb?: number | null;
  fit_score?: number;
  fit_reasons?: string[];
}

export interface ModelFitReport {
  models?: ModelFitEntry[];
  ranked?: ModelFitEntry[];
  detected_vram_gb?: number | null;
  vram_gb?: number | null;
  free_gb?: number | null;
  summary?: string;
}

export interface InstalledModel {
  name: string;
  size_bytes: number;
  size_gb: number;
  digest: string;
  modified_at: string;
  details?: Record<string, unknown>;
}

/** Fit-ranked catalog for the detected GPU/disk (drives the browser). */
export async function getModelFit(homeDir?: string): Promise<ModelFitReport> {
  const qs = homeDir ? `?home_dir=${encodeURIComponent(homeDir)}` : '';
  return apiGet<ModelFitReport>(`/v1/setup/model-fit${qs}`);
}

/** Installed Ollama models with on-disk sizes. */
export async function getInstalledModels(): Promise<{ models: InstalledModel[]; count: number }> {
  return apiGet<{ models: InstalledModel[]; count: number }>('/v1/ollama/models');
}

/** Delete an installed model, reclaiming disk. */
export async function deleteModel(name: string): Promise<{ deleted: boolean; model: string }> {
  const envelope = await apiFetch<ApiEnvelope<{ deleted: boolean; model: string }>>(
    `/v1/ollama/models/${encodeURIComponent(name)}`,
    { method: 'DELETE' },
  );
  return envelope.data;
}

export interface PullProgress {
  status: string;
  model?: string;
  completed?: number;
  total?: number;
  percent?: number | null;
}

/**
 * Stream an Ollama model pull. Parses the SSE the backend emits
 * (event: progress / complete / error). Returns an abort function.
 */
export function pullModelStream(
  model: string,
  cb: {
    onProgress?: (p: PullProgress) => void;
    onComplete?: (model: string) => void;
    onError?: (message: string) => void;
  },
): () => void {
  const controller = new AbortController();
  const base = getApiBase();
  void (async () => {
    try {
      const res = await fetch(`${base}/v1/ollama/pull`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...getApiAuthHeaders() },
        body: JSON.stringify({ model }),
        signal: controller.signal,
      });
      if (!res.ok || !res.body) {
        cb.onError?.(`Pull request failed (HTTP ${res.status})`);
        return;
      }
      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        // SSE frames are separated by a blank line.
        const frames = buffer.split('\n\n');
        buffer = frames.pop() ?? '';
        for (const frame of frames) {
          let event = 'message';
          let data = '';
          for (const line of frame.split('\n')) {
            if (line.startsWith('event:')) event = line.slice(6).trim();
            else if (line.startsWith('data:')) data += line.slice(5).trim();
          }
          if (!data) continue;
          let parsed: PullProgress & { error?: string };
          try {
            parsed = JSON.parse(data);
          } catch {
            continue;
          }
          if (event === 'error') cb.onError?.(parsed.error ?? 'pull failed');
          else if (event === 'complete') cb.onComplete?.(parsed.model ?? model);
          else cb.onProgress?.(parsed);
        }
      }
    } catch (err) {
      if ((err as Error).name !== 'AbortError') {
        cb.onError?.(err instanceof Error ? err.message : String(err));
      }
    }
  })();
  return () => controller.abort();
}
