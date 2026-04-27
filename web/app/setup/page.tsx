'use client';

import { useState, useEffect } from 'react';
import Link from 'next/link';
import {
  checkHealth,
  query,
  getGPUInfo,
  getRecommendations,
  getFreeProviders,
  saveProviderKey,
  getComfyUIStatus,
  getComfyUIExamples,
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
  StudioPack,
  StudioPackInstallEvent,
  StudioModel,
  StudioModelInstallEvent,
} from '@/lib/types';

type Step = 'welcome' | 'gpu' | 'models' | 'local-ai' | 'studio' | 'comfyui' | 'cloud' | 'test' | 'done';
type WizardProfile = 'student' | 'creator' | 'game' | 'full';

const STEPS: { id: Step; label: string; num: number }[] = [
  { id: 'welcome', label: 'Welcome', num: 1 },
  { id: 'gpu', label: 'GPU', num: 2 },
  { id: 'models', label: 'Models', num: 3 },
  { id: 'local-ai', label: 'Local AI', num: 4 },
  { id: 'studio', label: 'Packs', num: 5 },
  { id: 'comfyui', label: 'ComfyUI', num: 6 },
  { id: 'cloud', label: 'Cloud', num: 7 },
  { id: 'test', label: 'Test', num: 8 },
  { id: 'done', label: 'Done', num: 9 },
];

const CLOUD_PROVIDERS = [
  { id: 'openai', name: 'OpenAI', description: 'GPT-4o, GPT-4o-mini', envKey: 'OPENAI_API_KEY', placeholder: 'sk-...', signupUrl: 'https://platform.openai.com/api-keys' },
  { id: 'anthropic', name: 'Anthropic', description: 'Claude Sonnet, Haiku, Opus', envKey: 'ANTHROPIC_API_KEY', placeholder: 'sk-ant-...', signupUrl: 'https://console.anthropic.com/settings/keys' },
  { id: 'google', name: 'Google Gemini', description: 'Gemini 2.0 Flash, Pro', envKey: 'GOOGLE_API_KEY', placeholder: 'AIza...', signupUrl: 'https://aistudio.google.com/apikey' },
  { id: 'groq', name: 'Groq', description: 'Llama 3.3 70B (ultra-fast)', envKey: 'GROQ_API_KEY', placeholder: 'gsk_...', signupUrl: 'https://console.groq.com/keys' },
  { id: 'grok', name: 'xAI Grok', description: 'Grok 2, Grok 3', envKey: 'XAI_API_KEY', placeholder: 'xai-...', signupUrl: 'https://console.x.ai' },
  { id: 'mistral', name: 'Mistral', description: 'Mistral Large, Small', envKey: 'MISTRAL_API_KEY', placeholder: 'your-key...', signupUrl: 'https://console.mistral.ai/api-keys' },
];

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


  useEffect(() => {
    // Check API health
    checkHealth()
      .then(() => setApiStatus('connected'))
      .catch(() => setApiStatus('disconnected'));

    // Ollama status + configured-providers list are now fed by the
    // polled useProviderHealth hook below, so nothing to do here at
    // mount time.

    // Fetch free providers for cloud step
    setFreeProvidersLoading(true);
    getFreeProviders()
      .then(data => setFreeProviders(data.providers))
      .catch(() => {})
      .finally(() => setFreeProvidersLoading(false));

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
        setSelectedStudioPacks(new Set(data.bundles.starter ?? data.packs.map(pack => pack.id)));
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
  }, []);

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
        return new Set(data.bundles.starter ?? data.packs.map(pack => pack.id));
      });
    } catch {
      // keep current pack state
    } finally {
      setStudioLoading(false);
    }
  };

  const toggleStudioPack = (packId: string) => {
    setSelectedStudioPacks(prev => {
      const next = new Set(prev);
      if (next.has(packId)) next.delete(packId);
      else next.add(packId);
      return next;
    });
  };

  const selectStudioBundle = (bundleId: string) => {
    const packIds = studioBundles[bundleId] ?? [];
    setSelectedStudioPacks(new Set(packIds));
  };

  const applyWizardProfile = (profile: WizardProfile) => {
    const recommendedModels = studioModels
      .filter(model => model.recommended)
      .map(model => model.id);
    const allModelIds = studioModels
      .filter(model => model.recommended || model.fits_vram)
      .map(model => model.id);
    const vramLimit = detectedModelVram || 12;
    const starterPackIds = studioBundles.starter ?? studioPacks.map(pack => pack.id);
    const starterExamples = visibleComfyExamples
      .filter(example => example.recommended_vram_gb <= vramLimit)
      .map(example => example.id);

    if (profile === 'student') {
      setSelectedStudioModels(new Set(recommendedModels));
      setSelectedStudioPacks(new Set(starterPackIds));
      setSelectedComfyExamples(new Set(starterExamples));
      setStep('models');
      return;
    }

    if (profile === 'creator') {
      setSelectedStudioModels(new Set(recommendedModels));
      setSelectedStudioPacks(new Set(studioBundles.comfy ?? ['comfyui-power-nodes']));
      setSelectedComfyExamples(new Set(starterExamples));
      setStep('comfyui');
      return;
    }

    if (profile === 'game') {
      setSelectedStudioModels(new Set(recommendedModels));
      setSelectedStudioPacks(new Set(studioBundles.game ?? ['game-dev-lab', 'game-mod-helper']));
      setSelectedComfyExamples(new Set(starterExamples));
      setStep('studio');
      return;
    }

    setSelectedStudioModels(new Set(allModelIds.length ? allModelIds : recommendedModels));
    setSelectedStudioPacks(new Set(studioBundles.all ?? studioPacks.map(pack => pack.id)));
    setSelectedComfyExamples(new Set(starterExamples));
    setStep('models');
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

  const handleInstallStudioModels = () => {
    if (modelsInstalling) return;
    const selected = Array.from(selectedStudioModels);
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
        },
        onError: error => {
          setModelError(error);
          setModelsInstalling(false);
        },
      }
    );
  };

  const handleInstallStudioPacks = (packIds?: string[]) => {
    if (studioInstalling) return;
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
        },
        onError: error => {
          setStudioError(error);
          setStudioInstalling(false);
        },
      }
    );
  };

  const handleInstallComfyUI = () => {
    if (comfyInstalling) return;
    setComfyInstalling(true);
    setComfyError(null);
    setComfyEvents([]);

    installComfyUIStream(
      { torch_profile: 'nvidia-cu130', force_update: false },
      {
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
        },
        onError: error => {
          setComfyError(error);
          setComfyInstalling(false);
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
      setComfyPlanMessage(`Saved ${plan.model_count} model requirement(s) to ${plan.plan_path}`);
    } catch (err) {
      setComfyError(err instanceof Error ? err.message : 'Failed to save ComfyUI model plan');
    } finally {
      setComfyPlanSaving(false);
    }
  };

  const currentStepIdx = STEPS.findIndex(s => s.id === step);
  const visibleComfyExamples = comfyStatus?.examples?.length ? comfyStatus.examples : comfyExamples;
  const selectedComfyModelCount = new Set(
    visibleComfyExamples
      .filter(example => selectedComfyExamples.has(example.id))
      .flatMap(example => example.models)
  ).size;
  const selectedStudioPackIds = Array.from(selectedStudioPacks);
  const starterStudioPackIds = studioBundles.starter ?? [];
  const studioCategories = Array.from(new Set(studioPacks.map(pack => pack.category)));
  const selectedModelIds = Array.from(selectedStudioModels);
  const modelCategories = Array.from(new Set(studioModels.map(model => model.category)));
  const selectedModelDiskGb = studioModels
    .filter(model => selectedStudioModels.has(model.id))
    .reduce((total, model) => total + model.estimated_disk_gb, 0);

  return (
    <div className="p-6 max-w-3xl mx-auto space-y-6">
      {/* Header */}
      <div className="nvidia-corner relative border border-[#d4d4d4] bg-[#ffffff] p-5 overflow-hidden">
        <div className="absolute top-0 left-0 right-0 h-px bg-gradient-to-r from-transparent via-[#76B900] to-transparent" />
        <div className="relative">
          <div className="text-[10px] font-mono text-[#76B900] tracking-[0.2em] uppercase mb-0.5">First-Time Setup</div>
          <h1 className="text-2xl font-bold text-[#0a0a0a]">Setup Wizard</h1>
          <p className="text-xs font-mono text-[#a3a3a3] mt-1">Get Hive configured and running in minutes</p>
        </div>
      </div>

      {/* Step indicator */}
      <div className="flex items-center gap-0">
        {STEPS.map((s, i) => (
          <div key={s.id} className="flex items-center flex-1">
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
              <span className="hidden sm:inline">{s.label}</span>
            </button>
            {i < STEPS.length - 1 && (
              <div className={`flex-1 h-px mx-2 ${i < currentStepIdx ? 'bg-[#76B900]/40' : 'bg-[#e5e5e5]'}`} />
            )}
          </div>
        ))}
      </div>

      {/* Step content */}
      <div className="card p-6 nvidia-corner relative animate-fade-in">
        <div className="absolute top-0 left-0 right-0 h-px bg-[#76B900]/20" />

        {/* WELCOME */}
        {step === 'welcome' && (
          <div className="space-y-6">
            <div className="text-center space-y-4">
              <div className="w-20 h-20 mx-auto border border-[#76B900]/40 bg-[#76B900]/5 flex items-center justify-center"
                style={{ clipPath: 'polygon(50% 0%, 100% 25%, 100% 75%, 50% 100%, 0% 75%, 0% 25%)' }}>
                <span className="text-3xl font-bold text-[#76B900] font-mono">C</span>
              </div>
              <div>
                <h2 className="text-xl font-bold text-[#0a0a0a] font-mono">Welcome to Hive</h2>
                <p className="text-xs font-mono text-[#a3a3a3] mt-2">AI Command Center - NVIDIA Powered</p>
              </div>
              <div className="text-sm font-mono text-[#525252] max-w-lg mx-auto leading-relaxed">
                Hive lets you run multiple AI advisors in parallel - locally on your NVIDIA GPU with zero cost,
                or via cloud APIs. This wizard will get you set up in minutes.
              </div>
            </div>

            <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
              {[
                { icon: 'GPU', title: 'Local AI', desc: 'Run NVIDIA Nemotron on your GPU. Free forever.' },
                { icon: 'LLM', title: 'Multi-LLM', desc: 'Query multiple models at once. Compare results.' },
                { icon: '$0', title: 'Zero Cost', desc: 'Local models cost $0. Use cloud only when needed.' },
              ].map(f => (
                <div key={f.title} className="bg-[#ffffff] border border-[#e5e5e5] p-4 text-center">
                  <div className="text-2xl text-[#76B900] mb-2">{f.icon}</div>
                  <div className="text-xs font-mono font-bold text-[#0a0a0a] mb-1 uppercase">{f.title}</div>
                  <div className="text-[10px] font-mono text-[#a3a3a3]">{f.desc}</div>
                </div>
              ))}
            </div>

            <div className="space-y-3">
              <div className="section-label">Quick Profiles</div>
              <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                {[
                  {
                    id: 'student' as WizardProfile,
                    title: 'Student Starter',
                    desc: 'Recommended local models, agent lab, ComfyUI nodes, and beginner workflow examples.',
                    next: 'Models',
                  },
                  {
                    id: 'creator' as WizardProfile,
                    title: 'Creator / ComfyUI',
                    desc: 'ComfyUI power nodes plus image, edit, ControlNet, and video workflow planning.',
                    next: 'ComfyUI',
                  },
                  {
                    id: 'game' as WizardProfile,
                    title: 'Game Dev',
                    desc: 'Linux game-dev pack, mod helper workspace, and local AI helpers.',
                    next: 'Packs',
                  },
                  {
                    id: 'full' as WizardProfile,
                    title: 'Full Workstation',
                    desc: 'Everything that fits the detected GPU, with all rootless studio packs selected.',
                    next: 'Models',
                  },
                ].map(profile => (
                  <button
                    key={profile.id}
                    type="button"
                    onClick={() => applyWizardProfile(profile.id)}
                    className="text-left border border-[#e5e5e5] bg-[#ffffff] p-4 hover:border-[#76B900]/50 hover:bg-[#76B900]/5 transition-colors"
                  >
                    <div className="flex items-start justify-between gap-3">
                      <div>
                        <div className="text-xs font-mono font-bold text-[#0a0a0a] uppercase">{profile.title}</div>
                        <div className="text-[10px] font-mono text-[#737373] leading-relaxed mt-2">{profile.desc}</div>
                      </div>
                      <span className="text-[9px] font-mono text-[#76B900] border border-[#76B900]/30 px-1.5 py-0.5 flex-shrink-0">
                        {profile.next}
                      </span>
                    </div>
                  </button>
                ))}
              </div>
            </div>

            <div className="flex items-center gap-2 text-[10px] font-mono">
              <span className={`w-1.5 h-1.5 flex-shrink-0 ${apiStatus === 'connected' ? 'bg-[#76B900]' : apiStatus === 'disconnected' ? 'bg-[#dc2626]' : 'bg-[#a3a3a3] animate-pulse'}`}
                style={{ clipPath: 'polygon(50% 0%, 100% 50%, 50% 100%, 0% 50%)' }} />
              <span className={apiStatus === 'connected' ? 'text-[#76B900]' : apiStatus === 'disconnected' ? 'text-[#dc2626]' : 'text-[#a3a3a3]'}>
                {apiStatus === 'connected' ? 'Hive API is running' : apiStatus === 'disconnected' ? 'Hive API is offline - start it with: nvh serve' : 'Checking API...'}
              </span>
            </div>
          </div>
        )}

        {/* GPU DETECTION */}
        {step === 'gpu' && (
          <div className="space-y-6">
            <div>
              <div className="text-[10px] font-mono text-[#76B900] uppercase tracking-wider mb-1">Step 2</div>
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
                    <div className="text-sm font-bold text-[#0a0a0a] font-mono">No NVIDIA GPU Detected</div>
                    <div className="text-[10px] font-mono text-[#a3a3a3] mt-0.5">CPU MODE</div>
                    <div className="text-[10px] font-mono text-[#a3a3a3] mt-1">
                      Local models will run on CPU. Consider a cloud provider for better speed.
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
              <div className="text-[10px] font-mono text-[#76B900] uppercase tracking-wider mb-1">Step 3</div>
              <h2 className="text-lg font-bold text-[#0a0a0a] font-mono">Model Picker</h2>
              <p className="text-xs font-mono text-[#a3a3a3] mt-1">
                Choose exact local models to download. Recommendations are based on detected VRAM and student-friendly defaults.
              </p>
            </div>

            <div className="border border-[#76B900]/30 bg-[#76B900]/5 p-4">
              <div className="flex flex-col lg:flex-row lg:items-center gap-4 justify-between">
                <div>
                  <div className="text-sm font-mono font-bold text-[#0a0a0a]">Recommended Local Model Queue</div>
                  <div className="text-[10px] font-mono text-[#76B900] mt-0.5">
                    Detected VRAM: {detectedModelVram ? `${detectedModelVram} GB` : 'unknown'} / selected download: ~{selectedModelDiskGb.toFixed(1)} GB
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
                    onClick={handleInstallStudioModels}
                    disabled={modelsInstalling || selectedModelIds.length === 0}
                    className="btn-primary px-3 py-2 text-[10px] font-mono uppercase tracking-wider disabled:opacity-40"
                  >
                    {modelsInstalling ? 'Downloading...' : `Download ${selectedModelIds.length || ''}`}
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
              <div className="text-[10px] font-mono text-[#76B900] uppercase tracking-wider mb-1">Step 4</div>
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
              <div className="text-[10px] font-mono text-[#76B900] uppercase tracking-wider mb-1">Step 5</div>
              <h2 className="text-lg font-bold text-[#0a0a0a] font-mono">AI Studio Packs</h2>
              <p className="text-xs font-mono text-[#a3a3a3] mt-1">
                One-click rootless packs for LLMs, local agents, ComfyUI sub software, and Linux game projects.
              </p>
            </div>

            <div className="border border-[#76B900]/30 bg-[#76B900]/5 p-4">
              <div className="flex flex-col lg:flex-row lg:items-center gap-4 justify-between">
                <div>
                  <div className="text-sm font-mono font-bold text-[#0a0a0a]">Student Lab Starter</div>
                  <div className="text-[10px] font-mono text-[#76B900] mt-0.5">
                    No sudo. Installs under {studioRoot || '~/.nvh/studio'} and ~/.local/bin
                  </div>
                  <div className="text-[10px] font-mono text-[#737373] mt-2">
                    {starterStudioPackIds.length} packs - {studioPacks.filter(pack => pack.status.installed).length}/{studioPacks.length} installed
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
                    onClick={() => selectStudioBundle('all')}
                    className="btn-ghost px-3 py-2 text-[10px] font-mono uppercase tracking-wider"
                  >
                    Select All
                  </button>
                  <button
                    type="button"
                    onClick={() => handleInstallStudioPacks(selectedStudioPackIds)}
                    disabled={studioInstalling || selectedStudioPackIds.length === 0}
                    className="btn-primary px-3 py-2 text-[10px] font-mono uppercase tracking-wider disabled:opacity-40"
                  >
                    {studioInstalling ? 'Installing...' : `Install ${selectedStudioPackIds.length || ''}`}
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
                        const selected = selectedStudioPacks.has(pack.id);
                        return (
                          <label
                            key={pack.id}
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
                                      : 'border-[#d4d4d4] text-[#737373]'
                                  }`}>
                                    {pack.status.installed ? 'INSTALLED' : 'READY'}
                                  </span>
                                </div>
                                <div className="text-[10px] font-mono text-[#737373] leading-relaxed mt-2">
                                  {pack.description}
                                </div>
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
              <div className="text-[10px] font-mono text-[#76B900] uppercase tracking-wider mb-1">Step 6</div>
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
                      {comfyStatus?.installed ? comfyStatus.app_dir : 'Install target: ~/.nvh/comfyui/ComfyUI'}
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
                    disabled={comfyInstalling}
                    className="btn-primary px-3 py-2 text-[10px] font-mono uppercase tracking-wider disabled:opacity-40"
                  >
                    {comfyInstalling ? 'Installing...' : comfyStatus?.installed ? 'Refresh Install' : 'Install ComfyUI'}
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
                  <div className="section-label">Workflow Model Plan</div>
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
                  {comfyPlanSaving ? 'Saving...' : 'Save Model Plan'}
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
              <div className="text-[10px] font-mono text-[#76B900] uppercase tracking-wider mb-1">Step 7</div>
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
              <div className="text-[10px] font-mono text-[#76B900] uppercase tracking-wider mb-1">Step 8</div>
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

            {/* Quick start commands */}
            <div className="bg-[#ffffff] border border-[#e5e5e5] p-4 space-y-2">
              <div className="text-[10px] font-mono text-[#a3a3a3] uppercase tracking-wider mb-2">Quick Commands</div>
              <div className="text-[10px] font-mono text-[#a3a3a3]"># Rootless all-in-one student lab</div>
              <div className="text-[10px] font-mono text-[#76B900]">nvh workstation --all -y</div>
              <div className="text-[10px] font-mono text-[#a3a3a3] mt-2"># Packs only</div>
              <div className="text-[10px] font-mono text-[#76B900]">nvh studio --install starter -y</div>
              <div className="text-[10px] font-mono text-[#a3a3a3] mt-2"># Launch dashboard</div>
              <div className="text-[10px] font-mono text-[#76B900]">nvh webui</div>
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

        {/* Navigation */}
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
            {currentStepIdx + 1} / {STEPS.length}
          </span>

          {step !== 'done' ? (
            <button
              onClick={() => {
                const idx = STEPS.findIndex(s => s.id === step);
                if (idx < STEPS.length - 1) setStep(STEPS[idx + 1].id);
              }}
              className="btn-primary px-6 py-2 text-xs font-mono uppercase tracking-wider"
            >
              Next &gt;
            </button>
          ) : (
            <Link href="/" className="btn-primary px-6 py-2 text-xs font-mono uppercase tracking-wider">
              Done &gt;
            </Link>
          )}
        </div>
      </div>
    </div>
  );
}
