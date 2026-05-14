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
