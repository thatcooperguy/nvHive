'use client';

import { useState, useEffect, useCallback } from 'react';
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
  getSetupCatalog,
  getSetupBootPreflight,
  getSetupMissionControl,
  getSetupHelper,
  getSetupReceipts,
  repairSetupWorkspace,
  cancelInstallJob,
  getComfyUIStatus,
  getComfyUIExamples,
  getInstallJobs,
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
  SetupAssistantReply,
  SetupCatalogResult,
  SetupHelperReport,
  SetupReceiptsResult,
  MountAutopilotReport,
  StorageStatus,
  StudioPack,
  StudioPackInstallEvent,
  StudioModel,
  StudioModelInstallEvent,
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
  const [apiKeys, setApiKeys] = useState<Record<string, string>>({});
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

  const refreshSetupInventory = useCallback(async (refreshCatalog = false, homeDir?: string) => {
    try {
      const activeHome = homeDir ?? storageStatus?.layout.home;
      const [receipts, catalog, boot, mission] = await Promise.all([
        getSetupReceipts({ limit: 8 }),
        getSetupCatalog(refreshCatalog),
        getSetupBootPreflight(activeHome),
        getSetupMissionControl(activeHome),
      ]);
      setSetupReceipts(receipts);
      setSetupCatalog(catalog);
      setBootPreflight(boot);
      setSetupCompatibility(boot.compatibility);
      setMissionControl(mission);
      setSetupInventoryError(null);
    } catch (err) {
      setSetupInventoryError(err instanceof Error ? err.message : 'Could not load setup inventory');
    }
  }, [storageStatus?.layout.home]);

  useEffect(() => {
    void refreshInstallJobs();
    void refreshSetupInventory(false);
    const timer = window.setInterval(() => {
      void refreshInstallJobs();
    }, 3000);
    return () => window.clearInterval(timer);
  }, [refreshInstallJobs, refreshSetupInventory]);

  useEffect(() => {
    setComfyInstalling(installJobs.some(job => job.kind === 'comfyui-install' && isActiveInstallJob(job)));
    setStudioInstalling(installJobs.some(job => job.kind === 'studio-pack-install' && isActiveInstallJob(job)));
    setModelsInstalling(installJobs.some(job => job.kind === 'studio-model-install' && isActiveInstallJob(job)));
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

  const handleAskAssistant = async () => {
    const question = assistantQuestion.trim();
    if (!question) return;
    setAssistantLoading(true);
    setAssistantError(null);
    try {
      const reply = await askSetupAssistant(question, storageStatus?.layout.home);
      setAssistantReply(reply);
    } catch (err) {
      setAssistantError(err instanceof Error ? err.message : 'Setup helper could not answer');
    } finally {
      setAssistantLoading(false);
    }
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
    try {
      await repairSetupWorkspace(storageStatus?.layout.home);
      await Promise.all([
        refreshSetupInventory(false, storageStatus?.layout.home),
        refreshSetupHelper(storageStatus?.layout.home),
        refreshComfyUI(),
        refreshInstallJobs(),
      ]);
      setSetupInventoryError(null);
    } catch (err) {
      setSetupInventoryError(err instanceof Error ? err.message : 'Workspace repair failed');
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

        if (!status.ok || status.configured_by === 'default') {
          try {
            const report = await getMountAutopilot(20);
            if (cancelled) return;
            setMountAutopilot(report);
            if (report.recommended) {
              setStorageHomeInput(report.recommended.recommended_home);
            }
            if (shouldAutoActivateStorage(status, report)) {
              setMountActivating(true);
              try {
                const activated = await activateMountAutopilot(report.recommended?.recommended_home, 20);
                if (cancelled) return;
                setStorageStatus(activated.storage);
                setStorageHomeInput(activated.storage.layout.home);
                setMountAutopilot(activated.mount_autopilot);
                setWizardBuildMessage('nvWizard found the large writable block volume and prepared it for models, ComfyUI, Blender, and agents.');
                void refreshSetupHelper(activated.storage.layout.home);
                void refreshSetupInventory(false, activated.storage.layout.home);
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
          setStorageError('Storage preflight is unavailable. Start the API with nvh serve.');
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
  }, [refreshSetupHelper, refreshSetupInventory]);

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
        min_free_gb: 20,
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
      return expandStudioPackGroups(['rootless-ollama', 'agent-lab'], packs, bundles);
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
      const activated = await activateMountAutopilot(recommendedHome, 20);
      setStorageStatus(activated.storage);
      setStorageHomeInput(activated.storage.layout.home);
      setMountAutopilot(activated.mount_autopilot);
      setWizardBuildMessage('nvWizard prepared the persistent block volume. The big model treasure now lives somewhere that survives reboot.');
      await Promise.allSettled([
        refreshStudioModels(),
        refreshStudioPacks(),
        refreshComfyUI(),
        refreshSetupHelper(activated.storage.layout.home),
        refreshSetupInventory(false, activated.storage.layout.home),
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

  const buildStudioPacks = (packIds: string[]) => new Promise<void>((resolve, reject) => {
    if (packIds.length === 0) {
      resolve();
      return;
    }

    setStudioInstalling(true);
    setStudioError(null);
    setStudioEvents([]);

    installStudioPacksStream(
      { pack_ids: packIds, force_update: false },
      {
        onJob: job => mergeInstallJob(job),
        onStatus: job => mergeInstallJob(job),
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
          void refreshStudioPacks();
          void refreshInstallJobs();
          void refreshSetupInventory(false);
          void refreshSetupHelper(storageStatus?.layout.home);
          resolve();
        },
        onError: error => {
          setStudioError(error);
          setStudioInstalling(false);
          void refreshInstallJobs();
          void refreshSetupHelper(storageStatus?.layout.home);
          reject(new Error(error));
        },
      }
    );
  });

  const buildStudioModels = (modelIds: string[]) => new Promise<void>((resolve, reject) => {
    if (modelIds.length === 0) {
      resolve();
      return;
    }

    setModelsInstalling(true);
    setModelError(null);
    setModelEvents([]);

    installStudioModelsStream(
      { model_ids: modelIds, force_update: false },
      {
        onJob: job => mergeInstallJob(job),
        onStatus: job => mergeInstallJob(job),
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
          void refreshStudioModels();
          void refreshInstallJobs();
          void refreshSetupInventory(false);
          void refreshSetupHelper(storageStatus?.layout.home);
          resolve();
        },
        onError: error => {
          setModelError(error);
          setModelsInstalling(false);
          void refreshInstallJobs();
          void refreshSetupHelper(storageStatus?.layout.home);
          reject(new Error(error));
        },
      }
    );
  });

  const buildComfyUI = () => new Promise<void>((resolve, reject) => {
    setComfyInstalling(true);
    setComfyError(null);
    setComfyEvents([]);

    installComfyUIStream(
      { torch_profile: recommendedTorchProfile, force_update: false },
      {
        onJob: job => mergeInstallJob(job),
        onStatus: job => mergeInstallJob(job),
        onEvent: event => {
          setComfyEvents(prev => [...prev.slice(-8), event]);
          if (event.status_snapshot) {
            setComfyStatus(event.status_snapshot);
          }
        },
        onComplete: event => {
          setComfyEvents(prev => [...prev.slice(-8), event]);
          setComfyInstalling(false);
          void refreshComfyUI();
          void refreshInstallJobs();
          void refreshSetupInventory(false);
          void refreshSetupHelper(storageStatus?.layout.home);
          resolve();
        },
        onError: error => {
          setComfyError(error);
          setComfyInstalling(false);
          void refreshInstallJobs();
          void refreshSetupHelper(storageStatus?.layout.home);
          reject(new Error(error));
        },
      }
    );
  });

  const handleBuildWizardProfile = async (profile: WizardProfile) => {
    if (activeWizardBuild || studioInstalling || modelsInstalling || comfyInstalling || apiDisconnected) return;

    setActiveWizardBuild(profile);
    setWizardBuildMessage('nvWizard is checking the mission catalog, hardware, and persistent storage.');

    try {
      const catalog = await ensureWizardCatalogReady();
      const modelIds = wizardProfileModelIds(profile, catalog.models);
      const packIds = wizardProfilePackIds(profile, catalog.packs, catalog.bundles);
      const exampleIds = wizardProfileExampleIds(catalog.examples, catalog.vramGb);
      const comfyNodePackIds = packIds.filter(packId => packId === 'comfyui-power-nodes');
      const firstPackIds = wizardProfileNeedsComfy(profile)
        ? packIds.filter(packId => packId !== 'comfyui-power-nodes')
        : packIds;

      setSelectedStudioModels(new Set(modelIds));
      setSelectedStudioPacks(new Set(packIds));
      setSelectedComfyExamples(new Set(exampleIds));

      if (!storageReady) {
        setWizardBuildMessage('nvWizard is finding the persistent block storage first, then it will build the mission there.');
        const detectedStorage = await handleUseRecommendedStorage();
        if (!detectedStorage?.ok || detectedStorage.configured_by === 'default') {
          setWizardBuildMessage('nvWizard could not prove the persistent storage path yet. Advanced Details has the manual override if the host is unusual.');
          setAdvancedSetupOpen(true);
          return;
        }
      }

      setWizardBuildMessage('nvWizard picked the beginner-safe defaults and is building the mission in dependency order.');
      setStep(wizardProfileNeedsComfy(profile) ? 'comfyui' : 'studio');

      if (firstPackIds.length > 0) {
        setWizardBuildMessage('Installing rootless runtimes and mission tools on the persistent drive.');
        await buildStudioPacks(firstPackIds);
      }

      if (wizardProfileNeedsComfy(profile)) {
        setWizardBuildMessage('Installing ComfyUI with the NVIDIA-ready PyTorch profile.');
        await buildComfyUI();
        if (comfyNodePackIds.length > 0) {
          setWizardBuildMessage('Installing ComfyUI power nodes after the base app is ready.');
          await buildStudioPacks(comfyNodePackIds);
        }
        if (exampleIds.length > 0) {
          setWizardBuildMessage('Saving the starter workflow model plan beside ComfyUI.');
          try {
            await saveComfyUIModelPlan(exampleIds);
          } catch {
            // The mission can still run; the user can save the plan again from the ComfyUI step.
          }
        }
      }

      if (modelIds.length > 0) {
        setWizardBuildMessage('Downloading the local model queue that fits this GPU profile.');
        await buildStudioModels(modelIds);
      }

      setWizardBuildMessage('Mission build complete. Try the smoke test, then launch the tools.');
      setStep('test');
    } catch (err) {
      setWizardBuildMessage(err instanceof Error ? `nvWizard paused: ${err.message}` : 'nvWizard paused: setup needs attention.');
      setAdvancedSetupOpen(true);
    } finally {
      setActiveWizardBuild(null);
      void refreshInstallJobs();
      void refreshSetupInventory(false);
      void refreshSetupHelper(storageStatus?.layout.home);
    }
  };

  const currentStepIdx = STEPS.findIndex(s => s.id === step);
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
      : 'Auto-Find Storage';
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
  const detectedTorchProfile = setupCompatibility?.recommended_torch_profile
    ?? setupHelper?.compatibility?.recommended_torch_profile
    ?? 'nvidia-cu121';
  const recommendedTorchProfile: ComfyUITorchProfile = (
    ['nvidia-cu130', 'nvidia-cu121', 'cpu', 'skip'].includes(detectedTorchProfile)
      ? detectedTorchProfile
      : 'nvidia-cu121'
  ) as ComfyUITorchProfile;
  const setupConcernCount =
    (setupInventoryError ? 1 : 0) +
    (setupHelperError ? 1 : 0) +
    unhealthyReceiptCount +
    compatibilityIssueCount +
    bootChangeCount;
  const showAdvancedSetup = advancedSetupOpen;
  const showInstallJobs = activeInstallJobs.length > 0 || (advancedSetupOpen && (visibleInstallJobs.length > 0 || jobsError));
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
  const recommendedHardwareModels = studioModels
    .filter(model => model.recommended)
    .sort((a, b) => a.priority - b.priority)
    .slice(0, 4);
  const visibleHardwareModels = (
    recommendedHardwareModels.length > 0
      ? recommendedHardwareModels
      : studioModels
          .filter(model => model.fits_vram)
          .sort((a, b) => a.priority - b.priority)
          .slice(0, 4)
  );
  const hardwareName = gpuInfo?.gpus?.[0]?.name ?? 'GPU scan pending';
  const hardwareVramLabel = detectedModelVram ? `${detectedModelVram} GB VRAM` : 'VRAM scan pending';
  const gpuDetectionStatus = gpuInfo?.detection?.status ?? 'checking';
  const gpuDetectionIssue = gpuInfo?.detection?.issues?.[0]?.message ?? '';
  const visibleHardwareModelIds = visibleHardwareModels.map(model => model.id);
  const githubPack = studioPacks.find(pack => pack.id === 'github-login-helper') ?? null;
  const gameEnginePacks = studioPacks.filter(pack => ['godot-engine', 'unity-hub-helper', 'unreal-engine-helper'].includes(pack.id));
  const modelPickPreview = visibleHardwareModels.slice(0, 3);
  const softwareHighlights: Array<{ id: string; label: string; logo: BrandLogoId; sub: string; tone: string }> = [
    { id: 'openclaw', label: 'OpenClaw', logo: 'openclaw', sub: studioPacks.find(pack => pack.id === 'openclaw-agent')?.status.installed ? 'Ready' : 'Agent', tone: 'bg-white border-[#e5e5e5]' },
    { id: 'nemoclaw', label: 'NemoClaw', logo: 'nvidia', sub: studioPacks.find(pack => pack.id === 'nemoclaw-sandbox')?.status.installed ? 'Ready' : 'Guarded', tone: 'bg-white border-[#76B900]/40' },
    { id: 'comfyui', label: 'ComfyUI', logo: 'comfyui', sub: comfyStatus?.installed ? 'Ready' : 'Images', tone: 'bg-white border-[#e5e5e5]' },
    { id: 'blender', label: 'Blender', logo: 'blender', sub: studioPacks.find(pack => pack.id === 'blender-creative')?.status.installed ? 'Ready' : '3D', tone: 'bg-white border-[#e5e5e5]' },
    { id: 'audacity', label: 'Audacity', logo: 'audacity', sub: studioPacks.find(pack => pack.id === 'music-daw-helper')?.status.installed ? 'Ready' : 'Audio', tone: 'bg-white border-[#e5e5e5]' },
    { id: 'lmms', label: 'LMMS', logo: 'lmms', sub: studioPacks.find(pack => pack.id === 'music-daw-helper')?.status.installed ? 'Ready' : 'Beats', tone: 'bg-white border-[#e5e5e5]' },
    { id: 'godot', label: 'Godot', logo: 'godot', sub: studioPacks.find(pack => pack.id === 'godot-engine')?.status.installed ? 'Ready' : 'Games', tone: 'bg-white border-[#e5e5e5]' },
    { id: 'github', label: 'GitHub', logo: 'github', sub: githubPack?.status.installed ? 'Ready' : 'Repos', tone: 'bg-white border-[#e5e5e5]' },
    { id: 'unity', label: 'Unity', logo: 'unity', sub: 'Helper', tone: 'bg-white border-[#e5e5e5]' },
    { id: 'unreal', label: 'Unreal', logo: 'unreal', sub: 'Helper', tone: 'bg-white border-[#e5e5e5]' },
  ];
  const repoAndGameHighlights = softwareHighlights.filter(item => ['github', 'godot', 'unity', 'unreal'].includes(item.id));
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
      description: catalogText('student', 'description', 'Chat, research, homework, coding help, and starter local models.'),
      label: 'Recommended',
      outcome: 'A practical local AI desk for classes, projects, notes, and first model experiments.',
      includes: ['Rootless Ollama', 'Starter models', 'Agent lab', 'GitHub connect'],
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
    student: 'Local AI for classwork, coding, research, and first ComfyUI experiments.',
    creator: 'ComfyUI, Blender, and creative helpers for images, 3D, and video workflows.',
    game: 'Game engine helpers, Blender assets, GitHub repos, and mod workspace tools.',
    music: 'AI music generation, stem separation, transcription, and audio editor helpers.',
  };
  const selectedProfile = missionProfiles.find(profile => profile.id === selectedWizardProfile) ?? missionProfiles[0];
  const selectedProfilePackIds = wizardProfilePackIds(selectedProfile.id);
  const selectedProfileModelIds = wizardProfileModelIds(selectedProfile.id);
  const selectedProfileDiskGb = diskForPackIds(selectedProfilePackIds) + diskForModelIds(selectedProfileModelIds);
  const selectedProfilePacks = studioPacks.filter(pack => selectedProfilePackIds.includes(pack.id));
  const selectedProfileModels = studioModels.filter(model => selectedProfileModelIds.includes(model.id));
  const pythonFact = setupCompatibility?.facts.find(fact => fact.id.toLowerCase().includes('python') || fact.label.toLowerCase().includes('python'));
  const nodeFact = setupCompatibility?.facts.find(fact => fact.id.toLowerCase().includes('node') || fact.label.toLowerCase().includes('node'));
  const systemCheckItems: Array<{ label: string; value: string; state: SetupCheckState }> = [
    {
      label: 'Storage',
      value: storageReady ? (storageFreeGb === null ? 'persistent ready' : `${storageFreeGb} GB free`) : storageBeginnerLabel,
      state: storageReady ? 'ready' : storageAutopilotBusy ? 'checking' : 'fix',
    },
    {
      label: 'GPU / CUDA',
      value: gpuLoading
        ? 'scanning'
        : gpuInfo?.gpus?.length
          ? `${gpuInfo.gpus[0].name} / CUDA ${gpuInfo.gpus[0].cuda_version}`
          : 'CPU fallback',
      state: gpuLoading ? 'checking' : gpuInfo?.gpus?.length ? 'ready' : 'warn',
    },
    {
      label: 'Python env',
      value: pythonFact?.value ?? recommendedTorchProfile,
      state: setupCompatibility ? compatibilityBlockedCount > 0 ? 'fix' : compatibilityIssueCount > 0 ? 'warn' : 'ready' : 'checking',
    },
    {
      label: 'Node',
      value: nodeFact?.value ?? (setupCompatibility ? 'checked' : 'pending'),
      state: nodeFact?.status === 'blocked' ? 'fix' : nodeFact?.status === 'warning' || nodeFact?.status === 'fixable' ? 'warn' : setupCompatibility ? 'ready' : 'checking',
    },
    {
      label: 'GitHub',
      value: githubPack?.status.installed ? 'helper ready' : 'optional login',
      state: githubPack?.status.installed ? 'ready' : 'warn',
    },
    {
      label: 'Health',
      value: apiStatus === 'checking'
        ? 'checking'
        : apiDisconnected
          ? 'API offline'
          : setupConcernCount ? `${setupConcernCount} item${setupConcernCount === 1 ? '' : 's'}` : 'clear',
      state: apiStatus === 'checking' ? 'checking' : apiDisconnected ? 'fix' : setupConcernCount ? 'fix' : 'ready',
    },
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
    if (actionId.startsWith('repair-receipt:')) return !storageReady ? 'Auto Storage' : 'Repair';
    if (actionId === 'storage') return storageAutopilotBusy ? 'Finding' : 'Auto Storage';
    if (!storageReady) return storageAutopilotBusy ? 'Finding' : 'Auto Storage';
    if (actionId === 'starter-models') return modelsInstalling ? 'Downloading' : 'Download';
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
          <button
            type="button"
            onClick={() => setAdvancedSetupOpen(prev => !prev)}
            className="btn-ghost px-3 py-2 text-[10px] font-mono uppercase tracking-wider sm:flex-shrink-0"
          >
            {advancedSetupOpen ? 'Hide Details' : 'Advanced Details'}
          </button>
        </div>
        {advancedSetupOpen && (
          <div className="mt-3 flex items-center gap-0 overflow-x-auto pt-1">
            {STEPS.map((s, i) => (
              <div key={s.id} className="flex items-center flex-shrink-0">
                <button
                  onClick={() => setStep(s.id)}
                  className={`flex items-center gap-1.5 text-[10px] font-mono uppercase tracking-wider transition-all ${
                    s.id === step
                      ? 'text-[#76B900]'
                      : i < currentStepIdx
                      ? 'text-[#a3a3a3] hover:text-[#76B900]'
                      : 'text-[#333333]'
                  }`}
                >
                  <span className={`w-5 h-5 flex items-center justify-center text-[10px] font-bold border ${
                    s.id === step
                      ? 'border-[#76B900] bg-[#76B900] text-black'
                      : i < currentStepIdx
                      ? 'border-[#76B900]/40 text-[#76B900]'
                      : 'border-[#d4d4d4] text-[#333333]'
                  }`}>
                    {i < currentStepIdx ? 'OK' : s.num}
                  </span>
                  <span>{s.label}</span>
                </button>
                {i < STEPS.length - 1 && (
                  <div className={`w-6 h-px mx-2 ${i < currentStepIdx ? 'bg-[#76B900]/40' : 'bg-[#e5e5e5]'}`} />
                )}
              </div>
            ))}
          </div>
        )}
      </div>

      {step !== 'welcome' && (
        <div className="border border-[#76B900]/40 bg-[#f7fdf0] p-4 space-y-4">
          <div className="flex flex-col lg:flex-row lg:items-start lg:justify-between gap-4">
            <div className="min-w-0">
              <div className="section-label">Beginner Mode</div>
              <div className="text-lg font-mono font-bold text-[#0a0a0a] mt-1">
                {storageReady ? 'Start with the recommended lab' : 'nvWizard is finding persistent storage'}
              </div>
              <div className="text-xs font-mono text-[#525252] mt-2 leading-relaxed max-w-2xl">
                nvWizard checks storage, GPU, CUDA, Python, ComfyUI, models, and install receipts, then recommends the next safe action. Manual commands stay available under Advanced Details.
              </div>
              {topHelperAction && (
                <div className="mt-3 border border-[#76B900]/20 bg-white p-3">
                  <div className="text-[10px] font-mono text-[#737373] uppercase">Recommended next</div>
                  <div className="text-xs font-mono font-bold text-[#0a0a0a] mt-1">{topHelperAction.title}</div>
                  <div className="text-[10px] font-mono text-[#525252] mt-1 leading-relaxed">{topHelperAction.reason}</div>
                </div>
              )}
            </div>
            <div className="flex flex-col sm:flex-row lg:flex-col gap-2 lg:min-w-[190px]">
              <button
                type="button"
                onClick={() => {
                  if (!storageReady) {
                    void handleUseRecommendedStorage();
                    return;
                  }
                  if (topHelperAction && !helperActionDisabled(topHelperAction.id)) {
                    runHelperAction(topHelperAction.id);
                    return;
                  }
                  applyWizardProfile('student');
                }}
                disabled={apiDisconnected || Boolean(storageReady && topHelperAction && helperActionDisabled(topHelperAction.id))}
                className="btn-primary px-4 py-2 text-xs font-mono uppercase tracking-wider disabled:opacity-40"
              >
                {apiDisconnected
                  ? 'API Offline'
                  : topHelperAction ? helperActionLabel(topHelperAction.id) : 'Start AI Starter'}
              </button>
              <button
                type="button"
                onClick={() => void handleRepairWorkspace()}
                disabled={workspaceRepairing}
                className="btn-ghost px-4 py-2 text-xs font-mono uppercase tracking-wider disabled:opacity-40"
              >
                {workspaceRepairing ? 'Repairing' : 'Fix My Setup'}
              </button>
              <button
                type="button"
                onClick={() => setAdvancedSetupOpen(prev => !prev)}
                className="btn-ghost px-4 py-2 text-xs font-mono uppercase tracking-wider"
              >
                {advancedSetupOpen ? 'Hide Details' : 'Advanced Details'}
              </button>
            </div>
          </div>
          <div className="grid grid-cols-2 sm:grid-cols-4 gap-2">
            <div className="border border-[#76B900]/20 bg-white p-2">
              <div className="text-[9px] font-mono text-[#737373] uppercase">Storage</div>
              <div className={`text-[10px] font-mono mt-1 ${storageReady ? 'text-[#76B900]' : 'text-[#d97706]'}`}>
                {storageBeginnerLabel}
              </div>
            </div>
            <div className="border border-[#76B900]/20 bg-white p-2">
              <div className="text-[9px] font-mono text-[#737373] uppercase">Checks</div>
              <div className={`text-[10px] font-mono mt-1 ${setupConcernCount ? 'text-[#d97706]' : 'text-[#76B900]'}`}>
                {setupConcernCount ? `${setupConcernCount} to review` : 'clear'}
              </div>
            </div>
            <div className="border border-[#76B900]/20 bg-white p-2">
              <div className="text-[9px] font-mono text-[#737373] uppercase">Jobs</div>
              <div className={`text-[10px] font-mono mt-1 ${activeInstallJobs.length ? 'text-[#d97706]' : 'text-[#76B900]'}`}>
                {activeInstallJobs.length ? `${activeInstallJobs.length} running` : 'idle'}
              </div>
            </div>
            <div className="border border-[#76B900]/20 bg-white p-2">
              <div className="text-[9px] font-mono text-[#737373] uppercase">API</div>
              <div className={`text-[10px] font-mono mt-1 ${apiStatus === 'connected' ? 'text-[#76B900]' : 'text-[#d97706]'}`}>
                {apiStatus === 'connected' ? 'online' : 'checking'}
              </div>
            </div>
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
                  ? `${activeInstallJobs.length} active job${activeInstallJobs.length === 1 ? '' : 's'} running from persistent NVH_HOME`
                  : 'Recent setup jobs are saved under NVH_HOME/jobs'}
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
            </div>
          </div>
          {setupInventoryError && (
            <div className="bg-[#dc2626]/5 border border-[#dc2626]/20 p-2 text-[10px] font-mono text-[#dc2626]">
              {setupInventoryError}
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
              <div className="section-label">nvWizard Troubleshooting</div>
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
                      Setup guidance from jobs, receipts, storage, and catalog state
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
                    placeholder="What is blocked? Why did ComfyUI fail?"
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

            <div className="border border-[#e5e5e5] bg-white p-3">
              <div className="flex flex-col lg:flex-row lg:items-center lg:justify-between gap-3">
                <div>
                  <div className="section-label">System Check</div>
                  <div className="text-sm font-bold text-[#0a0a0a] mt-1">
                    {setupConcernCount ? `${setupConcernCount} item${setupConcernCount === 1 ? '' : 's'} need attention` : 'Ready for rootless installs'}
                  </div>
                </div>
                {topHelperAction && (
                  <button
                    type="button"
                    onClick={() => runHelperAction(topHelperAction.id)}
                    disabled={helperActionDisabled(topHelperAction.id)}
                    className="btn-primary px-3 py-2 text-[10px] font-mono uppercase tracking-wider disabled:opacity-40"
                  >
                    {helperActionLabel(topHelperAction.id)}
                  </button>
                )}
              </div>
              <div className="mt-3 grid grid-cols-2 md:grid-cols-3 xl:grid-cols-6 gap-2">
                {systemCheckItems.map(item => {
                  const tone = CHECK_TONES[item.state];
                  return (
                    <div key={item.label} className={`border ${tone.border} ${tone.bg} p-2 min-w-0`}>
                      <div className="flex items-center justify-between gap-2">
                        <span className="text-[9px] font-mono text-[#737373] uppercase truncate">{item.label}</span>
                        <span className={`w-1.5 h-1.5 flex-shrink-0 ${tone.dot}`} style={{ clipPath: 'polygon(50% 0%, 100% 50%, 50% 100%, 0% 50%)' }} />
                      </div>
                      <div className={`text-[10px] font-mono mt-1 truncate ${tone.text}`}>{item.value}</div>
                    </div>
                  );
                })}
              </div>
            </div>

            {advancedSetupOpen && (
            <div className="grid grid-cols-1 xl:grid-cols-3 gap-3">
              <div className={`border bg-white p-4 ${storageReady ? 'border-[#76B900]/30' : 'border-[#d97706]/30'}`}>
                <div className="flex items-start gap-3">
                  <span className={`w-12 h-12 flex items-center justify-center border flex-shrink-0 ${storageReady ? 'bg-white border-[#76B900]/50' : 'bg-white border-[#d97706]/30'}`}>
                    <BrandLogo id="nvidia" className="w-8 h-8" />
                  </span>
                  <div className="min-w-0 flex-1">
                    <div className="section-label">Storage Autopilot</div>
                    <div className="text-sm font-bold text-[#0a0a0a] mt-1">
                      {storageReady ? 'Persistent home ready' : storageAutopilotBusy ? 'Scanning storage' : 'Auto-find storage'}
                    </div>
                    <div className="text-[10px] font-mono text-[#737373] mt-1 truncate">
                      {storageReady
                        ? storageFreeGb === null ? 'Space unknown' : `${storageFreeGb} GB free`
                        : mountRecommendation?.recommended_home ?? 'Large writable block mount'}
                    </div>
                  </div>
                </div>
                <button
                  type="button"
                  onClick={() => void handleUseRecommendedStorage()}
                  disabled={storageReady || storageAutopilotBusy || apiDisconnected}
                  className="btn-primary w-full mt-3 px-3 py-2 text-[10px] font-mono uppercase tracking-wider disabled:opacity-40"
                >
                  {storageReady ? 'Storage Ready' : apiDisconnected ? 'API Offline' : storageAutopilotBusy ? 'Finding' : 'Run Auto-Detect'}
                </button>
                {advancedSetupOpen && (
                  <div className="mt-3 space-y-2">
                    {!storageReady && mountRecommendation && (
                      <div className="text-[10px] font-mono text-[#525252] break-all border border-[#e5e5e5] bg-[#fafafa] p-2">
                        {mountRecommendation.recommended_home}
                      </div>
                    )}
                    {!storageReady && (
                      <div className="flex flex-col gap-2">
                        <input
                          type="text"
                          value={storageHomeInput}
                          onChange={e => setStorageHomeInput(e.target.value)}
                          placeholder="/mnt/persist/nvhive"
                          className="input-base px-3 py-2 text-xs font-mono"
                          spellCheck={false}
                        />
                        <button
                          type="button"
                          onClick={handleConfigureStorage}
                          disabled={storageSaving}
                          className="btn-ghost px-3 py-2 text-[10px] font-mono uppercase tracking-wider disabled:opacity-40"
                        >
                          {storageSaving ? 'Checking' : 'Manual Override'}
                        </button>
                      </div>
                    )}
                  </div>
                )}
                {(storageError || storageStatus?.warnings?.length) && (
                  <div className="mt-3 space-y-1">
                    {storageError && <div className="text-[10px] font-mono text-[#dc2626]">{storageError}</div>}
                    {storageStatus?.warnings.map(warning => (
                      <div key={warning} className="text-[10px] font-mono text-[#d97706]">{warning}</div>
                    ))}
                  </div>
                )}
              </div>

              <div className="border border-[#e5e5e5] bg-white p-4">
                <div className="flex items-start gap-3">
                  <span className="w-12 h-12 flex items-center justify-center border bg-white border-[#e5e5e5] flex-shrink-0">
                    <BrandLogo id="ollama" className="w-8 h-8" />
                  </span>
                  <div className="min-w-0 flex-1">
                    <div className="section-label">LLM Picks</div>
                    <div className="text-sm font-bold text-[#0a0a0a] mt-1 truncate">{hardwareName}</div>
                    <div className="text-[10px] font-mono text-[#737373] mt-1">{hardwareVramLabel}</div>
                  </div>
                </div>
                <div className="mt-3 space-y-2">
                  {modelPickPreview.length > 0 ? modelPickPreview.map(model => (
                    <div key={model.id} className="flex items-center justify-between gap-2 border border-[#e5e5e5] bg-[#fafafa] px-2 py-2">
                      <span className="text-xs font-bold text-[#0a0a0a] truncate">{model.title}</span>
                      <span className="text-[9px] font-mono text-[#76B900] flex-shrink-0">{model.recommended_vram_gb}GB+</span>
                    </div>
                  )) : (
                    <div className="border border-[#e5e5e5] bg-[#fafafa] p-3 text-xs text-[#737373]">
                      Waiting for GPU scan.
                    </div>
                  )}
                </div>
                <button
                  type="button"
                  onClick={() => {
                    if (!storageReady) {
                      void handleUseRecommendedStorage();
                      return;
                    }
                    if (visibleHardwareModelIds.length > 0) handleInstallStudioModels(visibleHardwareModelIds);
                  }}
                  disabled={apiDisconnected || storageAutopilotBusy || anyInstallRunning || modelsInstalling || (storageReady && visibleHardwareModelIds.length === 0)}
                  className="btn-primary w-full mt-3 px-3 py-2 text-[10px] font-mono uppercase tracking-wider disabled:opacity-40"
                >
                  {!storageReady ? storagePrimaryLabel : modelsInstalling ? 'Downloading' : 'Download Picks'}
                </button>
              </div>

              <div className="border border-[#e5e5e5] bg-white p-4">
                <div className="flex items-center justify-between gap-2">
                  <div>
                    <div className="section-label">Connect & Build</div>
                    <div className="text-sm font-bold text-[#0a0a0a] mt-1">Repos and game engines</div>
                  </div>
                  <span className="text-[9px] font-mono text-[#737373] uppercase">{repoAndGameHighlights.length} tools</span>
                </div>
                <div className="mt-3 grid grid-cols-2 gap-2">
                  {repoAndGameHighlights.map(item => (
                    <div key={item.id} className="border border-[#e5e5e5] bg-[#fafafa] p-2 min-w-0">
                      <div className="flex items-center gap-2">
                        <span className={`w-9 h-9 flex items-center justify-center border flex-shrink-0 ${item.tone}`}>
                          <BrandLogo id={item.logo} className="w-6 h-6" />
                        </span>
                        <span className="min-w-0">
                          <span className="block text-[10px] font-bold text-[#0a0a0a] truncate">{item.label}</span>
                          <span className="block text-[9px] font-mono text-[#737373] uppercase truncate">{item.sub}</span>
                        </span>
                      </div>
                    </div>
                  ))}
                </div>
                <div className="grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-1 gap-2 mt-3">
                  <button
                    type="button"
                    onClick={() => {
                      if (!storageReady) {
                        void handleUseRecommendedStorage();
                        return;
                      }
                      handleInstallStudioPacks(['github-login-helper']);
                    }}
                    disabled={apiDisconnected || storageAutopilotBusy || anyInstallRunning || studioInstalling || !githubPack}
                    className="btn-primary px-3 py-2 text-[10px] font-mono uppercase tracking-wider disabled:opacity-40"
                  >
                    {!storageReady ? storagePrimaryLabel : githubPack?.status.installed ? 'Refresh GitHub' : 'Connect GitHub'}
                  </button>
                  <button
                    type="button"
                    onClick={() => {
                      if (!storageReady) {
                        void handleUseRecommendedStorage();
                        return;
                      }
                      handleInstallStudioPacks(['game']);
                    }}
                    disabled={apiDisconnected || storageAutopilotBusy || anyInstallRunning || studioInstalling || gameEnginePacks.length === 0}
                    className="btn-ghost px-3 py-2 text-[10px] font-mono uppercase tracking-wider disabled:opacity-40"
                  >
                    {!storageReady ? 'Auto-Find Storage' : 'Game Engines'}
                  </button>
                </div>
              </div>
            </div>
            )}

            <div className="space-y-3">
              <div className="flex items-center justify-between gap-2">
                <div className="section-label">Install Options</div>
              </div>
              {advancedSetupOpen && (
              <div className="hidden sm:block border border-[#76B900]/30 bg-[#f7fdf0] p-4">
                <div className="flex flex-col xl:flex-row xl:items-start xl:justify-between gap-4">
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-3">
                      <span className="w-12 h-12 flex items-center justify-center border border-[#76B900]/30 bg-white flex-shrink-0">
                        <div className="grid grid-cols-2 gap-0.5">
                          {selectedProfile.logos.slice(0, 4).map(logo => (
                            <BrandLogo key={logo} id={logo} className="w-5 h-5" />
                          ))}
                        </div>
                      </span>
                      <div className="min-w-0">
                        <div className="text-[10px] font-mono text-[#76B900] uppercase tracking-wider">Mission Summary</div>
                        <div className="text-base font-bold text-[#0a0a0a] leading-tight">{selectedProfile.title}</div>
                        <div className="text-xs text-[#525252] mt-1">{selectedProfile.outcome}</div>
                      </div>
                    </div>
                    <div className="mt-3 grid grid-cols-2 lg:grid-cols-4 gap-2">
                      <div className="border border-[#d8e8c3] bg-white/80 p-2 min-w-0">
                        <div className="text-[9px] font-mono text-[#737373] uppercase">Storage</div>
                        <div className={`text-[10px] font-mono mt-1 truncate ${storageReady ? 'text-[#76B900]' : 'text-[#d97706]'}`}>
                          {storageReady ? storageStatus?.layout.home ?? 'persistent ready' : 'auto-detect first'}
                        </div>
                      </div>
                      <div className="border border-[#d8e8c3] bg-white/80 p-2 min-w-0">
                        <div className="text-[9px] font-mono text-[#737373] uppercase">GPU</div>
                        <div className="text-[10px] font-mono text-[#0a0a0a] mt-1 truncate">{hardwareName}</div>
                      </div>
                      <div className="border border-[#d8e8c3] bg-white/80 p-2 min-w-0">
                        <div className="text-[9px] font-mono text-[#737373] uppercase">Disk</div>
                        <div className="text-[10px] font-mono text-[#0a0a0a] mt-1">
                          {hasCatalogSizing ? `~${Math.max(0, selectedProfileDiskGb).toFixed(1)} GB` : 'after check'}
                        </div>
                      </div>
                      <div className="border border-[#d8e8c3] bg-white/80 p-2 min-w-0">
                        <div className="text-[9px] font-mono text-[#737373] uppercase">Selected</div>
                        <div className="text-[10px] font-mono text-[#0a0a0a] mt-1">
                          {selectedProfilePacks.length} apps / {selectedProfileModels.length} models
                        </div>
                      </div>
                    </div>
                    <div className="mt-3 flex flex-wrap gap-1">
                      {[...selectedProfilePacks.slice(0, 4).map(pack => pack.title), ...selectedProfileModels.slice(0, 3).map(model => model.title)].map(item => (
                        <span key={item} className="text-[9px] font-mono text-[#525252] bg-white border border-[#d8e8c3] px-1.5 py-0.5">
                          {item}
                        </span>
                      ))}
                    </div>
                  </div>
                  <div className="flex flex-col gap-2 xl:w-44">
                    <button
                      type="button"
                      onClick={() => void handleBuildWizardProfile(selectedProfile.id)}
                      disabled={anyInstallRunning || apiDisconnected}
                      className="btn-primary px-4 py-3 text-xs font-mono uppercase tracking-wider disabled:opacity-40"
                    >
                      {apiDisconnected
                        ? 'API Offline'
                        : activeWizardBuild === selectedProfile.id ? 'Installing' : 'Install Mission'}
                    </button>
                    <button
                      type="button"
                      onClick={() => {
                        if (!storageReady) {
                          void handleUseRecommendedStorage().then(status => {
                            if (status?.ok && status.configured_by !== 'default') {
                              setAdvancedSetupOpen(true);
                              applyWizardProfile(selectedProfile.id);
                            } else {
                              setAdvancedSetupOpen(true);
                            }
                          });
                          return;
                        }
                        setAdvancedSetupOpen(true);
                        applyWizardProfile(selectedProfile.id);
                      }}
                      disabled={!profilesReady || Boolean(activeWizardBuild) || storageAutopilotBusy || apiDisconnected}
                      className="btn-ghost px-4 py-3 text-xs font-mono uppercase tracking-wider disabled:opacity-40"
                    >
                      Customize
                    </button>
                  </div>
                </div>
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
                                setAdvancedSetupOpen(true);
                                applyWizardProfile(profile.id);
                                return;
                              }
                              void handleBuildWizardProfile(profile.id);
                            }}
                            disabled={!profileInstalled && (anyInstallRunning || apiDisconnected)}
                            className={`${profileInstalled ? 'btn-ghost' : 'btn-primary'} w-full mt-4 px-3 py-2 text-[10px] font-mono uppercase tracking-wider disabled:opacity-40`}
                          >
                            {profileInstalled ? 'Open' : apiDisconnected ? 'API Offline' : building ? 'Installing' : 'Install'}
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
                            {apiDisconnected ? 'API Offline' : building ? 'Installing' : 'Install Mission'}
                          </button>
                          <button
                            type="button"
                            onClick={() => {
                              if (!storageReady) {
                                void handleUseRecommendedStorage().then(status => {
                                  if (status?.ok && status.configured_by !== 'default') {
                                    setAdvancedSetupOpen(true);
                                    applyWizardProfile(profile.id);
                                  } else {
                                    setAdvancedSetupOpen(true);
                                  }
                                });
                                return;
                              }
                              setAdvancedSetupOpen(true);
                              applyWizardProfile(profile.id);
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

            {advancedSetupOpen && (
            <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-2 text-[10px] font-mono border-t border-[#e5e5e5] pt-4">
              <div className="flex items-center gap-2">
                <span className={`w-1.5 h-1.5 flex-shrink-0 ${apiStatus === 'connected' ? 'bg-[#76B900]' : apiStatus === 'disconnected' ? 'bg-[#d97706]' : 'bg-[#a3a3a3] animate-pulse'}`}
                  style={{ clipPath: 'polygon(50% 0%, 100% 50%, 50% 100%, 0% 50%)' }} />
                <span className={apiStatus === 'connected' ? 'text-[#76B900]' : 'text-[#737373]'}>
                  {apiStatus === 'connected' ? 'nvHive API is online' : apiStatus === 'disconnected' ? 'nvHive API is not responding yet' : 'Checking nvHive API'}
                </span>
              </div>
              <button
                type="button"
                onClick={() => setAdvancedSetupOpen(prev => !prev)}
                className="text-[#737373] hover:text-[#76B900] uppercase tracking-wider"
              >
                {advancedSetupOpen ? 'Hide Details' : 'Advanced Details'}
              </button>
            </div>
            )}
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
              <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
                <div className="border border-[#e5e5e5] p-3">
                  <div className="text-[10px] font-mono text-[#a3a3a3] uppercase">Models</div>
                  <div className="text-[10px] font-mono text-[#525252] mt-1 break-all">{storageStatus?.layout.models_dir ?? 'Set NVH_HOME first'}</div>
                </div>
                <div className="border border-[#e5e5e5] p-3">
                  <div className="text-[10px] font-mono text-[#a3a3a3] uppercase">ComfyUI</div>
                  <div className="text-[10px] font-mono text-[#525252] mt-1 break-all">{storageStatus?.layout.comfyui_dir ?? 'Set NVH_HOME first'}</div>
                </div>
                <div className="border border-[#e5e5e5] p-3">
                  <div className="text-[10px] font-mono text-[#a3a3a3] uppercase">Cache</div>
                  <div className="text-[10px] font-mono text-[#525252] mt-1 break-all">{storageStatus?.layout.cache_dir ?? 'Set NVH_HOME first'}</div>
                </div>
              </div>
              <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
                <div className="border border-[#e5e5e5] p-3">
                  <div className="text-[10px] font-mono text-[#a3a3a3] uppercase">Runtimes</div>
                  <div className="text-[10px] font-mono text-[#525252] mt-1 break-all">{storageStatus?.layout.runtime_dir ?? 'Set NVH_HOME first'}</div>
                </div>
                <div className="border border-[#e5e5e5] p-3">
                  <div className="text-[10px] font-mono text-[#a3a3a3] uppercase">Apps</div>
                  <div className="text-[10px] font-mono text-[#525252] mt-1 break-all">{storageStatus?.layout.apps_dir ?? 'Set NVH_HOME first'}</div>
                </div>
                <div className="border border-[#e5e5e5] p-3">
                  <div className="text-[10px] font-mono text-[#a3a3a3] uppercase">WebUI</div>
                  <div className="text-[10px] font-mono text-[#525252] mt-1 break-all">{storageStatus?.layout.webui_dir ?? 'Set NVH_HOME first'}</div>
                </div>
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
                      {mountActivating ? 'Preparing...' : 'Use Recommended'}
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
                    {storageSaving ? 'Checking...' : 'Configure'}
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
                <div className="bg-[#0a0a0a] border border-[#333333] p-3 overflow-x-auto">
                  <code className="text-[10px] font-mono text-[#76B900] whitespace-pre">
                    {`source ${storageStatus.env_file}`}
                  </code>
                </div>
              )}
            </div>
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
              <div className="space-y-3">
                <div className="section-label">Install Local AI</div>
                <div className="bg-[#ffffff] border border-[#e5e5e5] p-4 font-mono text-sm space-y-2">
                  <div className="text-[#a3a3a3] text-[10px] uppercase tracking-wider"># Rootless Ollama runtime, no sudo</div>
                  <div className="text-[#76B900]">nvh studio --install rootless-ollama -y</div>
                  <div className="text-[#a3a3a3] text-[10px] uppercase tracking-wider mt-3"># Start the local model server</div>
                  <div className="text-[#76B900]">nvhive-ollama-serve</div>
                  <div className="text-[#a3a3a3] text-[10px] uppercase tracking-wider mt-3"># Pull recommended fitting models</div>
                  <div className="text-[#76B900]">nvh studio --install-models recommended -y</div>
                </div>
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

            {/* Quick setup via docker */}
            <div className="bg-[#ffffff] border border-[#e5e5e5] p-4">
              <div className="text-[10px] font-mono text-[#a3a3a3] mb-2 uppercase tracking-wider">Using Docker Compose?</div>
              <code className="text-[10px] font-mono text-[#76B900]">docker compose up -d</code>
              <div className="text-[10px] font-mono text-[#a3a3a3] mt-1">
                The docker-compose stack auto-pulls nemotron-mini on first start.
              </div>
            </div>
          </div>
        )}

        {/* AI STUDIO PACKS */}
        {step === 'studio' && (
          <div className="space-y-6">
            <div>
              <div className="text-[10px] font-mono text-[#76B900] uppercase tracking-wider mb-1">Step 6</div>
              <h2 className="text-lg font-bold text-[#0a0a0a] font-mono">AI Studio Packs</h2>
              <p className="text-xs font-mono text-[#a3a3a3] mt-1">
                One-click rootless packs for LLMs, OpenClaw/NemoClaw agents, ComfyUI sub software, Blender, runtime fallback, and Linux game projects.
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
                  <a
                    href={comfyStatus?.url ?? 'http://127.0.0.1:8188'}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="btn-ghost px-3 py-2 text-[10px] font-mono uppercase tracking-wider"
                  >
                    Open
                  </a>
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
                    PyTorch profile: NVIDIA CUDA 13.0
                  </div>
                </div>
                <div className="max-h-44 overflow-y-auto space-y-1">
                  {comfyEvents.map((event, index) => (
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
                        {apiKeys[provider.id] && (
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
                        value={apiKeys[provider.id] || ''}
                        onChange={e => setApiKeys(prev => ({ ...prev, [provider.id]: e.target.value }))}
                        onPaste={e => {
                          const pasted = e.clipboardData.getData('text').trim();
                          if (pasted) {
                            setApiKeys(prev => ({ ...prev, [provider.id]: pasted }));
                          }
                        }}
                        placeholder={`Paste ${provider.envKey} here...`}
                        className="input-base flex-1 px-3 py-2 text-xs font-mono"
                        spellCheck={false}
                        autoComplete="off"
                      />
                    </div>
                    <div className="text-[10px] font-mono text-[#333333] mt-1">
                      Or set as env var: <span className="text-[#a3a3a3]">{provider.envKey}=your-key</span>
                    </div>
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
                        Start the server: <span className="text-[#76B900]">nvh serve</span>
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
                    API server not connected. Start it with: nvh serve
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
                  { label: 'ComfyUI', value: comfyStatus?.running ? 'Running' : comfyStatus?.installed ? 'Installed' : 'Optional', ok: Boolean(comfyStatus?.installed || comfyStatus?.running) },
                  { label: 'Hive API', value: apiStatus === 'connected' ? 'Online' : 'Offline', ok: apiStatus === 'connected' },
                  { label: 'Active Advisors', value: configuredProviders.length > 0 ? configuredProviders.join(', ') : 'None yet', ok: configuredProviders.length > 0 },
                ].map(item => (
                  <div key={item.label} className="flex items-center gap-3 px-3 py-2 bg-[#ffffff] border border-[#e5e5e5]">
                    <span className={`w-1.5 h-1.5 flex-shrink-0 ${item.ok ? 'bg-[#76B900]' : 'bg-[#dc2626]'}`}
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
                START QUERYING
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
            Advanced step {currentStepIdx + 1} / {STEPS.length}
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
