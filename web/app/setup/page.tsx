'use client';

import { useState, useEffect } from 'react';
import Link from 'next/link';
import { checkHealth, query, getGPUInfo, getRecommendations, getFreeProviders, saveProviderKey } from '@/lib/api';
import { useProviderHealth } from '@/lib/useProviderHealth';
import type { GPUInfo, RecommendationsResult, FreeProvider } from '@/lib/types';

type Step = 'welcome' | 'gpu' | 'local-ai' | 'cloud' | 'test' | 'done';

const STEPS: { id: Step; label: string; num: number }[] = [
  { id: 'welcome', label: 'Welcome', num: 1 },
  { id: 'gpu', label: 'GPU', num: 2 },
  { id: 'local-ai', label: 'Local AI', num: 3 },
  { id: 'cloud', label: 'Cloud', num: 4 },
  { id: 'test', label: 'Test', num: 5 },
  { id: 'done', label: 'Done', num: 6 },
];

const CLOUD_PROVIDERS = [
  { id: 'openai', name: 'OpenAI', description: 'GPT-4o, GPT-4o-mini', envKey: 'OPENAI_API_KEY', placeholder: 'sk-...', signupUrl: 'https://platform.openai.com/api-keys' },
  { id: 'anthropic', name: 'Anthropic', description: 'Claude Sonnet, Haiku, Opus', envKey: 'ANTHROPIC_API_KEY', placeholder: 'sk-ant-...', signupUrl: 'https://console.anthropic.com/settings/keys' },
  { id: 'google', name: 'Google Gemini', description: 'Gemini 2.0 Flash, Pro', envKey: 'GOOGLE_API_KEY', placeholder: 'AIza...', signupUrl: 'https://aistudio.google.com/apikey' },
  { id: 'groq', name: 'Groq', description: 'Llama 3.3 70B (ultra-fast)', envKey: 'GROQ_API_KEY', placeholder: 'gsk_...', signupUrl: 'https://console.groq.com/keys' },
  { id: 'grok', name: 'xAI Grok', description: 'Grok 2, Grok 3', envKey: 'XAI_API_KEY', placeholder: 'xai-...', signupUrl: 'https://console.x.ai' },
  { id: 'mistral', name: 'Mistral', description: 'Mistral Large, Small', envKey: 'MISTRAL_API_KEY', placeholder: 'your-key...', signupUrl: 'https://console.mistral.ai/api-keys' },
];

// ─── Provider Card (used in Cloud step) ──────────────────────────────────────

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
    <div className={`border bg-[#111111] transition-colors ${isConfigured ? 'border-[#76B900]/40' : 'border-[#222222]'}`}>
      <div className="flex items-center gap-3 p-3">
        {/* Status indicator */}
        <span className={`w-1.5 h-1.5 flex-shrink-0 ${isConfigured ? 'bg-[#76B900]' : 'bg-[#333333]'}`}
          style={{ clipPath: 'polygon(50% 0%, 100% 50%, 50% 100%, 0% 50%)' }} />

        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 flex-wrap">
            <span className="text-sm font-mono font-bold text-white">{p.name}</span>
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
            <div className="text-[10px] font-mono text-[#555555] mt-0.5">{p.free_tier_limits}</div>
          )}
          {p.strengths && p.strengths.length > 0 && (
            <div className="flex flex-wrap gap-1 mt-1">
              {p.strengths.map(s => (
                <span key={s} className="text-[9px] font-mono text-[#444444] bg-[#1a1a1a] border border-[#2a2a2a] px-1.5 py-0.5">
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
                : 'border-[#333333] text-[#555555] hover:border-[#76B900]/30 hover:text-[#76B900]'
            }`}
          >
            {isExpanded ? 'Cancel' : 'Add Key'}
          </button>
        )}
      </div>

      {/* Inline key form */}
      {isExpanded && !isConfigured && (
        <div className="border-t border-[#1a1a1a] p-3 space-y-2">
          <div className="flex items-center justify-between">
            <div className="text-[10px] font-mono text-[#555555]">
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
            <div className="text-[10px] font-mono text-[#ef4444]">{keyErrors[p.id]}</div>
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

  // Live-polled provider health — drives Ollama status and the
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
        // GPU not available — leave null
      })
      .finally(() => setGpuLoading(false));
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

  const currentStepIdx = STEPS.findIndex(s => s.id === step);

  return (
    <div className="p-6 max-w-3xl mx-auto space-y-6">
      {/* Header */}
      <div className="nvidia-corner relative border border-[#333333] bg-[#111111] p-5 overflow-hidden">
        <div className="absolute top-0 left-0 right-0 h-px bg-gradient-to-r from-transparent via-[#76B900] to-transparent" />
        <div className="relative">
          <div className="text-[10px] font-mono text-[#76B900] tracking-[0.2em] uppercase mb-0.5">First-Time Setup</div>
          <h1 className="text-2xl font-bold text-white">Setup Wizard</h1>
          <p className="text-xs font-mono text-[#555555] mt-1">Get Hive configured and running in minutes</p>
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
                  ? 'text-[#555555] hover:text-[#76B900]'
                  : 'text-[#333333]'
              }`}
            >
              <span className={`w-5 h-5 flex items-center justify-center text-[10px] font-bold border ${
                s.id === step
                  ? 'border-[#76B900] bg-[#76B900] text-black'
                  : i < currentStepIdx
                  ? 'border-[#76B900]/40 text-[#76B900]'
                  : 'border-[#333333] text-[#333333]'
              }`}>
                {i < currentStepIdx ? '✓' : s.num}
              </span>
              <span className="hidden sm:inline">{s.label}</span>
            </button>
            {i < STEPS.length - 1 && (
              <div className={`flex-1 h-px mx-2 ${i < currentStepIdx ? 'bg-[#76B900]/40' : 'bg-[#222222]'}`} />
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
                <h2 className="text-xl font-bold text-white font-mono">Welcome to Hive</h2>
                <p className="text-xs font-mono text-[#555555] mt-2">AI Command Center — NVIDIA Powered</p>
              </div>
              <div className="text-sm font-mono text-[#999999] max-w-lg mx-auto leading-relaxed">
                Hive lets you run multiple AI advisors in parallel — locally on your NVIDIA GPU with zero cost,
                or via cloud APIs. This wizard will get you set up in minutes.
              </div>
            </div>

            <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
              {[
                { icon: '▣', title: 'Local AI', desc: 'Run NVIDIA Nemotron on your GPU. Free forever.' },
                { icon: '◈', title: 'Multi-LLM', desc: 'Query multiple models at once. Compare results.' },
                { icon: '⚡', title: 'Zero Cost', desc: 'Local models cost $0. Use cloud only when needed.' },
              ].map(f => (
                <div key={f.title} className="bg-[#111111] border border-[#222222] p-4 text-center">
                  <div className="text-2xl text-[#76B900] mb-2">{f.icon}</div>
                  <div className="text-xs font-mono font-bold text-white mb-1 uppercase">{f.title}</div>
                  <div className="text-[10px] font-mono text-[#555555]">{f.desc}</div>
                </div>
              ))}
            </div>

            <div className="flex items-center gap-2 text-[10px] font-mono">
              <span className={`w-1.5 h-1.5 flex-shrink-0 ${apiStatus === 'connected' ? 'bg-[#76B900]' : apiStatus === 'disconnected' ? 'bg-[#ef4444]' : 'bg-[#444444] animate-pulse'}`}
                style={{ clipPath: 'polygon(50% 0%, 100% 50%, 50% 100%, 0% 50%)' }} />
              <span className={apiStatus === 'connected' ? 'text-[#76B900]' : apiStatus === 'disconnected' ? 'text-[#ef4444]' : 'text-[#555555]'}>
                {apiStatus === 'connected' ? 'Hive API is running' : apiStatus === 'disconnected' ? 'Hive API is offline — start it with: council serve' : 'Checking API...'}
              </span>
            </div>
          </div>
        )}

        {/* GPU DETECTION */}
        {step === 'gpu' && (
          <div className="space-y-6">
            <div>
              <div className="text-[10px] font-mono text-[#76B900] uppercase tracking-wider mb-1">Step 2</div>
              <h2 className="text-lg font-bold text-white font-mono">GPU Detection</h2>
              <p className="text-xs font-mono text-[#555555] mt-1">Your NVIDIA GPU will power local AI inference</p>
            </div>

            {/* GPU detection result */}
            {gpuLoading ? (
              <div className="border border-[#333333] bg-[#111111] p-4 animate-pulse">
                <div className="flex items-center gap-4">
                  <div className="w-14 h-14 bg-[#1a1a1a]" />
                  <div className="flex-1 space-y-2">
                    <div className="h-3 bg-[#1a1a1a] w-1/2" />
                    <div className="h-2 bg-[#1a1a1a] w-1/3" />
                  </div>
                </div>
              </div>
            ) : gpuInfo && gpuInfo.gpus.length > 0 ? (
              <div className="space-y-3">
                {gpuInfo.gpus.map((g, i) => {
                  const usedPct = g.vram_mb > 0 ? Math.round((g.memory_used_mb / g.vram_mb) * 100) : 0;
                  const barColor = usedPct > 90 ? '#ef4444' : usedPct > 70 ? '#f59e0b' : '#76B900';
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
                          <div className="text-sm font-bold text-white font-mono">{g.name}</div>
                          <div className="text-[10px] font-mono text-[#76B900] mt-0.5">DETECTED · GPU {g.index}</div>
                          <div className="text-[10px] font-mono text-[#555555] mt-1 space-x-2">
                            <span>CUDA {g.cuda_version}</span>
                            <span>·</span>
                            <span>driver {g.driver_version}</span>
                          </div>
                          <div className="mt-2 space-y-1">
                            <div className="flex justify-between text-[10px] font-mono">
                              <span className="text-[#555555]">VRAM</span>
                              <span className="text-[#999999]">
                                {(g.memory_used_mb / 1024).toFixed(1)} used / {g.vram_gb} GB total
                              </span>
                            </div>
                            <div className="progress-bar">
                              <div className="progress-fill" style={{ width: `${usedPct}%`, backgroundColor: barColor }} />
                            </div>
                            <div className="text-[10px] font-mono text-[#444444]">
                              {(g.memory_free_mb / 1024).toFixed(1)} GB free · Utilization {g.utilization_pct}%
                            </div>
                          </div>
                        </div>
                      </div>
                    </div>
                  );
                })}

                {/* System RAM */}
                <div className="bg-[#111111] border border-[#222222] p-3">
                  <div className="text-[10px] font-mono text-[#555555] mb-1 uppercase tracking-wider">System RAM</div>
                  <div className="text-xs font-mono text-[#999999]">
                    {gpuInfo.system_ram.total_gb} GB total · {gpuInfo.system_ram.available_gb} GB available ·{' '}
                    {gpuInfo.system_ram.effective_for_llm_gb} GB usable for CPU offload
                  </div>
                </div>
              </div>
            ) : (
              <div className="border border-[#333333] bg-[#111111] p-4">
                <div className="flex items-center gap-4">
                  <div className="w-14 h-14 bg-[#222222] border border-[#333333] flex items-center justify-center flex-shrink-0">
                    <svg className="w-7 h-7 text-[#555555]" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
                      <path strokeLinecap="round" strokeLinejoin="round"
                        d="M9 3H5a2 2 0 00-2 2v4m6-6h10a2 2 0 012 2v4M9 3v18m0 0h10a2 2 0 002-2V9M9 21H5a2 2 0 01-2-2V9m0 0h18" />
                    </svg>
                  </div>
                  <div>
                    <div className="text-sm font-bold text-white font-mono">No NVIDIA GPU Detected</div>
                    <div className="text-[10px] font-mono text-[#555555] mt-0.5">CPU MODE</div>
                    <div className="text-[10px] font-mono text-[#444444] mt-1">
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
                        : 'border-[#222222] bg-[#111111]'
                    }`}>
                      <span className={`w-1.5 h-1.5 mt-1.5 flex-shrink-0 ${
                        safe && fitsGpu ? 'bg-[#76B900]' :
                        safe ? 'bg-[#f59e0b]' :
                        'bg-[#ef4444]'
                      }`} style={{ clipPath: 'polygon(50% 0%, 100% 50%, 50% 100%, 0% 50%)' }} />
                      <div className="flex-1 min-w-0">
                        <div className="flex items-center gap-2 flex-wrap">
                          <span className="text-xs font-mono font-bold text-white">{rec.model}</span>
                          <span className={`text-[10px] font-mono px-1.5 py-0.5 uppercase ${
                            i === 0 ? 'bg-[#76B900] text-black font-bold' : 'bg-[#222222] text-[#666666]'
                          }`}>{rec.tier}</span>
                          {oom && (
                            <span className={`text-[10px] font-mono px-1.5 py-0.5 uppercase ${
                              oom.fits_gpu ? 'bg-[#76B900]/10 text-[#76B900]' :
                              oom.fits_hybrid ? 'bg-[#f59e0b]/10 text-[#f59e0b]' :
                              'bg-[#ef4444]/10 text-[#ef4444]'
                            }`}>
                              {oom.fits_gpu ? 'GPU FIT' : oom.fits_hybrid ? 'HYBRID' : 'OOM RISK'}
                            </span>
                          )}
                        </div>
                        <div className="text-[10px] font-mono text-[#555555] mt-0.5">{rec.reason}</div>
                        {rec.vram_required_gb > 0 && (
                          <div className="text-[10px] font-mono text-[#444444] mt-0.5">
                            Requires ~{rec.vram_required_gb} GB VRAM
                          </div>
                        )}
                        <div className="mt-1.5 bg-[#0a0a0a] border border-[#222222] px-2 py-1 inline-block">
                          <code className={`text-[10px] font-mono ${i === 0 ? 'text-[#76B900]' : 'text-[#555555]'}`}>
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
              <div className="bg-[#0a0a0a] border border-[#222222] p-4 space-y-2">
                <div className="text-[10px] font-mono text-[#555555] uppercase tracking-wider mb-2">
                  Ollama Optimizations — {gpuRecs.optimizations.architecture}
                </div>
                <div className="grid grid-cols-2 gap-2 text-[10px] font-mono">
                  <div className="flex justify-between">
                    <span className="text-[#444444]">Flash Attention</span>
                    <span className={gpuRecs.optimizations.flash_attention ? 'text-[#76B900]' : 'text-[#555555]'}>
                      {gpuRecs.optimizations.flash_attention ? 'ENABLED' : 'N/A'}
                    </span>
                  </div>
                  <div className="flex justify-between">
                    <span className="text-[#444444]">Parallelism</span>
                    <span className="text-[#999999]">{gpuRecs.optimizations.num_parallel}x</span>
                  </div>
                  <div className="flex justify-between">
                    <span className="text-[#444444]">Context</span>
                    <span className="text-[#999999]">{(gpuRecs.optimizations.recommended_ctx / 1024).toFixed(0)}K</span>
                  </div>
                  <div className="flex justify-between">
                    <span className="text-[#444444]">Quantization</span>
                    <span className="text-[#999999]">{gpuRecs.optimizations.recommended_quant}</span>
                  </div>
                </div>
                {gpuRecs.optimizations.notes.map((note, i) => (
                  <div key={i} className="text-[10px] font-mono text-[#444444]">· {note}</div>
                ))}
              </div>
            )}

            {/* Fallback note for CPU mode */}
            {!gpuLoading && (!gpuInfo || gpuInfo.gpus.length === 0) && (
              <div className="bg-[#0a0a0a] border border-[#222222] p-3">
                <div className="text-[10px] font-mono text-[#555555]">
                  No NVIDIA GPU? Hive still works — Ollama runs on CPU (slower), or use cloud advisors (OpenAI, Anthropic, etc.)
                </div>
              </div>
            )}
          </div>
        )}

        {/* LOCAL AI */}
        {step === 'local-ai' && (
          <div className="space-y-6">
            <div>
              <div className="text-[10px] font-mono text-[#76B900] uppercase tracking-wider mb-1">Step 3</div>
              <h2 className="text-lg font-bold text-white font-mono">Local AI Setup</h2>
              <p className="text-xs font-mono text-[#555555] mt-1">Install NVIDIA Nemotron via Ollama — runs on your GPU, free forever</p>
            </div>

            {/* Ollama status */}
            <div className={`p-4 border ${ollamaStatus === 'online' ? 'border-[#76B900]/40 bg-[#76B900]/5' : 'border-[#333333] bg-[#111111]'}`}>
              <div className="flex items-center gap-3">
                <span className={`w-2 h-2 flex-shrink-0 ${
                  ollamaStatus === 'online' ? 'bg-[#76B900] nvidia-pulse' :
                  ollamaStatus === 'offline' ? 'bg-[#ef4444]' :
                  'bg-[#444444] animate-pulse'
                }`} style={{ clipPath: 'polygon(50% 0%, 100% 50%, 50% 100%, 0% 50%)' }} />
                <div>
                  <div className={`text-sm font-mono font-bold ${ollamaStatus === 'online' ? 'text-[#76B900]' : 'text-white'}`}>
                    Ollama {ollamaStatus === 'checking' ? 'CHECKING...' : ollamaStatus === 'online' ? 'RUNNING' : 'NOT DETECTED'}
                  </div>
                  <div className="text-[10px] font-mono text-[#555555]">
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
                <div className="section-label">Install Ollama</div>
                <div className="bg-[#0a0a0a] border border-[#222222] p-4 font-mono text-sm space-y-2">
                  <div className="text-[#555555] text-[10px] uppercase tracking-wider"># Install Ollama (Linux)</div>
                  <div className="text-[#76B900]">curl -fsSL https://ollama.com/install.sh | sh</div>
                  <div className="text-[#555555] text-[10px] uppercase tracking-wider mt-3"># Start Ollama service</div>
                  <div className="text-[#76B900]">ollama serve</div>
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
                    <div className="text-sm font-mono font-bold text-white">NVIDIA Nemotron Mini (2B)</div>
                    <div className="text-[10px] font-mono text-[#76B900]">~2 GB · Fast · 4K context · Instruction tuned</div>
                    <div className="mt-1 bg-[#0a0a0a] border border-[#222222] px-2 py-1">
                      <code className="text-[10px] font-mono text-[#76B900]">ollama pull nemotron-mini</code>
                    </div>
                  </div>
                </div>
              </div>

              <div className="border border-[#333333] p-4">
                <div className="flex items-start gap-3">
                  <div className="w-8 h-8 bg-[#222222] border border-[#333333] flex items-center justify-center flex-shrink-0 font-bold text-[#999999] text-sm font-mono">N</div>
                  <div>
                    <div className="text-sm font-mono font-bold text-white">NVIDIA Nemotron (8B)</div>
                    <div className="text-[10px] font-mono text-[#555555]">~8 GB · Best quality · 131K context · Tool calling</div>
                    <div className="mt-1 bg-[#0a0a0a] border border-[#222222] px-2 py-1">
                      <code className="text-[10px] font-mono text-[#999999]">ollama pull nemotron</code>
                    </div>
                  </div>
                </div>
              </div>
            </div>

            {/* Quick setup via docker */}
            <div className="bg-[#0a0a0a] border border-[#222222] p-4">
              <div className="text-[10px] font-mono text-[#555555] mb-2 uppercase tracking-wider">Using Docker Compose?</div>
              <code className="text-[10px] font-mono text-[#76B900]">docker compose up -d</code>
              <div className="text-[10px] font-mono text-[#444444] mt-1">
                The docker-compose stack auto-pulls nemotron-mini on first start.
              </div>
            </div>
          </div>
        )}

        {/* CLOUD PROVIDERS */}
        {step === 'cloud' && (
          <div className="space-y-6">
            <div>
              <div className="text-[10px] font-mono text-[#76B900] uppercase tracking-wider mb-1">Step 4</div>
              <h2 className="text-lg font-bold text-white font-mono">Cloud Providers</h2>
              <p className="text-xs font-mono text-[#555555] mt-1">
                Optional — add API keys for cloud providers. Local Nemotron works without any keys.
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
                  <div key={i} className="h-16 bg-[#111111] border border-[#222222]" />
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
                        <span className="text-[10px] font-mono text-[#f59e0b] bg-[#f59e0b]/10 px-1.5 py-0.5">PAID / LIMITED FREE</span>
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
                  <div key={provider.id} className="border border-[#222222] bg-[#111111] p-4">
                    <div className="flex items-center justify-between mb-2">
                      <div>
                        <div className="text-sm font-mono font-bold text-white">{provider.name}</div>
                        <div className="text-[10px] font-mono text-[#555555]">{provider.description}</div>
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
                      Or set as env var: <span className="text-[#555555]">{provider.envKey}=your-key</span>
                    </div>
                  </div>
                ))}
              </div>
            )}

            <div className="bg-[#0a0a0a] border border-[#222222] p-3">
              <div className="text-[10px] font-mono text-[#555555]">
                API keys entered here are saved via the Hive API. You can also set them as env vars in your <span className="text-[#76B900]">.env</span> file.
              </div>
            </div>
          </div>
        )}

        {/* TEST */}
        {step === 'test' && (
          <div className="space-y-6">
            <div>
              <div className="text-[10px] font-mono text-[#76B900] uppercase tracking-wider mb-1">Step 5</div>
              <h2 className="text-lg font-bold text-white font-mono">Quick Test</h2>
              <p className="text-xs font-mono text-[#555555] mt-1">Verify everything is working correctly</p>
            </div>

            {/* API status */}
            <div className={`p-4 border ${apiStatus === 'connected' ? 'border-[#76B900]/40 bg-[#76B900]/5' : 'border-[#ef4444]/40 bg-[#ef4444]/5'}`}>
              <div className="flex items-center gap-3">
                <span className={`w-2 h-2 flex-shrink-0 ${apiStatus === 'connected' ? 'bg-[#76B900]' : 'bg-[#ef4444]'}`}
                  style={{ clipPath: 'polygon(50% 0%, 100% 50%, 50% 100%, 0% 50%)' }} />
                <div>
                  <div className={`text-sm font-mono font-bold ${apiStatus === 'connected' ? 'text-[#76B900]' : 'text-[#ef4444]'}`}>
                    Hive API {apiStatus === 'connected' ? 'ONLINE' : 'OFFLINE'}
                  </div>
                  {apiStatus === 'disconnected' && (
                    <div className="text-[10px] font-mono text-[#555555] mt-0.5">
                      Start the server: <span className="text-[#76B900]">council serve</span>
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
              <div className="bg-[#0a0a0a] border border-[#222222] p-3">
                <div className="text-[10px] font-mono text-[#555555] mb-1">SENDING:</div>
                <div className="text-xs font-mono text-[#999999]">{testPrompt}</div>
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
                <div className="bg-[#f59e0b]/5 border border-[#f59e0b]/20 p-3">
                  <div className="text-xs font-mono text-[#f59e0b]">
                    API server not connected. Start it with: nvh serve
                  </div>
                </div>
              )}

              {testError && (
                <div className="bg-[#ef4444]/5 border border-[#ef4444]/20 p-3">
                  <div className="text-[10px] font-mono text-[#ef4444] uppercase tracking-wider mb-1">Error</div>
                  <div className="text-xs font-mono text-[#ef4444]">{testError}</div>
                  <button
                    onClick={handleTest}
                    className="mt-2 text-xs font-mono text-[#f59e0b] hover:underline"
                  >
                    Retry
                  </button>
                </div>
              )}

              {testResult && (
                <div className="bg-[#76B900]/5 border border-[#76B900]/30 p-3">
                  <div className="text-[10px] font-mono text-[#76B900] uppercase tracking-wider mb-1">Response</div>
                  <div className="text-sm font-mono text-white">{testResult}</div>
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
                <h2 className="text-xl font-bold text-white font-mono">SETUP COMPLETE</h2>
                <p className="text-xs font-mono text-[#76B900] mt-1">Hive AI Command Center is ready</p>
              </div>
            </div>

            {/* Summary */}
            <div className="space-y-2">
              <div className="section-label">Configuration Summary</div>
              <div className="space-y-2">
                {[
                  { label: 'Local AI', value: ollamaStatus === 'online' ? 'Ollama Running' : 'Not configured', ok: ollamaStatus === 'online' },
                  { label: 'NVIDIA Nemotron', value: 'Recommended model', ok: true },
                  { label: 'Hive API', value: apiStatus === 'connected' ? 'Online' : 'Offline', ok: apiStatus === 'connected' },
                  { label: 'Active Advisors', value: configuredProviders.length > 0 ? configuredProviders.join(', ') : 'None yet', ok: configuredProviders.length > 0 },
                ].map(item => (
                  <div key={item.label} className="flex items-center gap-3 px-3 py-2 bg-[#111111] border border-[#1a1a1a]">
                    <span className={`w-1.5 h-1.5 flex-shrink-0 ${item.ok ? 'bg-[#76B900]' : 'bg-[#ef4444]'}`}
                      style={{ clipPath: 'polygon(50% 0%, 100% 50%, 50% 100%, 0% 50%)' }} />
                    <span className="text-[10px] font-mono text-[#666666] uppercase w-32">{item.label}</span>
                    <span className="text-xs font-mono text-white">{item.value}</span>
                  </div>
                ))}
              </div>
            </div>

            {/* Quick start commands */}
            <div className="bg-[#0a0a0a] border border-[#222222] p-4 space-y-2">
              <div className="text-[10px] font-mono text-[#555555] uppercase tracking-wider mb-2">Quick Commands</div>
              <div className="text-[10px] font-mono text-[#555555]"># Pull Nemotron (if not done yet)</div>
              <div className="text-[10px] font-mono text-[#76B900]">ollama pull nemotron-mini</div>
              <div className="text-[10px] font-mono text-[#555555] mt-2"># Or use the full stack</div>
              <div className="text-[10px] font-mono text-[#76B900]">docker compose up -d</div>
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
        <div className="flex items-center justify-between mt-8 pt-6 border-t border-[#1a1a1a]">
          <button
            onClick={() => {
              const idx = STEPS.findIndex(s => s.id === step);
              if (idx > 0) setStep(STEPS[idx - 1].id);
            }}
            disabled={step === 'welcome'}
            className="btn-ghost px-4 py-2 text-xs font-mono uppercase tracking-wider disabled:opacity-30"
          >
            ← Back
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
              Next →
            </button>
          ) : (
            <Link href="/" className="btn-primary px-6 py-2 text-xs font-mono uppercase tracking-wider">
              Done →
            </Link>
          )}
        </div>
      </div>
    </div>
  );
}
