'use client';

import { useState, useEffect, useCallback, useRef } from 'react';
import Image from 'next/image';
import Link from 'next/link';
import {
  checkHealth,
  query,
  getGPUInfo,
  getRecommendations,
  getFreeProviders,
  saveProviderKey,
  askSetupAssistant,
  getStorageStatus,
  configureStorage,
  getMountAutopilot,
  activateMountAutopilot,
  getWizardPassport,
  getWizardRootlessPolicy,
  getWizardPlan,
  createSupportSnapshot,
  getSetupCatalog,
  getSetupBootPreflight,
  getSetupMissionControl,
  getSetupProductionReadiness,
  getSetupDiagnostics,
  getSetupHelper,
  getSetupReceipts,
  repairSetupWorkspace,
  cancelInstallJob,
  getComfyUIStatus,
  getComfyUIExamples,
  getInstallJobs,
  installWizardMissionStream,
  installComfyUIStream,
  startComfyUI,
  saveComfyUIModelPlan,
  getStudioPacks,
  installStudioPacksStream,
  getStudioModels,
  installStudioModelsStream,
} from '@/lib/api';
import { useProviderHealth } from '@/lib/useProviderHealth';
import type {
  GPUInfo,
  RecommendationsResult,
  FreeProvider,
  ComfyUIExample,
  ComfyUIInstallEvent,
  ComfyUIStatus,
  ComfyUITorchProfile,
  BootPreflightReport,
  CompatibilityReport,
  InstallJob,
  InstallReceipt,
  MissionControlReport,
  ProductionReadinessReport,
  DiagnosticsReport,
  SetupAssistantReply,
  AutoRepairResult,
  SetupCatalogResult,
  SetupHelperReport,
  SetupReceiptsResult,
  MountAutopilotReport,
  RootlessPolicyReport,
  StorageStatus,
  StudioPack,
  StudioPackInstallEvent,
  StudioModel,
  StudioModelInstallEvent,
  WizardMissionInstallEvent,
  WorkspacePassport,
  WizardPlanResult,
} from '@/lib/types';

type Step = 'welcome' | 'storage' | 'gpu' | 'models' | 'local-ai' | 'studio' | 'comfyui' | 'cloud' | 'test' | 'done';
type WizardProfile = 'student' | 'llm' | 'creator' | 'agent' | 'game' | 'music' | 'full';

type SetupCheckState = 'ready' | 'warn' | 'fix' | 'checking';

const CHECK_TONES: Record<SetupCheckState, { dot: string; text: string; border: string; bg: string; label: string }> = {
  ready: { dot: 'bg-[#76B900]', text: 'text-[#76B900]', border: 'border-[#76B900]/30', bg: 'bg-[#76B900]/5', label: 'Ready' },
  warn: { dot: 'bg-[#d97706]', text: 'text-[#d97706]', border: 'border-[#d97706]/30', bg: 'bg-[#fff7ed]', label: 'Review' },
  fix: { dot: 'bg-[#d97706]', text: 'text-[#d97706]', border: 'border-[#d97706]/30', bg: 'bg-[#fff7ed]', label: 'Fix queued' },
  checking: { dot: 'bg-[#a3a3a3]', text: 'text-[#737373]', border: 'border-[#e5e5e5]', bg: 'bg-[#fafafa]', label: 'Checking' },
};

const STEPS: { id: Step; label: string; num: number }[] = [
  { id: 'welcome', label: 'Welcome', num: 1 },
  { id: 'storage', label: 'Storage', num: 2 },
  { id: 'gpu', label: 'GPU', num: 3 },
  { id: 'models', label: 'Models', num: 4 },
  { id: 'local-ai', label: 'Local AI', num: 5 },
  { id: 'studio', label: 'Packs', num: 6 },
  { id: 'comfyui', label: 'ComfyUI', num: 7 },
  { id: 'cloud', label: 'Cloud', num: 8 },
  { id: 'test', label: 'Test', num: 9 },
  { id: 'done', label: 'Done', num: 10 },
];

const PERSISTENT_STORAGE_MIN_GB = 200;

const ADVANCED_GROUPS: { id: string; label: string; steps: Step[] }[] = [
  { id: 'overview', label: 'Overview', steps: ['welcome'] },
  { id: 'hardware', label: 'Hardware', steps: ['storage', 'gpu', 'models'] },
  { id: 'apps', label: 'Apps', steps: ['local-ai', 'studio', 'comfyui'] },
  { id: 'accounts', label: 'Accounts', steps: ['cloud'] },
  { id: 'verify', label: 'Verify', steps: ['test', 'done'] },
];

const CLOUD_PROVIDERS = [
  { id: 'openai', name: 'OpenAI', description: 'GPT-4o, GPT-4o-mini', envKey: 'OPENAI_API_KEY', placeholder: 'sk-...', signupUrl: 'https://platform.openai.com/api-keys' },
  { id: 'anthropic', name: 'Anthropic', description: 'Claude Sonnet, Haiku, Opus', envKey: 'ANTHROPIC_API_KEY', placeholder: 'sk-ant-...', signupUrl: 'https://console.anthropic.com/settings/keys' },
  { id: 'google', name: 'Google Gemini', description: 'Gemini 2.0 Flash, Pro', envKey: 'GOOGLE_API_KEY', placeholder: 'AIza...', signupUrl: 'https://aistudio.google.com/apikey' },
  { id: 'groq', name: 'Groq', description: 'Llama 3.3 70B (ultra-fast)', envKey: 'GROQ_API_KEY', placeholder: 'gsk_...', signupUrl: 'https://console.groq.com/keys' },
  { id: 'grok', name: 'xAI Grok', description: 'Grok 2, Grok 3', envKey: 'XAI_API_KEY', placeholder: 'xai-...', signupUrl: 'https://console.x.ai' },
  { id: 'mistral', name: 'Mistral', description: 'Mistral Large, Small', envKey: 'MISTRAL_API_KEY', placeholder: 'your-key...', signupUrl: 'https://console.mistral.ai/api-keys' },
];

type BrandLogoId = 'openclaw' | 'nvidia' | 'comfyui' | 'blender' | 'godot' | 'github' | 'unity' | 'unreal' | 'ollama' | 'audacity' | 'lmms';

const BRAND_LOGOS: Record<BrandLogoId, { src: string; alt: string }> = {
  openclaw: { src: '/brand-icons/openclaw.svg', alt: 'OpenClaw logo' },
  nvidia: { src: '/brand-icons/nvidia.svg', alt: 'NVIDIA logo' },
  comfyui: { src: '/brand-icons/comfyui.svg', alt: 'ComfyUI logo' },
  blender: { src: '/brand-icons/blender.svg', alt: 'Blender logo' },
  godot: { src: '/brand-icons/godot.svg', alt: 'Godot Engine logo' },
  github: { src: '/brand-icons/github.svg', alt: 'GitHub logo' },
  unity: { src: '/brand-icons/unity.svg', alt: 'Unity logo' },
  unreal: { src: '/brand-icons/unrealengine.svg', alt: 'Unreal Engine logo' },
  ollama: { src: '/brand-icons/ollama.svg', alt: 'Ollama logo' },
  audacity: { src: '/brand-icons/audacity.svg', alt: 'Audacity logo' },
  lmms: { src: '/brand-icons/lmms.svg', alt: 'LMMS logo' },
};

function BrandLogo({ id, className = 'w-7 h-7' }: { id: BrandLogoId; className?: string }) {
  const logo = BRAND_LOGOS[id];
  return (
    <Image
      src={logo.src}
      alt={logo.alt}
      width={32}
      height={32}
      className={`${className} object-contain`}
      draggable={false}
      unoptimized
    />
  );
}

// Provider Card (used in Cloud step)

interface ProviderCardProps {
  p: FreeProvider;
  expandedProvider: string | null;
  setExpandedProvider: (id: string | null) => void;
  keyInputs: Record<string, string>;
  setKeyInputs: (fn: (prev: Record<string, string>) => Record<string, string>) => void;
  savingKey: string | null;
  savedKeys: Set<string>;
  keyErrors: Record<string, string>;
  handleSaveKey: (id: string) => void;
}

function ProviderCard({ p, expandedProvider, setExpandedProvider, keyInputs, setKeyInputs, savingKey, savedKeys, keyErrors, handleSaveKey }: ProviderCardProps) {
  const isConfigured = p.configured || savedKeys.has(p.id);
  const isExpanded = expandedProvider === p.id;

  return (
    <div className={`border bg-[#ffffff] transition-colors ${isConfigured ? 'border-[#76B900]/40' : 'border-[#e5e5e5]'}`}>
      <div className="flex items-center gap-3 p-3">
        {/* Status indicator */}
        <span className={`w-1.5 h-1.5 flex-shrink-0 ${isConfigured ? 'bg-[#76B900]' : 'bg-[#333333]'}`}
          style={{ clipPath: 'polygon(50% 0%, 100% 50%, 50% 100%, 0% 50%)' }} />

        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 flex-wrap">
            <span className="text-sm font-mono font-bold text-[#0a0a0a]">{p.name}</span>
            {isConfigured && (
              <span className="text-[10px] font-mono text-[#76B900] bg-[#76B900]/10 px-1.5 py-0.5 flex items-center gap-1">
                <svg className="w-2.5 h-2.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={3}>
                  <path strokeLinecap="round" strokeLinejoin="round" d="M4.5 12.75l6 6 9-13.5" />
                </svg>
                CONFIGURED
              </span>
            )}
          </div>
          {p.free_tier_limits && (
            <div className="text-[10px] font-mono text-[#a3a3a3] mt-0.5">{p.free_tier_limits}</div>
          )}
          {p.strengths && p.strengths.length > 0 && (
            <div className="flex flex-wrap gap-1 mt-1">
              {p.strengths.map(s => (
                <span key={s} className="text-[9px] font-mono text-[#a3a3a3] bg-[#f5f5f5] border border-[#e5e5e5] px-1.5 py-0.5">
                  {s}
                </span>
              ))}
            </div>
          )}
        </div>

        {/* Add Key / collapse button */}
        {!isConfigured && (
          <button
            type="button"
            onClick={() => setExpandedProvider(isExpanded ? null : p.id)}
            className={`text-[10px] font-mono px-2 py-1 border transition-colors flex-shrink-0 ${
              isExpanded
                ? 'border-[#76B900]/40 bg-[#76B900]/10 text-[#76B900]'
                : 'border-[#d4d4d4] text-[#a3a3a3] hover:border-[#76B900]/30 hover:text-[#76B900]'
            }`}
          >
            {isExpanded ? 'Cancel' : 'Add Key'}
          </button>
        )}
      </div>

      {/* Inline key form */}
      {isExpanded && !isConfigured && (
        <div className="border-t border-[#e5e5e5] p-3 space-y-2">
          <div className="flex items-center justify-between">
            <div className="text-[10px] font-mono text-[#a3a3a3]">
              {p.env_key ? `Environment variable: ${p.env_key}` : 'Paste your API key below'}
            </div>
            {p.signup_url && (
              <a
                href={p.signup_url}
                target="_blank"
                rel="noopener noreferrer"
                className="text-[10px] font-mono text-[#76B900] hover:underline"
              >
                Get Key &rarr;
              </a>
            )}
          </div>
          <div className="flex gap-2">
            <input
              type="text"
              value={keyInputs[p.id] ?? ''}
              onChange={e => setKeyInputs(prev => ({ ...prev, [p.id]: e.target.value }))}
              placeholder={p.placeholder ?? 'Paste API key...'}
              className="input-base flex-1 px-3 py-2 text-xs font-mono"
              onKeyDown={e => { if (e.key === 'Enter') handleSaveKey(p.id); }}
              spellCheck={false}
              autoComplete="off"
              autoFocus
            />
            <button
              type="button"
              onClick={() => handleSaveKey(p.id)}
              disabled={savingKey === p.id || !keyInputs[p.id]?.trim()}
              className="btn-primary px-3 py-2 text-xs font-mono disabled:opacity-40"
            >
              {savingKey === p.id ? (
                <svg className="animate-spin w-3.5 h-3.5" viewBox="0 0 24 24" fill="none">
                  <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
                  <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
                </svg>
              ) : 'Save'}
            </button>
          </div>
          {keyErrors[p.id] && (
            <div className="text-[10px] font-mono text-[#dc2626]">{keyErrors[p.id]}</div>
          )}
        </div>
      )}
    </div>
  );
}

const isActiveInstallJob = (job: InstallJob) => job.status === 'queued' || job.status === 'running';

const studioPackDetails = (pack: StudioPack): Record<string, unknown> => pack.status?.details ?? {};

const studioPackInstallable = (pack: StudioPack) => studioPackDetails(pack).installable !== false;

const studioPackBlockedReason = (pack: StudioPack) => {
  const reason = studioPackDetails(pack).blocked_reason;
  return typeof reason === 'string' ? reason : '';
};

const selectableStudioPackIds = (packs: StudioPack[], packIds: string[]) => (
  packIds.filter(packId => {
    const pack = packs.find(item => item.id === packId);
    return !pack || studioPackInstallable(pack);
  })
);

const shouldAutoActivateStorage = (status: StorageStatus, report: MountAutopilotReport) => {
  const candidate = report.recommended;
  const needsStorage = !status.ok || status.configured_by === 'default';
  return Boolean(
    needsStorage &&
    candidate &&
    ['high', 'medium'].includes(report.confidence) &&
    candidate.writable &&
    (candidate.large_block_mount || (candidate.total_gb ?? 0) >= 180 || candidate.source.startsWith('env:')) &&
    !candidate.read_only &&
    !candidate.network_mount &&
    !candidate.os_mount
  );
};

export default function SetupPage() {
  const [step, setStep] = useState<Step>('welcome');
  const [ollamaStatus, setOllamaStatus] = useState<'checking' | 'online' | 'offline'>('checking');
  const [apiStatus, setApiStatus] = useState<'checking' | 'connected' | 'disconnected'>('checking');
  const [testPrompt] = useState('Hello! Respond with exactly: "Hive is operational. NVIDIA Nemotron ready."');
  const [testResult, setTestResult] = useState<string | null>(null);
  const [testLoading, setTestLoading] = useState(false);
  const [testError, setTestError] = useState<string | null>(null);
  const [configuredProviders, setConfiguredProviders] = useState<string[]>([]);
  const [freeProviders, setFreeProviders] = useState<FreeProvider[]>([]);
  const [freeProvidersLoading, setFreeProvidersLoading] = useState(false);
  const [storageStatus, setStorageStatus] = useState<StorageStatus | null>(null);
  const [storageHomeInput, setStorageHomeInput] = useState('');
  const [storageSaving, setStorageSaving] = useState(false);
  const [storageError, setStorageError] = useState<string | null>(null);
  const [mountAutopilot, setMountAutopilot] = useState<MountAutopilotReport | null>(null);
  const [mountActivating, setMountActivating] = useState(false);
  const [setupHelper, setSetupHelper] = useState<SetupHelperReport | null>(null);
  const [setupHelperError, setSetupHelperError] = useState<string | null>(null);
  const [setupReceipts, setSetupReceipts] = useState<SetupReceiptsResult | null>(null);
  const [setupCatalog, setSetupCatalog] = useState<SetupCatalogResult | null>(null);
  const [setupCompatibility, setSetupCompatibility] = useState<CompatibilityReport | null>(null);
  const [bootPreflight, setBootPreflight] = useState<BootPreflightReport | null>(null);
  const [missionControl, setMissionControl] = useState<MissionControlReport | null>(null);
  const [productionReadiness, setProductionReadiness] = useState<ProductionReadinessReport | null>(null);
  const [workspacePassport, setWorkspacePassport] = useState<WorkspacePassport | null>(null);
  const [rootlessPolicy, setRootlessPolicy] = useState<RootlessPolicyReport | null>(null);
  const [wizardPlan, setWizardPlan] = useState<WizardPlanResult | null>(null);
  const [diagnosticsReport, setDiagnosticsReport] = useState<DiagnosticsReport | null>(null);
  const [diagnosticsLoading, setDiagnosticsLoading] = useState(false);
  const [diagnosticsMessage, setDiagnosticsMessage] = useState<string | null>(null);
  const [diagnosticsError, setDiagnosticsError] = useState<string | null>(null);
  const [supportSnapshotMessage, setSupportSnapshotMessage] = useState<string | null>(null);
  const [supportSnapshotLoading, setSupportSnapshotLoading] = useState(false);
  const [workspaceRepairing, setWorkspaceRepairing] = useState(false);
  const [setupInventoryError, setSetupInventoryError] = useState<string | null>(null);
  const [activeWizardBuild, setActiveWizardBuild] = useState<WizardProfile | null>(null);
  const [wizardBuildMessage, setWizardBuildMessage] = useState<string | null>(null);
  const [assistantQuestion, setAssistantQuestion] = useState('');
  const [assistantReply, setAssistantReply] = useState<SetupAssistantReply | null>(null);
  const [assistantLoading, setAssistantLoading] = useState(false);
  const [assistantError, setAssistantError] = useState<string | null>(null);
  const [expandedProvider, setExpandedProvider] = useState<string | null>(null);
  const [keyInputs, setKeyInputs] = useState<Record<string, string>>({});
  const [savingKey, setSavingKey] = useState<string | null>(null);
  const [savedKeys, setSavedKeys] = useState<Set<string>>(new Set());
  const [keyErrors, setKeyErrors] = useState<Record<string, string>>({});
  const [gpuInfo, setGpuInfo] = useState<GPUInfo | null>(null);
  const [gpuRecs, setGpuRecs] = useState<RecommendationsResult | null>(null);
  const [gpuLoading, setGpuLoading] = useState(false);
  const [comfyStatus, setComfyStatus] = useState<ComfyUIStatus | null>(null);
  const [comfyExamples, setComfyExamples] = useState<ComfyUIExample[]>([]);
  const [comfyLoading, setComfyLoading] = useState(false);
  const [comfyInstalling, setComfyInstalling] = useState(false);
  const [comfyStarting, setComfyStarting] = useState(false);
  const [comfyEvents, setComfyEvents] = useState<ComfyUIInstallEvent[]>([]);
  const [comfyError, setComfyError] = useState<string | null>(null);
  const [selectedComfyExamples, setSelectedComfyExamples] = useState<Set<string>>(new Set());
  const [comfyPlanSaving, setComfyPlanSaving] = useState(false);
  const [comfyPlanMessage, setComfyPlanMessage] = useState<string | null>(null);
  const [studioPacks, setStudioPacks] = useState<StudioPack[]>([]);
  const [studioBundles, setStudioBundles] = useState<Record<string, string[]>>({});
  const [studioRoot, setStudioRoot] = useState<string>('');
  const [selectedStudioPacks, setSelectedStudioPacks] = useState<Set<string>>(new Set());
  const [studioLoading, setStudioLoading] = useState(false);
  const [studioInstalling, setStudioInstalling] = useState(false);
  const [studioEvents, setStudioEvents] = useState<StudioPackInstallEvent[]>([]);
  const [studioError, setStudioError] = useState<string | null>(null);
  const [studioModels, setStudioModels] = useState<StudioModel[]>([]);
  const [detectedModelVram, setDetectedModelVram] = useState(0);
  const [selectedStudioModels, setSelectedStudioModels] = useState<Set<string>>(new Set());
  const [modelsLoading, setModelsLoading] = useState(false);
  const [modelsInstalling, setModelsInstalling] = useState(false);
  const [modelEvents, setModelEvents] = useState<StudioModelInstallEvent[]>([]);
  const [modelError, setModelError] = useState<string | null>(null);
  const [installJobs, setInstallJobs] = useState<InstallJob[]>([]);
  const [jobsError, setJobsError] = useState<string | null>(null);
  const [cancelingJobId, setCancelingJobId] = useState<string | null>(null);
  const [advancedSetupOpen, setAdvancedSetupOpen] = useState(false);
  const [selectedWizardProfile, setSelectedWizardProfile] = useState<WizardProfile>('student');
  const handledMissionJobsRef = useRef<Set<string>>(new Set());
  const autoDebugJobsRef = useRef<Set<string>>(new Set());

  // Live-polled provider health drives Ollama status and the
  // configured-providers list so the setup screen reflects newly
  // added keys within 30s without requiring a manual refresh.
  const { providers: polledProviders } = useProviderHealth();
  useEffect(() => {
    if (polledProviders.length === 0) return;
    const ollamaProvider = polledProviders.find(p => p.name === 'ollama');
    setOllamaStatus(ollamaProvider?.healthy ? 'online' : 'offline');
    setConfiguredProviders(polledProviders.filter(p => p.healthy).map(p => p.name));
  }, [polledProviders]);

  const mergeInstallJob = useCallback((job: InstallJob) => {
    setInstallJobs(prev => {
      const next = [job, ...prev.filter(item => item.id !== job.id)];
      return next
        .sort((a, b) => b.created_at.localeCompare(a.created_at))
        .slice(0, 8);
    });
  }, []);

  const refreshInstallJobs = useCallback(async () => {
    try {
      const data = await getInstallJobs({ limit: 8 });
      setInstallJobs(data.jobs);
      setJobsError(null);
    } catch (err) {
      setJobsError(err instanceof Error ? err.message : 'Could not load install jobs');
    }
  }, []);

  const refreshSetupHelper = useCallback(async (homeDir?: string) => {
    try {
      const data = await getSetupHelper(homeDir);
      setSetupHelper(data);
      setSetupHelperError(null);
    } catch (err) {
      setSetupHelperError(err instanceof Error ? err.message : 'Setup helper unavailable');
    }
  }, []);

  const refreshWorkspacePassport = useCallback(async (homeDir?: string, profile?: WizardProfile) => {
    const activeHome = homeDir ?? storageStatus?.layout.home;
    const activeProfile = profile ?? selectedWizardProfile;
    const canPersistPassport = Boolean(activeHome && storageStatus?.configured_by !== 'default');
    const planPromise = canPersistPassport
      ? getWizardPlan(activeProfile, activeHome)
      : Promise.resolve(null);
    const [passportResult, policyResult, planResult] = await Promise.allSettled([
      getWizardPassport(activeHome, canPersistPassport),
      getWizardRootlessPolicy(activeHome),
      planPromise,
    ]);
    if (passportResult.status === 'fulfilled') setWorkspacePassport(passportResult.value);
    if (policyResult.status === 'fulfilled') setRootlessPolicy(policyResult.value);
    if (planResult.status === 'fulfilled') setWizardPlan(planResult.value);
  }, [selectedWizardProfile, storageStatus?.configured_by, storageStatus?.layout.home]);

  const refreshSetupInventory = useCallback(async (refreshCatalog = false, homeDir?: string) => {
    try {
      const activeHome = homeDir ?? storageStatus?.layout.home;
      const [receipts, catalog, boot, mission, readiness] = await Promise.all([
        getSetupReceipts({ limit: 8 }),
        getSetupCatalog(refreshCatalog),
        getSetupBootPreflight(activeHome),
        getSetupMissionControl(activeHome),
        getSetupProductionReadiness(activeHome),
      ]);
      setSetupReceipts(receipts);
      setSetupCatalog(catalog);
      setBootPreflight(boot);
      setSetupCompatibility(boot.compatibility);
      setMissionControl(mission);
      setProductionReadiness(readiness);
      void refreshWorkspacePassport(activeHome);
      setSetupInventoryError(null);
    } catch (err) {
      setSetupInventoryError(err instanceof Error ? err.message : 'Could not load setup inventory');
    }
  }, [refreshWorkspacePassport, storageStatus?.layout.home]);

  const handleDiagnosticsReport = async () => {
    if (diagnosticsLoading) return;
    setDiagnosticsLoading(true);
    setDiagnosticsError(null);
    setDiagnosticsMessage(null);
    try {
      const report = await getSetupDiagnostics(storageStatus?.layout.home, true);
      setDiagnosticsReport(report);
      const reportText = JSON.stringify(report, null, 2);
      if (navigator.clipboard?.writeText) {
        try {
          await navigator.clipboard.writeText(reportText);
          setDiagnosticsMessage(`Copied redacted report ${report.report_id}`);
        } catch {
          setDiagnosticsMessage(`Report ${report.report_id} is ready below`);
        }
      } else {
        setDiagnosticsMessage(`Report ${report.report_id} is ready below`);
      }
    } catch (err) {
      setDiagnosticsError(err instanceof Error ? err.message : 'Could not build diagnostics report');
    } finally {
      setDiagnosticsLoading(false);
    }
  };

  const handleSupportSnapshot = async () => {
    if (supportSnapshotLoading) return;
    setSupportSnapshotLoading(true);
    setDiagnosticsError(null);
    setSupportSnapshotMessage(null);
    try {
      const snapshot = await createSupportSnapshot(storageStatus?.layout.home, true);
      setSupportSnapshotMessage(`Support snapshot saved at ${snapshot.path}`);
    } catch (err) {
      setDiagnosticsError(err instanceof Error ? err.message : 'Could not create support snapshot');
    } finally {
      setSupportSnapshotLoading(false);
    }
  };

  useEffect(() => {
    void refreshInstallJobs();
    void refreshSetupInventory(false);
    const timer = window.setInterval(() => {
      void refreshInstallJobs();
    }, 3000);
    return () => window.clearInterval(timer);
  }, [refreshInstallJobs, refreshSetupInventory]);

  useEffect(() => {
    void refreshWorkspacePassport(storageStatus?.layout.home, selectedWizardProfile);
  }, [refreshWorkspacePassport, selectedWizardProfile, storageStatus?.layout.home]);

  useEffect(() => {
    const activeMission = installJobs.find(job => job.kind === 'wizard-mission' && isActiveInstallJob(job));
    const missionProfile = activeMission?.request?.profile;
    const missionProfileId = typeof missionProfile === 'string' ? missionProfile as WizardProfile : null;
    const missionRunning = Boolean(activeMission);
    const missionNeedsComfy = missionProfileId === 'creator' || missionProfileId === 'game' || missionProfileId === 'full';
    setActiveWizardBuild(missionProfileId);
    setComfyInstalling(
      installJobs.some(job => job.kind === 'comfyui-install' && isActiveInstallJob(job)) ||
      (missionRunning && missionNeedsComfy)
    );
    setStudioInstalling(missionRunning || installJobs.some(job => job.kind === 'studio-pack-install' && isActiveInstallJob(job)));
    setModelsInstalling(missionRunning || installJobs.some(job => job.kind === 'studio-model-install' && isActiveInstallJob(job)));
  }, [installJobs]);

  const handleCancelInstallJob = async (jobId: string) => {
    setCancelingJobId(jobId);
    setJobsError(null);
    try {
      const job = await cancelInstallJob(jobId);
      mergeInstallJob(job);
      void refreshInstallJobs();
    } catch (err) {
      setJobsError(err instanceof Error ? err.message : 'Could not cancel install job');
    } finally {
      setCancelingJobId(null);
    }
  };

  const askSetupQuestion = useCallback(async (rawQuestion: string, reflectQuestion = false) => {
    const question = rawQuestion.trim();
    if (!question) return;
    if (reflectQuestion) setAssistantQuestion(question);
    setAssistantLoading(true);
    setAssistantError(null);
    try {
      const reply = await askSetupAssistant(question, storageStatus?.layout.home);
      setAssistantReply(reply);
      if (reply.focus === 'debugger' || reply.focus === 'repair') {
        const concise = reply.answer.length > 320 ? `${reply.answer.slice(0, 320)}...` : reply.answer;
        setWizardBuildMessage(concise);
      }
    } catch (err) {
      setAssistantError(err instanceof Error ? err.message : 'Setup helper could not answer');
    } finally {
      setAssistantLoading(false);
    }
  }, [storageStatus?.layout.home]);

  const handleAskAssistant = async () => {
    await askSetupQuestion(assistantQuestion);
  };

  const handleQuickDiagnosis = () => {
    setWizardBuildMessage('nvWizard is checking local state, jobs, receipts, logs, and rootless repair options.');
    void askSetupQuestion(
      'Debug this Linux GPU desktop setup. Read local jobs, receipts, logs, diagnostics, persistent storage, GPU/VRAM, Python/runtime, Ollama, local models, ComfyUI, and any failed install jobs. Explain what likely broke and recommend the safest next button to press.',
      true,
    );
  };

  const handleBootRecheck = async () => {
    try {
      const boot = await getSetupBootPreflight(storageStatus?.layout.home, true);
      setBootPreflight(boot);
      setSetupCompatibility(boot.compatibility);
      setMissionControl(await getSetupMissionControl(storageStatus?.layout.home));
      setSetupInventoryError(null);
      void refreshSetupHelper(storageStatus?.layout.home);
    } catch (err) {
      setSetupInventoryError(err instanceof Error ? err.message : 'Boot preflight could not run');
    }
  };

  const handleRepairWorkspace = async () => {
    if (workspaceRepairing) return;
    setWorkspaceRepairing(true);
    setWizardBuildMessage('nvWizard is running safe rootless repairs. No sudo, no system changes.');
    try {
      const result: AutoRepairResult = await repairSetupWorkspace(storageStatus?.layout.home);
      setWizardBuildMessage(result.summary);
      if (result.errors.length > 0) {
        setSetupInventoryError(result.errors.map(error => error.error ?? error.summary).join(' | '));
      } else {
        setSetupInventoryError(null);
      }
      await Promise.all([
        refreshSetupInventory(false, storageStatus?.layout.home),
        refreshSetupHelper(storageStatus?.layout.home),
        refreshWorkspacePassport(storageStatus?.layout.home),
        refreshComfyUI(),
        refreshInstallJobs(),
      ]);
    } catch (err) {
      setSetupInventoryError(err instanceof Error ? err.message : 'Workspace repair failed');
      setWizardBuildMessage(err instanceof Error ? `nvWizard repair failed: ${err.message}` : 'nvWizard repair failed.');
    } finally {
      setWorkspaceRepairing(false);
    }
  };


  useEffect(() => {
    let cancelled = false;
    let healthRetryTimer: ReturnType<typeof setTimeout> | null = null;
    let storageRetryTimer: ReturnType<typeof setTimeout> | null = null;

    const pollApiHealth = async (retry = false) => {
      if (cancelled) return;
      if (!retry) setApiStatus('checking');
      try {
        await checkHealth();
        if (!cancelled) setApiStatus('connected');
      } catch {
        if (!cancelled) {
          setApiStatus('disconnected');
          healthRetryTimer = setTimeout(() => void pollApiHealth(true), 5000);
        }
      }
    };
    void pollApiHealth();

    // Ollama status + configured-providers list are now fed by the
    // polled useProviderHealth hook below, so nothing to do here at
    // mount time.

    // Fetch free providers for cloud step
    setFreeProvidersLoading(true);
    getFreeProviders()
      .then(data => setFreeProviders(data.providers))
      .catch(() => {})
      .finally(() => setFreeProvidersLoading(false));

    // Fetch persistent storage preflight before any large local downloads.
    const loadStorage = async () => {
      if (cancelled) return;
      try {
        const status = await getStorageStatus();
        if (cancelled) return;
        setStorageStatus(status);
        setStorageHomeInput(status.layout.home);
        setStorageError(null);
        void refreshSetupHelper(status.layout.home);
        void refreshWorkspacePassport(status.layout.home);

        if (!status.ok || status.configured_by === 'default') {
          try {
            const report = await getMountAutopilot(PERSISTENT_STORAGE_MIN_GB);
            if (cancelled) return;
            setMountAutopilot(report);
            if (report.recommended) {
              setStorageHomeInput(report.recommended.recommended_home);
            }
            if (shouldAutoActivateStorage(status, report)) {
              setMountActivating(true);
              try {
                const activated = await activateMountAutopilot(report.recommended?.recommended_home, PERSISTENT_STORAGE_MIN_GB);
                if (cancelled) return;
                setStorageStatus(activated.storage);
                setStorageHomeInput(activated.storage.layout.home);
                setMountAutopilot(activated.mount_autopilot);
                setWizardBuildMessage('nvWizard found the large writable block volume and prepared it for models, ComfyUI, Blender, and agents.');
                void refreshSetupHelper(activated.storage.layout.home);
                void refreshSetupInventory(false, activated.storage.layout.home);
                void refreshWorkspacePassport(activated.storage.layout.home);
              } catch (err) {
                setStorageError(err instanceof Error ? err.message : 'Could not activate recommended persistent storage');
              } finally {
                setMountActivating(false);
              }
            }
          } catch {
            if (!cancelled) {
              setStorageError('Mount autopilot could not inspect persistent volumes yet. You can still paste NVH_HOME.');
            }
          }
        }
      } catch {
        if (!cancelled) {
          setStorageError('Storage preflight is unavailable. Launch nvHive from the desktop icon, or use the terminal fallback: nvh webui.');
          storageRetryTimer = setTimeout(() => void loadStorage(), 5000);
        }
      }
    };
    void loadStorage();

    // Fetch GPU info for the GPU step
    setGpuLoading(true);
    Promise.all([getGPUInfo(), getRecommendations()])
      .then(([gpu, recs]) => {
        setGpuInfo(gpu);
        setGpuRecs(recs);
      })
      .catch(() => {
        // GPU not available; leave null
      })
      .finally(() => setGpuLoading(false));

    // Fetch ComfyUI status and curated example manifest for the visual workflow step.
    setComfyLoading(true);
    getComfyUIStatus()
      .then(data => {
        setComfyStatus(data);
        setComfyExamples(data.examples ?? []);
        setSelectedComfyExamples(new Set((data.examples ?? []).filter(example => example.recommended_vram_gb <= 12).map(example => example.id)));
      })
      .catch(() => {
        getComfyUIExamples()
          .then(data => {
            setComfyExamples(data.examples);
            setSelectedComfyExamples(new Set(data.examples.filter(example => example.recommended_vram_gb <= 12).map(example => example.id)));
          })
          .catch(() => {});
      })
      .finally(() => setComfyLoading(false));

    // Fetch rootless AI Studio packs for LLMs, agents, ComfyUI nodes, and game-dev tools.
    setStudioLoading(true);
    getStudioPacks()
      .then(data => {
        setStudioPacks(data.packs);
        setStudioBundles(data.bundles);
        setStudioRoot(data.root);
        const starterIds = data.bundles.starter ?? data.packs.map(pack => pack.id);
        setSelectedStudioPacks(new Set(selectableStudioPackIds(data.packs, starterIds)));
      })
      .catch(() => {})
      .finally(() => setStudioLoading(false));

    // Fetch model-by-model recommendations for the local model picker.
    setModelsLoading(true);
    getStudioModels()
      .then(data => {
        setStudioModels(data.models);
        setDetectedModelVram(data.detected_vram_gb);
        setSelectedStudioModels(new Set(data.recommended_ids));
      })
      .catch(() => {})
      .finally(() => setModelsLoading(false));

    return () => {
      cancelled = true;
      if (healthRetryTimer) clearTimeout(healthRetryTimer);
      if (storageRetryTimer) clearTimeout(storageRetryTimer);
    };
  }, [refreshSetupHelper, refreshSetupInventory, refreshWorkspacePassport]);

  const handleTest = async () => {
    setTestLoading(true);
    setTestError(null);
    setTestResult(null);
    try {
      const resp = await query(testPrompt);
      setTestResult(resp.content);
    } catch (err) {
      setTestError(err instanceof Error ? err.message : 'Test failed');
    } finally {
      setTestLoading(false);
    }
  };

  const handleSaveKey = async (providerId: string) => {
    const key = keyInputs[providerId]?.trim();
    if (!key) return;
    setSavingKey(providerId);
    setKeyErrors(prev => ({ ...prev, [providerId]: '' }));
    try {
      await saveProviderKey(providerId, key);
      setSavedKeys(prev => { const s = new Set(Array.from(prev)); s.add(providerId); return s; });
      setExpandedProvider(null);
      setFreeProviders(prev =>
        prev.map(p => p.id === providerId ? { ...p, configured: true } : p)
      );
    } catch (err) {
      setKeyErrors(prev => ({
        ...prev,
        [providerId]: err instanceof Error ? err.message : 'Failed to save key',
      }));
    } finally {
      setSavingKey(null);
    }
  };

  const handleConfigureStorage = async () => {
    const home = storageHomeInput.trim();
    if (!home) {
      setStorageError('Choose the mounted folder that survives every session.');
      return;
    }

    setStorageSaving(true);
    setStorageError(null);
    try {
      const status = await configureStorage({
        home_dir: home,
        min_free_gb: PERSISTENT_STORAGE_MIN_GB,
        activate: true,
      });
      setStorageStatus(status);
      setStorageHomeInput(status.layout.home);
      await Promise.allSettled([
        refreshStudioModels(),
        refreshStudioPacks(),
        refreshComfyUI(),
        refreshSetupHelper(status.layout.home),
        refreshSetupInventory(false, status.layout.home),
        refreshWorkspacePassport(status.layout.home),
      ]);
    } catch (err) {
      setStorageError(err instanceof Error ? err.message : 'Could not configure persistent storage');
    } finally {
      setStorageSaving(false);
    }
  };

  const refreshComfyUI = async () => {
    setComfyLoading(true);
    try {
      const status = await getComfyUIStatus();
      setComfyStatus(status);
      setComfyExamples(status.examples ?? []);
    } catch {
      try {
        const examples = await getComfyUIExamples();
        setComfyExamples(examples.examples);
      } catch {
        // keep whatever we already have
      }
    } finally {
      setComfyLoading(false);
    }
  };

  const refreshStudioPacks = async () => {
    setStudioLoading(true);
    try {
      const data = await getStudioPacks();
      setStudioPacks(data.packs);
      setStudioBundles(data.bundles);
      setStudioRoot(data.root);
      setSelectedStudioPacks(prev => {
        if (prev.size > 0) return prev;
        const starterIds = data.bundles.starter ?? data.packs.map(pack => pack.id);
        return new Set(selectableStudioPackIds(data.packs, starterIds));
      });
    } catch {
      // keep current pack state
    } finally {
      setStudioLoading(false);
    }
  };

  const toggleStudioPack = (packId: string) => {
    const pack = studioPacks.find(item => item.id === packId);
    if (pack && !studioPackInstallable(pack)) {
      setStudioError(studioPackBlockedReason(pack) || `${pack.title} is blocked on this host.`);
      return;
    }
    setSelectedStudioPacks(prev => {
      const next = new Set(prev);
      if (next.has(packId)) next.delete(packId);
      else next.add(packId);
      return next;
    });
  };

  const selectStudioBundle = (bundleId: string) => {
    const packIds = studioBundles[bundleId] ?? [];
    setSelectedStudioPacks(new Set(selectableStudioPackIds(studioPacks, packIds)));
  };

  const expandStudioPackGroups = (
    groups: string[],
    packs: StudioPack[] = studioPacks,
    bundles: Record<string, string[]> = studioBundles,
  ) => (
    selectableStudioPackIds(
      packs,
      groups.flatMap(group => bundles[group] ?? [group])
    )
  );

  const wizardProfilePackIds = (
    profile: WizardProfile,
    packs: StudioPack[] = studioPacks,
    bundles: Record<string, string[]> = studioBundles,
  ) => {
    if (profile === 'student') {
      return expandStudioPackGroups(['rootless-ollama', 'agent-lab', 'nvidia-omni-agent'], packs, bundles);
    }
    if (profile === 'llm') {
      return expandStudioPackGroups(['rootless-ollama'], packs, bundles);
    }
    if (profile === 'creator') {
      return expandStudioPackGroups(['rootless-ollama', 'creative', 'comfy', 'github-login-helper'], packs, bundles);
    }
    if (profile === 'agent') {
      return expandStudioPackGroups(['rootless-ollama', 'agents', 'claw'], packs, bundles);
    }
    if (profile === 'game') {
      return expandStudioPackGroups(['rootless-ollama', 'game', 'creative', 'comfy'], packs, bundles);
    }
    if (profile === 'music') {
      return expandStudioPackGroups(['rootless-ollama', 'music'], packs, bundles);
    }
    return expandStudioPackGroups(['all'], packs, bundles)
      .filter(packId => packId !== 'llm-starter' && packId !== 'llm-coder-reasoner');
  };

  const wizardProfileStep = (profile: WizardProfile): Step => {
    if (profile === 'creator' || profile === 'game') return 'comfyui';
    if (profile === 'student' || profile === 'llm' || profile === 'agent' || profile === 'music') return 'studio';
    return 'models';
  };

  const wizardProfileNeedsComfy = (profile: WizardProfile) => (
    profile === 'creator' || profile === 'game' || profile === 'full'
  );

  const wizardProfileModelIds = (profile: WizardProfile, models: StudioModel[] = studioModels) => {
    const recommendedModels = models
      .filter(model => model.recommended)
      .map(model => model.id);
    const allModelIds = models
      .filter(model => model.recommended || model.fits_vram)
      .map(model => model.id);

    if (profile === 'agent') {
      const agentModels = models
        .filter(model => ['code', 'embedding'].includes(model.category))
        .filter(model => model.recommended || model.fits_vram)
        .map(model => model.id);
      return agentModels.length ? agentModels : recommendedModels;
    }

    if (profile === 'full') {
      return allModelIds.length ? allModelIds : recommendedModels;
    }

    return recommendedModels;
  };

  const wizardProfileExampleIds = (
    examples: ComfyUIExample[] = visibleComfyExamples,
    vramGb: number = detectedModelVram,
  ) => {
    const vramLimit = vramGb || 12;
    const starterExamples = examples
      .filter(example => example.recommended_vram_gb <= vramLimit)
      .map(example => example.id);
    return starterExamples;
  };

  const applyWizardProfile = (profile: WizardProfile) => {
    setSelectedStudioModels(new Set(wizardProfileModelIds(profile)));
    setSelectedStudioPacks(new Set(wizardProfilePackIds(profile)));
    setSelectedComfyExamples(new Set(wizardProfileExampleIds()));
    setStep(wizardProfileStep(profile));
  };

  const refreshStudioModels = async () => {
    setModelsLoading(true);
    try {
      const data = await getStudioModels();
      setStudioModels(data.models);
      setDetectedModelVram(data.detected_vram_gb);
      setSelectedStudioModels(prev => {
        if (prev.size > 0) return prev;
        return new Set(data.recommended_ids);
      });
    } catch {
      // keep current model state
    } finally {
      setModelsLoading(false);
    }
  };

  const ensureWizardCatalogReady = async () => {
    let packs = studioPacks;
    let bundles = studioBundles;
    let models = studioModels;
    let examples = visibleComfyExamples;
    let vramGb = detectedModelVram;

    const [packData, modelData, comfyData] = await Promise.all([
      packs.length > 0 ? Promise.resolve(null) : getStudioPacks().catch(() => null),
      models.length > 0 ? Promise.resolve(null) : getStudioModels().catch(() => null),
      examples.length > 0 ? Promise.resolve(null) : getComfyUIStatus().catch(() => null),
    ]);

    if (packData) {
      packs = packData.packs;
      bundles = packData.bundles;
      setStudioPacks(packData.packs);
      setStudioBundles(packData.bundles);
      setStudioRoot(packData.root);
      setSelectedStudioPacks(prev => {
        if (prev.size > 0) return prev;
        const starterIds = packData.bundles.starter ?? packData.packs.map(pack => pack.id);
        return new Set(selectableStudioPackIds(packData.packs, starterIds));
      });
    }

    if (modelData) {
      models = modelData.models;
      vramGb = modelData.detected_vram_gb;
      setStudioModels(modelData.models);
      setDetectedModelVram(modelData.detected_vram_gb);
      setSelectedStudioModels(prev => {
        if (prev.size > 0) return prev;
        return new Set(modelData.recommended_ids);
      });
    }

    if (comfyData) {
      setComfyStatus(comfyData);
      if (comfyData.examples?.length) {
        examples = comfyData.examples;
        setComfyExamples(comfyData.examples);
      }
    }

    return { packs, bundles, models, examples, vramGb };
  };

  const handleUseRecommendedStorage = async (): Promise<StorageStatus | null> => {
    const recommendedHome = mountRecommendation?.recommended_home;
    setMountActivating(true);
    setStorageSaving(true);
    setStorageError(null);
    try {
      const activated = await activateMountAutopilot(recommendedHome, PERSISTENT_STORAGE_MIN_GB);
      setStorageStatus(activated.storage);
      setStorageHomeInput(activated.storage.layout.home);
      setMountAutopilot(activated.mount_autopilot);
      setWizardBuildMessage('nvWizard prepared the persistent block volume. Models, apps, and projects will live somewhere that survives reboot.');
      await Promise.allSettled([
        refreshStudioModels(),
        refreshStudioPacks(),
        refreshComfyUI(),
        refreshSetupHelper(activated.storage.layout.home),
        refreshSetupInventory(false, activated.storage.layout.home),
        refreshWorkspacePassport(activated.storage.layout.home),
      ]);
      return activated.storage;
    } catch (err) {
      setStorageError(err instanceof Error ? err.message : 'Could not activate recommended persistent storage');
      return null;
    } finally {
      setStorageSaving(false);
      setMountActivating(false);
    }
  };

  const toggleStudioModel = (modelId: string) => {
    setSelectedStudioModels(prev => {
      const next = new Set(prev);
      if (next.has(modelId)) next.delete(modelId);
      else next.add(modelId);
      return next;
    });
  };

  const selectRecommendedModels = () => {
    setSelectedStudioModels(new Set(studioModels.filter(model => model.recommended).map(model => model.id)));
  };

  const selectInstalledMissingModels = () => {
    setSelectedStudioModels(new Set(
      studioModels
        .filter(model => model.recommended && !model.installed)
        .map(model => model.id)
    ));
  };

  const handleInstallStudioModels = (modelIds?: string[]) => {
    if (modelsInstalling) return;
    if (!storageStatus?.ok || storageStatus.configured_by === 'default') {
      setModelError('nvWizard is finding persistent storage before downloading models.');
      void handleUseRecommendedStorage();
      return;
    }
    const selected = modelIds?.length ? modelIds : Array.from(selectedStudioModels);
    if (selected.length === 0) {
      setModelError('Select at least one local model.');
      return;
    }

    setModelsInstalling(true);
    setModelError(null);
    setModelEvents([]);

    installStudioModelsStream(
      { model_ids: selected, force_update: false },
      {
        onJob: job => {
          mergeInstallJob(job);
        },
        onStatus: job => {
          mergeInstallJob(job);
        },
        onEvent: event => {
          setModelEvents(prev => [...prev.slice(-10), event]);
          if (event.status_snapshot) {
            setStudioModels(event.status_snapshot.models);
            setDetectedModelVram(event.status_snapshot.detected_vram_gb);
          }
        },
        onComplete: event => {
          setModelEvents(prev => [...prev.slice(-10), event]);
          setModelsInstalling(false);
          refreshStudioModels();
          void refreshInstallJobs();
          void refreshSetupInventory(false);
          void refreshSetupHelper(storageStatus?.layout.home);
        },
        onError: error => {
          setModelError(error);
          setModelsInstalling(false);
          void refreshInstallJobs();
          void refreshSetupHelper(storageStatus?.layout.home);
        },
      }
    );
  };

  const recommendedMissingModelIds = () => (
    studioModels
      .filter(model => model.recommended && !model.installed)
      .map(model => model.id)
  );

  const handleInstallStudioPacks = (packIds?: string[]) => {
    if (studioInstalling) return;
    if (!storageStatus?.ok || storageStatus.configured_by === 'default') {
      setStudioError('nvWizard is finding persistent storage before installing packs.');
      void handleUseRecommendedStorage();
      return;
    }
    const selected = packIds?.length ? packIds : Array.from(selectedStudioPacks);
    if (selected.length === 0) {
      setStudioError('Select at least one AI Studio pack.');
      return;
    }

    setStudioInstalling(true);
    setStudioError(null);
    setStudioEvents([]);

    installStudioPacksStream(
      { pack_ids: selected, force_update: false },
      {
        onJob: job => {
          mergeInstallJob(job);
        },
        onStatus: job => {
          mergeInstallJob(job);
        },
        onEvent: event => {
          setStudioEvents(prev => [...prev.slice(-10), event]);
          if (event.status_snapshot) {
            setStudioPacks(event.status_snapshot.packs);
            setStudioBundles(event.status_snapshot.bundles);
            setStudioRoot(event.status_snapshot.root);
          }
        },
        onComplete: event => {
          setStudioEvents(prev => [...prev.slice(-10), event]);
          setStudioInstalling(false);
          refreshStudioPacks();
          void refreshInstallJobs();
          void refreshSetupInventory(false);
          void refreshSetupHelper(storageStatus?.layout.home);
        },
        onError: error => {
          setStudioError(error);
          setStudioInstalling(false);
          void refreshInstallJobs();
          void refreshSetupHelper(storageStatus?.layout.home);
        },
      }
    );
  };

  const handleInstallComfyUI = () => {
    if (comfyInstalling) return;
    if (!storageStatus?.ok || storageStatus.configured_by === 'default') {
      setComfyError('nvWizard is finding persistent storage before installing ComfyUI.');
      void handleUseRecommendedStorage();
      return;
    }
    setComfyInstalling(true);
    setComfyError(null);
    setComfyEvents([]);

    installComfyUIStream(
      { torch_profile: recommendedTorchProfile, force_update: false },
      {
        onJob: job => {
          mergeInstallJob(job);
        },
        onStatus: job => {
          mergeInstallJob(job);
        },
        onEvent: event => {
          setComfyEvents(prev => [...prev.slice(-8), event]);
          if (event.status_snapshot) {
            setComfyStatus(event.status_snapshot);
          }
        },
        onComplete: event => {
          setComfyEvents(prev => [...prev.slice(-8), event]);
          setComfyInstalling(false);
          refreshComfyUI();
          void refreshInstallJobs();
          void refreshSetupInventory(false);
          void refreshSetupHelper(storageStatus?.layout.home);
        },
        onError: error => {
          setComfyError(error);
          setComfyInstalling(false);
          void refreshInstallJobs();
          void refreshSetupHelper(storageStatus?.layout.home);
        },
      }
    );
  };

  const handleStartComfyUI = async () => {
    setComfyStarting(true);
    setComfyError(null);
    try {
      const status = await startComfyUI();
      setComfyStatus(status);
    } catch (err) {
      setComfyError(err instanceof Error ? err.message : 'Failed to start ComfyUI');
    } finally {
      setComfyStarting(false);
    }
  };

  const toggleComfyExample = (exampleId: string) => {
    setSelectedComfyExamples(prev => {
      const next = new Set(prev);
      if (next.has(exampleId)) next.delete(exampleId);
      else next.add(exampleId);
      return next;
    });
  };

  const handleSaveComfyPlan = async () => {
    setComfyPlanSaving(true);
    setComfyError(null);
    setComfyPlanMessage(null);
    try {
      const plan = await saveComfyUIModelPlan(Array.from(selectedComfyExamples));
      setComfyPlanMessage(
        `Saved ${plan.model_count} model requirement(s), folder targets, and ${plan.download_helper} beside ${plan.plan_path}`
      );
    } catch (err) {
      setComfyError(err instanceof Error ? err.message : 'Failed to save ComfyUI model plan');
    } finally {
      setComfyPlanSaving(false);
    }
  };

  const handleBuildWizardProfile = async (profile: WizardProfile) => {
    if (activeWizardBuild || studioInstalling || modelsInstalling || comfyInstalling || apiDisconnected) return;

    setActiveWizardBuild(profile);
    setWizardBuildMessage('nvWizard is checking the mission catalog, hardware, and persistent storage.');

    try {
      const catalog = await ensureWizardCatalogReady();
      const modelIds = wizardProfileModelIds(profile, catalog.models);
      const packIds = wizardProfilePackIds(profile, catalog.packs, catalog.bundles);
      const exampleIds = wizardProfileExampleIds(catalog.examples, catalog.vramGb);

      setSelectedStudioModels(new Set(modelIds));
      setSelectedStudioPacks(new Set(packIds));
      setSelectedComfyExamples(new Set(exampleIds));

      let missionHomeDir = storageStatus?.layout.home;
      if (!storageReady) {
        setWizardBuildMessage('nvWizard is finding the persistent block storage first, then it will build the mission there.');
        const detectedStorage = await handleUseRecommendedStorage();
        if (!detectedStorage?.ok || detectedStorage.configured_by === 'default') {
          setWizardBuildMessage('nvWizard could not prove the persistent storage path yet. It will stay on the simple view; open Advanced Details only if you want the manual override.');
          return;
        }
        missionHomeDir = detectedStorage.layout.home;
      }

      setWizardBuildMessage('nvWizard picked the beginner-safe defaults and handed the mission to the backend job runner.');
      setStep(wizardProfileNeedsComfy(profile) ? 'comfyui' : 'studio');
      setStudioEvents([]);
      setModelEvents([]);
      setComfyEvents([]);
      setStudioInstalling(packIds.length > 0);
      setModelsInstalling(modelIds.length > 0);
      setComfyInstalling(wizardProfileNeedsComfy(profile));

      installWizardMissionStream(
        {
          profile,
          home_dir: missionHomeDir,
          torch_profile: recommendedTorchProfile,
          force_update: false,
          min_free_gb: PERSISTENT_STORAGE_MIN_GB,
        },
        {
          onJob: job => {
            mergeInstallJob(job);
            setWizardBuildMessage('Mission job started. Downloads and setup are tracked under the persistent workspace.');
          },
          onStatus: job => {
            mergeInstallJob(job);
            if (job.message) setWizardBuildMessage(job.message);
          },
          onEvent: (event: WizardMissionInstallEvent) => {
            setWizardBuildMessage(event.message || 'Mission build is running.');
            if (event.stage === 'models') {
              setModelEvents(prev => [...prev.slice(-10), event as StudioModelInstallEvent]);
            } else if (event.stage === 'comfyui' || event.stage === 'comfyui-plan' || event.stage === 'comfyui-nodes') {
              setComfyEvents(prev => [...prev.slice(-8), event as ComfyUIInstallEvent]);
              if ((event as ComfyUIInstallEvent).status_snapshot) {
                setComfyStatus((event as ComfyUIInstallEvent).status_snapshot as ComfyUIStatus);
              }
            } else {
              setStudioEvents(prev => [...prev.slice(-10), event as StudioPackInstallEvent]);
            }
          },
          onComplete: event => {
            setWizardBuildMessage(event.message || 'Mission build complete. Try the smoke test, then launch the tools.');
            setActiveWizardBuild(null);
            setStudioInstalling(false);
            setModelsInstalling(false);
            setComfyInstalling(false);
            setStep('test');
            void refreshInstallJobs();
            void refreshStudioPacks();
            void refreshStudioModels();
            void refreshComfyUI();
            void refreshSetupInventory(false);
            void refreshSetupHelper(missionHomeDir);
          },
          onError: error => {
            setWizardBuildMessage(`nvWizard paused: ${error}. Ask nvWizard to read the logs, or open Advanced Details if you want the full job trail.`);
            setActiveWizardBuild(null);
            setStudioInstalling(false);
            setModelsInstalling(false);
            setComfyInstalling(false);
            void refreshInstallJobs();
            void refreshSetupInventory(false);
            void refreshSetupHelper(missionHomeDir);
          },
        }
      );
    } catch (err) {
      setWizardBuildMessage(err instanceof Error
        ? `nvWizard paused: ${err.message}. Ask nvWizard to read logs, jobs, and receipts before opening Advanced Details.`
        : 'nvWizard paused: setup needs attention. Ask nvWizard to read logs, jobs, and receipts before opening Advanced Details.'
      );
      setActiveWizardBuild(null);
      void refreshInstallJobs();
      void refreshSetupInventory(false);
      void refreshSetupHelper(storageStatus?.layout.home);
    }
  };

  useEffect(() => {
    const finishedMission = installJobs.find(job =>
      job.kind === 'wizard-mission' &&
      !isActiveInstallJob(job) &&
      !handledMissionJobsRef.current.has(job.id)
    );
    if (!finishedMission) return;

    handledMissionJobsRef.current.add(finishedMission.id);
    setActiveWizardBuild(null);
    setStudioInstalling(false);
    setModelsInstalling(false);
    setComfyInstalling(false);

    const missionHomeDir = typeof finishedMission.request?.home_dir === 'string'
      ? finishedMission.request.home_dir
      : storageStatus?.layout.home;

    if (finishedMission.status === 'complete') {
      setWizardBuildMessage(finishedMission.message || 'Mission build complete. Try the smoke test, then launch the tools.');
      if (!advancedSetupOpen) setStep('test');
      void refreshStudioPacks();
      void refreshStudioModels();
      void refreshComfyUI();
      void refreshSetupInventory(false, missionHomeDir);
      void refreshWorkspacePassport(missionHomeDir);
      void refreshSetupHelper(missionHomeDir);
    } else if (['failed', 'interrupted', 'canceled'].includes(finishedMission.status)) {
      setWizardBuildMessage(finishedMission.message || `Mission build ${finishedMission.status}. Ask nvWizard to explain the logs, or open Advanced Details for the full job trail.`);
      void refreshSetupInventory(false, missionHomeDir);
      void refreshSetupHelper(missionHomeDir);
    }
  }, [
    advancedSetupOpen,
    installJobs,
    refreshSetupHelper,
    refreshSetupInventory,
    refreshWorkspacePassport,
    storageStatus?.layout.home,
  ]);

  useEffect(() => {
    const failedJob = installJobs.find(job =>
      ['failed', 'interrupted', 'canceled'].includes(job.status) &&
      !autoDebugJobsRef.current.has(job.id)
    );
    if (!failedJob) return;

    autoDebugJobsRef.current.add(failedJob.id);
    void askSetupQuestion(
      `A setup install job needs debugging. Job: ${failedJob.title}. Status: ${failedJob.status}. Message: ${failedJob.message || 'no message'}. Explain what likely broke and what rootless repair button should run next.`,
    );
  }, [askSetupQuestion, installJobs]);

  const currentStepIdx = STEPS.findIndex(s => s.id === step);
  const currentStep = STEPS[currentStepIdx] ?? STEPS[0];
  const currentAdvancedGroup = ADVANCED_GROUPS.find(group => group.steps.includes(step)) ?? ADVANCED_GROUPS[0];
  const apiDisconnected = apiStatus === 'disconnected';
  const storageReady = Boolean(storageStatus?.ok && storageStatus.configured_by !== 'default');
  const storageFreeGb = storageStatus?.free_gb ?? null;
  const mountRecommendation = mountAutopilot?.recommended ?? missionControl?.mount_autopilot.recommended ?? bootPreflight?.mount_autopilot?.recommended ?? null;
  const storageAutopilotBusy = !storageReady && !apiDisconnected && (mountActivating || storageSaving || apiStatus === 'checking' || storageStatus === null);
  const storageBeginnerLabel = storageReady
    ? 'ready'
    : apiDisconnected
      ? 'api offline'
    : storageAutopilotBusy
      ? 'finding'
      : mountRecommendation
        ? 'detected'
        : 'checking';
  const storagePrimaryLabel = storageReady
    ? 'Build AI Starter'
    : apiDisconnected
      ? 'API Offline'
    : storageAutopilotBusy
      ? 'Finding Storage'
      : 'Use Persistent Drive';
  const workspaceHome = workspacePassport?.storage_home ?? storageStatus?.layout.home ?? 'finding persistent drive';
  const workspaceFreeText = storageFreeGb === null ? 'free space checking' : `${storageFreeGb} GB free`;
  const workspaceReceipts =
    typeof workspacePassport?.receipts?.data === 'object' && workspacePassport.receipts.data !== null && 'count' in workspacePassport.receipts.data
      ? Number((workspacePassport.receipts.data as { count?: number }).count ?? 0)
      : 0;
  const workspaceActiveJobs = workspacePassport?.jobs.active_count ?? 0;
  const rootlessRuntimeStrategy = rootlessPolicy?.runtime.strategy ?? setupCompatibility?.recommended_torch_profile ?? 'checking';
  const rootlessStatus = rootlessPolicy?.status ?? workspacePassport?.rootless.policy_status ?? 'checking';
  const profilesReady = !modelsLoading && !studioLoading && !comfyLoading;
  const visibleComfyExamples = comfyStatus?.examples?.length ? comfyStatus.examples : comfyExamples;
  const selectedComfyModelCount = new Set(
    visibleComfyExamples
      .filter(example => selectedComfyExamples.has(example.id))
      .flatMap(example => example.models)
  ).size;
  const selectedStudioPackIds = selectableStudioPackIds(studioPacks, Array.from(selectedStudioPacks));
  const starterStudioPackIds = studioBundles.starter ?? [];
  const clawStudioPackIds = studioBundles.claw ?? [];
  const blockedStudioPackCount = studioPacks.filter(pack => !studioPackInstallable(pack)).length;
  const studioCategories = Array.from(new Set(studioPacks.map(pack => pack.category)));
  const selectedStudioPackDiskGb = studioPacks
    .filter(pack => selectedStudioPacks.has(pack.id))
    .reduce((total, pack) => total + pack.estimated_disk_gb, 0);
  const selectedModelIds = Array.from(selectedStudioModels);
  const modelCategories = Array.from(new Set(studioModels.map(model => model.category)));
  const selectedModelDiskGb = studioModels
    .filter(model => selectedStudioModels.has(model.id))
    .reduce((total, model) => total + model.estimated_disk_gb, 0);
  const activeInstallJobs = installJobs.filter(isActiveInstallJob);
  const visibleInstallJobs = installJobs.slice(0, 5);
  const helperActions = setupHelper?.actions.slice(0, 4) ?? [];
  const helperIssues = setupHelper?.issues?.slice(0, 4) ?? [];
  const visibleReceipts = setupReceipts?.receipts.slice(0, 5) ?? [];
  const unhealthyReceiptCount = setupReceipts?.summary.unhealthy ?? setupHelper?.receipts?.unhealthy ?? 0;
  const receiptCount = setupReceipts?.count ?? setupHelper?.receipts?.count ?? 0;
  const catalogSource = setupCatalog?.source ?? setupHelper?.catalog?.source ?? 'bundled';
  const visibleCompatibilityApps = setupCompatibility?.apps
    .filter(app => app.status !== 'ready')
    .slice(0, 5) ?? [];
  const compatibilityIssueCount = setupCompatibility?.issue_count ?? setupHelper?.compatibility?.issue_count ?? 0;
  const compatibilityBlockedCount = setupCompatibility?.blocked_count ?? setupHelper?.compatibility?.blocked_count ?? 0;
  const compatibilityFixableCount = setupCompatibility?.rootless_fixable_count ?? setupHelper?.compatibility?.rootless_fixable_count ?? 0;
  const bootChangeCount = bootPreflight?.changes.length ?? setupHelper?.boot_preflight?.change_count ?? 0;
  const bootAgentHelper = bootPreflight?.agent_helper ?? setupHelper?.boot_preflight?.agent_helper;
  const missionStages = missionControl?.stages ?? [];
  const autoRepair = missionControl?.auto_repair ?? bootPreflight?.auto_repair ?? null;
  const autoRepairActions = autoRepair && 'actions' in autoRepair
    ? autoRepair.actions
    : autoRepair && 'plan' in autoRepair
      ? autoRepair.plan.actions
      : [];
  const smokeTests = missionControl?.smoke_tests ?? bootPreflight?.smoke_tests ?? null;
  const modelFit = missionControl?.model_fit ?? bootPreflight?.model_fit ?? null;
  const visibleReadinessGates = productionReadiness?.gates
    .filter(gate => gate.status !== 'pass')
    .slice(0, 4) ?? [];
  const diagnosticsLogLineCount = diagnosticsReport?.logs.recent.reduce(
    (total, item) => total + item.lines.length,
    0
  ) ?? 0;
  const readinessTone = productionReadiness?.status === 'production-ready'
    ? 'text-[#76B900]'
    : productionReadiness?.status === 'blocked'
      ? 'text-[#dc2626]'
      : 'text-[#d97706]';
  const detectedTorchProfile = setupCompatibility?.recommended_torch_profile
    ?? setupHelper?.compatibility?.recommended_torch_profile
    ?? 'nvidia-cu121';
  const recommendedTorchProfile: ComfyUITorchProfile = (
    ['nvidia-cu130', 'nvidia-cu121', 'cpu', 'skip'].includes(detectedTorchProfile)
      ? detectedTorchProfile
      : 'nvidia-cu121'
  ) as ComfyUITorchProfile;
  const selectedComfyTorchProfile = [...comfyEvents].reverse()
    .find(event => typeof event.torch_profile === 'string')?.torch_profile
    ?? recommendedTorchProfile;
  const setupConcernCount =
    (setupInventoryError ? 1 : 0) +
    (setupHelperError ? 1 : 0) +
    unhealthyReceiptCount +
    compatibilityIssueCount +
    bootChangeCount;
  const showAdvancedSetup = advancedSetupOpen && step !== 'welcome';
  const showInstallJobs = activeInstallJobs.length > 0 || (showAdvancedSetup && (visibleInstallJobs.length > 0 || jobsError));
  const anyInstallRunning = Boolean(activeWizardBuild) || studioInstalling || modelsInstalling || comfyInstalling;
  const topHelperAction = helperActions[0] ?? null;
  const catalogProfiles = setupCatalog?.catalog.profiles ?? [];
  const catalogProfileFor = (profileId: WizardProfile) => (
    catalogProfiles.find(profile => profile.id === profileId) ?? null
  );
  const catalogText = (profileId: WizardProfile, key: 'title' | 'description', fallback: string) => {
    const value = catalogProfileFor(profileId)?.[key];
    return typeof value === 'string' ? value : fallback;
  };
  const diskForPackIds = (packIds: string[]) => studioPacks
    .filter(pack => packIds.includes(pack.id))
    .reduce((total, pack) => total + pack.estimated_disk_gb, 0);
  const diskForModelIds = (modelIds: string[]) => studioModels
    .filter(model => modelIds.includes(model.id))
    .reduce((total, model) => total + model.estimated_disk_gb, 0);
  const hasCatalogSizing = studioPacks.length > 0 || studioModels.length > 0;
  const gpuDetectionStatus = gpuInfo?.detection?.status ?? 'checking';
  const gpuDetectionIssue = gpuInfo?.detection?.issues?.[0]?.message ?? '';
  const githubPack = studioPacks.find(pack => pack.id === 'github-login-helper') ?? null;
  const missionProfiles: Array<{
    id: WizardProfile;
    title: string;
    description: string;
    label: string;
    outcome: string;
    includes: string[];
    logos: BrandLogoId[];
    primary?: boolean;
    advanced?: boolean;
  }> = [
    {
      id: 'student',
      title: catalogText('student', 'title', 'AI Starter'),
      description: catalogText('student', 'description', 'Local chat, coding help, research support, starter models, and optional NVIDIA agent tools.'),
      label: 'Recommended',
      outcome: 'A practical AI desk for classes, projects, notes, and first local model experiments.',
      includes: ['Local chat', 'Recommended models', 'NVIDIA option', 'Agent helper'],
      logos: ['ollama', 'github', 'openclaw', 'nvidia'],
      primary: true,
    },
    {
      id: 'llm',
      title: 'Local LLM Lab',
      description: 'Build a local chat, code, and embeddings bench without touching the base OS.',
      label: 'LLMs',
      outcome: 'Compare local models, write code, summarize notes, and stay offline-friendly.',
      includes: ['Ollama runtime', 'VRAM-fit models', 'Coder model', 'Embeddings'],
      logos: ['ollama', 'nvidia'],
      advanced: true,
    },
    {
      id: 'creator',
      title: catalogText('creator', 'title', 'Graphics Creator Studio'),
      description: catalogText('creator', 'description', 'Image generation, ComfyUI workflows, Blender, and 3D asset helpers.'),
      label: 'Creative',
      outcome: 'Generate images, prep video/3D workflows, and keep creative assets on persistent storage.',
      includes: ['ComfyUI', 'Power nodes', 'Blender LTS', 'GitHub connect'],
      logos: ['comfyui', 'blender', 'github', 'nvidia'],
    },
    {
      id: 'agent',
      title: catalogText('agent', 'title', 'Agent Builder'),
      description: catalogText('agent', 'description', 'Local agent libraries, a coding model, and embeddings.'),
      label: 'Agents',
      outcome: 'Install the local agent lab, OpenClaw, and NemoClaw only when the host can support it.',
      includes: ['Agent lab', 'OpenClaw', 'Conditional NemoClaw', 'Coder model'],
      logos: ['openclaw', 'nvidia', 'ollama'],
      advanced: true,
    },
    {
      id: 'game',
      title: catalogText('game', 'title', 'Game Dev Lab'),
      description: catalogText('game', 'description', 'Game prototyping, Blender assets, and mod helper workspace.'),
      label: 'Game',
      outcome: 'Prototype games, build assets, connect repos, and keep engines on persistent storage.',
      includes: ['Godot', 'Unity helper', 'Unreal helper', 'GitHub connect'],
      logos: ['godot', 'unity', 'unreal', 'github'],
    },
    {
      id: 'music',
      title: catalogText('music', 'title', 'Music Producer Studio'),
      description: catalogText('music', 'description', 'AI music generation, stem separation, transcription, and rootless DAW helpers.'),
      label: 'Music',
      outcome: 'Create songs, split stems, transcribe vocals, clean audio, and launch music tools from the persistent drive.',
      includes: ['ACE-Step', 'Demucs', 'WhisperX', 'Audacity/LMMS'],
      logos: ['audacity', 'lmms', 'nvidia', 'github'],
    },
    {
      id: 'full',
      title: catalogText('full', 'title', 'Power User Workstation'),
      description: catalogText('full', 'description', 'Everything nvHive can install without root access, guarded by host checks.'),
      label: 'Power',
      outcome: 'Install every supported rootless tool that passes the host checks.',
      includes: ['LLMs', 'Agents', 'ComfyUI', 'Blender', 'Game', 'Music'],
      logos: ['nvidia', 'ollama', 'comfyui', 'blender', 'audacity', 'github'],
      advanced: true,
    },
  ];

  const beginnerProfileIds = new Set<WizardProfile>(['student', 'creator', 'game', 'music']);
  const beginnerProfiles = missionProfiles.filter(profile => beginnerProfileIds.has(profile.id));
  const beginnerProfileCopy: Partial<Record<WizardProfile, string>> = {
    student: 'Local AI for classwork, coding, research, and optional NVIDIA agent guidance.',
    creator: 'ComfyUI, Blender, and creative helpers for images, 3D, and video workflows.',
    game: 'Game engine helpers, Blender assets, GitHub repos, and mod workspace tools.',
    music: 'AI music generation, stem separation, transcription, and audio editor helpers.',
  };
  const profileActionLabels: Record<WizardProfile, string> = {
    student: 'Install Local AI',
    llm: 'Install Local Chat',
    creator: 'Install Creator Studio',
    agent: 'Install Agent Tools',
    game: 'Install Game Lab',
    music: 'Install Music Studio',
    full: 'Install Workstation',
  };
  const profileBusyLabels: Record<WizardProfile, string> = {
    student: 'Installing AI Starter',
    llm: 'Installing Local Chat',
    creator: 'Installing Creator Studio',
    agent: 'Installing Agent Tools',
    game: 'Installing Game Lab',
    music: 'Installing Music Studio',
    full: 'Installing Workstation',
  };
  const modelFitRecommendedIds = modelFit?.recommended_ids ?? [];
  const modelFitStorageFits = modelFit && 'storage_fits_queue' in modelFit
    ? modelFit.storage_fits_queue
    : undefined;
  const modelFitOllamaRunning = Boolean(modelFit && 'ollama_running' in modelFit && modelFit.ollama_running);
  const recommendedModelLabel = modelFitRecommendedIds.length
    ? modelFitRecommendedIds.slice(0, 2).join(', ')
    : detectedModelVram
      ? `${detectedModelVram} GB VRAM`
      : 'checking';
  const modelFitState: SetupCheckState = modelFitStorageFits === false
    ? 'fix'
    : modelFitRecommendedIds.length || selectedStudioModels.size > 0
      ? 'ready'
      : modelsLoading
        ? 'checking'
        : 'warn';
  const localAiReady = ollamaStatus === 'online' || modelFitOllamaRunning;
  const systemCheckItems: Array<{ label: string; value: string; state: SetupCheckState }> = [
    {
      label: 'Storage',
      value: storageReady ? (storageFreeGb === null ? 'persistent ready' : `${storageFreeGb} GB free`) : storageBeginnerLabel,
      state: storageReady ? 'ready' : storageAutopilotBusy ? 'checking' : 'fix',
    },
    {
      label: 'GPU',
      value: gpuLoading
        ? 'scanning'
        : gpuInfo?.gpus?.length
          ? `${gpuInfo.gpus[0].name} / ${gpuInfo.gpus[0].vram_gb} GB`
          : 'CPU fallback',
      state: gpuLoading ? 'checking' : gpuInfo?.gpus?.length ? 'ready' : 'warn',
    },
    {
      label: 'Models',
      value: recommendedModelLabel,
      state: modelFitState,
    },
    {
      label: 'Local AI',
      value: ollamaStatus === 'checking' ? 'checking' : localAiReady ? 'Ollama online' : 'install on click',
      state: ollamaStatus === 'checking' ? 'checking' : localAiReady ? 'ready' : 'warn',
    },
    {
      label: 'Runtime',
      value: rootlessRuntimeStrategy,
      state: rootlessStatus === 'blocked' ? 'fix' : rootlessStatus === 'warn' ? 'warn' : rootlessStatus === 'checking' ? 'checking' : 'ready',
    },
    {
      label: 'Boot',
      value: apiStatus === 'checking'
        ? 'checking'
        : apiDisconnected
          ? 'API offline'
          : bootChangeCount ? `${bootChangeCount} change${bootChangeCount === 1 ? '' : 's'}` : 'clean',
      state: apiStatus === 'checking' ? 'checking' : apiDisconnected ? 'fix' : bootChangeCount ? 'warn' : setupConcernCount ? 'fix' : 'ready',
    },
  ];
  const advisorModeLabel = bootAgentHelper?.local_agent_ready
    ? 'local agent ready'
    : setupHelper?.assistant?.mode === 'offline-deterministic'
      ? 'offline guide ready'
      : setupHelper?.assistant?.mode ?? 'offline guide ready';
  const advisorSummary = assistantReply?.answer
    ?? setupHelper?.issues?.[0]?.title
    ?? setupHelper?.summary
    ?? missionControl?.summary
    ?? 'Ready to inspect installs, logs, and rootless repair options.';
  const suggestedSetupQuestions = [
    'Which mission should I pick?',
    'Where are my files saved?',
    'Will my data leave this VM?',
    'Can you fix this without sudo?',
    'What broke in the logs?',
    'Which models fit this GPU?',
  ];

  const runHelperAction = (actionId: string) => {
    if (actionId.startsWith('repair-receipt:')) {
      const receiptId = actionId.slice('repair-receipt:'.length);
      const receipt = [
        ...(setupReceipts?.receipts ?? []),
        ...(setupHelper?.receipts?.receipts ?? []),
      ].find(item => item.id === receiptId);
      if (receipt) handleRepairReceipt(receipt);
      else void refreshSetupInventory(false);
      return;
    }
    if (actionId === 'storage') {
      void handleUseRecommendedStorage();
      return;
    }
    if (actionId === 'starter-models') {
      const missing = recommendedMissingModelIds();
      if (missing.length > 0) handleInstallStudioModels(missing);
      else setStep('models');
      return;
    }
    if (actionId === 'rootless-ollama') {
      handleInstallStudioPacks(['rootless-ollama']);
      return;
    }
    if (actionId === 'runtime-fallback') {
      handleInstallStudioPacks(['python-runtime-fallback']);
      return;
    }
    if (actionId === 'comfyui' || actionId === 'comfyui-examples') {
      handleInstallComfyUI();
      return;
    }
    if (actionId === 'creative-tools') {
      handleInstallStudioPacks(['creative']);
      return;
    }
    if (actionId === 'music-tools') {
      handleInstallStudioPacks(['music']);
      return;
    }
    if (actionId === 'claw-agents') {
      const installableClawIds = selectableStudioPackIds(studioPacks, studioBundles.claw ?? ['openclaw-agent', 'nemoclaw-sandbox']);
      if (installableClawIds.length > 0) handleInstallStudioPacks(installableClawIds);
      else {
        setStudioError('No Claw agent option is installable on this host yet. Check Node.js and Docker/OpenShell readiness in Advanced Details.');
        setStep('studio');
      }
      return;
    }
    if (actionId === 'repair-workspace') {
      void handleRepairWorkspace();
      return;
    }
    if (actionId === 'smoke-tests') {
      setStep('test');
      void refreshSetupInventory(false);
      return;
    }
    if (actionId === 'repair-receipts') {
      void refreshSetupInventory(false);
      return;
    }
    if (studioPacks.some(pack => pack.id === actionId)) {
      handleInstallStudioPacks([actionId]);
      return;
    }
    setStep('studio');
  };

  const helperActionLabel = (actionId: string) => {
    if (apiDisconnected) return 'API Offline';
    if (actionId.startsWith('repair-receipt:')) return !storageReady ? 'Use Drive' : 'Repair';
    if (actionId === 'storage') return storageAutopilotBusy ? 'Finding' : 'Use Drive';
    if (!storageReady) return storageAutopilotBusy ? 'Finding' : 'Use Drive';
    if (actionId === 'starter-models') return modelsInstalling ? 'Downloading' : 'Download';
    if (actionId === 'rootless-ollama') return studioInstalling ? 'Installing' : 'Install Runtime';
    if (actionId === 'agent-lab') return studioInstalling ? 'Installing' : 'Install Agent Lab';
    if (studioPacks.some(pack => pack.id === actionId)) return studioInstalling ? 'Installing' : 'Install';
    if (actionId === 'comfyui' || actionId === 'comfyui-examples') {
      return comfyInstalling ? 'Installing' : 'Install';
    }
    if (actionId === 'repair-workspace') return workspaceRepairing ? 'Repairing' : 'Repair';
    if (actionId === 'smoke-tests') return 'Open';
    if (actionId === 'repair-receipts') return 'Review';
    return studioInstalling ? 'Installing' : 'Run';
  };

  const helperActionDisabled = (actionId: string) => {
    if (apiDisconnected) return true;
    if (actionId.startsWith('repair-receipt:')) return !storageReady || studioInstalling || modelsInstalling || comfyInstalling;
    if (actionId === 'storage') return storageAutopilotBusy;
    if (actionId === 'starter-models') return modelsInstalling || !storageReady;
    if (actionId === 'comfyui' || actionId === 'comfyui-examples') {
      return comfyInstalling || !storageReady;
    }
    if (actionId === 'repair-workspace') return workspaceRepairing;
    if (actionId === 'smoke-tests' || actionId === 'repair-receipts') return false;
    return studioInstalling || !storageReady;
  };

  const handleSystemCheckClick = (label: string) => {
    if (apiDisconnected || apiStatus === 'checking') return;
    if (label === 'Storage') {
      if (!storageReady) void handleUseRecommendedStorage();
      else setWizardBuildMessage(`Storage looks ready at ${workspaceHome}. Large apps, models, jobs, logs, and outputs should stay on this persistent volume.`);
      return;
    }
    if (label === 'GPU') {
      const gpu = gpuInfo?.gpus?.[0];
      setWizardBuildMessage(
        gpu
          ? `GPU detected: ${gpu.name} with ${gpu.vram_gb} GB VRAM. nvWizard will use that to recommend model sizes and GPU-fit installs.`
          : 'No NVIDIA GPU was detected from the WebUI yet. nvWizard can still explain the host state, but the VM image may need provider/admin attention if nvidia-smi is unavailable.'
      );
      return;
    }
    if (label === 'Models') {
      const missing = recommendedMissingModelIds();
      if (missing.length > 0 && storageReady) handleInstallStudioModels(missing);
      else setWizardBuildMessage(`Model fit looks aligned with this GPU profile: ${recommendedModelLabel}.`);
      return;
    }
    if (label === 'Local AI') {
      if (!localAiReady && storageReady) handleInstallStudioPacks(['rootless-ollama']);
      else setWizardBuildMessage(localAiReady ? 'Local AI runtime is online. Chat can use local models when they are installed.' : 'Local AI can be installed rootlessly under the persistent workspace.');
      return;
    }
    if (label === 'Runtime') {
      if ((rootlessStatus === 'blocked' || rootlessStatus === 'warn') && storageReady) {
        handleInstallStudioPacks(['python-runtime-fallback']);
      } else {
        setWizardBuildMessage(`Runtime strategy: ${rootlessRuntimeStrategy}. nvHive will stay rootless and avoid OS-level package changes.`);
      }
      return;
    }
    if (label === 'Boot') {
      setWizardBuildMessage('nvWizard is rechecking the VM image, runtime, storage, and app drift without opening the advanced panel.');
      void handleBootRecheck();
    }
  };

  const handleRepairReceipt = (receipt: InstallReceipt) => {
    if (receipt.kind === 'comfyui') {
      setStep('comfyui');
      handleInstallComfyUI();
      return;
    }
    if (receipt.kind === 'studio-model') {
      setStep('models');
      handleInstallStudioModels([receipt.item_id]);
      return;
    }
    if (receipt.kind === 'studio-pack') {
      setStep('studio');
      handleInstallStudioPacks([receipt.item_id]);
      return;
    }
    setStep('studio');
  };

  return (
    <div className="p-4 sm:p-6 max-w-6xl mx-auto space-y-4">
      {/* Header */}
      <div className="border-b border-[#e5e5e5] pb-2">
        <div className="flex items-center justify-between gap-3">
          <div className="text-[10px] font-mono text-[#76B900] tracking-[0.2em] uppercase">nvWizard Setup</div>
          <div className="flex items-center gap-2">
            <Link href="/" className="btn-ghost px-3 py-2 text-[10px] font-mono uppercase tracking-wider">
              Chat
            </Link>
            <Link href="/query" className="btn-ghost px-3 py-2 text-[10px] font-mono uppercase tracking-wider">
              Ask AI
            </Link>
            <button
              type="button"
              onClick={() => setAdvancedSetupOpen(prev => !prev)}
              className="btn-ghost px-3 py-2 text-[10px] font-mono uppercase tracking-wider sm:flex-shrink-0"
            >
              {advancedSetupOpen ? 'Hide Advanced Details' : 'Show Advanced Details'}
            </button>
          </div>
        </div>
        {advancedSetupOpen && (
          <div className="mt-3 space-y-2">
            <div className="flex items-center gap-2 overflow-x-auto pt-1">
              {ADVANCED_GROUPS.map(group => {
                const firstStepIdx = STEPS.findIndex(s => s.id === group.steps[0]);
                const isActive = group.id === currentAdvancedGroup.id;
                const isComplete = firstStepIdx > -1 && firstStepIdx < currentStepIdx;
                return (
                  <button
                    key={group.id}
                    type="button"
                    onClick={() => setStep(group.steps[0])}
                    className={`flex items-center gap-1.5 border px-2.5 py-1.5 text-[10px] font-mono uppercase tracking-wider transition-all flex-shrink-0 ${
                      isActive
                        ? 'border-[#76B900] bg-[#76B900] text-black'
                        : isComplete
                          ? 'border-[#76B900]/40 text-[#76B900] bg-[#f7fdf0]'
                          : 'border-[#d4d4d4] text-[#525252] bg-white hover:border-[#76B900]/50 hover:text-[#0a0a0a]'
                    }`}
                  >
                    <span>{isComplete ? 'OK' : group.label}</span>
                  </button>
                );
              })}
            </div>
            {currentAdvancedGroup.steps.length > 1 && (
              <div className="flex flex-wrap gap-2">
                {currentAdvancedGroup.steps.map(groupStep => {
                  const detail = STEPS.find(s => s.id === groupStep);
                  return (
                    <button
                      key={groupStep}
                      type="button"
                      onClick={() => setStep(groupStep)}
                      className={`border px-2 py-1 text-[9px] font-mono uppercase tracking-wider ${
                        groupStep === step
                          ? 'border-[#76B900] text-[#0a0a0a] bg-[#f7fdf0]'
                          : 'border-[#e5e5e5] text-[#737373] bg-white hover:text-[#0a0a0a]'
                      }`}
                    >
                      {detail?.label ?? groupStep}
                    </button>
                  );
                })}
              </div>
            )}
          </div>
        )}
      </div>

      {step !== 'welcome' && (
        <div className="border border-[#76B900]/30 bg-[#f7fdf0] p-3 flex flex-col xl:flex-row xl:items-center xl:justify-between gap-3">
          <div className="min-w-0">
            <div className="section-label">Workspace</div>
            <div className="text-[10px] text-[#525252] mt-1 break-all">
              {workspaceHome} - {workspaceFreeText} - {setupConcernCount ? `${setupConcernCount} item${setupConcernCount === 1 ? '' : 's'} to review` : 'checks clear'}
            </div>
          </div>
          <div className="flex flex-wrap gap-1.5">
            {systemCheckItems.slice(0, 4).map(item => {
              const tone = CHECK_TONES[item.state];
              return (
                <button
                  key={item.label}
                  type="button"
                  onClick={() => handleSystemCheckClick(item.label)}
                  className={`inline-flex items-center gap-1.5 border ${tone.border} ${tone.bg} px-2 py-1 transition-colors hover:border-[#76B900]`}
                  title={`${item.label}: ${item.value}`}
                >
                  <span className={`w-1.5 h-1.5 flex-shrink-0 ${tone.dot}`} style={{ clipPath: 'polygon(50% 0%, 100% 50%, 50% 100%, 0% 50%)' }} />
                  <span className="text-[9px] font-mono text-[#737373] uppercase">{item.label}</span>
                </button>
              );
            })}
          </div>
          <div className="flex flex-wrap gap-2">
            <button
              type="button"
              onClick={() => setStep('welcome')}
              className="btn-primary px-3 py-2 text-[10px] font-mono uppercase tracking-wider"
            >
              Change Mission
            </button>
            <button
              type="button"
              onClick={() => void handleRepairWorkspace()}
              disabled={workspaceRepairing || apiDisconnected || anyInstallRunning}
              className="btn-ghost px-3 py-2 text-[10px] font-mono uppercase tracking-wider disabled:opacity-40"
            >
              {workspaceRepairing ? 'Repairing' : 'Fix My Setup'}
            </button>
          </div>
        </div>
      )}

      {showInstallJobs && (
        <div className="border border-[#d4d4d4] bg-[#ffffff] p-4 space-y-3">
          <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-2">
            <div>
              <div className="section-label">Install Jobs</div>
              <div className="text-[10px] font-mono text-[#737373] mt-1">
                {activeInstallJobs.length > 0
                  ? `${activeInstallJobs.length} active job${activeInstallJobs.length === 1 ? '' : 's'} tracked under persistent NVH_HOME`
                  : 'Recent setup jobs are saved under NVH_HOME/jobs'}
              </div>
              <div className="text-[10px] text-[#737373] mt-1">
                Job history stays in the workspace, so refreshes and retries have a trail to follow.
              </div>
            </div>
            <button
              type="button"
              onClick={() => void refreshInstallJobs()}
              className="btn-ghost px-3 py-2 text-[10px] font-mono uppercase tracking-wider"
            >
              Refresh
            </button>
          </div>
          {jobsError && (
            <div className="bg-[#dc2626]/5 border border-[#dc2626]/20 p-2 text-[10px] font-mono text-[#dc2626]">
              {jobsError}
            </div>
          )}
          <div className="space-y-2">
            {visibleInstallJobs.map(job => {
              const active = isActiveInstallJob(job);
              const failed = job.status === 'failed' || job.status === 'interrupted';
              const complete = job.status === 'complete';
              const bar = Math.max(0, Math.min(100, job.progress || 0));
              return (
                <div key={job.id} className="border border-[#e5e5e5] bg-[#fafafa] p-3">
                  <div className="flex flex-col sm:flex-row sm:items-start gap-3 sm:justify-between">
                    <div className="min-w-0 flex-1">
                      <div className="flex items-center gap-2">
                        <span className={`w-1.5 h-1.5 flex-shrink-0 ${
                          complete ? 'bg-[#76B900]' : failed ? 'bg-[#dc2626]' : 'bg-[#d97706]'
                        }`} style={{ clipPath: 'polygon(50% 0%, 100% 50%, 50% 100%, 0% 50%)' }} />
                        <div className="text-xs font-mono font-bold text-[#0a0a0a] truncate">
                          {job.title}
                        </div>
                        <span className="text-[9px] font-mono text-[#737373] uppercase border border-[#d4d4d4] px-1.5 py-0.5">
                          {job.status}
                        </span>
                      </div>
                      <div className="text-[10px] font-mono text-[#525252] mt-1 break-words">
                        {job.message || job.kind}
                      </div>
                      <div className="text-[9px] font-mono text-[#a3a3a3] mt-1 break-all">
                        {job.id} / {job.storage_home}
                      </div>
                    </div>
                    {active && (
                      <button
                        type="button"
                        onClick={() => void handleCancelInstallJob(job.id)}
                        disabled={cancelingJobId === job.id}
                        className="btn-ghost px-3 py-2 text-[10px] font-mono uppercase tracking-wider disabled:opacity-40"
                      >
                        {cancelingJobId === job.id ? 'Canceling' : 'Cancel'}
                      </button>
                    )}
                  </div>
                  <div className="mt-3 h-1.5 bg-[#e5e5e5] overflow-hidden">
                    <div
                      className={`h-full transition-all ${
                        complete ? 'bg-[#76B900]' : failed ? 'bg-[#dc2626]' : 'bg-[#d97706]'
                      }`}
                      style={{ width: `${bar}%` }}
                    />
                  </div>
                </div>
              );
            })}
          </div>
        </div>
      )}

      {showAdvancedSetup && (setupReceipts || setupCatalog || setupInventoryError) && (
        <div className="border border-[#d4d4d4] bg-[#ffffff] p-4 space-y-3">
          <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-2">
            <div>
              <div className="section-label">Setup Inventory</div>
              <div className="text-[10px] font-mono text-[#737373] mt-1">
                {receiptCount} receipt{receiptCount === 1 ? '' : 's'} tracked / catalog source {catalogSource}
              </div>
            </div>
            <div className="flex gap-2">
              <button
                type="button"
                onClick={() => {
                  void refreshSetupInventory(false);
                  void handleBootRecheck();
                }}
                className="btn-ghost px-3 py-2 text-[10px] font-mono uppercase tracking-wider"
              >
                Recheck
              </button>
              <button
                type="button"
                onClick={() => void refreshSetupInventory(true)}
                className="btn-ghost px-3 py-2 text-[10px] font-mono uppercase tracking-wider"
              >
                Refresh Catalog
              </button>
              <button
                type="button"
                onClick={() => void handleDiagnosticsReport()}
                disabled={diagnosticsLoading}
                className="btn-ghost px-3 py-2 text-[10px] font-mono uppercase tracking-wider disabled:opacity-40"
              >
                {diagnosticsLoading ? 'Building Report' : 'Copy Error Report'}
              </button>
            </div>
          </div>
          {setupInventoryError && (
            <div className="bg-[#dc2626]/5 border border-[#dc2626]/20 p-2 text-[10px] font-mono text-[#dc2626]">
              {setupInventoryError}
            </div>
          )}
          {(diagnosticsMessage || diagnosticsError || diagnosticsReport) && (
            <div className="border border-[#e5e5e5] bg-[#fafafa] p-3 space-y-2">
              <div className="flex flex-col sm:flex-row sm:items-start sm:justify-between gap-2">
                <div>
                  <div className="text-xs font-mono font-bold text-[#0a0a0a]">Error Report</div>
                  <div className="text-[10px] font-mono text-[#737373] mt-0.5">
                    Redacted diagnostics for support; API keys and bearer tokens are masked.
                  </div>
                </div>
                {diagnosticsReport && (
                  <span className="text-[9px] font-mono uppercase px-1.5 py-0.5 border border-[#76B900]/40 text-[#76B900]">
                    {diagnosticsLogLineCount} log line{diagnosticsLogLineCount === 1 ? '' : 's'}
                  </span>
                )}
              </div>
              {diagnosticsMessage && (
                <div className="text-[10px] font-mono text-[#76B900]">{diagnosticsMessage}</div>
              )}
              {diagnosticsError && (
                <div className="text-[10px] font-mono text-[#dc2626]">{diagnosticsError}</div>
              )}
              {diagnosticsReport && (
                <details>
                  <summary className="cursor-pointer text-[9px] font-mono text-[#737373] uppercase">
                    Report summary
                  </summary>
                  <div className="mt-2 grid grid-cols-1 sm:grid-cols-3 gap-2">
                    <div className="border border-[#e5e5e5] bg-white p-2">
                      <div className="text-[9px] font-mono text-[#737373] uppercase">Report</div>
                      <div className="text-[10px] font-mono text-[#0a0a0a] break-all">{diagnosticsReport.report_id}</div>
                    </div>
                    <div className="border border-[#e5e5e5] bg-white p-2">
                      <div className="text-[9px] font-mono text-[#737373] uppercase">Logs</div>
                      <div className="text-[10px] font-mono text-[#0a0a0a]">{diagnosticsReport.logs.files.length} file(s)</div>
                    </div>
                    <div className="border border-[#e5e5e5] bg-white p-2">
                      <div className="text-[9px] font-mono text-[#737373] uppercase">Home</div>
                      <div className="text-[10px] font-mono text-[#0a0a0a] break-all">{diagnosticsReport.paths.home}</div>
                    </div>
                  </div>
                  {diagnosticsReport.logs.recent.length > 0 && (
                    <div className="mt-2 space-y-2">
                      {diagnosticsReport.logs.recent.slice(0, 2).map(item => (
                        <div key={item.path} className="border border-[#e5e5e5] bg-white p-2">
                          <div className="text-[9px] font-mono text-[#737373] break-all">{item.path}</div>
                          <div className="mt-1 space-y-1">
                            {item.lines.slice(-4).map((line, index) => (
                              <div key={`${item.path}-${index}`} className="text-[9px] font-mono text-[#525252] break-words">
                                {line}
                              </div>
                            ))}
                          </div>
                        </div>
                      ))}
                    </div>
                  )}
                </details>
              )}
            </div>
          )}
          <div className="grid grid-cols-2 sm:grid-cols-4 gap-2">
            <div className="border border-[#e5e5e5] bg-[#fafafa] p-3">
              <div className="text-[9px] font-mono text-[#737373] uppercase">Receipts</div>
              <div className="text-lg font-mono font-bold text-[#0a0a0a]">{receiptCount}</div>
            </div>
            <div className="border border-[#e5e5e5] bg-[#fafafa] p-3">
              <div className="text-[9px] font-mono text-[#737373] uppercase">Needs Repair</div>
              <div className={`text-lg font-mono font-bold ${unhealthyReceiptCount ? 'text-[#d97706]' : 'text-[#76B900]'}`}>{unhealthyReceiptCount}</div>
            </div>
            <div className="border border-[#e5e5e5] bg-[#fafafa] p-3">
              <div className="text-[9px] font-mono text-[#737373] uppercase">Profiles</div>
              <div className="text-lg font-mono font-bold text-[#0a0a0a]">
                {setupCatalog?.catalog.profiles.length ?? setupHelper?.catalog?.profile_count ?? 0}
              </div>
            </div>
            <div className="border border-[#e5e5e5] bg-[#fafafa] p-3">
              <div className="text-[9px] font-mono text-[#737373] uppercase">Compat</div>
              <div className="text-lg font-mono font-bold text-[#0a0a0a]">
                {compatibilityIssueCount}
              </div>
            </div>
          </div>
          {(bootPreflight || setupHelper?.boot_preflight) && (
            <div className="border border-[#0a0a0a] bg-[#0a0a0a] text-[#f5f5f5] p-3 space-y-3">
              <div className="flex flex-col sm:flex-row sm:items-start sm:justify-between gap-2">
                <div>
                  <div className="text-xs font-mono font-bold text-[#76B900]">nvWizard Boot Watch</div>
                  <div className="text-[10px] font-mono text-[#d4d4d4] mt-0.5">
                    {bootPreflight?.summary ?? setupHelper?.boot_preflight?.summary ?? 'Boot preflight runs when nvHive launches.'}
                  </div>
                </div>
                <div className="flex gap-1 flex-wrap">
                  <span className={`text-[9px] font-mono uppercase px-1.5 py-0.5 border ${
                    bootChangeCount ? 'border-[#d97706]/60 text-[#fbbf24]' : 'border-[#76B900]/60 text-[#76B900]'
                  }`}>
                    {bootChangeCount ? `${bootChangeCount} shift${bootChangeCount === 1 ? '' : 's'}` : 'image steady'}
                  </span>
                  <span className={`text-[9px] font-mono uppercase px-1.5 py-0.5 border ${
                    bootAgentHelper?.local_agent_ready ? 'border-[#76B900]/60 text-[#76B900]' : 'border-[#d97706]/60 text-[#fbbf24]'
                  }`}>
                    {bootAgentHelper?.local_agent_ready ? 'agent awake' : 'offline guide'}
                  </span>
                </div>
              </div>
              <div className="text-[10px] font-mono text-[#e5e5e5] leading-relaxed">
                {bootAgentHelper?.summary ?? 'Offline setup helper is available before any cloud or local model is installed.'}
              </div>
              {bootAgentHelper?.recommended_action_id && (
                <button
                  type="button"
                  onClick={() => runHelperAction(bootAgentHelper.recommended_action_id as string)}
                  disabled={helperActionDisabled(bootAgentHelper.recommended_action_id)}
                  className="bg-[#76B900] text-[#0a0a0a] px-3 py-2 text-[10px] font-mono uppercase tracking-wider disabled:opacity-40"
                >
                  {helperActionLabel(bootAgentHelper.recommended_action_id)}
                </button>
              )}
              {bootPreflight?.changes && bootPreflight.changes.length > 0 && (
                <div className="space-y-1">
                  {bootPreflight.changes.slice(0, 5).map(change => (
                    <div key={change.id} className="flex items-start gap-2 text-[9px] font-mono text-[#d4d4d4]">
                      <span className="mt-1 w-1.5 h-1.5 flex-shrink-0 bg-[#fbbf24]" />
                      <span>{change.label}: {change.before} {'->'} {change.after}</span>
                    </div>
                  ))}
                </div>
              )}
            </div>
          )}
          {missionControl && (
            <div className="border border-[#e5e5e5] bg-[#ffffff] p-3 space-y-3">
              <div className="flex flex-col sm:flex-row sm:items-start sm:justify-between gap-2">
                <div>
                  <div className="text-xs font-mono font-bold text-[#0a0a0a]">Mission Timeline</div>
                  <div className="text-[10px] font-mono text-[#737373] mt-0.5">{missionControl.summary}</div>
                </div>
                <button
                  type="button"
                  onClick={() => void handleRepairWorkspace()}
                  disabled={workspaceRepairing}
                  className="btn-primary px-3 py-2 text-[10px] font-mono uppercase tracking-wider disabled:opacity-40"
                >
                  {workspaceRepairing ? 'Repairing' : 'Fix My Setup'}
                </button>
              </div>
              <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
                {missionStages.slice(0, 6).map(stage => (
                  <button
                    key={stage.id}
                    type="button"
                    onClick={() => stage.action_id && runHelperAction(stage.action_id)}
                    disabled={!stage.action_id || helperActionDisabled(stage.action_id)}
                    className="text-left border border-[#e5e5e5] bg-[#fafafa] p-3 hover:border-[#76B900]/50 transition-colors disabled:opacity-70"
                  >
                    <div className="flex items-center justify-between gap-2">
                      <div className="text-xs font-mono font-bold text-[#0a0a0a]">{stage.title}</div>
                      <span className={`text-[9px] font-mono uppercase px-1.5 py-0.5 border ${
                        stage.status === 'pass'
                          ? 'border-[#76B900]/40 text-[#76B900]'
                          : 'border-[#d97706]/40 text-[#d97706]'
                      }`}>
                        {stage.status}
                      </span>
                    </div>
                    <div className="text-[10px] font-mono text-[#525252] mt-1 leading-relaxed">{stage.summary}</div>
                  </button>
                ))}
              </div>
              <div className="grid grid-cols-1 sm:grid-cols-3 gap-2">
                <div className="border border-[#e5e5e5] bg-[#fafafa] p-3">
                  <div className="text-[9px] font-mono text-[#737373] uppercase">Mount Autopilot</div>
                  <div className="text-[10px] font-mono text-[#0a0a0a] mt-1 break-all">
                    {mountRecommendation?.recommended_home ?? 'No mount picked yet'}
                  </div>
                  <div className="text-[9px] font-mono text-[#a3a3a3] mt-1">
                    score {mountRecommendation?.score ?? 0}
                    {mountRecommendation?.fs_type ? ` / ${mountRecommendation.fs_type}` : ''}
                    {mountRecommendation?.large_block_mount ? ' / block' : ''}
                    {mountRecommendation?.read_only ? ' / read-only' : ''}
                  </div>
                </div>
                <div className="border border-[#e5e5e5] bg-[#fafafa] p-3">
                  <div className="text-[9px] font-mono text-[#737373] uppercase">Auto Repair</div>
                  <div className="text-[10px] font-mono text-[#0a0a0a] mt-1">
                    {autoRepairActions.filter(action => action.safe_to_auto_run).length} safe / {autoRepairActions.filter(action => !action.safe_to_auto_run).length} confirm
                  </div>
                  <div className="text-[9px] font-mono text-[#a3a3a3] mt-1">
                    env, catalog, examples only
                  </div>
                </div>
                <div className="border border-[#e5e5e5] bg-[#fafafa] p-3">
                  <div className="text-[9px] font-mono text-[#737373] uppercase">Smoke Tests</div>
                  <div className="text-[10px] font-mono text-[#0a0a0a] mt-1">
                    {smokeTests?.summary ?? 'Waiting for checks'}
                  </div>
                  <div className="text-[9px] font-mono text-[#a3a3a3] mt-1">
                    models: {modelFit?.recommended_ids?.slice(0, 3).join(', ') || 'no queue'}
                  </div>
                </div>
              </div>
            </div>
          )}
          {productionReadiness && (
            <div className="border border-[#e5e5e5] bg-[#fafafa] p-3 space-y-2">
              <div className="flex flex-col sm:flex-row sm:items-start sm:justify-between gap-2">
                <div>
                  <div className="text-xs font-mono font-bold text-[#0a0a0a]">Release Readiness</div>
                  <div className="text-[10px] font-mono text-[#737373] mt-0.5">
                    {productionReadiness.summary}
                  </div>
                </div>
                <div className="flex gap-1 flex-wrap">
                  <span className={`text-[9px] font-mono uppercase px-1.5 py-0.5 border border-current ${readinessTone}`}>
                    {productionReadiness.status}
                  </span>
                  <span className="text-[9px] font-mono uppercase px-1.5 py-0.5 border border-[#d4d4d4] text-[#737373]">
                    {productionReadiness.counts.blocked} blocked
                  </span>
                  <span className="text-[9px] font-mono uppercase px-1.5 py-0.5 border border-[#d97706]/40 text-[#d97706]">
                    {productionReadiness.counts.warnings} review
                  </span>
                </div>
              </div>
              {visibleReadinessGates.length > 0 ? (
                <div className="space-y-2">
                  {visibleReadinessGates.map(gate => (
                    <div key={gate.id} className="border border-[#e5e5e5] bg-white p-2">
                      <div className="flex items-center gap-2">
                        <span className={`w-1.5 h-1.5 flex-shrink-0 ${gate.status === 'blocked' ? 'bg-[#dc2626]' : 'bg-[#d97706]'}`} />
                        <div className="text-[10px] font-mono font-bold text-[#0a0a0a]">{gate.title}</div>
                        <span className="text-[9px] font-mono uppercase text-[#737373]">{gate.status}</span>
                      </div>
                      <div className="text-[10px] font-mono text-[#525252] mt-1 leading-relaxed">
                        {gate.summary}
                      </div>
                      {gate.recommendation && (
                        <div className="text-[9px] font-mono text-[#737373] mt-1 leading-relaxed">
                          {gate.recommendation}
                        </div>
                      )}
                    </div>
                  ))}
                </div>
              ) : (
                <div className="text-[10px] font-mono text-[#76B900] border border-[#76B900]/20 bg-white p-2">
                  All release gates are passing.
                </div>
              )}
              <details>
                <summary className="cursor-pointer text-[9px] font-mono text-[#737373] uppercase">
                  Target VM checklist
                </summary>
                <div className="mt-2 space-y-1">
                  {productionReadiness.target_vm_checklist.map(item => (
                    <div key={item} className="text-[9px] font-mono text-[#525252] flex items-start gap-2">
                      <span className="mt-1 w-1.5 h-1.5 bg-[#76B900] flex-shrink-0" />
                      <span>{item}</span>
                    </div>
                  ))}
                </div>
              </details>
            </div>
          )}
          {(setupCompatibility || setupHelper?.compatibility) && (
            <div className="border border-[#e5e5e5] bg-[#fafafa] p-3 space-y-2">
              <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-2">
                <div>
                  <div className="text-xs font-mono font-bold text-[#0a0a0a]">Compatibility Preflight</div>
                  <div className="text-[10px] font-mono text-[#737373] mt-0.5">
                    {setupCompatibility?.summary ?? setupHelper?.compatibility?.summary ?? 'Host/app compatibility checks'}
                  </div>
                </div>
                <div className="flex gap-1 flex-wrap">
                  <span className={`text-[9px] font-mono uppercase px-1.5 py-0.5 border ${
                    compatibilityBlockedCount ? 'border-[#dc2626]/40 text-[#dc2626]' : 'border-[#76B900]/40 text-[#76B900]'
                  }`}>
                    {compatibilityBlockedCount} blocked
                  </span>
                  <span className="text-[9px] font-mono uppercase px-1.5 py-0.5 border border-[#d97706]/40 text-[#d97706]">
                    {compatibilityFixableCount} fixable
                  </span>
                  <span className="text-[9px] font-mono uppercase px-1.5 py-0.5 border border-[#d4d4d4] text-[#737373]">
                    {setupCompatibility?.recommended_torch_profile ?? setupHelper?.compatibility?.recommended_torch_profile ?? 'torch auto'}
                  </span>
                </div>
              </div>
              {visibleCompatibilityApps.length > 0 && (
                <div className="space-y-2">
                  {visibleCompatibilityApps.map(app => (
                    <div key={app.id} className="border border-[#e5e5e5] bg-[#ffffff] p-3">
                      <div className="flex flex-col sm:flex-row sm:items-start sm:justify-between gap-2">
                        <div className="min-w-0">
                          <div className="flex items-center gap-2">
                            <span className={`w-1.5 h-1.5 flex-shrink-0 ${
                              app.status === 'blocked' ? 'bg-[#dc2626]' : app.status === 'fixable' ? 'bg-[#d97706]' : 'bg-[#76B900]'
                            }`} style={{ clipPath: 'polygon(50% 0%, 100% 50%, 50% 100%, 0% 50%)' }} />
                            <div className="text-xs font-mono font-bold text-[#0a0a0a] truncate">{app.title}</div>
                            <span className="text-[9px] font-mono text-[#737373] uppercase border border-[#d4d4d4] px-1.5 py-0.5">
                              {app.status}
                            </span>
                          </div>
                          <div className="text-[10px] font-mono text-[#525252] mt-1 leading-relaxed">
                            {app.summary}
                          </div>
                          <details className="mt-2">
                            <summary className="cursor-pointer text-[9px] font-mono text-[#737373] uppercase">
                              Requirements
                            </summary>
                            <div className="mt-2 space-y-1">
                              {app.requirements.map(req => (
                                <div key={req.id} className="text-[9px] font-mono text-[#525252] flex items-start gap-2">
                                  <span className={`mt-1 w-1.5 h-1.5 flex-shrink-0 ${
                                    req.status === 'ok' ? 'bg-[#76B900]' : req.status === 'blocked' ? 'bg-[#dc2626]' : 'bg-[#d97706]'
                                  }`} />
                                  <span>{req.label}: {req.detail}</span>
                                </div>
                              ))}
                            </div>
                          </details>
                        </div>
                        {app.recommended_action_id && (
                          <button
                            type="button"
                            onClick={() => runHelperAction(app.recommended_action_id as string)}
                            disabled={helperActionDisabled(app.recommended_action_id)}
                            className="btn-primary px-3 py-2 text-[10px] font-mono uppercase tracking-wider disabled:opacity-40"
                          >
                            {helperActionLabel(app.recommended_action_id)}
                          </button>
                        )}
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </div>
          )}
          {visibleReceipts.length > 0 && (
            <div className="space-y-2">
              {visibleReceipts.map(receipt => (
                <div key={receipt.id} className="border border-[#e5e5e5] bg-[#fafafa] p-3 flex flex-col sm:flex-row sm:items-center sm:justify-between gap-2">
                  <div className="min-w-0">
                    <div className="flex items-center gap-2">
                      <span className={`w-1.5 h-1.5 flex-shrink-0 ${receipt.health.healthy ? 'bg-[#76B900]' : 'bg-[#d97706]'}`}
                        style={{ clipPath: 'polygon(50% 0%, 100% 50%, 50% 100%, 0% 50%)' }} />
                      <div className="text-xs font-mono font-bold text-[#0a0a0a] truncate">{receipt.title}</div>
                      <span className="text-[9px] font-mono text-[#737373] uppercase border border-[#d4d4d4] px-1.5 py-0.5">
                        {receipt.kind}
                      </span>
                    </div>
                    <div className="text-[9px] font-mono text-[#a3a3a3] mt-1 break-all">{receipt.install_path}</div>
                  </div>
                  <button
                    type="button"
                    onClick={() => handleRepairReceipt(receipt)}
                    disabled={
                      !storageReady ||
                      (receipt.kind === 'comfyui' && comfyInstalling) ||
                      (receipt.kind === 'studio-model' && modelsInstalling) ||
                      (receipt.kind === 'studio-pack' && studioInstalling)
                    }
                    className="btn-primary px-3 py-2 text-[10px] font-mono uppercase tracking-wider disabled:opacity-40"
                  >
                    {!storageReady ? storagePrimaryLabel : receipt.health.healthy ? 'Refresh' : 'Repair'}
                  </button>
                </div>
              ))}
            </div>
          )}
        </div>
      )}

      {showAdvancedSetup && (setupHelper || setupHelperError) && (
        <div className="border border-[#d4d4d4] bg-[#ffffff] p-4 space-y-3">
          <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-2">
            <div>
              <div className="section-label">nvWizard Advanced Details</div>
              <div className="text-[10px] font-mono text-[#737373] mt-1">
                {setupHelper?.summary ?? 'Offline setup recommendations'}
              </div>
            </div>
            <button
              type="button"
              onClick={() => void refreshSetupHelper(storageStatus?.layout.home)}
              className="btn-ghost px-3 py-2 text-[10px] font-mono uppercase tracking-wider"
            >
              Recheck
            </button>
          </div>
          {setupHelperError && (
            <div className="bg-[#dc2626]/5 border border-[#dc2626]/20 p-2 text-[10px] font-mono text-[#dc2626]">
              {setupHelperError}
            </div>
          )}
          {setupHelper && (
            <div className="space-y-3">
              {helperIssues.length > 0 && (
                <div className="space-y-2">
                  {helperIssues.map(issue => (
                    <div key={issue.id} className="border border-[#e5e5e5] bg-[#fafafa] p-3 flex flex-col sm:flex-row sm:items-center sm:justify-between gap-2">
                      <div className="min-w-0">
                        <div className="flex items-center gap-2">
                          <span className={`w-1.5 h-1.5 flex-shrink-0 ${
                            issue.severity === 'required' ? 'bg-[#dc2626]' : issue.severity === 'recommended' ? 'bg-[#d97706]' : 'bg-[#76B900]'
                          }`} style={{ clipPath: 'polygon(50% 0%, 100% 50%, 50% 100%, 0% 50%)' }} />
                          <div className="text-xs font-mono font-bold text-[#0a0a0a] truncate">{issue.title}</div>
                          <span className="text-[9px] font-mono text-[#737373] uppercase border border-[#d4d4d4] px-1.5 py-0.5">
                            {issue.severity}
                          </span>
                        </div>
                        <div className="text-[10px] font-mono text-[#525252] mt-1 leading-relaxed">
                          {issue.reason}
                        </div>
                        {(issue.current_version || issue.available_version) && (
                          <div className="text-[9px] font-mono text-[#a3a3a3] mt-1">
                            {issue.current_version ?? 'unknown'} {'>'} {issue.available_version ?? 'unknown'}
                          </div>
                        )}
                      </div>
                      {issue.fix_action_id && (
                        <button
                          type="button"
                          onClick={() => runHelperAction(issue.fix_action_id as string)}
                          disabled={helperActionDisabled(issue.fix_action_id)}
                          className="btn-primary px-3 py-2 text-[10px] font-mono uppercase tracking-wider disabled:opacity-40"
                        >
                          {helperActionLabel(issue.fix_action_id)}
                        </button>
                      )}
                    </div>
                  ))}
                </div>
              )}
              <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
                {helperActions.map(action => (
                  <div
                    key={action.id}
                    className="text-left border border-[#e5e5e5] bg-[#fafafa] p-3 hover:border-[#76B900]/50 transition-colors"
                  >
                    <div className="flex items-center justify-between gap-2">
                      <div className="text-xs font-mono font-bold text-[#0a0a0a]">{action.title}</div>
                      <span className={`text-[9px] font-mono uppercase px-1.5 py-0.5 border ${
                        action.status === 'required'
                          ? 'border-[#dc2626]/40 text-[#dc2626]'
                          : action.status === 'recommended'
                          ? 'border-[#76B900]/40 text-[#76B900]'
                          : 'border-[#d4d4d4] text-[#737373]'
                      }`}>
                        {action.status}
                      </span>
                    </div>
                    <div className="text-[10px] font-mono text-[#525252] mt-1 leading-relaxed">
                      {action.reason}
                    </div>
                    <div className="mt-3 flex flex-col sm:flex-row sm:items-center gap-2">
                      <button
                        type="button"
                        onClick={() => runHelperAction(action.id)}
                        disabled={helperActionDisabled(action.id)}
                        className="btn-primary px-3 py-2 text-[10px] font-mono uppercase tracking-wider disabled:opacity-40"
                      >
                        {helperActionLabel(action.id)}
                      </button>
                      <button
                        type="button"
                        onClick={() => {
                          if (action.id === 'starter-models') setStep('models');
                          else if (action.id === 'comfyui' || action.id === 'comfyui-examples') setStep('comfyui');
                          else if (action.id === 'storage') void handleUseRecommendedStorage();
                          else setStep('studio');
                        }}
                        className="btn-ghost px-3 py-2 text-[10px] font-mono uppercase tracking-wider"
                      >
                        Review
                      </button>
                    </div>
                    <details className="mt-2">
                      <summary className="cursor-pointer text-[9px] font-mono text-[#737373] uppercase">
                        Manual override
                      </summary>
                      <div className="text-[9px] font-mono text-[#a3a3a3] mt-1 break-all">
                        {action.command}
                      </div>
                    </details>
                  </div>
                ))}
              </div>

              <div className="border border-[#e5e5e5] bg-[#fafafa] p-3 space-y-2">
                <div className="flex items-center justify-between gap-2">
                  <div>
                    <div className="text-xs font-mono font-bold text-[#0a0a0a]">Ask nvWizard</div>
                    <div className="text-[10px] font-mono text-[#737373] mt-0.5">
                      Product-aware guidance from local state, jobs, receipts, and the nvHive brief
                    </div>
                  </div>
                  <span className="text-[9px] font-mono text-[#76B900] border border-[#76B900]/40 px-1.5 py-0.5 uppercase">
                    {setupHelper.assistant?.mode ?? 'offline'}
                  </span>
                </div>
                <div className="flex flex-col sm:flex-row gap-2">
                  <input
                    value={assistantQuestion}
                    onChange={event => setAssistantQuestion(event.target.value)}
                    onKeyDown={event => { if (event.key === 'Enter') void handleAskAssistant(); }}
                    placeholder="Which mission should I pick? Where are files? Why is GPU blocked?"
                    className="input-base flex-1 px-3 py-2 text-xs font-mono"
                  />
                  <button
                    type="button"
                    onClick={() => void handleAskAssistant()}
                    disabled={assistantLoading || !assistantQuestion.trim()}
                    className="btn-primary px-3 py-2 text-xs font-mono disabled:opacity-40"
                  >
                    {assistantLoading ? 'Thinking' : 'Ask'}
                  </button>
                </div>
                <div className="flex flex-wrap gap-1.5">
                  {suggestedSetupQuestions.map(question => (
                    <button
                      key={question}
                      type="button"
                      onClick={() => void askSetupQuestion(question, true)}
                      disabled={assistantLoading}
                      className="border border-[#e5e5e5] bg-white px-2 py-1 text-[9px] font-mono text-[#525252] hover:border-[#76B900]/50 disabled:opacity-40"
                    >
                      {question}
                    </button>
                  ))}
                </div>
                {assistantError && (
                  <div className="bg-[#dc2626]/5 border border-[#dc2626]/20 p-2 text-[10px] font-mono text-[#dc2626]">
                    {assistantError}
                  </div>
                )}
                {assistantReply && (
                  <div className="border border-[#d4d4d4] bg-[#ffffff] p-3 space-y-2">
                    <div className="text-xs font-mono text-[#0a0a0a] leading-relaxed">
                      {assistantReply.answer}
                    </div>
                    {((assistantReply.debug_findings?.length ?? 0) > 0 || (assistantReply.log_highlights?.length ?? 0) > 0) && (
                      <details className="border border-[#e5e5e5] bg-[#fafafa] p-2">
                        <summary className="cursor-pointer text-[9px] font-mono text-[#737373] uppercase">
                          Debug evidence{assistantReply.diagnostics_report_id ? ` / ${assistantReply.diagnostics_report_id}` : ''}
                        </summary>
                        <div className="mt-2 space-y-1">
                          {assistantReply.debug_findings?.slice(0, 3).map(finding => (
                            <div key={finding} className="text-[10px] font-mono text-[#525252] leading-relaxed">
                              {finding}
                            </div>
                          ))}
                          {assistantReply.log_highlights?.slice(0, 4).map(line => (
                            <div key={line} className="text-[10px] font-mono text-[#525252] leading-relaxed break-all">
                              {line}
                            </div>
                          ))}
                        </div>
                      </details>
                    )}
                    {(assistantReply.official_repo_url || assistantReply.readme_url || assistantReply.grounding_sources?.length) && (
                      <div className="flex flex-wrap gap-1.5">
                        {assistantReply.official_repo_url && (
                          <a
                            href={assistantReply.official_repo_url}
                            target="_blank"
                            rel="noreferrer"
                            className="text-[9px] font-mono text-[#76B900] border border-[#76B900]/30 px-1.5 py-0.5 uppercase"
                          >
                            Official Repo
                          </a>
                        )}
                        {assistantReply.readme_url && (
                          <a
                            href={assistantReply.readme_url}
                            target="_blank"
                            rel="noreferrer"
                            className="text-[9px] font-mono text-[#76B900] border border-[#76B900]/30 px-1.5 py-0.5 uppercase"
                          >
                            README
                          </a>
                        )}
                        {assistantReply.grounding_sources?.slice(0, 3).map(source => (
                          <span
                            key={source}
                            className="text-[9px] font-mono text-[#737373] border border-[#e5e5e5] bg-[#fafafa] px-1.5 py-0.5 uppercase"
                          >
                            {source}
                          </span>
                        ))}
                      </div>
                    )}
                    {assistantReply.actions.length > 0 && (
                      <div className="flex flex-wrap gap-2">
                        {assistantReply.actions.slice(0, 3).map(action => (
                          <button
                            key={action.id}
                            type="button"
                            onClick={() => runHelperAction(action.id)}
                            disabled={helperActionDisabled(action.id)}
                            title={action.title}
                            className="btn-primary px-3 py-2 text-[10px] font-mono uppercase tracking-wider disabled:opacity-40"
                          >
                            {helperActionLabel(action.id)}
                          </button>
                        ))}
                      </div>
                    )}
                    {assistantReply.commands.length > 0 && (
                      <details>
                        <summary className="cursor-pointer text-[9px] font-mono text-[#737373] uppercase">
                          Manual overrides
                        </summary>
                        <div className="space-y-1 mt-2">
                          {assistantReply.commands.map(command => (
                            <div key={command} className="text-[10px] font-mono text-[#525252] bg-[#f5f5f5] border border-[#e5e5e5] px-2 py-1 break-all">
                              {command}
                            </div>
                          ))}
                        </div>
                      </details>
                    )}
                  </div>
                )}
              </div>
            </div>
          )}
        </div>
      )}

      {/* Step content */}
      <div className="card p-4 sm:p-6 nvidia-corner relative">
        <div className="absolute top-0 left-0 right-0 h-px bg-[#76B900]/20" />

        {/* WELCOME */}
        {step === 'welcome' && (
          <div className="space-y-4">
            {wizardBuildMessage && (
              <div className="border border-[#76B900]/25 bg-[#f7fdf0] px-3 py-2 text-xs text-[#315f00]">
                {wizardBuildMessage}
              </div>
            )}

            <div className="border border-[#e5e5e5] bg-white p-3 space-y-3">
              <div className="flex flex-col xl:flex-row xl:items-center xl:justify-between gap-3">
                <div className="min-w-0">
                  <div className="flex flex-wrap items-center gap-2">
                    <div className="section-label">System Check</div>
                    <div className="text-xs font-bold text-[#0a0a0a]">
                      {setupConcernCount ? `${setupConcernCount} item${setupConcernCount === 1 ? '' : 's'} need attention` : 'Ready to install without sudo'}
                    </div>
                  </div>
                  <div className="text-[10px] text-[#525252] mt-1">
                    {workspaceFreeText} on persistent storage; {workspaceReceipts} receipt{workspaceReceipts === 1 ? '' : 's'}; {workspaceActiveJobs} active job{workspaceActiveJobs === 1 ? '' : 's'}.
                  </div>
                </div>
                <div className="flex flex-wrap gap-2">
                  {topHelperAction && (
                    <button
                      type="button"
                      onClick={() => runHelperAction(topHelperAction.id)}
                      disabled={apiDisconnected || anyInstallRunning || helperActionDisabled(topHelperAction.id)}
                      className="btn-primary px-3 py-2 text-[10px] font-mono uppercase tracking-wider disabled:opacity-40"
                    >
                      {helperActionLabel(topHelperAction.id)}
                    </button>
                  )}
                  <button
                    type="button"
                    onClick={() => void handleBootRecheck()}
                    disabled={apiDisconnected}
                    className="btn-ghost px-3 py-2 text-[10px] font-mono uppercase tracking-wider disabled:opacity-40"
                  >
                    Recheck
                  </button>
                  <button
                    type="button"
                    onClick={handleQuickDiagnosis}
                    disabled={assistantLoading || apiDisconnected}
                    className="btn-ghost px-3 py-2 text-[10px] font-mono uppercase tracking-wider disabled:opacity-40"
                  >
                    {assistantLoading ? 'Thinking' : 'Ask nvWizard'}
                  </button>
                </div>
              </div>
              <div className="flex flex-wrap gap-1.5">
                {systemCheckItems.map(item => {
                  const tone = CHECK_TONES[item.state];
                  return (
                    <button
                      key={item.label}
                      type="button"
                      onClick={() => handleSystemCheckClick(item.label)}
                      className={`inline-flex items-center gap-1.5 border ${tone.border} ${tone.bg} px-2 py-1 min-w-0 transition-colors hover:border-[#76B900]`}
                      title={`${item.label}: ${item.value}`}
                    >
                      <span className={`w-1.5 h-1.5 flex-shrink-0 ${tone.dot}`} style={{ clipPath: 'polygon(50% 0%, 100% 50%, 50% 100%, 0% 50%)' }} />
                      <span className="text-[9px] font-mono text-[#737373] uppercase">{item.label}</span>
                      <span className={`text-[10px] font-mono truncate max-w-[11rem] ${tone.text}`}>{item.value}</span>
                    </button>
                  );
                })}
              </div>
              <div className="border border-[#e5e5e5] bg-[#fafafa] px-3 py-2 flex flex-col lg:flex-row lg:items-center lg:justify-between gap-2">
                <div className="min-w-0">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="text-[10px] font-mono font-bold text-[#0a0a0a]">nvWizard Advisor</span>
                    <span className="text-[9px] font-mono text-[#76B900] uppercase border border-[#76B900]/40 px-1.5 py-0.5">
                      {advisorModeLabel}
                    </span>
                  </div>
                  <div className="text-[10px] text-[#525252] mt-1 leading-relaxed">
                    {advisorSummary}
                  </div>
                  <details className="mt-1">
                    <summary className="cursor-pointer text-[9px] font-mono text-[#737373] uppercase">
                      Storage path
                    </summary>
                    <div className="text-[9px] font-mono text-[#737373] mt-1 break-all">{workspaceHome}</div>
                  </details>
                </div>
                {setupConcernCount > 0 && (
                  <button
                    type="button"
                    onClick={() => void handleRepairWorkspace()}
                    disabled={workspaceRepairing || apiDisconnected || anyInstallRunning}
                    className="btn-primary px-3 py-2 text-[10px] font-mono uppercase tracking-wider disabled:opacity-40 flex-shrink-0"
                  >
                    {workspaceRepairing ? 'Repairing' : 'Fix Issues'}
                  </button>
                )}
              </div>
              {(assistantError || assistantReply) && (
                <div className="border border-[#d4d4d4] bg-white p-3 space-y-2">
                  <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-2">
                    <div>
                      <div className="text-xs font-mono font-bold text-[#0a0a0a]">nvWizard Debugger</div>
                      <div className="text-[10px] text-[#737373] mt-0.5">
                        Reads jobs, receipts, boot checks, and redacted logs without opening Advanced Details.
                      </div>
                    </div>
                    {assistantReply?.diagnostics_report_id && (
                      <span className="text-[9px] font-mono text-[#76B900] border border-[#76B900]/40 px-1.5 py-0.5 uppercase">
                        {assistantReply.diagnostics_report_id}
                      </span>
                    )}
                  </div>
                  {assistantError && (
                    <div className="bg-[#dc2626]/5 border border-[#dc2626]/20 p-2 text-[10px] font-mono text-[#dc2626]">
                      {assistantError}
                    </div>
                  )}
                  {assistantReply && (
                    <>
                      <div className="text-xs text-[#0a0a0a] leading-relaxed">
                        {assistantReply.answer}
                      </div>
                      {((assistantReply.debug_findings?.length ?? 0) > 0 || (assistantReply.log_highlights?.length ?? 0) > 0) && (
                        <details className="border border-[#e5e5e5] bg-[#fafafa] p-2">
                          <summary className="cursor-pointer text-[9px] font-mono text-[#737373] uppercase">
                            Debug evidence
                          </summary>
                          <div className="mt-2 space-y-1">
                            {assistantReply.debug_findings?.slice(0, 3).map(finding => (
                              <div key={finding} className="text-[10px] font-mono text-[#525252] leading-relaxed">
                                {finding}
                              </div>
                            ))}
                            {assistantReply.log_highlights?.slice(0, 4).map(line => (
                              <div key={line} className="text-[10px] font-mono text-[#525252] leading-relaxed break-all">
                                {line}
                              </div>
                            ))}
                          </div>
                        </details>
                      )}
                      {assistantReply.actions.length > 0 && (
                        <div className="flex flex-wrap gap-2">
                          {assistantReply.actions.slice(0, 3).map(action => (
                            <button
                              key={action.id}
                              type="button"
                              onClick={() => runHelperAction(action.id)}
                              disabled={helperActionDisabled(action.id)}
                              title={action.title}
                              className="btn-primary px-3 py-2 text-[10px] font-mono uppercase tracking-wider disabled:opacity-40"
                            >
                              {helperActionLabel(action.id)}
                            </button>
                          ))}
                        </div>
                      )}
                      {assistantReply.commands.length > 0 && (
                        <details>
                          <summary className="cursor-pointer text-[9px] font-mono text-[#737373] uppercase">
                            Manual overrides
                          </summary>
                          <div className="space-y-1 mt-2">
                            {assistantReply.commands.map(command => (
                              <div key={command} className="text-[10px] font-mono text-[#525252] bg-[#f5f5f5] border border-[#e5e5e5] px-2 py-1 break-all">
                                {command}
                              </div>
                            ))}
                          </div>
                        </details>
                      )}
                    </>
                  )}
                </div>
              )}
            </div>

            {advancedSetupOpen && (
              <div className="border border-[#e5e5e5] bg-[#fafafa] p-3">
                <div className="flex flex-col lg:flex-row lg:items-center lg:justify-between gap-3">
                  <div className="min-w-0">
                    <div className="section-label">Advanced Details</div>
                    <div className="text-xs text-[#525252] mt-1">
                      Use the tabs above for storage, hardware, apps, accounts, and tests. The main install choices stay below.
                    </div>
                  </div>
                  <div className="flex flex-wrap gap-2">
                    <button
                      type="button"
                      onClick={() => void handleRepairWorkspace()}
                      disabled={workspaceRepairing || apiDisconnected || anyInstallRunning}
                      className="btn-ghost px-3 py-2 text-[10px] font-mono uppercase tracking-wider disabled:opacity-40"
                    >
                      {workspaceRepairing ? 'Repairing' : 'Fix My Setup'}
                    </button>
                    <button
                      type="button"
                      onClick={() => void handleSupportSnapshot()}
                      disabled={supportSnapshotLoading || apiDisconnected}
                      className="btn-ghost px-3 py-2 text-[10px] font-mono uppercase tracking-wider disabled:opacity-40"
                    >
                      {supportSnapshotLoading ? 'Saving' : 'Support Snapshot'}
                    </button>
                  </div>
                </div>
                {supportSnapshotMessage && (
                  <div className="text-[10px] font-mono text-[#76B900] mt-2 break-all">{supportSnapshotMessage}</div>
                )}
                <div className="mt-3 grid grid-cols-2 md:grid-cols-4 gap-2">
                  {[
                    ['Storage', storageReady ? 'Ready' : storageBeginnerLabel],
                    ['Compatibility', compatibilityIssueCount ? `${compatibilityIssueCount} issues` : 'Clear'],
                    ['Boot Changes', bootChangeCount ? `${bootChangeCount} found` : 'None'],
                    ['Logs', diagnosticsLogLineCount ? `${diagnosticsLogLineCount} lines` : 'Quiet'],
                  ].map(([label, value]) => (
                    <div key={label} className="border border-[#e5e5e5] bg-white p-2 min-w-0">
                      <div className="text-[9px] font-mono text-[#737373] uppercase">{label}</div>
                      <div className="text-[10px] font-mono text-[#0a0a0a] mt-1 truncate">{value}</div>
                    </div>
                  ))}
                </div>
              </div>
            )}

            <div className="space-y-3">
              <div className="flex items-center justify-between gap-2">
                <div>
                  <div className="section-label">Install Options</div>
                  <div className="text-sm font-bold text-[#0a0a0a] mt-1">Choose a workload; nvWizard picks GPU-fit dependencies.</div>
                </div>
              </div>
              {apiDisconnected && (
                <div className="border border-[#d97706]/30 bg-[#fff8ed] p-3 flex flex-col sm:flex-row sm:items-center sm:justify-between gap-2">
                  <div className="min-w-0">
                    <div className="text-xs font-bold text-[#7c2d12]">nvHive API is not connected</div>
                    <div className="text-[10px] font-mono text-[#9a3412] mt-0.5">
                      Launch from nvHive AI Studio. Terminal fallback: nvh webui.
                    </div>
                  </div>
                  <span className="text-[9px] font-mono uppercase text-[#9a3412] border border-[#d97706]/30 px-2 py-1 flex-shrink-0">
                    Waiting for API
                  </span>
                </div>
              )}
              <div className={`grid grid-cols-1 gap-3 ${advancedSetupOpen ? 'lg:grid-cols-2' : 'lg:grid-cols-2 xl:grid-cols-4'}`}>
                {(advancedSetupOpen ? missionProfiles : beginnerProfiles)
                  .map(profile => {
                    const packIds = wizardProfilePackIds(profile.id);
                    const modelIds = wizardProfileModelIds(profile.id);
                    const estimatedGb = diskForPackIds(packIds) + diskForModelIds(modelIds);
                    const building = activeWizardBuild === profile.id;
                    const profilePackItems = studioPacks.filter(pack => packIds.includes(pack.id));
                    const profileModelItems = studioModels.filter(model => modelIds.includes(model.id));
                    const needsComfy = wizardProfileNeedsComfy(profile.id);
                    const requiredUnits = profilePackItems.length + profileModelItems.length + (needsComfy ? 1 : 0);
                    const installedUnits = profilePackItems.filter(pack => pack.status.installed).length
                      + profileModelItems.filter(model => model.installed).length
                      + (needsComfy && comfyStatus?.installed ? 1 : 0);
                    const profileInstalled = requiredUnits > 0 && installedUnits >= requiredUnits;
                    if (!advancedSetupOpen) {
                      return (
                        <div
                          key={profile.id}
                          className={`border p-4 transition-colors ${
                            profileInstalled
                              ? 'border-[#d4d4d4] bg-[#fafafa]'
                              : profile.primary
                              ? 'border-[#76B900]/60 bg-white'
                              : 'border-[#e5e5e5] bg-white'
                          }`}
                        >
                          <div className="flex items-start justify-between gap-3">
                            <span className="w-14 h-14 flex items-center justify-center border border-[#e5e5e5] bg-white flex-shrink-0">
                              <div className="grid grid-cols-2 gap-0.5">
                                {profile.logos.slice(0, 4).map(logo => (
                                  <BrandLogo key={logo} id={logo} className="w-5 h-5" />
                                ))}
                              </div>
                            </span>
                            <span className={`text-[9px] font-mono uppercase px-1.5 py-0.5 border flex-shrink-0 ${
                              profileInstalled
                                ? 'border-[#76B900]/40 text-[#76B900] bg-[#76B900]/10'
                                : profile.primary
                                  ? 'border-[#76B900]/40 text-[#76B900] bg-[#76B900]/10'
                                  : 'border-[#d4d4d4] text-[#737373]'
                            }`}>
                              {profileInstalled ? 'Installed' : profile.primary ? 'Recommended' : profile.label}
                            </span>
                          </div>
                          <div className="mt-3">
                            <h3 className="text-base font-bold text-[#0a0a0a] leading-tight">{profile.title}</h3>
                            <div className="text-xs text-[#525252] mt-2 leading-relaxed min-h-[3rem]">
                              {beginnerProfileCopy[profile.id] ?? profile.description}
                            </div>
                            <div className="mt-3 flex flex-wrap gap-1">
                              {profile.includes.slice(0, 3).map(item => (
                                <span key={item} className="text-[9px] font-mono text-[#737373] bg-[#fafafa] border border-[#e5e5e5] px-1.5 py-0.5">
                                  {item}
                                </span>
                              ))}
                            </div>
                          </div>
                          <button
                            type="button"
                            onClick={() => {
                              if (profileInstalled) {
                                applyWizardProfile(profile.id);
                                setWizardBuildMessage(`${profile.title} is installed. The relevant setup controls are open below; Advanced Details stays hidden unless you show it.`);
                                return;
                              }
                              void handleBuildWizardProfile(profile.id);
                            }}
                            disabled={!profileInstalled && (anyInstallRunning || apiDisconnected)}
                            className={`${profileInstalled ? 'btn-ghost' : 'btn-primary'} w-full mt-4 px-3 py-2 text-[10px] font-mono uppercase tracking-wider disabled:opacity-40`}
                          >
                            {profileInstalled ? 'Open' : building ? profileBusyLabels[profile.id] : profileActionLabels[profile.id]}
                          </button>
                        </div>
                      );
                    }
                    return (
                      <div
                        key={profile.id}
                        onMouseEnter={() => setSelectedWizardProfile(profile.id)}
                        onFocus={() => setSelectedWizardProfile(profile.id)}
                        onClick={() => setSelectedWizardProfile(profile.id)}
                        className={`border bg-white p-4 transition-colors ${
                          selectedWizardProfile === profile.id
                            ? 'border-[#76B900] shadow-[0_0_0_1px_rgba(118,185,0,0.16)]'
                            : profile.primary
                            ? 'border-[#76B900]/50 shadow-[0_0_0_1px_rgba(118,185,0,0.08)]'
                            : 'border-[#e5e5e5]'
                        }`}
                      >
                        <div className="flex items-start gap-3">
                          <span className="w-12 h-12 flex items-center justify-center border border-[#e5e5e5] bg-white flex-shrink-0">
                            <div className="grid grid-cols-2 gap-0.5">
                              {profile.logos.slice(0, 4).map(logo => (
                                <BrandLogo key={logo} id={logo} className="w-5 h-5" />
                              ))}
                            </div>
                          </span>
                          <div className="min-w-0 flex-1">
                            <div className="flex flex-wrap items-center justify-between gap-2">
                              <h3 className="text-base font-bold text-[#0a0a0a] leading-tight min-w-0">{profile.title}</h3>
                              <span className={`text-[9px] font-mono uppercase px-1.5 py-0.5 border flex-shrink-0 ${
                                profile.primary
                                  ? 'border-[#76B900]/40 text-[#76B900] bg-[#76B900]/10'
                                  : 'border-[#d4d4d4] text-[#737373]'
                              }`}>
                                {profile.label}
                              </span>
                            </div>
                            <p className="text-xs text-[#525252] leading-relaxed mt-1">{profile.description}</p>
                          </div>
                          <span className="text-[9px] font-mono text-[#737373] border border-[#e5e5e5] px-1.5 py-0.5 flex-shrink-0">
                            {hasCatalogSizing ? `~${Math.max(0, estimatedGb).toFixed(1)} GB` : 'after check'}
                          </span>
                        </div>
                        <div className="mt-3 flex flex-wrap gap-1">
                          {profile.includes.map(item => (
                            <span key={item} className="text-[9px] font-mono text-[#737373] bg-[#f5f5f5] border border-[#e5e5e5] px-1.5 py-0.5">
                              {item}
                            </span>
                          ))}
                        </div>
                        <div className="mt-4 flex flex-col sm:flex-row gap-2">
                          <button
                            type="button"
                            onClick={() => void handleBuildWizardProfile(profile.id)}
                            disabled={anyInstallRunning || apiDisconnected}
                            className="btn-primary px-3 py-2 text-[10px] font-mono uppercase tracking-wider disabled:opacity-40"
                          >
                            {building ? profileBusyLabels[profile.id] : profileActionLabels[profile.id]}
                          </button>
                          <button
                            type="button"
                            onClick={() => {
                              if (!storageReady) {
                                void handleUseRecommendedStorage().then(status => {
                                  if (status?.ok && status.configured_by !== 'default') {
                                    applyWizardProfile(profile.id);
                                    setWizardBuildMessage(`${profile.title} customization controls are open below. Advanced Details stays hidden unless you show it.`);
                                  } else {
                                    setWizardBuildMessage('nvWizard could not prove persistent storage yet. It will stay in the simple view; show Advanced Details only if you want manual overrides.');
                                  }
                                });
                                return;
                              }
                              applyWizardProfile(profile.id);
                              setWizardBuildMessage(`${profile.title} customization controls are open below. Advanced Details stays hidden unless you show it.`);
                            }}
                            disabled={!profilesReady || Boolean(activeWizardBuild) || storageAutopilotBusy || apiDisconnected}
                            className="btn-ghost px-3 py-2 text-[10px] font-mono uppercase tracking-wider disabled:opacity-40"
                          >
                            Customize
                          </button>
                        </div>
                      </div>
                    );
                  })}
              </div>
            </div>

          </div>
        )}

        {/* STORAGE */}
        {step === 'storage' && (
          <div className="space-y-6">
            <div>
              <div className="text-[10px] font-mono text-[#76B900] uppercase tracking-wider mb-1">Step 2</div>
              <h2 className="text-lg font-bold text-[#0a0a0a] font-mono">Persistent Storage</h2>
              <p className="text-xs font-mono text-[#a3a3a3] mt-1">nvWizard prefers a writable 200GB+ block-backed home/data mount and avoids read-only OS or network shares</p>
            </div>

            <div className="border border-[#d4d4d4] bg-[#ffffff] p-4 space-y-4">
              <div className="border border-[#76B900]/30 bg-[#76B900]/5 p-3">
                <div className="flex flex-col lg:flex-row lg:items-center lg:justify-between gap-3">
                  <div className="min-w-0">
                    <div className="text-xs font-bold text-[#0a0a0a]">Persistent home</div>
                    <div className="text-[10px] font-mono text-[#76B900] mt-1 break-all">
                      {storageStatus?.layout.home ?? 'nvWizard is finding the durable block volume'}
                    </div>
                    <div className="text-[10px] text-[#525252] mt-1">
                      Models, apps, projects, outputs, logs, and support snapshots live under this one workspace.
                    </div>
                  </div>
                  <span className={`text-[9px] font-mono uppercase px-2 py-1 border flex-shrink-0 ${storageReady ? 'border-[#76B900]/40 text-[#76B900]' : 'border-[#d97706]/40 text-[#d97706]'}`}>
                    {storageReady ? 'ready' : storageBeginnerLabel}
                  </span>
                </div>
                {storageStatus && (
                  <details className="mt-3">
                    <summary className="cursor-pointer text-[10px] font-mono text-[#737373] uppercase tracking-wider">
                      Workspace paths
                    </summary>
                    <div className="mt-2 grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-2">
                      {[
                        ['Models', storageStatus.layout.models_dir],
                        ['ComfyUI', storageStatus.layout.comfyui_dir],
                        ['Apps', storageStatus.layout.apps_dir],
                        ['Projects', storageStatus.layout.projects_dir],
                        ['Outputs', storageStatus.layout.outputs_dir],
                        ['Support', storageStatus.layout.support_dir],
                      ].map(([label, value]) => (
                        <div key={label} className="border border-[#e5e5e5] bg-white p-2 min-w-0">
                          <div className="text-[9px] font-mono text-[#737373] uppercase">{label}</div>
                          <div className="text-[10px] font-mono text-[#525252] mt-1 break-all">{value}</div>
                        </div>
                      ))}
                    </div>
                  </details>
                )}
              </div>

              <div>
                <label className="text-[10px] font-mono text-[#737373] uppercase tracking-wider">NVH_HOME</label>
                {mountRecommendation && (
                  <div className="mt-2 border border-[#76B900]/30 bg-[#76B900]/5 p-3 flex flex-col lg:flex-row lg:items-center lg:justify-between gap-3">
                    <div className="min-w-0">
                      <div className="text-[10px] font-mono uppercase tracking-wider text-[#76B900]">nvWizard Recommendation</div>
                      <div className="text-xs font-mono text-[#0a0a0a] mt-1 break-all">{mountRecommendation.recommended_home}</div>
                      <div className="text-[10px] text-[#525252] mt-1">
                        {mountRecommendation.large_block_mount ? 'block storage candidate' : 'candidate'}
                        {mountRecommendation.fs_type ? ` / ${mountRecommendation.fs_type}` : ''}
                        {mountRecommendation.mount_point ? ` / mounted at ${mountRecommendation.mount_point}` : ''}
                        {mountRecommendation.total_gb ? ` / ${mountRecommendation.total_gb} GB total` : ''}
                      </div>
                    </div>
                    <button
                      type="button"
                      onClick={handleUseRecommendedStorage}
                      disabled={storageSaving || mountActivating || !mountRecommendation.writable || mountRecommendation.read_only}
                      className="btn-primary px-4 py-2 text-xs font-mono disabled:opacity-40 flex-shrink-0"
                    >
                      {mountActivating ? 'Preparing...' : 'Use Persistent Drive'}
                    </button>
                  </div>
                )}
                <div className="mt-2 flex flex-col sm:flex-row gap-2">
                  <input
                    type="text"
                    value={storageHomeInput}
                    onChange={e => setStorageHomeInput(e.target.value)}
                    onKeyDown={e => { if (e.key === 'Enter') handleConfigureStorage(); }}
                    placeholder="/mnt/persist/nvhive or /workspace/nvhive"
                    className="input-base flex-1 px-3 py-2 text-xs font-mono"
                    spellCheck={false}
                  />
                  <button
                    type="button"
                    onClick={handleConfigureStorage}
                    disabled={storageSaving}
                    className="btn-primary px-4 py-2 text-xs font-mono disabled:opacity-40"
                  >
                    {storageSaving ? 'Checking...' : 'Use This Drive'}
                  </button>
                </div>
              </div>

              <div className={`border p-3 ${storageReady ? 'border-[#76B900]/40 bg-[#76B900]/5' : 'border-[#d97706]/40 bg-[#d97706]/5'}`}>
                <div className="flex items-center justify-between gap-3">
                  <div>
                    <div className={`text-xs font-mono font-bold ${storageReady ? 'text-[#76B900]' : 'text-[#d97706]'}`}>
                      {storageReady ? 'Storage preflight passed' : 'Storage preflight needs attention'}
                    </div>
                    <div className="text-[10px] font-mono text-[#737373] mt-1">
                      {storageStatus ? `${storageStatus.free_gb ?? 'Unknown'} GB free / ${storageStatus.total_gb ?? 'unknown'} GB total` : 'Waiting for API status'}
                    </div>
                  </div>
                  <span className={`text-[9px] font-mono px-2 py-1 border ${storageReady ? 'border-[#76B900]/40 text-[#76B900]' : 'border-[#d97706]/40 text-[#d97706]'}`}>
                    {storageStatus?.configured_by ?? 'unchecked'}
                  </span>
                </div>

                {(storageError || storageStatus?.warnings?.length) && (
                  <div className="mt-3 space-y-1">
                    {storageError && <div className="text-[10px] font-mono text-[#dc2626]">{storageError}</div>}
                    {storageStatus?.warnings.map(warning => (
                      <div key={warning} className="text-[10px] font-mono text-[#d97706]">{warning}</div>
                    ))}
                  </div>
                )}
              </div>

              {storageStatus && (
                <details className="border border-[#e5e5e5] bg-[#fafafa] p-3">
                  <summary className="cursor-pointer text-[10px] font-mono text-[#737373] uppercase tracking-wider">
                    Manual shell override
                  </summary>
                  <div className="mt-3 bg-[#0a0a0a] border border-[#333333] p-3 overflow-x-auto">
                    <code className="text-[10px] font-mono text-[#76B900] whitespace-pre">
                      {`source ${storageStatus.env_file}`}
                    </code>
                  </div>
                </details>
              )}
            </div>

            {!advancedSetupOpen && (
              <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
                <button
                  type="button"
                  onClick={() => setStep('done')}
                  className="btn-primary py-3 text-xs font-mono uppercase tracking-widest"
                >
                  Finish Setup
                </button>
                <Link href="/query" className="btn-secondary py-3 text-xs font-mono uppercase tracking-widest text-center">
                  Open Chat
                </Link>
                <button
                  type="button"
                  onClick={() => setStep('welcome')}
                  className="btn-ghost py-3 text-xs font-mono uppercase tracking-widest"
                >
                  Back To Missions
                </button>
              </div>
            )}
          </div>
        )}

        {/* GPU DETECTION */}
        {step === 'gpu' && (
          <div className="space-y-6">
            <div>
              <div className="text-[10px] font-mono text-[#76B900] uppercase tracking-wider mb-1">Step 3</div>
              <h2 className="text-lg font-bold text-[#0a0a0a] font-mono">GPU Detection</h2>
              <p className="text-xs font-mono text-[#a3a3a3] mt-1">Your NVIDIA GPU will power local AI inference</p>
            </div>

            {/* GPU detection result */}
            {gpuLoading ? (
              <div className="border border-[#d4d4d4] bg-[#ffffff] p-4 animate-pulse">
                <div className="flex items-center gap-4">
                  <div className="w-14 h-14 bg-[#f5f5f5]" />
                  <div className="flex-1 space-y-2">
                    <div className="h-3 bg-[#f5f5f5] w-1/2" />
                    <div className="h-2 bg-[#f5f5f5] w-1/3" />
                  </div>
                </div>
              </div>
            ) : gpuInfo && gpuInfo.gpus.length > 0 ? (
              <div className="space-y-3">
                {gpuInfo.gpus.map((g, i) => {
                  const usedPct = g.vram_mb > 0 ? Math.round((g.memory_used_mb / g.vram_mb) * 100) : 0;
                  const barColor = usedPct > 90 ? '#dc2626' : usedPct > 70 ? '#d97706' : '#76B900';
                  return (
                    <div key={i} className="border border-[#76B900]/40 bg-[#76B900]/5 p-4">
                      <div className="flex items-start gap-4">
                        <div className="w-14 h-14 bg-[#76B900]/10 border border-[#76B900]/30 flex items-center justify-center flex-shrink-0">
                          <svg className="w-7 h-7 text-[#76B900]" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
                            <path strokeLinecap="round" strokeLinejoin="round"
                              d="M9 3H5a2 2 0 00-2 2v4m6-6h10a2 2 0 012 2v4M9 3v18m0 0h10a2 2 0 002-2V9M9 21H5a2 2 0 01-2-2V9m0 0h18" />
                          </svg>
                        </div>
                        <div className="flex-1 min-w-0">
                          <div className="text-sm font-bold text-[#0a0a0a] font-mono">{g.name}</div>
                          <div className="text-[10px] font-mono text-[#76B900] mt-0.5">DETECTED / GPU {g.index}</div>
                          <div className="text-[10px] font-mono text-[#a3a3a3] mt-1 space-x-2">
                            <span>CUDA {g.cuda_version}</span>
                            <span>/</span>
                            <span>driver {g.driver_version}</span>
                            {g.architecture && (
                              <>
                                <span>/</span>
                                <span>{g.architecture}{g.architecture_heuristic ? ' (estimated)' : ''}</span>
                              </>
                            )}
                          </div>
                          <div className="mt-2 space-y-1">
                            <div className="flex justify-between text-[10px] font-mono">
                              <span className="text-[#a3a3a3]">VRAM</span>
                              <span className="text-[#525252]">
                                {(g.memory_used_mb / 1024).toFixed(1)} used / {g.vram_gb} GB total
                              </span>
                            </div>
                            <div className="progress-bar">
                              <div className="progress-fill" style={{ width: `${usedPct}%`, backgroundColor: barColor }} />
                            </div>
                            <div className="text-[10px] font-mono text-[#a3a3a3]">
                              {(g.memory_free_mb / 1024).toFixed(1)} GB free / Utilization {g.utilization_pct}%
                            </div>
                          </div>
                        </div>
                      </div>
                    </div>
                  );
                })}

                {/* System RAM */}
                <div className="bg-[#ffffff] border border-[#e5e5e5] p-3">
                  <div className="text-[10px] font-mono text-[#a3a3a3] mb-1 uppercase tracking-wider">System RAM</div>
                  <div className="text-xs font-mono text-[#525252]">
                    {gpuInfo.system_ram.total_gb} GB total / {gpuInfo.system_ram.available_gb} GB available /{' '}
                    {gpuInfo.system_ram.effective_for_llm_gb} GB usable for CPU offload
                  </div>
                </div>
              </div>
            ) : (
              <div className="border border-[#d4d4d4] bg-[#ffffff] p-4">
                <div className="flex items-center gap-4">
                  <div className="w-14 h-14 bg-[#e5e5e5] border border-[#d4d4d4] flex items-center justify-center flex-shrink-0">
                    <svg className="w-7 h-7 text-[#a3a3a3]" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
                      <path strokeLinecap="round" strokeLinejoin="round"
                        d="M9 3H5a2 2 0 00-2 2v4m6-6h10a2 2 0 012 2v4M9 3v18m0 0h10a2 2 0 002-2V9M9 21H5a2 2 0 01-2-2V9m0 0h18" />
                    </svg>
                  </div>
                  <div>
                    <div className="text-sm font-bold text-[#0a0a0a] font-mono">
                      {gpuDetectionStatus === 'blocked' ? 'GPU Present, Access Blocked' : 'No NVIDIA GPU Detected'}
                    </div>
                    <div className="text-[10px] font-mono text-[#a3a3a3] mt-0.5">{gpuDetectionStatus.toUpperCase()}</div>
                    <div className="text-[10px] font-mono text-[#a3a3a3] mt-1">
                      {gpuDetectionIssue || 'Local models will run on CPU. Consider a cloud provider for better speed.'}
                    </div>
                  </div>
                </div>
              </div>
            )}

            {/* Model recommendations from API */}
            {gpuRecs && gpuRecs.recommendations.length > 0 && (
              <div className="space-y-3">
                <div className="section-label">Model Recommendations</div>
                {gpuRecs.recommendations.map((rec, i) => {
                  const oom = gpuRecs.oom_check[rec.model];
                  const safe = oom ? oom.safe : true;
                  const fitsGpu = oom ? oom.fits_gpu : true;
                  return (
                    <div key={i} className={`flex items-start gap-3 px-3 py-3 border ${
                      i === 0
                        ? 'border-[#76B900]/40 bg-[#76B900]/5'
                        : 'border-[#e5e5e5] bg-[#ffffff]'
                    }`}>
                      <span className={`w-1.5 h-1.5 mt-1.5 flex-shrink-0 ${
                        safe && fitsGpu ? 'bg-[#76B900]' :
                        safe ? 'bg-[#d97706]' :
                        'bg-[#dc2626]'
                      }`} style={{ clipPath: 'polygon(50% 0%, 100% 50%, 50% 100%, 0% 50%)' }} />
                      <div className="flex-1 min-w-0">
                        <div className="flex items-center gap-2 flex-wrap">
                          <span className="text-xs font-mono font-bold text-[#0a0a0a]">{rec.model}</span>
                          <span className={`text-[10px] font-mono px-1.5 py-0.5 uppercase ${
                            i === 0 ? 'bg-[#76B900] text-black font-bold' : 'bg-[#e5e5e5] text-[#737373]'
                          }`}>{rec.tier}</span>
                          {oom && (
                            <span className={`text-[10px] font-mono px-1.5 py-0.5 uppercase ${
                              oom.fits_gpu ? 'bg-[#76B900]/10 text-[#76B900]' :
                              oom.fits_hybrid ? 'bg-[#d97706]/10 text-[#d97706]' :
                              'bg-[#dc2626]/10 text-[#dc2626]'
                            }`}>
                              {oom.fits_gpu ? 'GPU FIT' : oom.fits_hybrid ? 'HYBRID' : 'OOM RISK'}
                            </span>
                          )}
                        </div>
                        <div className="text-[10px] font-mono text-[#a3a3a3] mt-0.5">{rec.reason}</div>
                        {rec.vram_required_gb > 0 && (
                          <div className="text-[10px] font-mono text-[#a3a3a3] mt-0.5">
                            Requires ~{rec.vram_required_gb} GB VRAM
                          </div>
                        )}
                        <div className="mt-1.5 bg-[#ffffff] border border-[#e5e5e5] px-2 py-1 inline-block">
                          <code className={`text-[10px] font-mono ${i === 0 ? 'text-[#76B900]' : 'text-[#a3a3a3]'}`}>
                            ollama pull {rec.model}
                          </code>
                        </div>
                      </div>
                    </div>
                  );
                })}
              </div>
            )}

            {/* Ollama optimizations */}
            {gpuRecs?.optimizations && gpuInfo && gpuInfo.gpus.length > 0 && (
              <div className="bg-[#ffffff] border border-[#e5e5e5] p-4 space-y-2">
                <div className="text-[10px] font-mono text-[#a3a3a3] uppercase tracking-wider mb-2">
                  Ollama Optimizations - {gpuRecs.optimizations.architecture}
                </div>
                <div className="grid grid-cols-2 gap-2 text-[10px] font-mono">
                  <div className="flex justify-between">
                    <span className="text-[#a3a3a3]">Flash Attention</span>
                    <span className={gpuRecs.optimizations.flash_attention ? 'text-[#76B900]' : 'text-[#a3a3a3]'}>
                      {gpuRecs.optimizations.flash_attention ? 'ENABLED' : 'N/A'}
                    </span>
                  </div>
                  <div className="flex justify-between">
                    <span className="text-[#a3a3a3]">Parallelism</span>
                    <span className="text-[#525252]">{gpuRecs.optimizations.num_parallel}x</span>
                  </div>
                  <div className="flex justify-between">
                    <span className="text-[#a3a3a3]">Context</span>
                    <span className="text-[#525252]">{(gpuRecs.optimizations.recommended_ctx / 1024).toFixed(0)}K</span>
                  </div>
                  <div className="flex justify-between">
                    <span className="text-[#a3a3a3]">Quantization</span>
                    <span className="text-[#525252]">{gpuRecs.optimizations.recommended_quant}</span>
                  </div>
                </div>
                {gpuRecs.optimizations.notes.map((note, i) => (
                  <div key={i} className="text-[10px] font-mono text-[#a3a3a3]">- {note}</div>
                ))}
              </div>
            )}

            {/* Fallback note for CPU mode */}
            {!gpuLoading && (!gpuInfo || gpuInfo.gpus.length === 0) && (
              <div className="bg-[#ffffff] border border-[#e5e5e5] p-3">
                <div className="text-[10px] font-mono text-[#a3a3a3]">
                  No NVIDIA GPU? Hive still works - Ollama runs on CPU (slower), or use cloud advisors (OpenAI, Anthropic, etc.)
                </div>
              </div>
            )}
          </div>
        )}

        {/* MODEL PICKER */}
        {step === 'models' && (
          <div className="space-y-6">
            <div>
              <div className="text-[10px] font-mono text-[#76B900] uppercase tracking-wider mb-1">Step 4</div>
              <h2 className="text-lg font-bold text-[#0a0a0a] font-mono">Model Picker</h2>
              <p className="text-xs font-mono text-[#a3a3a3] mt-1">
                Choose exact local models to download. Recommendations are based on detected VRAM and beginner-friendly defaults.
              </p>
            </div>

            <div className="border border-[#76B900]/30 bg-[#76B900]/5 p-4">
              <div className="flex flex-col lg:flex-row lg:items-center gap-4 justify-between">
                <div>
                  <div className="text-sm font-mono font-bold text-[#0a0a0a]">Recommended Local Model Queue</div>
                  <div className="text-[10px] font-mono text-[#76B900] mt-0.5">
                    Detected VRAM: {detectedModelVram ? `${detectedModelVram} GB` : 'unknown'} / selected download: ~{selectedModelDiskGb.toFixed(1)} GB / persistent free: {storageFreeGb === null ? 'unknown' : `${storageFreeGb} GB`}
                  </div>
                  <div className="text-[10px] font-mono text-[#737373] mt-2">
                    {studioModels.filter(model => model.installed).length}/{studioModels.length} installed
                  </div>
                </div>
                <div className="flex flex-wrap gap-2">
                  <button
                    type="button"
                    onClick={selectRecommendedModels}
                    className="btn-secondary px-3 py-2 text-[10px] font-mono uppercase tracking-wider"
                  >
                    Recommended
                  </button>
                  <button
                    type="button"
                    onClick={selectInstalledMissingModels}
                    className="btn-ghost px-3 py-2 text-[10px] font-mono uppercase tracking-wider"
                  >
                    Missing Only
                  </button>
                  <button
                    type="button"
                    onClick={() => handleInstallStudioModels()}
                    disabled={modelsInstalling || selectedModelIds.length === 0 || !storageReady}
                    className="btn-primary px-3 py-2 text-[10px] font-mono uppercase tracking-wider disabled:opacity-40"
                  >
                    {!storageReady ? storagePrimaryLabel : modelsInstalling ? 'Downloading...' : `Download ${selectedModelIds.length || ''}`}
                  </button>
                </div>
              </div>
            </div>

            {modelError && (
              <div className="bg-[#dc2626]/5 border border-[#dc2626]/20 p-3">
                <div className="text-[10px] font-mono text-[#dc2626] uppercase tracking-wider mb-1">Model Error</div>
                <div className="text-xs font-mono text-[#dc2626]">{modelError}</div>
              </div>
            )}

            {(modelsInstalling || modelEvents.length > 0) && (
              <div className="bg-[#ffffff] border border-[#e5e5e5] p-4 space-y-2">
                <div className="flex items-center justify-between">
                  <div className="section-label">Install Queue</div>
                  <div className="text-[10px] font-mono text-[#a3a3a3]">Ollama rootless runtime</div>
                </div>
                <div className="max-h-44 overflow-y-auto space-y-1">
                  {modelEvents.map((event, index) => (
                    <div key={`${event.event}-${index}`} className="grid grid-cols-[72px_1fr] gap-2 text-[10px] font-mono">
                      <span className={event.event === 'error' ? 'text-[#dc2626]' : event.event === 'complete' ? 'text-[#76B900]' : 'text-[#737373]'}>
                        {event.event.toUpperCase()}
                      </span>
                      <span className="text-[#525252] break-words">{event.message}</span>
                    </div>
                  ))}
                </div>
              </div>
            )}

            {modelsLoading ? (
              <div className="grid grid-cols-1 md:grid-cols-2 gap-3 animate-pulse">
                {[1, 2, 3, 4].map(i => (
                  <div key={i} className="h-32 bg-[#ffffff] border border-[#e5e5e5]" />
                ))}
              </div>
            ) : (
              <div className="space-y-5">
                {modelCategories.map(category => (
                  <div key={category} className="space-y-2">
                    <div className="flex items-center gap-2">
                      <div className="section-label">{category}</div>
                      <span className="text-[10px] font-mono text-[#a3a3a3]">
                        {studioModels.filter(model => model.category === category).length} model(s)
                      </span>
                    </div>
                    <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
                      {studioModels.filter(model => model.category === category).map(model => {
                        const selected = selectedStudioModels.has(model.id);
                        return (
                          <label
                            key={model.id}
                            className={`block border p-4 cursor-pointer transition-colors ${
                              selected
                                ? 'border-[#76B900]/50 bg-[#76B900]/5'
                                : 'border-[#e5e5e5] bg-[#ffffff] hover:border-[#d4d4d4]'
                            }`}
                          >
                            <div className="flex items-start gap-3">
                              <input
                                type="checkbox"
                                checked={selected}
                                onChange={() => toggleStudioModel(model.id)}
                                className="mt-1 accent-[#76B900]"
                              />
                              <div className="min-w-0 flex-1">
                                <div className="flex items-start justify-between gap-3">
                                  <div>
                                    <div className="text-xs font-mono font-bold text-[#0a0a0a]">{model.title}</div>
                                    <div className="text-[10px] font-mono text-[#76B900] mt-0.5">{model.install_target}</div>
                                  </div>
                                  <span className={`text-[9px] font-mono px-1.5 py-0.5 border ${
                                    model.installed
                                      ? 'border-[#76B900]/40 text-[#76B900]'
                                      : model.fits_vram
                                      ? 'border-[#d4d4d4] text-[#525252]'
                                      : 'border-[#f59e0b]/40 text-[#b45309]'
                                  }`}>
                                    {model.installed ? 'INSTALLED' : model.fits_vram ? 'FITS' : 'CHECK VRAM'}
                                  </span>
                                </div>
                                <div className="text-[10px] font-mono text-[#737373] leading-relaxed mt-2">
                                  {model.why_recommended}
                                </div>
                                <div className="flex flex-wrap gap-1 mt-3">
                                  {model.recommended && (
                                    <span className="text-[9px] font-mono text-[#76B900] bg-[#76B900]/10 border border-[#76B900]/20 px-1.5 py-0.5">
                                      recommended
                                    </span>
                                  )}
                                  <span className="text-[9px] font-mono text-[#737373] bg-[#f5f5f5] border border-[#e5e5e5] px-1.5 py-0.5">
                                    {model.recommended_vram_gb ? `${model.recommended_vram_gb}GB VRAM` : 'CPU OK'}
                                  </span>
                                  <span className="text-[9px] font-mono text-[#737373] bg-[#f5f5f5] border border-[#e5e5e5] px-1.5 py-0.5">
                                    ~{model.estimated_disk_gb}GB
                                  </span>
                                  {model.capabilities.slice(0, 3).map(capability => (
                                    <span key={capability} className="text-[9px] font-mono text-[#737373] bg-[#f5f5f5] border border-[#e5e5e5] px-1.5 py-0.5">
                                      {capability}
                                    </span>
                                  ))}
                                </div>
                              </div>
                            </div>
                          </label>
                        );
                      })}
                    </div>
                  </div>
                ))}
              </div>
            )}
          </div>
        )}

        {/* LOCAL AI */}
        {step === 'local-ai' && (
          <div className="space-y-6">
            <div>
              <div className="text-[10px] font-mono text-[#76B900] uppercase tracking-wider mb-1">Step 5</div>
              <h2 className="text-lg font-bold text-[#0a0a0a] font-mono">Local AI Setup</h2>
              <p className="text-xs font-mono text-[#a3a3a3] mt-1">Install NVIDIA Nemotron via Ollama - runs on your GPU, free forever</p>
            </div>

            {/* Ollama status */}
            <div className={`p-4 border ${ollamaStatus === 'online' ? 'border-[#76B900]/40 bg-[#76B900]/5' : 'border-[#d4d4d4] bg-[#ffffff]'}`}>
              <div className="flex items-center gap-3">
                <span className={`w-2 h-2 flex-shrink-0 ${
                  ollamaStatus === 'online' ? 'bg-[#76B900] nvidia-pulse' :
                  ollamaStatus === 'offline' ? 'bg-[#dc2626]' :
                  'bg-[#a3a3a3] animate-pulse'
                }`} style={{ clipPath: 'polygon(50% 0%, 100% 50%, 50% 100%, 0% 50%)' }} />
                <div>
                  <div className={`text-sm font-mono font-bold ${ollamaStatus === 'online' ? 'text-[#76B900]' : 'text-[#0a0a0a]'}`}>
                    Ollama {ollamaStatus === 'checking' ? 'CHECKING...' : ollamaStatus === 'online' ? 'RUNNING' : 'NOT DETECTED'}
                  </div>
                  <div className="text-[10px] font-mono text-[#a3a3a3]">
                    {ollamaStatus === 'online' ? 'Local inference server is active at localhost:11434' :
                     ollamaStatus === 'offline' ? 'Install and start Ollama to enable local models' :
                     'Connecting...'}
                  </div>
                </div>
              </div>
            </div>

            {/* Install instructions */}
            {ollamaStatus !== 'online' && (
              <div className="bg-[#ffffff] border border-[#e5e5e5] p-4 space-y-3">
                <div>
                  <div className="section-label">Local AI Action</div>
                  <div className="text-sm font-bold text-[#0a0a0a] mt-1">Install the rootless runtime and the GPU-fit model queue.</div>
                  <div className="text-xs text-[#525252] mt-1">
                    nvWizard keeps the runtime, models, and launchers under the persistent workspace.
                  </div>
                </div>
                <div className="flex flex-wrap gap-2">
                  <button
                    type="button"
                    onClick={() => handleInstallStudioPacks(['rootless-ollama'])}
                    disabled={!storageReady || studioInstalling}
                    className="btn-primary px-3 py-2 text-[10px] font-mono uppercase tracking-wider disabled:opacity-40"
                  >
                    {studioInstalling ? 'Installing' : 'Install Local Runtime'}
                  </button>
                  <button
                    type="button"
                    onClick={() => handleInstallStudioModels(recommendedMissingModelIds())}
                    disabled={!storageReady || modelsInstalling || recommendedMissingModelIds().length === 0}
                    className="btn-ghost px-3 py-2 text-[10px] font-mono uppercase tracking-wider disabled:opacity-40"
                  >
                    {modelsInstalling ? 'Downloading' : 'Download Recommended Models'}
                  </button>
                </div>
                <details className="border border-[#e5e5e5] bg-[#fafafa] p-3">
                  <summary className="cursor-pointer text-[10px] font-mono text-[#737373] uppercase tracking-wider">
                    Manual overrides
                  </summary>
                  <div className="mt-3 font-mono text-sm space-y-2">
                    <div className="text-[#a3a3a3] text-[10px] uppercase tracking-wider"># Rootless Ollama runtime, no sudo</div>
                    <div className="text-[#76B900] break-all">nvh studio --install rootless-ollama -y</div>
                    <div className="text-[#a3a3a3] text-[10px] uppercase tracking-wider mt-3"># Start the local model server</div>
                    <div className="text-[#76B900] break-all">nvhive-ollama-serve</div>
                    <div className="text-[#a3a3a3] text-[10px] uppercase tracking-wider mt-3"># Pull recommended fitting models</div>
                    <div className="text-[#76B900] break-all">nvh studio --install-models recommended -y</div>
                  </div>
                </details>
              </div>
            )}

            {/* Model recommendations */}
            <div className="space-y-3">
              <div className="section-label">Recommended Models</div>

              {/* Nemotron featured */}
              <div className="border border-[#76B900]/40 bg-[#76B900]/5 p-4 relative">
                <div className="absolute top-2 right-2 text-[10px] font-mono px-1.5 py-0.5 bg-[#76B900] text-black font-bold">
                  RECOMMENDED
                </div>
                <div className="flex items-start gap-3 pr-24">
                  <div className="w-8 h-8 bg-[#76B900]/20 border border-[#76B900]/40 flex items-center justify-center flex-shrink-0 font-bold text-[#76B900] text-sm font-mono">N</div>
                  <div>
                    <div className="text-sm font-mono font-bold text-[#0a0a0a]">NVIDIA Nemotron Mini (2B)</div>
                    <div className="text-[10px] font-mono text-[#76B900]">~2 GB / Fast / 4K context / Instruction tuned</div>
                    <div className="mt-1 bg-[#ffffff] border border-[#e5e5e5] px-2 py-1">
                      <code className="text-[10px] font-mono text-[#76B900]">ollama pull nemotron-mini</code>
                    </div>
                  </div>
                </div>
              </div>

              <div className="border border-[#d4d4d4] p-4">
                <div className="flex items-start gap-3">
                  <div className="w-8 h-8 bg-[#e5e5e5] border border-[#d4d4d4] flex items-center justify-center flex-shrink-0 font-bold text-[#525252] text-sm font-mono">N</div>
                  <div>
                    <div className="text-sm font-mono font-bold text-[#0a0a0a]">NVIDIA Nemotron (8B)</div>
                    <div className="text-[10px] font-mono text-[#a3a3a3]">~8 GB / Best quality / 131K context / Tool calling</div>
                    <div className="mt-1 bg-[#ffffff] border border-[#e5e5e5] px-2 py-1">
                      <code className="text-[10px] font-mono text-[#525252]">ollama pull nemotron</code>
                    </div>
                  </div>
                </div>
              </div>
            </div>

            <details className="bg-[#ffffff] border border-[#e5e5e5] p-4">
              <summary className="cursor-pointer text-[10px] font-mono text-[#737373] uppercase tracking-wider">Rootless container option</summary>
              <div className="mt-3">
                <code className="text-[10px] font-mono text-[#76B900]">docker compose up -d</code>
                <div className="text-[10px] text-[#737373] mt-1">
                  Only use this when Docker/Podman is available without sudo. Otherwise nvWizard uses the workspace runtime.
                </div>
              </div>
            </details>
          </div>
        )}

        {/* AI STUDIO PACKS */}
        {step === 'studio' && (
          <div className="space-y-6">
            <div>
              <div className="text-[10px] font-mono text-[#76B900] uppercase tracking-wider mb-1">Step 6</div>
              <h2 className="text-lg font-bold text-[#0a0a0a] font-mono">AI Studio Packs</h2>
              <p className="text-xs font-mono text-[#a3a3a3] mt-1">
                One-click rootless packs for LLMs, OpenClaw/NemoClaw agents, ComfyUI nodes, Blender, runtime fallback, and Linux game projects.
              </p>
            </div>

            <div className="border border-[#76B900]/30 bg-[#76B900]/5 p-4">
              <div className="flex flex-col lg:flex-row lg:items-center gap-4 justify-between">
                <div>
                  <div className="text-sm font-mono font-bold text-[#0a0a0a]">AI Starter Pack</div>
                  <div className="text-[10px] font-mono text-[#76B900] mt-0.5">
                    No sudo. Installs under {studioRoot || storageStatus?.layout.studio_dir || 'NVH_HOME/studio'} and {storageStatus?.layout.bin_dir || 'NVH_HOME/bin'}
                  </div>
                  <div className="text-[10px] font-mono text-[#737373] mt-2">
                    {starterStudioPackIds.length} starter packs - {clawStudioPackIds.length} Claw options - {studioPacks.filter(pack => pack.status.installed).length}/{studioPacks.length} installed - {blockedStudioPackCount} blocked by host - selected ~{selectedStudioPackDiskGb.toFixed(1)} GB - free {storageFreeGb === null ? 'unknown' : `${storageFreeGb} GB`}
                  </div>
                </div>
                <div className="flex flex-wrap gap-2">
                  <button
                    type="button"
                    onClick={() => selectStudioBundle('starter')}
                    className="btn-secondary px-3 py-2 text-[10px] font-mono uppercase tracking-wider"
                  >
                    Select Starter
                  </button>
                  <button
                    type="button"
                    onClick={() => selectStudioBundle('claw')}
                    className="btn-secondary px-3 py-2 text-[10px] font-mono uppercase tracking-wider"
                  >
                    Select Claw Agents
                  </button>
                  <button
                    type="button"
                    onClick={() => selectStudioBundle('all')}
                    className="btn-ghost px-3 py-2 text-[10px] font-mono uppercase tracking-wider"
                  >
                    Select All
                  </button>
                  <button
                    type="button"
                    onClick={() => handleInstallStudioPacks(selectedStudioPackIds)}
                    disabled={studioInstalling || selectedStudioPackIds.length === 0 || !storageReady}
                    className="btn-primary px-3 py-2 text-[10px] font-mono uppercase tracking-wider disabled:opacity-40"
                  >
                    {!storageReady ? storagePrimaryLabel : studioInstalling ? 'Installing...' : `Install ${selectedStudioPackIds.length || ''}`}
                  </button>
                </div>
              </div>
            </div>

            {studioError && (
              <div className="bg-[#dc2626]/5 border border-[#dc2626]/20 p-3">
                <div className="text-[10px] font-mono text-[#dc2626] uppercase tracking-wider mb-1">Pack Error</div>
                <div className="text-xs font-mono text-[#dc2626]">{studioError}</div>
              </div>
            )}

            {(studioInstalling || studioEvents.length > 0) && (
              <div className="bg-[#ffffff] border border-[#e5e5e5] p-4 space-y-2">
                <div className="flex items-center justify-between">
                  <div className="section-label">Pack Stream</div>
                  <div className="text-[10px] font-mono text-[#a3a3a3]">rootless mode</div>
                </div>
                <div className="max-h-44 overflow-y-auto space-y-1">
                  {studioEvents.map((event, index) => (
                    <div key={`${event.event}-${index}`} className="grid grid-cols-[72px_1fr] gap-2 text-[10px] font-mono">
                      <span className={event.event === 'error' ? 'text-[#dc2626]' : event.event === 'complete' ? 'text-[#76B900]' : 'text-[#a3a3a3]'}>
                        {event.event.toUpperCase()}
                      </span>
                      <span className="text-[#525252] break-words">{event.message}</span>
                    </div>
                  ))}
                </div>
              </div>
            )}

            {studioLoading ? (
              <div className="grid grid-cols-1 md:grid-cols-2 gap-3 animate-pulse">
                {[1, 2, 3, 4].map(i => (
                  <div key={i} className="h-32 bg-[#ffffff] border border-[#e5e5e5]" />
                ))}
              </div>
            ) : (
              <div className="space-y-5">
                {studioCategories.map(category => (
                  <div key={category} className="space-y-2">
                    <div className="flex items-center gap-2">
                      <div className="section-label">{category}</div>
                      <span className="text-[10px] font-mono text-[#a3a3a3]">
                        {studioPacks.filter(pack => pack.category === category).length} pack(s)
                      </span>
                    </div>
                    <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
                      {studioPacks.filter(pack => pack.category === category).map(pack => {
                        const installable = studioPackInstallable(pack);
                        const selected = selectedStudioPacks.has(pack.id) && installable;
                        const blockedReason = studioPackBlockedReason(pack);
                        const badgeText = pack.status.installed ? 'INSTALLED' : installable ? 'READY' : 'BLOCKED';
                        return (
                          <label
                            key={pack.id}
                            className={`block border p-4 transition-colors ${
                              selected
                                ? 'border-[#76B900]/50 bg-[#76B900]/5'
                                : installable
                                  ? 'border-[#e5e5e5] bg-[#ffffff] hover:border-[#d4d4d4] cursor-pointer'
                                  : 'border-[#e5e5e5] bg-[#fafafa] opacity-75 cursor-not-allowed'
                            }`}
                          >
                            <div className="flex items-start gap-3">
                              <input
                                type="checkbox"
                                checked={selected}
                                disabled={!installable}
                                onChange={() => toggleStudioPack(pack.id)}
                                className="mt-1 accent-[#76B900]"
                              />
                              <div className="min-w-0 flex-1">
                                <div className="flex items-start justify-between gap-3">
                                  <div>
                                    <div className="text-xs font-mono font-bold text-[#0a0a0a]">{pack.title}</div>
                                    <div className="text-[10px] font-mono text-[#76B900] mt-0.5">{pack.tagline}</div>
                                  </div>
                                  <span className={`text-[9px] font-mono px-1.5 py-0.5 border ${
                                    pack.status.installed
                                      ? 'border-[#76B900]/40 text-[#76B900]'
                                      : installable
                                        ? 'border-[#d4d4d4] text-[#737373]'
                                        : 'border-[#dc2626]/30 text-[#dc2626]'
                                  }`}>
                                    {badgeText}
                                  </span>
                                </div>
                                <div className="text-[10px] font-mono text-[#737373] leading-relaxed mt-2">
                                  {pack.description}
                                </div>
                                {blockedReason && (
                                  <div className="text-[10px] font-mono text-[#dc2626] leading-relaxed mt-2 border border-[#dc2626]/20 bg-[#dc2626]/5 p-2">
                                    {blockedReason}
                                  </div>
                                )}
                                <div className="flex flex-wrap gap-1 mt-3">
                                  <span className="text-[9px] font-mono text-[#a3a3a3] bg-[#f5f5f5] border border-[#e5e5e5] px-1.5 py-0.5">
                                    {pack.recommended_vram_gb ? `${pack.recommended_vram_gb}GB VRAM` : 'any GPU'}
                                  </span>
                                  <span className="text-[9px] font-mono text-[#a3a3a3] bg-[#f5f5f5] border border-[#e5e5e5] px-1.5 py-0.5">
                                    ~{pack.estimated_disk_gb}GB
                                  </span>
                                  {pack.models.slice(0, 2).map(model => (
                                    <span key={model} className="text-[9px] font-mono text-[#a3a3a3] bg-[#f5f5f5] border border-[#e5e5e5] px-1.5 py-0.5">
                                      {model}
                                    </span>
                                  ))}
                                  {pack.comfy_nodes.length > 0 && (
                                    <span className="text-[9px] font-mono text-[#a3a3a3] bg-[#f5f5f5] border border-[#e5e5e5] px-1.5 py-0.5">
                                      {pack.comfy_nodes.length} nodes
                                    </span>
                                  )}
                                  {pack.python_packages.length > 0 && (
                                    <span className="text-[9px] font-mono text-[#a3a3a3] bg-[#f5f5f5] border border-[#e5e5e5] px-1.5 py-0.5">
                                      {pack.python_packages.length} packages
                                    </span>
                                  )}
                                </div>
                              </div>
                            </div>
                          </label>
                        );
                      })}
                    </div>
                  </div>
                ))}
              </div>
            )}
          </div>
        )}

        {/* COMFYUI */}
        {step === 'comfyui' && (
          <div className="space-y-6">
            <div>
              <div className="text-[10px] font-mono text-[#76B900] uppercase tracking-wider mb-1">Step 7</div>
              <h2 className="text-lg font-bold text-[#0a0a0a] font-mono">ComfyUI Visual Workflows</h2>
              <p className="text-xs font-mono text-[#a3a3a3] mt-1">
                Auto-install a local ComfyUI workspace with NVIDIA-ready PyTorch, Manager support, and nvHive example packs.
              </p>
            </div>

            <div className={`p-4 border ${comfyStatus?.running ? 'border-[#76B900]/40 bg-[#76B900]/5' : comfyStatus?.installed ? 'border-[#d4d4d4] bg-[#ffffff]' : 'border-[#e5e5e5] bg-white'}`}>
              <div className="flex flex-col sm:flex-row sm:items-center gap-4 justify-between">
                <div className="flex items-start gap-3">
                  <span className={`w-2 h-2 mt-1.5 flex-shrink-0 ${
                    comfyStatus?.running ? 'bg-[#76B900] nvidia-pulse' :
                    comfyStatus?.installed ? 'bg-[#d97706]' :
                    comfyLoading ? 'bg-[#a3a3a3] animate-pulse' :
                    'bg-[#dc2626]'
                  }`} style={{ clipPath: 'polygon(50% 0%, 100% 50%, 50% 100%, 0% 50%)' }} />
                  <div>
                    <div className={`text-sm font-mono font-bold ${comfyStatus?.running ? 'text-[#76B900]' : 'text-[#0a0a0a]'}`}>
                      ComfyUI {comfyLoading ? 'CHECKING...' : comfyStatus?.running ? 'RUNNING' : comfyStatus?.installed ? 'INSTALLED' : 'NOT INSTALLED'}
                    </div>
                    <div className="text-[10px] font-mono text-[#a3a3a3] mt-0.5 break-all">
                      {comfyStatus?.installed ? comfyStatus.app_dir : `Install target: ${storageStatus?.layout.comfyui_dir ?? 'NVH_HOME/comfyui'}/ComfyUI`}
                    </div>
                    <div className="text-[10px] font-mono text-[#737373] mt-1">
                      Persistent free: {storageFreeGb === null ? 'unknown' : `${storageFreeGb} GB`}
                    </div>
                    {comfyStatus?.examples_installed && (
                      <div className="text-[10px] font-mono text-[#76B900] mt-1 break-all">
                        nvHive examples installed at {comfyStatus.examples_dir}
                      </div>
                    )}
                  </div>
                </div>

                <div className="flex flex-wrap gap-2">
                  <button
                    type="button"
                    onClick={handleInstallComfyUI}
                    disabled={comfyInstalling || !storageReady}
                    className="btn-primary px-3 py-2 text-[10px] font-mono uppercase tracking-wider disabled:opacity-40"
                  >
                    {!storageReady ? storagePrimaryLabel : comfyInstalling ? 'Installing...' : comfyStatus?.installed ? 'Refresh Install' : 'Install ComfyUI'}
                  </button>
                  <button
                    type="button"
                    onClick={handleStartComfyUI}
                    disabled={comfyStarting || !comfyStatus?.installed}
                    className="btn-secondary px-3 py-2 text-[10px] font-mono uppercase tracking-wider disabled:opacity-40"
                  >
                    {comfyStarting ? 'Starting...' : comfyStatus?.running ? 'Restart Check' : 'Start'}
                  </button>
                  {comfyStatus?.running ? (
                    <a
                      href={comfyStatus.url}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="btn-ghost px-3 py-2 text-[10px] font-mono uppercase tracking-wider"
                    >
                      Open ComfyUI
                    </a>
                  ) : (
                    <button
                      type="button"
                      disabled
                      className="btn-ghost px-3 py-2 text-[10px] font-mono uppercase tracking-wider opacity-40"
                    >
                      Open When Running
                    </button>
                  )}
                </div>
              </div>
            </div>

            {comfyError && (
              <div className="bg-[#dc2626]/5 border border-[#dc2626]/20 p-3">
                <div className="text-[10px] font-mono text-[#dc2626] uppercase tracking-wider mb-1">ComfyUI Error</div>
                <div className="text-xs font-mono text-[#dc2626]">{comfyError}</div>
              </div>
            )}

            {(comfyInstalling || comfyEvents.length > 0) && (
              <div className="bg-[#ffffff] border border-[#e5e5e5] p-4 space-y-2">
                <div className="flex items-center justify-between">
                  <div className="section-label">Install Stream</div>
                  <div className="text-[10px] font-mono text-[#a3a3a3]">
                    PyTorch profile: {selectedComfyTorchProfile}
                  </div>
                </div>
                <div className="max-h-44 overflow-y-auto space-y-1">
                  {comfyEvents.length === 0 ? (
                    <div className="grid grid-cols-[72px_1fr] gap-2 text-[10px] font-mono">
                      <span className="text-[#a3a3a3]">START</span>
                      <span className="text-[#525252] break-words">Preparing ComfyUI inside the persistent workspace.</span>
                    </div>
                  ) : (
                    comfyEvents.map((event, index) => (
                      <div key={`${event.event}-${index}`} className="grid grid-cols-[72px_1fr] gap-2 text-[10px] font-mono">
                        <span className={event.event === 'error' ? 'text-[#dc2626]' : event.event === 'complete' ? 'text-[#76B900]' : 'text-[#a3a3a3]'}>
                          {event.event.toUpperCase()}
                        </span>
                        <span className="text-[#525252] break-words">{event.message}</span>
                      </div>
                    ))
                  )}
                </div>
              </div>
            )}

            <div className="space-y-3">
              <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-3">
                <div>
                  <div className="section-label">Workflow Download Checklist</div>
                  <div className="text-[10px] font-mono text-[#a3a3a3] mt-1">
                    {selectedComfyExamples.size} workflow(s), {selectedComfyModelCount} model requirement(s)
                  </div>
                </div>
                <button
                  type="button"
                  onClick={handleSaveComfyPlan}
                  disabled={comfyPlanSaving || selectedComfyExamples.size === 0}
                  className="btn-secondary px-3 py-2 text-[10px] font-mono uppercase tracking-wider disabled:opacity-40"
                >
                  {comfyPlanSaving ? 'Saving...' : 'Save Download Checklist'}
                </button>
              </div>

              {comfyPlanMessage && (
                <div className="bg-[#76B900]/5 border border-[#76B900]/20 p-3">
                  <div className="text-[10px] font-mono text-[#76B900]">{comfyPlanMessage}</div>
                </div>
              )}

              <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
                {visibleComfyExamples.slice(0, 6).map(example => {
                  const selected = selectedComfyExamples.has(example.id);
                  return (
                    <label
                      key={example.id}
                      className={`block border p-4 space-y-3 cursor-pointer transition-colors ${
                        selected ? 'border-[#76B900]/50 bg-[#76B900]/5' : 'border-[#e5e5e5] bg-[#ffffff] hover:border-[#d4d4d4]'
                      }`}
                    >
                      <div className="flex items-start justify-between gap-3">
                        <div className="flex items-start gap-3">
                          <input
                            type="checkbox"
                            checked={selected}
                            onChange={() => toggleComfyExample(example.id)}
                            className="mt-1 accent-[#76B900]"
                          />
                          <div>
                            <div className="text-xs font-mono font-bold text-[#0a0a0a]">{example.title}</div>
                            <div className="text-[10px] font-mono text-[#76B900] uppercase mt-0.5">
                              {example.category} / {example.install_profile}
                            </div>
                          </div>
                        </div>
                        <div className="text-[10px] font-mono text-[#525252] border border-[#d4d4d4] px-1.5 py-0.5">
                          {example.recommended_vram_gb}GB
                        </div>
                      </div>

                      <div className="text-[10px] font-mono text-[#737373] leading-relaxed">
                        {example.why_trending}
                      </div>

                      <div className="bg-[#ffffff] border border-[#e5e5e5] p-2">
                        <div className="text-[9px] font-mono text-[#a3a3a3] uppercase mb-1">Load path</div>
                        <div className="text-[10px] font-mono text-[#525252]">{example.workflow_hint}</div>
                      </div>

                      <div className="flex flex-wrap gap-1">
                        {example.models.slice(0, 3).map(model => (
                          <span key={model} className="text-[9px] font-mono text-[#a3a3a3] bg-[#f5f5f5] border border-[#e5e5e5] px-1.5 py-0.5">
                            {model}
                          </span>
                        ))}
                        {example.models.length > 3 && (
                          <span className="text-[9px] font-mono text-[#a3a3a3] bg-[#f5f5f5] border border-[#e5e5e5] px-1.5 py-0.5">
                            +{example.models.length - 3} more
                          </span>
                        )}
                      </div>
                    </label>
                  );
                })}
              </div>
            </div>

            <div className="bg-[#76B900]/5 border border-[#76B900]/20 p-3">
              <div className="text-[10px] font-mono text-[#76B900] leading-relaxed">
                The installer writes an nvHive example manifest into ComfyUI so the WebUI can keep showing the same starter deck after install. Model downloads still stay explicit because several image/video models require upstream terms or large storage.
              </div>
            </div>
          </div>
        )}

        {/* CLOUD PROVIDERS */}
        {step === 'cloud' && (
          <div className="space-y-6">
            <div>
              <div className="text-[10px] font-mono text-[#76B900] uppercase tracking-wider mb-1">Step 8</div>
              <h2 className="text-lg font-bold text-[#0a0a0a] font-mono">Cloud Providers</h2>
              <p className="text-xs font-mono text-[#a3a3a3] mt-1">
                Optional - add API keys for cloud providers. Local Nemotron works without any keys.
              </p>
            </div>

            <div className="bg-[#76B900]/5 border border-[#76B900]/20 p-3">
              <div className="text-[10px] font-mono text-[#76B900]">
                TIP: Start with Nemotron (free!) then add cloud advisors for tasks that need more power.
                Mix local + cloud in Convene mode for the best of both worlds.
              </div>
            </div>

            {freeProvidersLoading ? (
              <div className="space-y-2 animate-pulse">
                {[1, 2, 3].map(i => (
                  <div key={i} className="h-16 bg-[#ffffff] border border-[#e5e5e5]" />
                ))}
              </div>
            ) : freeProviders.length > 0 ? (
              <div className="space-y-5">
                {/* Group: No Signup Needed */}
                {(() => {
                  const group = freeProviders.filter(p => p.signup_tier === 'none');
                  if (!group.length) return null;
                  return (
                    <div className="space-y-2">
                      <div className="flex items-center gap-2">
                        <div className="section-label">No Signup Needed</div>
                        <span className="text-[10px] font-mono text-[#76B900] bg-[#76B900]/10 px-1.5 py-0.5">FREE</span>
                      </div>
                      {group.map(p => <ProviderCard key={p.id} p={p} {...{ expandedProvider, setExpandedProvider, keyInputs, setKeyInputs, savingKey, savedKeys, keyErrors, handleSaveKey }} />)}
                    </div>
                  );
                })()}

                {/* Group: Email Signup */}
                {(() => {
                  const group = freeProviders.filter(p => p.signup_tier === 'email');
                  if (!group.length) return null;
                  return (
                    <div className="space-y-2">
                      <div className="flex items-center gap-2">
                        <div className="section-label">Email Signup (Free Tier)</div>
                      </div>
                      {group.map(p => <ProviderCard key={p.id} p={p} {...{ expandedProvider, setExpandedProvider, keyInputs, setKeyInputs, savingKey, savedKeys, keyErrors, handleSaveKey }} />)}
                    </div>
                  );
                })()}

                {/* Group: Account Needed */}
                {(() => {
                  const group = freeProviders.filter(p => p.signup_tier === 'account');
                  if (!group.length) return null;
                  return (
                    <div className="space-y-2">
                      <div className="flex items-center gap-2">
                        <div className="section-label">Account Needed</div>
                        <span className="text-[10px] font-mono text-[#d97706] bg-[#d97706]/10 px-1.5 py-0.5">PAID / LIMITED FREE</span>
                      </div>
                      {group.map(p => <ProviderCard key={p.id} p={p} {...{ expandedProvider, setExpandedProvider, keyInputs, setKeyInputs, savingKey, savedKeys, keyErrors, handleSaveKey }} />)}
                    </div>
                  );
                })()}
              </div>
            ) : (
              /* Fallback: static list when API endpoint not available */
              <div className="space-y-3">
                {CLOUD_PROVIDERS.map(provider => (
                  <div key={provider.id} className="border border-[#e5e5e5] bg-[#ffffff] p-4">
                    <div className="flex items-center justify-between mb-2">
                      <div>
                        <div className="text-sm font-mono font-bold text-[#0a0a0a]">{provider.name}</div>
                        <div className="text-[10px] font-mono text-[#a3a3a3]">{provider.description}</div>
                      </div>
                      <div className="flex items-center gap-2">
                        {savedKeys.has(provider.id) && (
                          <span className="text-[10px] font-mono text-[#76B900] bg-[#76B900]/10 px-1.5 py-0.5">CONFIGURED</span>
                        )}
                        {provider.signupUrl && (
                          <a
                            href={provider.signupUrl}
                            target="_blank"
                            rel="noopener noreferrer"
                            className="text-[10px] font-mono text-[#76B900] hover:underline"
                          >
                            Get Key &rarr;
                          </a>
                        )}
                      </div>
                    </div>
                    <div className="flex gap-2">
                      <input
                        type="text"
                        value={keyInputs[provider.id] || ''}
                        onChange={e => setKeyInputs(prev => ({ ...prev, [provider.id]: e.target.value }))}
                        onPaste={e => {
                          const pasted = e.clipboardData.getData('text').trim();
                          if (pasted) {
                            setKeyInputs(prev => ({ ...prev, [provider.id]: pasted }));
                          }
                        }}
                        placeholder={`Paste ${provider.envKey} here...`}
                        className="input-base flex-1 px-3 py-2 text-xs font-mono"
                        spellCheck={false}
                        autoComplete="off"
                      />
                    </div>
                    <div className="mt-2 flex flex-col sm:flex-row sm:items-center sm:justify-between gap-2">
                      <div className="text-[10px] font-mono text-[#333333]">
                        Or set as env var: <span className="text-[#a3a3a3]">{provider.envKey}=your-key</span>
                      </div>
                      <button
                        type="button"
                        onClick={() => void handleSaveKey(provider.id)}
                        disabled={savingKey === provider.id || !(keyInputs[provider.id] || '').trim()}
                        className="btn-primary px-3 py-2 text-[10px] font-mono uppercase tracking-wider disabled:opacity-40"
                      >
                        {savingKey === provider.id ? 'Saving' : 'Save Key'}
                      </button>
                    </div>
                    {keyErrors[provider.id] && (
                      <div className="text-[10px] font-mono text-[#dc2626] mt-1">{keyErrors[provider.id]}</div>
                    )}
                    {savedKeys.has(provider.id) && (
                      <div className="text-[10px] font-mono text-[#76B900] mt-1">Saved through the local Hive API.</div>
                    )}
                  </div>
                ))}
              </div>
            )}

            <div className="bg-[#ffffff] border border-[#e5e5e5] p-3">
              <div className="text-[10px] font-mono text-[#a3a3a3]">
                API keys entered here are saved via the Hive API. You can also set them as env vars in your <span className="text-[#76B900]">.env</span> file.
              </div>
            </div>
          </div>
        )}

        {/* TEST */}
        {step === 'test' && (
          <div className="space-y-6">
            <div>
              <div className="text-[10px] font-mono text-[#76B900] uppercase tracking-wider mb-1">Step 9</div>
              <h2 className="text-lg font-bold text-[#0a0a0a] font-mono">Quick Test</h2>
              <p className="text-xs font-mono text-[#a3a3a3] mt-1">Verify everything is working correctly</p>
            </div>

            {/* API status */}
            <div className={`p-4 border ${apiStatus === 'connected' ? 'border-[#76B900]/40 bg-[#76B900]/5' : 'border-[#dc2626]/40 bg-[#dc2626]/5'}`}>
              <div className="flex items-center gap-3">
                <span className={`w-2 h-2 flex-shrink-0 ${apiStatus === 'connected' ? 'bg-[#76B900]' : 'bg-[#dc2626]'}`}
                  style={{ clipPath: 'polygon(50% 0%, 100% 50%, 50% 100%, 0% 50%)' }} />
                <div>
                  <div className={`text-sm font-mono font-bold ${apiStatus === 'connected' ? 'text-[#76B900]' : 'text-[#dc2626]'}`}>
                    Hive API {apiStatus === 'connected' ? 'ONLINE' : 'OFFLINE'}
                  </div>
                    {apiStatus === 'disconnected' && (
                      <div className="text-[10px] font-mono text-[#a3a3a3] mt-0.5">
                        Launch nvHive, or use terminal fallback: <span className="text-[#76B900]">nvh webui</span>
                      </div>
                    )}
                </div>
              </div>
            </div>

            {/* Configured providers */}
            {configuredProviders.length > 0 && (
              <div className="space-y-2">
                <div className="section-label">Active Advisors</div>
                <div className="flex flex-wrap gap-2">
                  {configuredProviders.map(p => (
                    <span key={p} className="text-[10px] font-mono px-2 py-1 bg-[#76B900]/10 text-[#76B900] border border-[#76B900]/20 uppercase">
                      {p}
                    </span>
                  ))}
                </div>
              </div>
            )}

            {/* Test prompt */}
            <div className="space-y-3">
              <div className="section-label">Test Query</div>
              <div className="bg-[#ffffff] border border-[#e5e5e5] p-3">
                <div className="text-[10px] font-mono text-[#a3a3a3] mb-1">SENDING:</div>
                <div className="text-xs font-mono text-[#525252]">{testPrompt}</div>
              </div>

              <button
                onClick={handleTest}
                disabled={testLoading || apiStatus !== 'connected'}
                className="btn-primary w-full py-2.5 text-sm font-mono uppercase tracking-widest flex items-center justify-center gap-2"
              >
                {testLoading ? (
                  <>
                    <svg className="animate-spin w-4 h-4" viewBox="0 0 24 24" fill="none">
                      <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
                      <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
                    </svg>
                    RUNNING TEST...
                  </>
                ) : testResult ? 'RUN TEST AGAIN' : 'RUN TEST QUERY'}
              </button>

              {apiStatus !== 'connected' && (
                <div className="bg-[#d97706]/5 border border-[#d97706]/20 p-3">
                  <div className="text-xs font-mono text-[#d97706]">
                    API server not connected. Launch nvHive, or use terminal fallback: nvh webui
                  </div>
                </div>
              )}

              {testError && (
                <div className="bg-[#dc2626]/5 border border-[#dc2626]/20 p-3">
                  <div className="text-[10px] font-mono text-[#dc2626] uppercase tracking-wider mb-1">Error</div>
                  <div className="text-xs font-mono text-[#dc2626]">{testError}</div>
                  <button
                    onClick={handleTest}
                    className="mt-2 text-xs font-mono text-[#d97706] hover:underline"
                  >
                    Retry
                  </button>
                </div>
              )}

              {testResult && (
                <div className="bg-[#76B900]/5 border border-[#76B900]/30 p-3">
                  <div className="text-[10px] font-mono text-[#76B900] uppercase tracking-wider mb-1">Response</div>
                  <div className="text-sm font-mono text-[#0a0a0a]">{testResult}</div>
                  <div className="mt-2 text-[10px] font-mono text-[#76B900]">TEST PASSED</div>
                </div>
              )}
            </div>
          </div>
        )}

        {/* DONE */}
        {step === 'done' && (
          <div className="space-y-6">
            <div className="text-center space-y-4">
              <div className="w-16 h-16 mx-auto bg-[#76B900] flex items-center justify-center"
                style={{ clipPath: 'polygon(50% 0%, 100% 25%, 100% 75%, 50% 100%, 0% 75%, 0% 25%)' }}>
                <svg className="w-8 h-8 text-black" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={3}>
                  <path strokeLinecap="round" strokeLinejoin="round" d="M4.5 12.75l6 6 9-13.5" />
                </svg>
              </div>
              <div>
                <h2 className="text-xl font-bold text-[#0a0a0a] font-mono">SETUP COMPLETE</h2>
                <p className="text-xs font-mono text-[#76B900] mt-1">Hive AI Command Center is ready</p>
              </div>
            </div>

            {/* Summary */}
            <div className="space-y-2">
              <div className="section-label">Configuration Summary</div>
              <div className="space-y-2">
                {[
                  { label: 'Persistent Home', value: storageStatus?.layout.home ?? 'Not configured', ok: storageReady },
                  { label: 'Local AI', value: ollamaStatus === 'online' ? 'Ollama Running' : 'Not configured', ok: ollamaStatus === 'online' },
                  { label: 'Local Models', value: `${studioModels.filter(model => model.installed).length}/${studioModels.length || 0} installed`, ok: studioModels.some(model => model.installed) },
                  { label: 'AI Studio Packs', value: `${studioPacks.filter(pack => pack.status.installed).length}/${studioPacks.length || 0} installed`, ok: studioPacks.some(pack => pack.status.installed) },
                  { label: 'ComfyUI', value: comfyStatus?.running ? 'Running' : comfyStatus?.installed ? 'Installed' : 'Optional', ok: Boolean(comfyStatus?.installed || comfyStatus?.running), optional: !comfyStatus?.installed && !comfyStatus?.running },
                  { label: 'Hive API', value: apiStatus === 'connected' ? 'Online' : 'Offline', ok: apiStatus === 'connected' },
                  { label: 'Active Advisors', value: configuredProviders.length > 0 ? configuredProviders.join(', ') : 'Optional', ok: configuredProviders.length > 0, optional: configuredProviders.length === 0 },
                ].map(item => (
                  <div key={item.label} className="flex items-center gap-3 px-3 py-2 bg-[#ffffff] border border-[#e5e5e5]">
                    <span className={`w-1.5 h-1.5 flex-shrink-0 ${item.ok ? 'bg-[#76B900]' : item.optional ? 'bg-[#a3a3a3]' : 'bg-[#dc2626]'}`}
                      style={{ clipPath: 'polygon(50% 0%, 100% 50%, 50% 100%, 0% 50%)' }} />
                    <span className="text-[10px] font-mono text-[#737373] uppercase w-32">{item.label}</span>
                    <span className="text-xs font-mono text-[#0a0a0a]">{item.value}</span>
                  </div>
                ))}
              </div>
            </div>

            <div className="bg-[#ffffff] border border-[#e5e5e5] p-4 space-y-3">
              <div>
                <div className="section-label">Launcher Dashboard</div>
                <div className="text-sm font-bold text-[#0a0a0a] mt-1">Open the lab from buttons, not terminal memory</div>
              </div>
              <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-3">
                <div className="border border-[#e5e5e5] bg-[#fafafa] p-3">
                  <div className="flex items-center gap-3">
                    <span className="w-10 h-10 flex items-center justify-center border border-[#e5e5e5] bg-white flex-shrink-0">
                      <BrandLogo id="ollama" />
                    </span>
                    <div className="min-w-0">
                      <div className="text-xs font-bold text-[#0a0a0a]">Local Chat</div>
                      <div className="text-[9px] font-mono text-[#737373] uppercase">{ollamaStatus === 'online' ? 'Ollama online' : 'Uses best available advisor'}</div>
                    </div>
                  </div>
                  <Link href="/query" className="btn-primary block text-center mt-3 px-3 py-2 text-[10px] font-mono uppercase tracking-wider">
                    Open Chat
                  </Link>
                </div>
                <div className="border border-[#e5e5e5] bg-[#fafafa] p-3">
                  <div className="flex items-center gap-3">
                    <span className="w-10 h-10 flex items-center justify-center border border-[#e5e5e5] bg-white flex-shrink-0">
                      <BrandLogo id="comfyui" />
                    </span>
                    <div className="min-w-0">
                      <div className="text-xs font-bold text-[#0a0a0a]">ComfyUI</div>
                      <div className="text-[9px] font-mono text-[#737373] uppercase">{comfyStatus?.running ? 'running' : comfyStatus?.installed ? 'installed' : 'not installed'}</div>
                    </div>
                  </div>
                  {comfyStatus?.running ? (
                    <a href={comfyStatus.url} target="_blank" rel="noreferrer" className="btn-primary block text-center mt-3 px-3 py-2 text-[10px] font-mono uppercase tracking-wider">
                      Open ComfyUI
                    </a>
                  ) : (
                    <button
                      type="button"
                      onClick={comfyStatus?.installed ? handleStartComfyUI : handleInstallComfyUI}
                      disabled={comfyStarting || comfyInstalling || !storageReady}
                      className="btn-primary w-full mt-3 px-3 py-2 text-[10px] font-mono uppercase tracking-wider disabled:opacity-40"
                    >
                      {comfyStatus?.installed ? comfyStarting ? 'Starting' : 'Start ComfyUI' : comfyInstalling ? 'Installing' : 'Install ComfyUI'}
                    </button>
                  )}
                </div>
                {[
                  { id: 'blender-creative', title: 'Blender', logo: 'blender' as BrandLogoId, action: 'Install Blender' },
                  { id: 'godot-engine', title: 'Godot', logo: 'godot' as BrandLogoId, action: 'Install Godot' },
                  { id: 'github-login-helper', title: 'GitHub Workspace', logo: 'github' as BrandLogoId, action: 'Connect GitHub' },
                  { id: 'unreal-engine-helper', title: 'Unreal Helper', logo: 'unreal' as BrandLogoId, action: 'Install Helper' },
                ].map(item => {
                  const pack = studioPacks.find(candidate => candidate.id === item.id);
                  const installed = Boolean(pack?.status.installed);
                  return (
                    <div key={item.id} className="border border-[#e5e5e5] bg-[#fafafa] p-3">
                      <div className="flex items-center gap-3">
                        <span className="w-10 h-10 flex items-center justify-center border border-[#e5e5e5] bg-white flex-shrink-0">
                          <BrandLogo id={item.logo} />
                        </span>
                        <div className="min-w-0">
                          <div className="text-xs font-bold text-[#0a0a0a]">{item.title}</div>
                          <div className="text-[9px] font-mono text-[#737373] uppercase">{installed ? 'launcher ready' : 'optional'}</div>
                        </div>
                      </div>
                      <button
                        type="button"
                        onClick={() => installed ? setStep('studio') : handleInstallStudioPacks([item.id])}
                        disabled={!storageReady || studioInstalling || !pack}
                        className="btn-primary w-full mt-3 px-3 py-2 text-[10px] font-mono uppercase tracking-wider disabled:opacity-40"
                      >
                        {installed ? 'Open Setup' : item.action}
                      </button>
                    </div>
                  );
                })}
              </div>
              <details className="border border-[#e5e5e5] bg-[#fafafa] p-3">
                <summary className="cursor-pointer text-[10px] font-mono text-[#737373] uppercase tracking-wider">
                  Manual command overrides
                </summary>
                <div className="mt-3 space-y-2">
                  {storageStatus && (
                    <>
                      <div className="text-[10px] font-mono text-[#a3a3a3]"># Persist this shell session</div>
                      <div className="text-[10px] font-mono text-[#76B900] break-all">source {storageStatus.env_file}</div>
                    </>
                  )}
                  <div className="text-[10px] font-mono text-[#a3a3a3]"># Rootless all-in-one AI workstation</div>
                  <div className="text-[10px] font-mono text-[#76B900] break-all">
                    {`nvh workstation --home-dir "${storageStatus?.layout.home ?? '$NVH_HOME'}" --all -y`}
                  </div>
                  <div className="text-[10px] font-mono text-[#a3a3a3]"># Launch dashboard</div>
                  <div className="text-[10px] font-mono text-[#76B900]">nvh webui</div>
                </div>
              </details>
            </div>

            <div className="flex gap-3">
              <Link href="/" className="btn-primary flex-1 py-3 text-sm font-mono uppercase tracking-widest text-center">
                GO TO DASHBOARD
              </Link>
              <Link href="/query" className="btn-secondary flex-1 py-3 text-xs font-mono uppercase tracking-widest text-center">
                ASK YOUR FIRST QUESTION
              </Link>
            </div>
          </div>
        )}

        {/* Advanced navigation */}
        {advancedSetupOpen && (
        <div className="flex items-center justify-between mt-8 pt-6 border-t border-[#e5e5e5]">
          <button
            onClick={() => {
              const idx = STEPS.findIndex(s => s.id === step);
              if (idx > 0) setStep(STEPS[idx - 1].id);
            }}
            disabled={step === 'welcome'}
            className="btn-ghost px-4 py-2 text-xs font-mono uppercase tracking-wider disabled:opacity-30"
          >
            &lt; Back
          </button>

          <span className="text-[10px] font-mono text-[#333333]">
            {currentAdvancedGroup.label}: {currentStep.label}
          </span>

          {step !== 'done' ? (
            <button
              onClick={() => {
                if (step === 'storage' && !storageReady) {
                  void handleUseRecommendedStorage();
                  return;
                }
                const idx = STEPS.findIndex(s => s.id === step);
                if (idx < STEPS.length - 1) setStep(STEPS[idx + 1].id);
              }}
              disabled={step === 'storage' && (storageAutopilotBusy || apiDisconnected)}
              className="btn-primary px-6 py-2 text-xs font-mono uppercase tracking-wider disabled:opacity-40"
            >
              {step === 'storage' && !storageReady ? storagePrimaryLabel : 'Next >'}
            </button>
          ) : (
            <Link href="/" className="btn-primary px-6 py-2 text-xs font-mono uppercase tracking-wider">
              Done &gt;
            </Link>
          )}
        </div>
        )}
      </div>
    </div>
  );
}
