'use client';

import { useEffect, useState, useCallback } from 'react';
import Link from 'next/link';
import ProviderCard from '@/components/ProviderCard';
import { getModels, getGPUInfo, getRecommendations, getFreeProviders, saveProviderKey } from '@/lib/api';
import { useProviderHealth } from '@/lib/useProviderHealth';
import type { ModelInfo, GPUInfo, RecommendationsResult, FreeProvider } from '@/lib/types';

const MODEL_STATUS_COLORS: Record<string, string> = {
  available: '#76B900',
  deprecated: '#d97706',
  unavailable: '#dc2626',
};

function CloudKeyCard({
  provider,
  expanded,
  saved,
  saving,
  keyValue,
  error,
  onExpand,
  onChange,
  onSave,
}: {
  provider: FreeProvider;
  expanded: boolean;
  saved: boolean;
  saving: boolean;
  keyValue: string;
  error?: string;
  onExpand: () => void;
  onChange: (value: string) => void;
  onSave: () => void;
}) {
  const configured = provider.configured || saved;
  const keyUrl = provider.key_url || provider.signup_url;

  return (
    <div className={`border p-4 bg-white ${configured ? 'border-[#76B900]/40' : 'border-[#e5e5e5]'}`}>
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex items-center gap-2 flex-wrap">
            <span className="text-sm font-mono font-bold text-[#0a0a0a]">{provider.display_name || provider.name}</span>
            <span className={`text-[10px] font-mono px-1.5 py-0.5 border ${configured ? 'border-[#76B900]/30 text-[#76B900] bg-[#76B900]/10' : 'border-[#d97706]/30 text-[#d97706] bg-[#d97706]/5'}`}>
              {configured ? 'CONNECTED' : provider.signup_tier.toUpperCase()}
            </span>
          </div>
          <div className="text-[10px] font-mono text-[#737373] mt-1">
            {provider.free_tier_limits || provider.daily_limit || 'Optional cloud provider'}
          </div>
          {provider.env_key && (
            <div className="text-[10px] font-mono text-[#a3a3a3] mt-1">{provider.env_key}</div>
          )}
        </div>
        <div className="flex gap-2 flex-shrink-0">
          {keyUrl && (
            <a href={keyUrl} target="_blank" rel="noopener noreferrer" className="btn-ghost px-2 py-1 text-[10px] font-mono uppercase">
              Key
            </a>
          )}
          <button type="button" onClick={onExpand} disabled={configured} className="btn-secondary px-2 py-1 text-[10px] font-mono uppercase disabled:opacity-40">
            {expanded ? 'Close' : configured ? 'Saved' : 'Add Key'}
          </button>
        </div>
      </div>

      {provider.strengths && provider.strengths.length > 0 && (
        <div className="flex flex-wrap gap-1 mt-3">
          {provider.strengths.slice(0, 3).map(strength => (
            <span key={strength} className="text-[9px] font-mono text-[#737373] bg-[#f5f5f5] border border-[#e5e5e5] px-1.5 py-0.5">
              {strength}
            </span>
          ))}
        </div>
      )}

      {expanded && !configured && (
        <div className="border-t border-[#e5e5e5] mt-3 pt-3 space-y-2">
          <div className="text-[10px] font-mono text-[#737373]">
            Open the key link, paste the key here, and nvHive saves it under the rootless workspace config.
          </div>
          <div className="flex gap-2">
            <input
              type="password"
              value={keyValue}
              onChange={event => onChange(event.target.value)}
              placeholder={provider.placeholder || 'Paste API key...'}
              className="input-base flex-1 px-3 py-2 text-xs font-mono"
              autoComplete="off"
              spellCheck={false}
            />
            <button type="button" onClick={onSave} disabled={saving || !keyValue.trim()} className="btn-primary px-3 py-2 text-xs font-mono disabled:opacity-40">
              {saving ? 'Saving' : 'Save'}
            </button>
          </div>
          <div className="flex gap-3 text-[10px] font-mono">
            {keyUrl && <a href={keyUrl} target="_blank" rel="noopener noreferrer" className="text-[#76B900] hover:underline">Get API key</a>}
            {provider.docs_url && <a href={provider.docs_url} target="_blank" rel="noopener noreferrer" className="text-[#737373] hover:text-[#76B900]">Docs</a>}
          </div>
          {error && <div className="text-[10px] font-mono text-[#dc2626]">{error}</div>}
        </div>
      )}
    </div>
  );
}

export default function ProvidersPage() {
  // Live-polled provider health with a manual refresh escape hatch.
  const { providers, loading, refresh: loadProviders } = useProviderHealth();
  const [models, setModels] = useState<ModelInfo[]>([]);
  const [modelsLoading, setModelsLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [selectedProvider, setSelectedProvider] = useState<string>('');
  const [modelSearch, setModelSearch] = useState('');
  const [activeTab, setActiveTab] = useState<'providers' | 'models' | 'local-ai'>('providers');
  const [gpuInfo, setGpuInfo] = useState<GPUInfo | null>(null);
  const [gpuRecs, setGpuRecs] = useState<RecommendationsResult | null>(null);
  const [gpuLoading, setGpuLoading] = useState(true);
  const [freeProviders, setFreeProviders] = useState<FreeProvider[]>([]);
  const [freeProvidersLoading, setFreeProvidersLoading] = useState(true);
  const [expandedProvider, setExpandedProvider] = useState<string | null>(null);
  const [keyInputs, setKeyInputs] = useState<Record<string, string>>({});
  const [savingKey, setSavingKey] = useState<string | null>(null);
  const [savedKeys, setSavedKeys] = useState<Set<string>>(new Set());
  const [keyErrors, setKeyErrors] = useState<Record<string, string>>({});

  const loadModels = useCallback(async (provider?: string) => {
    setModelsLoading(true);
    try {
      const data = await getModels(provider || undefined);
      setModels(data.models);
    } catch {
      setModels([]);
    } finally {
      setModelsLoading(false);
    }
  }, []);

  const loadCloudKeys = useCallback(async () => {
    setFreeProvidersLoading(true);
    try {
      const data = await getFreeProviders();
      setFreeProviders(data.providers);
    } catch {
      setFreeProviders([]);
    } finally {
      setFreeProvidersLoading(false);
    }
  }, []);

  useEffect(() => {
    loadModels();
    loadCloudKeys();
    // GPU info for Local AI tab
    setGpuLoading(true);
    Promise.all([getGPUInfo(), getRecommendations()])
      .then(([gpu, recs]) => {
        setGpuInfo(gpu);
        setGpuRecs(recs);
      })
      .catch(() => {})
      .finally(() => setGpuLoading(false));
  }, [loadModels, loadCloudKeys]);

  const handleSaveProviderKey = async (providerId: string) => {
    const apiKey = keyInputs[providerId]?.trim();
    if (!apiKey) return;
    setSavingKey(providerId);
    setKeyErrors(prev => ({ ...prev, [providerId]: '' }));
    try {
      await saveProviderKey(providerId, apiKey);
      setSavedKeys(prev => {
        const next = new Set(prev);
        next.add(providerId);
        return next;
      });
      setExpandedProvider(null);
      setKeyInputs(prev => ({ ...prev, [providerId]: '' }));
      await Promise.all([loadCloudKeys(), loadProviders()]);
    } catch (err) {
      setKeyErrors(prev => ({
        ...prev,
        [providerId]: err instanceof Error ? err.message : 'Could not save this key',
      }));
    } finally {
      setSavingKey(null);
    }
  };

  const handleProviderFilter = (p: string) => {
    setSelectedProvider(p);
    loadModels(p || undefined);
  };

  const filteredModels = models.filter(m => {
    if (!modelSearch) return true;
    const q = modelSearch.toLowerCase();
    return (
      m.model_id.toLowerCase().includes(q) ||
      m.display_name.toLowerCase().includes(q) ||
      m.provider.toLowerCase().includes(q)
    );
  });

  // Separate local (Ollama/Nemotron) models from cloud
  const localModels = filteredModels.filter(m => m.provider === 'ollama');
  const cloudModels = filteredModels.filter(m => m.provider !== 'ollama');
  const nemotronModels = filteredModels.filter(m =>
    m.model_id.toLowerCase().includes('nemotron')
  );

  const healthyCount = providers.filter(p => p.healthy).length;
  const cloudKeyProviders = freeProviders.filter(p => p.requires_key !== false);

  return (
    <div className="p-6 space-y-6 max-w-7xl mx-auto">
      {/* Header */}
      <div className="nvidia-corner relative border border-[#d4d4d4] bg-[#ffffff] p-5 overflow-hidden">
        <div className="absolute top-0 left-0 right-0 h-px bg-gradient-to-r from-transparent via-[#76B900] to-transparent" />
        <div className="relative flex items-center justify-between">
          <div>
            <div className="text-[10px] font-mono text-[#76B900] tracking-[0.2em] uppercase mb-0.5">AI Connections</div>
            <h1 className="text-2xl font-bold text-[#0a0a0a]">AI Connections</h1>
            <p className="text-xs font-mono text-[#737373] mt-1">
              {providers.length > 0
                ? `${healthyCount} of ${providers.length} AI connections healthy`
                : 'Install a local GPU model or connect optional cloud providers'}
            </p>
          </div>
          <button
            onClick={loadProviders}
            disabled={loading}
            className="btn-secondary px-4 py-2 text-xs font-mono flex items-center gap-2"
          >
            <svg className={`w-4 h-4 ${loading ? 'animate-spin' : ''}`} fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
              <path strokeLinecap="round" strokeLinejoin="round"
                d="M16.023 9.348h4.992v-.001M2.985 19.644v-4.992m0 0h4.992m-4.993 0l3.181 3.183a8.25 8.25 0 0013.803-3.7M4.031 9.865a8.25 8.25 0 0113.803-3.7l3.181 3.182m0-4.991v4.99" />
            </svg>
            CHECK AGAIN
          </button>
        </div>
      </div>

      {/* Status summary */}
      {providers.length > 0 && (
        <div className="grid grid-cols-3 gap-4">
          <div className="card p-4 text-center">
            <div className="text-2xl font-bold font-mono text-[#76B900]">{healthyCount}</div>
            <div className="text-[10px] font-mono text-[#a3a3a3] mt-1 uppercase tracking-wider">Healthy</div>
          </div>
          <div className="card p-4 text-center">
            <div className="text-2xl font-bold font-mono text-[#dc2626]">{providers.length - healthyCount}</div>
            <div className="text-[10px] font-mono text-[#a3a3a3] mt-1 uppercase tracking-wider">Down</div>
          </div>
          <div className="card p-4 text-center">
            <div className="text-2xl font-bold font-mono text-[#76B900]">
              {providers.reduce((s, p) => s + p.models_available, 0)}
            </div>
            <div className="text-[10px] font-mono text-[#a3a3a3] mt-1 uppercase tracking-wider">Total Models</div>
          </div>
        </div>
      )}

      {/* NVIDIA Nemotron featured card */}
      {(providers.some(p => p.name === 'ollama' && p.healthy) || nemotronModels.length > 0) && (
        <div className="border border-[#76B900]/40 bg-[#76B900]/5 p-4 nvidia-corner relative overflow-hidden">
          <div className="absolute top-0 left-0 right-0 h-px bg-[#76B900]/40" />
          <div className="flex items-center gap-4">
            <div className="w-12 h-12 bg-[#76B900]/10 border border-[#76B900]/30 flex items-center justify-center flex-shrink-0 font-mono font-bold text-[#76B900] text-lg">N</div>
            <div className="flex-1">
              <div className="flex items-center gap-2 mb-1">
                <span className="text-sm font-bold text-[#0a0a0a]">NVIDIA Nemotron</span>
                <span className="text-[10px] font-mono px-1.5 py-0.5 bg-[#76B900] text-black font-bold">RECOMMENDED</span>
              </div>
              <div className="text-[10px] font-mono text-[#76B900]">
                Local · Free · NVIDIA GPU Optimized · 131K context
              </div>
              <div className="text-[10px] font-mono text-[#a3a3a3] mt-0.5">
                Run NVIDIA&apos;s Nemotron models locally on your GPU via Ollama — zero cost, full privacy
              </div>
            </div>
            <div className="text-right hidden sm:block">
              <div className="text-[10px] font-mono text-[#a3a3a3]">via Ollama</div>
              <div className="text-[10px] font-mono text-[#76B900]">$0.00 / 1M tokens</div>
            </div>
          </div>
        </div>
      )}

      {/* Tabs */}
      <div className="flex gap-0 border border-[#d4d4d4] w-fit">
        {(['providers', 'models', 'local-ai'] as const).map(tab => (
          <button
            key={tab}
            onClick={() => setActiveTab(tab)}
            className={`px-6 py-2 text-xs font-mono uppercase tracking-wider transition-all ${
              activeTab === tab
                ? 'bg-[#76B900] text-black font-bold'
                : 'text-[#a3a3a3] hover:text-[#525252] hover:bg-[#f5f5f5]'
            }`}
          >
            {tab === 'local-ai' ? 'My GPU Model' : tab === 'providers' ? 'Connections' : 'Available Models'}
            {tab === 'providers' && providers.length > 0 && (
              <span className={`ml-2 text-[10px] px-1 ${activeTab === tab ? 'text-black' : 'text-[#a3a3a3]'}`}>
                {providers.length}
              </span>
            )}
            {tab === 'models' && models.length > 0 && (
              <span className={`ml-2 text-[10px] px-1 ${activeTab === tab ? 'text-black' : 'text-[#a3a3a3]'}`}>
                {models.length}
              </span>
            )}
          </button>
        ))}
      </div>

      {/* Providers tab */}
      {activeTab === 'providers' && (
        <div className="space-y-6">
          <div className="border border-[#d4d4d4] bg-[#ffffff] p-5">
            <div className="flex flex-col sm:flex-row sm:items-start justify-between gap-3 mb-4">
              <div>
                <div className="section-label">Connect Cloud API Keys</div>
                <p className="text-xs font-mono text-[#737373] mt-1">
                  Optional cloud providers. nvHive keeps keys in the rootless workspace and refreshes routing after save.
                </p>
              </div>
              <Link href="/setup" className="btn-ghost px-3 py-2 text-[10px] font-mono uppercase tracking-wider w-fit">
                Setup Wizard
              </Link>
            </div>
            {freeProvidersLoading ? (
              <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
                {[1, 2, 3].map(i => (
                  <div key={i} className="h-32 border border-[#e5e5e5] bg-[#fafafa] animate-pulse" />
                ))}
              </div>
            ) : cloudKeyProviders.length === 0 ? (
              <div className="text-xs font-mono text-[#a3a3a3] border border-[#e5e5e5] p-4">
                Cloud key catalog is unavailable. Make sure the Hive API is online, then check again.
              </div>
            ) : (
              <div className="grid grid-cols-1 lg:grid-cols-2 gap-3">
                {cloudKeyProviders.map(provider => (
                  <CloudKeyCard
                    key={provider.id}
                    provider={provider}
                    expanded={expandedProvider === provider.id}
                    saved={savedKeys.has(provider.id)}
                    saving={savingKey === provider.id}
                    keyValue={keyInputs[provider.id] ?? ''}
                    error={keyErrors[provider.id]}
                    onExpand={() => setExpandedProvider(expandedProvider === provider.id ? null : provider.id)}
                    onChange={value => setKeyInputs(prev => ({ ...prev, [provider.id]: value }))}
                    onSave={() => void handleSaveProviderKey(provider.id)}
                  />
                ))}
              </div>
            )}
          </div>

          {error ? (
            <div className="card p-6 text-center border-[#dc2626]/30">
              <div className="text-[#dc2626] font-mono text-sm mb-2">{error}</div>
              <div className="text-[10px] font-mono text-[#a3a3a3] mb-4">
                Make sure the Hive API server is running at localhost:8000
              </div>
              <button onClick={loadProviders} className="btn-secondary px-4 py-2 text-xs font-mono">
                RETRY
              </button>
            </div>
          ) : loading ? (
            <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4">
              {[1, 2, 3, 4, 5, 6].map(i => (
                <div key={i} className="card p-5 h-52 animate-pulse">
                  <div className="flex items-center gap-3 mb-4">
                    <div className="w-10 h-10 bg-[#e5e5e5]" />
                    <div className="flex-1">
                      <div className="h-3 bg-[#f5f5f5] mb-2 w-1/2" />
                      <div className="h-2 bg-[#f5f5f5] w-1/4" />
                    </div>
                  </div>
                  <div className="h-12 bg-[#f5f5f5] mb-3" />
                  <div className="h-8 bg-[#f5f5f5]" />
                </div>
              ))}
            </div>
          ) : providers.length === 0 ? (
            <div className="card p-10 text-center">
              <div className="text-4xl mb-4 text-[#333333]">▣</div>
              <div className="text-base font-mono font-bold text-[#737373] mb-2 uppercase">No AI Connections Yet</div>
              <div className="text-xs font-mono text-[#a3a3a3] max-w-md mx-auto mb-6">
                Start with the recommended local GPU model. Cloud keys can stay optional until you need them.
              </div>
              <div className="bg-[#ffffff] border border-[#e5e5e5] p-4 text-left max-w-sm mx-auto">
                <div className="section-label mb-2">Recommended Next Step</div>
                <div className="font-mono text-xs text-[#737373] space-y-1">
                  <div className="text-[#a3a3a3]">Use the wizard to install without sudo.</div>
                  <Link href="/setup" className="inline-flex bg-[#76B900] px-3 py-2 text-black font-bold mt-2">
                    Install best model for this GPU
                  </Link>
                </div>
              </div>
            </div>
          ) : (
            <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4">
              {providers.map(p => (
                <ProviderCard key={p.name} provider={p} onRefresh={loadProviders} />
              ))}
            </div>
          )}
        </div>
      )}

      {/* Local AI tab */}
      {activeTab === 'local-ai' && (
        <div className="space-y-6">
          {gpuLoading ? (
            <div className="space-y-4">
              {[1, 2].map(i => (
                <div key={i} className="card p-5 h-32 animate-pulse">
                  <div className="h-3 bg-[#f5f5f5] w-1/3 mb-3" />
                  <div className="h-2 bg-[#f5f5f5] mb-2" />
                  <div className="h-2 bg-[#f5f5f5] w-3/4" />
                </div>
              ))}
            </div>
          ) : (
            <>
              {/* GPU Hardware */}
              <div>
                <div className="flex items-center gap-2 mb-3">
                  <div className="section-label">GPU Hardware</div>
                  <div className="flex-1 h-px bg-[#e5e5e5]" />
                </div>

                {gpuInfo && gpuInfo.gpus.length > 0 ? (
                  <div className="space-y-3">
                    {gpuInfo.gpus.map((g, i) => {
                      const usedPct = g.vram_mb > 0 ? Math.round((g.memory_used_mb / g.vram_mb) * 100) : 0;
                      const barColor = usedPct > 90 ? '#dc2626' : usedPct > 70 ? '#d97706' : '#76B900';
                      return (
                        <div key={i} className="border border-[#76B900]/30 bg-[#76B900]/5 p-4">
                          <div className="flex items-start gap-4">
                            <div className="w-12 h-12 bg-[#76B900]/10 border border-[#76B900]/20 flex items-center justify-center flex-shrink-0">
                              <svg className="w-6 h-6 text-[#76B900]" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
                                <path strokeLinecap="round" strokeLinejoin="round"
                                  d="M9 3H5a2 2 0 00-2 2v4m6-6h10a2 2 0 012 2v4M9 3v18m0 0h10a2 2 0 002-2V9M9 21H5a2 2 0 01-2-2V9m0 0h18" />
                              </svg>
                            </div>
                            <div className="flex-1 min-w-0">
                              <div className="flex items-center gap-2 mb-1">
                                <span className="text-sm font-bold text-[#0a0a0a] font-mono">{g.name}</span>
                                {gpuInfo.gpus.length > 1 && (
                                  <span className="text-[10px] font-mono text-[#a3a3a3]">GPU {g.index}</span>
                                )}
                              </div>
                              <div className="text-[10px] font-mono text-[#a3a3a3] mb-2 space-x-3">
                                <span>CUDA {g.cuda_version}</span>
                                <span>·</span>
                                <span>driver {g.driver_version}</span>
                                <span>·</span>
                                <span>util {g.utilization_pct}%</span>
                              </div>
                              <div className="space-y-1">
                                <div className="flex justify-between text-[10px] font-mono">
                                  <span className="text-[#a3a3a3]">VRAM</span>
                                  <span className="text-[#525252]">
                                    {(g.memory_used_mb / 1024).toFixed(1)} / {g.vram_gb} GB ({usedPct}% used)
                                  </span>
                                </div>
                                <div className="progress-bar">
                                  <div className="progress-fill" style={{ width: `${usedPct}%`, backgroundColor: barColor }} />
                                </div>
                                <div className="text-[10px] font-mono text-[#a3a3a3]">
                                  {(g.memory_free_mb / 1024).toFixed(1)} GB free
                                </div>
                              </div>
                            </div>
                          </div>
                        </div>
                      );
                    })}

                    {/* System RAM summary */}
                    {gpuInfo.system_ram && (
                      <div className="bg-[#ffffff] border border-[#e5e5e5] p-3 grid grid-cols-3 gap-3 text-center">
                        <div>
                          <div className="text-sm font-bold font-mono text-[#0a0a0a]">{gpuInfo.system_ram.total_gb} GB</div>
                          <div className="text-[10px] font-mono text-[#a3a3a3] mt-0.5">Total RAM</div>
                        </div>
                        <div>
                          <div className="text-sm font-bold font-mono text-[#76B900]">{gpuInfo.system_ram.available_gb} GB</div>
                          <div className="text-[10px] font-mono text-[#a3a3a3] mt-0.5">Available</div>
                        </div>
                        <div>
                          <div className="text-sm font-bold font-mono text-[#d97706]">{gpuInfo.system_ram.effective_for_llm_gb} GB</div>
                          <div className="text-[10px] font-mono text-[#a3a3a3] mt-0.5">LLM Offload</div>
                        </div>
                      </div>
                    )}
                  </div>
                ) : (
                  <div className="card p-6 text-center">
                    <div className="text-2xl mb-2 text-[#333333]">▣</div>
                    <div className="text-xs font-mono text-[#a3a3a3] uppercase mb-1">No NVIDIA GPU Detected</div>
                    <div className="text-[10px] font-mono text-[#a3a3a3]">
                      CPU mode — local models will run on CPU (slower). Cloud providers work normally.
                    </div>
                  </div>
                )}
              </div>

              {/* Ollama Status */}
              <div>
                <div className="flex items-center gap-2 mb-3">
                  <div className="section-label">Ollama Status</div>
                  <div className="flex-1 h-px bg-[#e5e5e5]" />
                </div>
                {(() => {
                  const ollamaProvider = providers.find(p => p.name === 'ollama');
                  const online = ollamaProvider?.healthy ?? false;
                  return (
                    <div className={`p-4 border ${online ? 'border-[#76B900]/30 bg-[#76B900]/5' : 'border-[#d4d4d4] bg-[#ffffff]'}`}>
                      <div className="flex items-center gap-3">
                        <span className={`w-2 h-2 flex-shrink-0 ${online ? 'bg-[#76B900] nvidia-pulse' : 'bg-[#a3a3a3]'}`}
                          style={{ clipPath: 'polygon(50% 0%, 100% 50%, 50% 100%, 0% 50%)' }} />
                        <div className="flex-1">
                          <div className={`text-sm font-mono font-bold ${online ? 'text-[#76B900]' : 'text-[#a3a3a3]'}`}>
                            Ollama {online ? 'RUNNING' : 'NOT DETECTED'}
                          </div>
                          <div className="text-[10px] font-mono text-[#a3a3a3]">
                            {online
                              ? `${ollamaProvider?.models_available ?? 0} model(s) installed at localhost:11434`
                              : 'Install local AI without sudo: nvh studio --install rootless-ollama -y && nvhive-ollama-serve'}
                          </div>
                        </div>
                        {online && ollamaProvider?.latency_ms != null && (
                          <div className="text-right">
                            <div className="text-sm font-mono font-bold text-[#0a0a0a]">{ollamaProvider.latency_ms}ms</div>
                            <div className="text-[10px] font-mono text-[#a3a3a3]">latency</div>
                          </div>
                        )}
                      </div>
                    </div>
                  );
                })()}
              </div>

              {/* Installed models vs recommended */}
              <div>
                <div className="flex items-center gap-2 mb-3">
                  <div className="section-label">Models — Installed vs Recommended</div>
                  <div className="flex-1 h-px bg-[#e5e5e5]" />
                </div>

                {gpuRecs && gpuRecs.recommendations.length > 0 ? (
                  <div className="space-y-2">
                    {gpuRecs.recommendations.map((rec, i) => {
                      const isInstalled = localModels.some(m => m.model_id === rec.model || m.model_id.startsWith(rec.model + ':'));
                      const oom = gpuRecs.oom_check[rec.model];
                      return (
                        <div key={i} className={`p-3 border ${
                          isInstalled ? 'border-[#76B900]/30 bg-[#76B900]/5' : 'border-[#e5e5e5] bg-[#ffffff]'
                        }`}>
                          <div className="flex items-start gap-3">
                            <span className={`w-1.5 h-1.5 mt-1.5 flex-shrink-0 ${
                              isInstalled ? 'bg-[#76B900]' : 'bg-[#a3a3a3]'
                            }`} style={{ clipPath: 'polygon(50% 0%, 100% 50%, 50% 100%, 0% 50%)' }} />
                            <div className="flex-1 min-w-0">
                              <div className="flex items-center gap-2 flex-wrap">
                                <span className="text-xs font-mono font-bold text-[#0a0a0a]">{rec.model}</span>
                                <span className={`text-[10px] font-mono px-1.5 py-0.5 uppercase ${
                                  i === 0 ? 'bg-[#76B900]/10 text-[#76B900]' : 'bg-[#e5e5e5] text-[#a3a3a3]'
                                }`}>{rec.tier}</span>
                                {isInstalled && (
                                  <span className="text-[10px] font-mono px-1.5 py-0.5 bg-[#76B900] text-black font-bold">INSTALLED</span>
                                )}
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
                              {!isInstalled && (
                                <div className="mt-1.5 inline-flex bg-[#ffffff] border border-[#e5e5e5] px-2 py-1">
                                  <code className="text-[10px] font-mono text-[#a3a3a3]">ollama pull {rec.model}</code>
                                </div>
                              )}
                            </div>
                            {rec.vram_required_gb > 0 && (
                              <div className="text-right flex-shrink-0">
                                <div className="text-xs font-mono font-bold text-[#525252]">{rec.vram_required_gb} GB</div>
                                <div className="text-[10px] font-mono text-[#a3a3a3]">VRAM</div>
                              </div>
                            )}
                          </div>
                        </div>
                      );
                    })}
                  </div>
                ) : (
                  <div className="card p-6 text-center">
                    <div className="text-xs font-mono text-[#a3a3a3]">No recommendations available</div>
                  </div>
                )}
              </div>

              {/* Ollama Optimizations */}
              {gpuRecs?.optimizations && gpuInfo && gpuInfo.gpus.length > 0 && (
                <div>
                  <div className="flex items-center gap-2 mb-3">
                    <div className="section-label">Ollama Optimizations</div>
                    <div className="flex-1 h-px bg-[#e5e5e5]" />
                    <span className="text-[10px] font-mono text-[#a3a3a3]">{gpuRecs.optimizations.architecture}</span>
                  </div>
                  <div className="card p-4 space-y-3">
                    <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
                      {[
                        { label: 'Flash Attn', value: gpuRecs.optimizations.flash_attention ? 'ENABLED' : 'N/A', ok: gpuRecs.optimizations.flash_attention },
                        { label: 'Parallelism', value: `${gpuRecs.optimizations.num_parallel}x`, ok: true },
                        { label: 'Context', value: `${(gpuRecs.optimizations.recommended_ctx / 1024).toFixed(0)}K`, ok: true },
                        { label: 'Quantization', value: gpuRecs.optimizations.recommended_quant, ok: true },
                      ].map(item => (
                        <div key={item.label} className="bg-[#ffffff] border border-[#e5e5e5] p-3 text-center">
                          <div className={`text-sm font-bold font-mono ${item.ok ? 'text-[#76B900]' : 'text-[#a3a3a3]'}`}>{item.value}</div>
                          <div className="text-[10px] font-mono text-[#a3a3a3] mt-0.5 uppercase">{item.label}</div>
                        </div>
                      ))}
                    </div>
                    <div className="space-y-1">
                      {gpuRecs.optimizations.notes.map((note, i) => (
                        <div key={i} className="text-[10px] font-mono text-[#a3a3a3]">· {note}</div>
                      ))}
                    </div>
                  </div>
                </div>
              )}
            </>
          )}
        </div>
      )}

      {/* Models tab */}
      {activeTab === 'models' && (
        <div className="space-y-4">
          {/* Filters */}
          <div className="flex flex-col sm:flex-row gap-3">
            <div className="relative flex-1">
              <svg className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-[#a3a3a3]" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                <path strokeLinecap="round" strokeLinejoin="round" d="M21 21l-5.197-5.197m0 0A7.5 7.5 0 105.196 5.196a7.5 7.5 0 0010.607 10.607z" />
              </svg>
              <input
                type="text"
                value={modelSearch}
                onChange={e => setModelSearch(e.target.value)}
                placeholder="Search models..."
                className="input-base w-full pl-9 pr-4 py-2.5 text-sm font-mono"
              />
            </div>
            <select
              value={selectedProvider}
              onChange={e => handleProviderFilter(e.target.value)}
              className="input-base px-3 py-2.5 text-sm font-mono min-w-[160px]"
            >
              <option value="">All Providers</option>
              {providers.map(p => (
                <option key={p.name} value={p.name}>{p.name}</option>
              ))}
            </select>
          </div>

          {/* Local models section — highlighted */}
          {localModels.length > 0 && !selectedProvider && (
            <div className="space-y-2">
              <div className="flex items-center gap-2">
                <div className="section-label text-[#76B900]">Local Models (Free)</div>
                <div className="flex-1 h-px bg-[#76B900]/20" />
                <span className="text-[10px] font-mono text-[#76B900]">{localModels.length} installed</span>
              </div>
              <div className="border border-[#76B900]/20 overflow-hidden">
                <table className="w-full text-sm">
                  <tbody>
                    {localModels.map(m => (
                      <ModelRow key={`${m.provider}-${m.model_id}`} m={m} highlight={m.model_id.includes('nemotron')} />
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          )}

          {/* Cloud / all models */}
          {modelsLoading ? (
            <div className="space-y-2">
              {[1, 2, 3, 4, 5].map(i => (
                <div key={i} className="card p-4 h-14 animate-pulse">
                  <div className="flex gap-3">
                    <div className="h-3 bg-[#f5f5f5] w-1/4" />
                    <div className="h-3 bg-[#f5f5f5] w-1/6" />
                    <div className="ml-auto h-3 bg-[#f5f5f5] w-1/6" />
                  </div>
                </div>
              ))}
            </div>
          ) : (cloudModels.length === 0 && localModels.length === 0) ? (
            <div className="card p-8 text-center">
              <div className="text-2xl mb-3 text-[#333333]">◎</div>
              <div className="text-xs font-mono text-[#a3a3a3] uppercase">
                {modelSearch ? 'No models match your search' : 'No models available'}
              </div>
            </div>
          ) : (
            <div className="space-y-2">
              {cloudModels.length > 0 && (
                <>
                  {localModels.length > 0 && !selectedProvider && (
                    <div className="flex items-center gap-2 mt-4">
                      <div className="section-label">Cloud Models</div>
                      <div className="flex-1 h-px bg-[#e5e5e5]" />
                      <span className="text-[10px] font-mono text-[#a3a3a3]">{cloudModels.length} available</span>
                    </div>
                  )}
                  <div className="card overflow-hidden">
                    <div className="overflow-x-auto">
                      <table className="w-full text-sm">
                        <thead>
                          <tr className="border-b border-[#e5e5e5] bg-[#ffffff]">
                            <th className="text-left py-2 px-4 font-mono text-[#a3a3a3] text-[10px] uppercase tracking-wider">Model</th>
                            <th className="text-left py-2 px-4 font-mono text-[#a3a3a3] text-[10px] uppercase tracking-wider">Provider</th>
                            <th className="text-right py-2 px-4 font-mono text-[#a3a3a3] text-[10px] uppercase tracking-wider hidden md:table-cell">Context</th>
                            <th className="text-right py-2 px-4 font-mono text-[#a3a3a3] text-[10px] uppercase tracking-wider hidden lg:table-cell">Input</th>
                            <th className="text-right py-2 px-4 font-mono text-[#a3a3a3] text-[10px] uppercase tracking-wider hidden lg:table-cell">Output</th>
                            <th className="py-2 px-4 font-mono text-[#a3a3a3] text-[10px] uppercase tracking-wider hidden xl:table-cell">Capabilities</th>
                            <th className="text-center py-2 px-4 font-mono text-[#a3a3a3] text-[10px] uppercase tracking-wider">Status</th>
                          </tr>
                        </thead>
                        <tbody>
                          {cloudModels.map(m => (
                            <ModelRow key={`${m.provider}-${m.model_id}`} m={m} tableMode />
                          ))}
                        </tbody>
                      </table>
                    </div>
                    <div className="px-4 py-2 border-t border-[#e5e5e5]">
                      <span className="text-[10px] font-mono text-[#a3a3a3]">{cloudModels.length} models</span>
                    </div>
                  </div>
                </>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function ModelRow({ m, highlight = false, tableMode = false }: { m: ModelInfo; highlight?: boolean; tableMode?: boolean }) {
  const isNemotron = m.model_id.toLowerCase().includes('nemotron');
  const isFree = !m.input_cost_per_1m_tokens || parseFloat(String(m.input_cost_per_1m_tokens)) === 0;

  if (!tableMode) {
    return (
      <tr className={`border-b border-[#e5e5e5] hover:bg-[#76B900]/5 transition-colors ${highlight ? 'bg-[#76B900]/3' : ''}`}>
        <td className="py-3 px-4">
          <div className="flex items-center gap-2">
            {isNemotron && <span className="text-[10px] font-mono px-1 py-0.5 bg-[#76B900] text-black font-bold">N</span>}
            <div>
              <div className="font-mono text-xs text-[#0a0a0a]">{m.model_id}</div>
              {m.display_name && m.display_name !== m.model_id && (
                <div className="text-[10px] font-mono text-[#a3a3a3] mt-0.5">{m.display_name}</div>
              )}
            </div>
          </div>
        </td>
        <td className="py-3 px-4">
          <span className="text-[10px] font-mono px-2 py-1 bg-[#76B900]/10 text-[#76B900] uppercase">
            {m.provider} LOCAL
          </span>
        </td>
        <td className="py-3 px-4 text-right">
          <span className="font-mono text-[10px] text-[#76B900] font-bold">FREE</span>
        </td>
        <td className="py-3 px-4 text-center">
          <span className="text-[10px] font-mono px-2 py-0.5 bg-[#76B900]/10 text-[#76B900]">AVAILABLE</span>
        </td>
      </tr>
    );
  }

  return (
    <tr className="border-b border-[#e5e5e5] hover:bg-[#f5f5f5] transition-colors">
      <td className="py-3 px-4">
        <div className="font-mono text-xs text-[#0a0a0a]">{m.model_id}</div>
        {m.display_name && m.display_name !== m.model_id && (
          <div className="text-[10px] font-mono text-[#a3a3a3] mt-0.5">{m.display_name}</div>
        )}
      </td>
      <td className="py-3 px-4">
        <span className="text-[10px] font-mono px-2 py-1 bg-[#e5e5e5] text-[#737373] uppercase">
          {m.provider}
        </span>
      </td>
      <td className="py-3 px-4 text-right hidden md:table-cell">
        <span className="font-mono text-[10px] text-[#737373]">
          {m.context_window > 0 ? `${(m.context_window / 1000).toFixed(0)}K` : '—'}
        </span>
      </td>
      <td className="py-3 px-4 text-right hidden lg:table-cell">
        <span className="font-mono text-[10px] text-[#d97706]">
          {m.input_cost_per_1m_tokens ? `$${parseFloat(String(m.input_cost_per_1m_tokens)).toFixed(2)}` : (
            isFree ? <span className="text-[#76B900]">FREE</span> : '—'
          )}
        </span>
      </td>
      <td className="py-3 px-4 text-right hidden lg:table-cell">
        <span className="font-mono text-[10px] text-[#d97706]">
          {m.output_cost_per_1m_tokens ? `$${parseFloat(String(m.output_cost_per_1m_tokens)).toFixed(2)}` : (
            isFree ? <span className="text-[#76B900]">FREE</span> : '—'
          )}
        </span>
      </td>
      <td className="py-3 px-4 hidden xl:table-cell">
        <div className="flex gap-1 flex-wrap">
          {m.supports_streaming && <CapBadge label="Stream" />}
          {m.supports_tools && <CapBadge label="Tools" />}
          {m.supports_vision && <CapBadge label="Vision" />}
          {m.supports_json_mode && <CapBadge label="JSON" />}
        </div>
      </td>
      <td className="py-3 px-4 text-center">
        <span
          className="text-[10px] font-mono px-2 py-0.5"
          style={{
            color: MODEL_STATUS_COLORS[m.status] ?? '#a3a3a3',
            backgroundColor: `${MODEL_STATUS_COLORS[m.status] ?? '#a3a3a3'}15`,
          }}
        >
          {m.status?.toUpperCase()}
        </span>
      </td>
    </tr>
  );
}

function CapBadge({ label }: { label: string }) {
  return (
    <span className="text-[10px] font-mono px-1.5 py-0.5 bg-[#f5f5f5] text-[#a3a3a3] border border-[#e5e5e5]">
      {label.toUpperCase()}
    </span>
  );
}
