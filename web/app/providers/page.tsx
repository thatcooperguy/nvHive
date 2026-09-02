'use client';

import { useEffect, useState, useCallback } from 'react';
import Link from 'next/link';
import PageHeader from '@/components/PageHeader';
import ProviderCard from '@/components/ProviderCard';
import UnifiedMemoryTag, { GpuBlockedSummary, MemoryUnreadableTag, gpuStatusOf, isMemoryUnreadable, isUnifiedMemoryPool } from '@/components/UnifiedMemoryTag';
import {
  getModels,
  getGPUInfo,
  getRecommendations,
  getFreeProviders,
  saveProviderKey,
  validateProviderKey,
} from '@/lib/api';
import { useProviderHealth } from '@/lib/useProviderHealth';
import type { ModelInfo, GPUInfo, RecommendationsResult, FreeProvider } from '@/lib/types';

const MODEL_STATUS_COLORS: Record<string, string> = {
  available: '#76B900',
  deprecated: '#d97706',
  unavailable: '#dc2626',
};

const LOBE_ICON_CDN = 'https://cdn.jsdelivr.net/npm/@lobehub/icons-static-png@latest/light';

function ProviderLogo({
  slug,
  name,
  size = 28,
}: {
  slug?: string | null;
  name: string;
  size?: number;
}) {
  // Render a real brand mark from the lobe-icons CDN when we have a slug,
  // otherwise fall back to a typography monogram so the layout stays stable.
  if (slug) {
    return (
      <img
        src={`${LOBE_ICON_CDN}/${slug}.png`}
        alt={`${name} logo`}
        width={size}
        height={size}
        className="flex-shrink-0 rounded-md object-contain"
        loading="lazy"
        onError={(event) => {
          // CDN miss: hide the broken image so the monogram fallback wins.
          (event.currentTarget as HTMLImageElement).style.display = 'none';
        }}
      />
    );
  }
  const initial = (name || '?').trim().charAt(0).toUpperCase();
  return (
    <div
      style={{ width: size, height: size }}
      className="flex flex-shrink-0 items-center justify-center rounded-md bg-[#f5f5f5] text-xs font-bold text-[#737373] dark:bg-[#141414] dark:text-[#a3a3a3]"
    >
      {initial}
    </div>
  );
}

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
  saving: 'idle' | 'validating' | 'saving';
  keyValue: string;
  error?: string;
  onExpand: () => void;
  onChange: (value: string) => void;
  onSave: () => void;
}) {
  const configured = provider.configured || saved;
  const keyUrl = provider.key_url || provider.signup_url;
  const [revealKey, setRevealKey] = useState(false);

  return (
    <div className={`rounded-lg border p-4 bg-white dark:bg-[#0a0a0a] transition-colors ${configured ? 'border-[#76B900]/40' : 'border-[#e5e5e5] dark:border-[#262626]'}`}>
      <div className="flex items-start justify-between gap-3">
        <div className="flex min-w-0 items-start gap-3">
          <ProviderLogo slug={provider.logo_slug ?? undefined} name={provider.display_name || provider.name} />
          <div className="min-w-0">
            <div className="flex items-center gap-2 flex-wrap">
              <span className="text-sm font-mono font-bold text-[#0a0a0a] dark:text-[#fafafa]">{provider.display_name || provider.name}</span>
              <span className={`text-[10px] font-mono px-1.5 py-0.5 border rounded ${configured ? 'border-[#76B900]/30 text-[#76B900] bg-[#76B900]/10' : 'border-[#d97706]/30 text-[#d97706] bg-[#d97706]/5'}`}>
                {configured ? 'CONNECTED' : provider.signup_tier.toUpperCase()}
              </span>
            </div>
            <div className="text-[10px] font-mono text-[#737373] mt-1 dark:text-[#a3a3a3]">
              {provider.free_tier_limits || provider.daily_limit || 'Optional cloud provider'}
            </div>
            {provider.env_key && (
              <div className="text-[10px] font-mono text-[#a3a3a3] mt-1 dark:text-[#737373]">{provider.env_key}</div>
            )}
          </div>
        </div>
        <div className="flex gap-2 flex-shrink-0">
          {keyUrl && (
            <a href={keyUrl} target="_blank" rel="noopener noreferrer" className="btn-ghost px-2 py-1 text-[10px] font-mono uppercase">
              Get key →
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
            <span key={strength} className="text-[9px] font-mono text-[#737373] bg-[#f5f5f5] border border-[#e5e5e5] px-1.5 py-0.5 rounded dark:bg-[#141414] dark:border-[#262626] dark:text-[#a3a3a3]">
              {strength}
            </span>
          ))}
        </div>
      )}

      {expanded && !configured && (
        <div className="border-t border-[#e5e5e5] mt-3 pt-3 space-y-2 dark:border-[#262626]">
          <div className="text-[10px] font-mono text-[#737373] dark:text-[#a3a3a3]">
            Click <strong className="text-[#76B900]">Get key →</strong> to open the provider&apos;s key page in a new tab, paste the key below, then save. We&apos;ll validate it against the provider before persisting.
          </div>
          <div className="flex gap-2">
            <div className="relative flex-1">
              <input
                type={revealKey ? 'text' : 'password'}
                value={keyValue}
                onChange={event => onChange(event.target.value)}
                placeholder={provider.placeholder || 'Paste API key...'}
                className="input-base w-full px-3 py-2 pr-10 text-xs font-mono"
                autoComplete="off"
                spellCheck={false}
              />
              <button
                type="button"
                onClick={() => setRevealKey(prev => !prev)}
                className="absolute right-1 top-1/2 -translate-y-1/2 rounded p-1 text-[#737373] hover:bg-[#f5f5f5] hover:text-[#0a0a0a] dark:text-[#a3a3a3] dark:hover:bg-[#1f1f1f]"
                aria-label={revealKey ? 'Hide key' : 'Reveal key'}
                tabIndex={-1}
              >
                <svg className="h-3.5 w-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.8}>
                  {revealKey ? (
                    <path strokeLinecap="round" strokeLinejoin="round" d="M3.98 8.223A10.477 10.477 0 001.934 12C3.226 16.338 7.244 19.5 12 19.5c.993 0 1.953-.138 2.863-.395M6.228 6.228A10.45 10.45 0 0112 4.5c4.756 0 8.773 3.162 10.065 7.498a10.523 10.523 0 01-4.293 5.774M6.228 6.228L3 3m3.228 3.228l3.65 3.65m7.894 7.894L21 21m-3.228-3.228l-3.65-3.65m0 0a3 3 0 10-4.243-4.243m4.242 4.242L9.88 9.88" />
                  ) : (
                    <>
                      <path strokeLinecap="round" strokeLinejoin="round" d="M2.036 12.322a1.012 1.012 0 010-.639C3.423 7.51 7.36 4.5 12 4.5c4.638 0 8.573 3.007 9.963 7.178.07.207.07.431 0 .639C20.577 16.49 16.64 19.5 12 19.5c-4.638 0-8.573-3.007-9.963-7.178z" />
                      <path strokeLinecap="round" strokeLinejoin="round" d="M15 12a3 3 0 11-6 0 3 3 0 016 0z" />
                    </>
                  )}
                </svg>
              </button>
            </div>
            <button type="button" onClick={onSave} disabled={saving !== 'idle' || !keyValue.trim()} className="btn-primary px-3 py-2 text-xs font-mono disabled:opacity-40 whitespace-nowrap">
              {saving === 'validating' ? 'Testing...' : saving === 'saving' ? 'Saving' : 'Save'}
            </button>
          </div>
          <div className="flex gap-3 text-[10px] font-mono">
            {keyUrl && <a href={keyUrl} target="_blank" rel="noopener noreferrer" className="text-[#76B900] hover:underline">Get API key</a>}
            {provider.docs_url && <a href={provider.docs_url} target="_blank" rel="noopener noreferrer" className="text-[#737373] hover:text-[#76B900] dark:text-[#a3a3a3]">Docs</a>}
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
  const [savingKey, setSavingKey] = useState<{ id: string; phase: 'validating' | 'saving' } | null>(null);
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

  // Clipboard auto-detect: when the user lands on /providers, peek at the
  // clipboard once and offer to auto-fill the matching provider's key field.
  // Read-only — we never write the clipboard back and never silently save.
  const [clipboardOffer, setClipboardOffer] = useState<{ providerId: string; key: string; preview: string } | null>(null);

  useEffect(() => {
    if (freeProviders.length === 0) return;
    if (typeof navigator === 'undefined' || !navigator.clipboard?.readText) return;
    let cancelled = false;
    void (async () => {
      try {
        const text = (await navigator.clipboard.readText()).trim();
        if (cancelled || !text || text.length > 256 || text.includes(' ')) return;
        // Map common API-key prefixes to provider ids. We match on the
        // visible prefix and let the user confirm — the validate step
        // verifies the actual key before saving.
        const matchers: Array<[RegExp, string]> = [
          [/^sk-proj-/, 'openai'],
          [/^sk-ant-/, 'anthropic'],
          [/^sk-or-/, 'openrouter'],
          [/^sk-/, 'openai'],
          [/^gsk_/, 'groq'],
          [/^xai-/, 'xai'],
          [/^pcsk_/, 'perplexity'],
          [/^AIza/, 'google'],
          [/^nvapi-/, 'nvidia'],
          [/^tvly-/, 'tavily'],
          [/^brv-/, 'brave'],
        ];
        for (const [re, id] of matchers) {
          if (!re.test(text)) continue;
          if (!freeProviders.some(p => p.name === id)) continue;
          if (savedKeys.has(id)) return;
          setClipboardOffer({
            providerId: id,
            key: text,
            preview: `${text.slice(0, 6)}…${text.slice(-4)}`,
          });
          break;
        }
      } catch {
        // Permission denied / no clipboard / hardened browser — silently bail.
      }
    })();
    return () => {
      cancelled = true;
    };
    // Re-run when the list of providers (or already-saved set) changes.
  }, [freeProviders, savedKeys]);

  const acceptClipboardKey = () => {
    if (!clipboardOffer) return;
    setKeyInputs(prev => ({ ...prev, [clipboardOffer.providerId]: clipboardOffer.key }));
    setExpandedProvider(clipboardOffer.providerId);
    setClipboardOffer(null);
  };

  const dismissClipboardKey = () => setClipboardOffer(null);

  // .env bulk import — paste multiple KEY=value lines and we route each to
  // the matching provider via the same validate-then-save path the single
  // input uses. Common .env aliases (OPENAI_API_KEY, GROQ_API_KEY, etc.)
  // are mapped to provider ids via the env_key field on each FreeProvider.
  const [envImportOpen, setEnvImportOpen] = useState(false);
  const [envImportText, setEnvImportText] = useState('');
  const [envImportLog, setEnvImportLog] = useState<{ line: string; status: 'ok' | 'skip' | 'error'; detail?: string }[]>([]);
  const [envImporting, setEnvImporting] = useState(false);

  const handleEnvImport = async () => {
    setEnvImporting(true);
    setEnvImportLog([]);
    const log: { line: string; status: 'ok' | 'skip' | 'error'; detail?: string }[] = [];
    const lines = envImportText
      .split(/\r?\n/)
      .map(l => l.trim())
      .filter(l => l && !l.startsWith('#'));

    // Build a map: env-key -> provider id (e.g. OPENAI_API_KEY -> openai).
    const envIndex = new Map<string, string>();
    for (const p of freeProviders) {
      if (p.env_key) envIndex.set(p.env_key.toUpperCase(), p.name);
    }

    for (const raw of lines) {
      const eq = raw.indexOf('=');
      if (eq <= 0) {
        log.push({ line: raw, status: 'skip', detail: 'no = separator' });
        continue;
      }
      const key = raw.slice(0, eq).trim().toUpperCase().replace(/^EXPORT\s+/, '');
      let val = raw.slice(eq + 1).trim();
      // Strip surrounding quotes for `KEY="value"` shapes.
      if ((val.startsWith('"') && val.endsWith('"')) || (val.startsWith("'") && val.endsWith("'"))) {
        val = val.slice(1, -1);
      }
      const providerId = envIndex.get(key);
      if (!providerId) {
        log.push({ line: key, status: 'skip', detail: 'no matching provider' });
        continue;
      }
      if (!val) {
        log.push({ line: key, status: 'skip', detail: 'empty value' });
        continue;
      }
      try {
        const validation = await validateProviderKey(providerId, val);
        if (!validation.valid) {
          log.push({ line: key, status: 'error', detail: validation.error ?? 'validation failed' });
          continue;
        }
        await saveProviderKey(providerId, val);
        setSavedKeys(prev => new Set(prev).add(providerId));
        log.push({ line: key, status: 'ok', detail: `${providerId} saved` });
      } catch (err) {
        log.push({ line: key, status: 'error', detail: err instanceof Error ? err.message : 'save failed' });
      }
    }
    setEnvImportLog(log);
    setEnvImporting(false);
    // Refresh the provider list so newly-configured ones show CONNECTED.
    void loadCloudKeys();
  };

  const handleSaveProviderKey = async (providerId: string) => {
    const apiKey = keyInputs[providerId]?.trim();
    if (!apiKey) return;
    setKeyErrors(prev => ({ ...prev, [providerId]: '' }));

    // Validate against the provider first so the user gets a real ✗ with the
    // upstream error message instead of a silent save that fails on first query.
    setSavingKey({ id: providerId, phase: 'validating' });
    try {
      const validation = await validateProviderKey(providerId, apiKey);
      if (!validation.valid) {
        setKeyErrors(prev => ({
          ...prev,
          [providerId]: validation.error
            ? `Key rejected: ${validation.error}`
            : 'The provider rejected this key. Double-check you copied it correctly.',
        }));
        setSavingKey(null);
        return;
      }
    } catch (err) {
      // Validation endpoint missing or transport error — fall through and try
      // to save anyway so users on older nvh-api builds don't get stuck.
      console.warn('validate-key failed, attempting save anyway:', err);
    }

    setSavingKey({ id: providerId, phase: 'saving' });
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
    <div>
      <PageHeader
        eyebrow="Local & Cloud"
        title="AI Connections"
        subtitle={
          providers.length > 0
            ? `${healthyCount} of ${providers.length} AI connections healthy`
            : 'Install a local GPU model or connect optional cloud providers'
        }
        trailing={
          <div className="flex items-center gap-2">
            <button
              onClick={() => setEnvImportOpen(true)}
              className="btn-secondary px-3 py-2 text-xs font-mono"
              title="Paste a .env block and we'll wire up every key we recognize"
            >
              IMPORT .ENV
            </button>
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
        }
      />
      <div className="p-6 space-y-6 max-w-7xl mx-auto">

      {/* Status summary */}
      {providers.length > 0 && (
        <div className="grid grid-cols-3 gap-4">
          <div className="card p-4 text-center">
            <div className="text-2xl font-bold font-mono text-[#76B900]">{healthyCount}</div>
            <div className="text-[10px] font-mono text-[#a3a3a3] mt-1 uppercase tracking-wider dark:text-[#737373]">Healthy</div>
          </div>
          <div className="card p-4 text-center">
            <div className="text-2xl font-bold font-mono text-[#dc2626]">{providers.length - healthyCount}</div>
            <div className="text-[10px] font-mono text-[#a3a3a3] mt-1 uppercase tracking-wider dark:text-[#737373]">Down</div>
          </div>
          <div className="card p-4 text-center">
            <div className="text-2xl font-bold font-mono text-[#76B900]">
              {providers.reduce((s, p) => s + p.models_available, 0)}
            </div>
            <div className="text-[10px] font-mono text-[#a3a3a3] mt-1 uppercase tracking-wider dark:text-[#737373]">Total Models</div>
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
                <span className="text-sm font-bold text-[#0a0a0a] dark:text-[#fafafa]">NVIDIA Nemotron</span>
                <span className="text-[10px] font-mono px-1.5 py-0.5 bg-[#76B900] text-black font-bold">RECOMMENDED</span>
              </div>
              <div className="text-[10px] font-mono text-[#76B900]">
                Local · Free · NVIDIA GPU Optimized · 131K context
              </div>
              <div className="text-[10px] font-mono text-[#a3a3a3] mt-0.5 dark:text-[#737373]">
                Run NVIDIA&apos;s Nemotron models locally on your GPU via Ollama — zero cost, full privacy
              </div>
            </div>
            <div className="text-right hidden sm:block">
              <div className="text-[10px] font-mono text-[#a3a3a3] dark:text-[#737373]">via Ollama</div>
              <div className="text-[10px] font-mono text-[#76B900]">$0.00 / 1M tokens</div>
            </div>
          </div>
        </div>
      )}

      {/* .env bulk-import modal */}
      {envImportOpen && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center p-4"
          style={{ background: 'rgba(0,0,0,0.5)' }}
          onClick={(e) => { if (e.target === e.currentTarget) setEnvImportOpen(false); }}
        >
          <div className="w-full max-w-xl rounded-lg border bg-white p-4 shadow-xl dark:bg-[#0a0a0a]">
            <div className="text-[10px] font-mono uppercase tracking-[0.18em] text-[#76B900]">
              Bulk import from .env
            </div>
            <div className="mt-1 text-xs text-[#737373] dark:text-[#a3a3a3]">
              Paste any number of <code className="font-mono">KEY=value</code> lines.
              We&apos;ll match them to providers by env-var name, validate each,
              and save the ones that pass. Existing keys aren&apos;t replaced unless
              the new one validates.
            </div>
            <textarea
              value={envImportText}
              onChange={(e) => setEnvImportText(e.target.value)}
              placeholder={'OPENAI_API_KEY=sk-…\nGROQ_API_KEY=gsk_…\nANTHROPIC_API_KEY=sk-ant-…'}
              rows={8}
              className="mt-3 w-full resize-none rounded-md border border-[#d4d4d4] bg-[#fafafa] p-2 text-xs font-mono dark:bg-[#141414] dark:border-[#404040]"
              spellCheck={false}
            />
            <div className="mt-3 flex gap-2">
              <button
                type="button"
                onClick={handleEnvImport}
                disabled={envImporting || envImportText.trim().length === 0}
                className="btn-primary px-3 py-1.5 text-xs font-mono disabled:opacity-50"
              >
                {envImporting ? 'Validating…' : 'Import + validate'}
              </button>
              <button
                type="button"
                onClick={() => setEnvImportOpen(false)}
                className="btn-secondary px-3 py-1.5 text-xs font-mono"
              >
                Close
              </button>
            </div>
            {envImportLog.length > 0 && (
              <div className="mt-3 max-h-48 overflow-y-auto rounded border border-[#e5e5e5] bg-[#fafafa] p-2 text-[10px] font-mono dark:bg-[#141414] dark:border-[#262626]">
                {envImportLog.map((entry, i) => (
                  <div
                    key={i}
                    style={{
                      color:
                        entry.status === 'ok' ? '#16a34a' :
                          entry.status === 'error' ? '#dc2626' : '#737373',
                    }}
                  >
                    {entry.status === 'ok' ? '✓' : entry.status === 'error' ? '✗' : '·'}{' '}
                    {entry.line}
                    {entry.detail && <span className="text-[#a3a3a3] dark:text-[#737373]"> — {entry.detail}</span>}
                  </div>
                ))}
              </div>
            )}
          </div>
        </div>
      )}

      {/* Clipboard auto-detect — offer to fill the matching key field. */}
      {clipboardOffer && (
        <div
          className="flex items-center justify-between gap-3 rounded-md border px-3 py-2 text-xs"
          style={{
            background: 'rgba(118, 185, 0, 0.08)',
            borderColor: 'rgba(118, 185, 0, 0.4)',
            color: '#0a0a0a',
          }}
        >
          <div>
            <span className="font-mono font-semibold">Clipboard key detected</span> —
            looks like a {clipboardOffer.providerId} key ({clipboardOffer.preview}). Use it?
          </div>
          <div className="flex gap-2">
            <button
              type="button"
              onClick={acceptClipboardKey}
              className="btn-primary px-3 py-1 text-[10px] font-mono"
            >
              Fill {clipboardOffer.providerId}
            </button>
            <button
              type="button"
              onClick={dismissClipboardKey}
              className="btn-ghost px-3 py-1 text-[10px] font-mono"
            >
              Dismiss
            </button>
          </div>
        </div>
      )}

      {/* Tabs */}
      <div className="flex gap-0 border border-[#d4d4d4] w-fit dark:border-[#404040]">
        {(['providers', 'models', 'local-ai'] as const).map(tab => (
          <button
            key={tab}
            onClick={() => setActiveTab(tab)}
            className={`px-6 py-2 text-xs font-mono uppercase tracking-wider transition-all ${
              activeTab === tab
                ? 'bg-[#76B900] text-black font-bold'
                : 'text-[#a3a3a3] hover:text-[#525252] hover:bg-[#f5f5f5] dark:text-[#737373] dark:hover:text-[#d4d4d4] dark:hover:bg-[#141414]'
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
          <div className="border border-[#d4d4d4] bg-[#ffffff] p-5 dark:bg-[#0a0a0a] dark:border-[#404040]">
            <div className="flex flex-col sm:flex-row sm:items-start justify-between gap-3 mb-4">
              <div>
                <div className="section-label">Connect Cloud API Keys</div>
                <p className="text-xs font-mono text-[#737373] mt-1 dark:text-[#a3a3a3]">
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
                  <div key={i} className="h-32 border border-[#e5e5e5] bg-[#fafafa] animate-pulse dark:bg-[#141414] dark:border-[#262626]" />
                ))}
              </div>
            ) : cloudKeyProviders.length === 0 ? (
              <div className="text-xs font-mono text-[#a3a3a3] border border-[#e5e5e5] p-4 dark:border-[#262626] dark:text-[#737373]">
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
                    saving={savingKey?.id === provider.id ? savingKey.phase : 'idle'}
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
              <div className="text-[10px] font-mono text-[#a3a3a3] mb-4 dark:text-[#737373]">
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
                      <div className="h-3 bg-[#f5f5f5] mb-2 w-1/2 dark:bg-[#141414]" />
                      <div className="h-2 bg-[#f5f5f5] w-1/4 dark:bg-[#141414]" />
                    </div>
                  </div>
                  <div className="h-12 bg-[#f5f5f5] mb-3 dark:bg-[#141414]" />
                  <div className="h-8 bg-[#f5f5f5] dark:bg-[#141414]" />
                </div>
              ))}
            </div>
          ) : providers.length === 0 ? (
            <div className="card p-10 text-center">
              <div className="text-4xl mb-4 text-[#333333] dark:text-[#525252]">▣</div>
              <div className="text-base font-mono font-bold text-[#737373] mb-2 uppercase dark:text-[#a3a3a3]">No AI Connections Yet</div>
              <div className="text-xs font-mono text-[#a3a3a3] max-w-md mx-auto mb-6 dark:text-[#737373]">
                Start with the recommended local GPU model. Cloud keys can stay optional until you need them.
              </div>
              <div className="bg-[#ffffff] border border-[#e5e5e5] p-4 text-left max-w-sm mx-auto dark:bg-[#0a0a0a] dark:border-[#262626]">
                <div className="section-label mb-2">Recommended Next Step</div>
                <div className="font-mono text-xs text-[#737373] space-y-1 dark:text-[#a3a3a3]">
                  <div className="text-[#a3a3a3] dark:text-[#737373]">Use the wizard to install without sudo.</div>
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
                  <div className="h-3 bg-[#f5f5f5] w-1/3 mb-3 dark:bg-[#141414]" />
                  <div className="h-2 bg-[#f5f5f5] mb-2 dark:bg-[#141414]" />
                  <div className="h-2 bg-[#f5f5f5] w-3/4 dark:bg-[#141414]" />
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
                      // Visible but unsized: the API keeps the row at 0 MiB so the GPU's name is
                      // still seen; "0.0 / 0 GB" would present that unknown as a figure.
                      const unreadable = isMemoryUnreadable(g);
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
                                <span className="text-sm font-bold text-[#0a0a0a] font-mono dark:text-[#fafafa]">{g.name}</span>
                                {gpuInfo.gpus.length > 1 && (
                                  <span className="text-[10px] font-mono text-[#a3a3a3] dark:text-[#737373]">GPU {g.index}</span>
                                )}
                              </div>
                              <div className="text-[10px] font-mono text-[#a3a3a3] mb-2 space-x-3 dark:text-[#737373]">
                                <span>CUDA {g.cuda_version}</span>
                                <span>·</span>
                                <span>driver {g.driver_version}</span>
                                <span>·</span>
                                <span>util {g.utilization_pct}%</span>
                              </div>
                              <div className="space-y-1">
                                <div className="flex justify-between text-[10px] font-mono">
                                  <span className="text-[#a3a3a3] dark:text-[#737373]">VRAM</span>
                                  {unreadable ? (
                                    <MemoryUnreadableTag summary={gpuInfo.summary} />
                                  ) : (
                                    <span className="text-[#525252] dark:text-[#a3a3a3]">
                                      {(g.memory_used_mb / 1024).toFixed(1)} / {g.vram_gb} GB ({usedPct}% used)
                                      <UnifiedMemoryTag show={g.unified_memory} className="ml-1" />
                                    </span>
                                  )}
                                </div>
                                <div className="progress-bar">
                                  <div className="progress-fill" style={{ width: `${usedPct}%`, backgroundColor: barColor }} />
                                </div>
                                {!unreadable && (
                                  <div className="text-[10px] font-mono text-[#a3a3a3] dark:text-[#737373]">
                                    {(g.memory_free_mb / 1024).toFixed(1)} GB free
                                  </div>
                                )}
                              </div>
                            </div>
                          </div>
                        </div>
                      );
                    })}

                    {/* Once, under the list: why detection is 'blocked' (a visible GPU with no readable memory
                        pool), or which row is unreadable beside sized ones on an otherwise ready machine. */}
                    <GpuBlockedSummary status={gpuStatusOf(gpuInfo)} summary={gpuInfo.summary} gpus={gpuInfo.gpus} />

                    {/* System RAM summary. On a unified pool (GB10 / DGX Spark) the RAM *is* the
                        GPU memory, so the "LLM Offload" tile would advertise headroom that does not exist. */}
                    {gpuInfo.system_ram && (
                      <div className="bg-[#ffffff] border border-[#e5e5e5] p-3 grid grid-cols-3 gap-3 text-center dark:bg-[#0a0a0a] dark:border-[#262626]">
                        <div>
                          <div className="text-sm font-bold font-mono text-[#0a0a0a] dark:text-[#fafafa]">{gpuInfo.system_ram.total_gb} GB</div>
                          <div className="text-[10px] font-mono text-[#a3a3a3] mt-0.5 dark:text-[#737373]">Total RAM</div>
                        </div>
                        <div>
                          <div className="text-sm font-bold font-mono text-[#76B900]">{gpuInfo.system_ram.available_gb} GB</div>
                          <div className="text-[10px] font-mono text-[#a3a3a3] mt-0.5 dark:text-[#737373]">Available</div>
                        </div>
                        {isUnifiedMemoryPool(gpuInfo) ? (
                          <div>
                            <div className="text-sm font-bold font-mono text-[#0a0a0a] dark:text-[#fafafa]">Unified pool</div>
                            <div className="text-[10px] font-mono text-[#a3a3a3] mt-0.5 dark:text-[#737373]">No separate CPU-offload headroom</div>
                          </div>
                        ) : (
                          <div>
                            <div className="text-sm font-bold font-mono text-[#d97706]">{gpuInfo.system_ram.effective_for_llm_gb} GB</div>
                            <div className="text-[10px] font-mono text-[#a3a3a3] mt-0.5 dark:text-[#737373]">LLM Offload</div>
                          </div>
                        )}
                      </div>
                    )}
                  </div>
                ) : (
                  <div className="card p-6 text-center">
                    <div className="text-2xl mb-2 text-[#333333] dark:text-[#525252]">▣</div>
                    <div className="text-xs font-mono text-[#a3a3a3] uppercase mb-1 dark:text-[#737373]">No NVIDIA GPU Detected</div>
                    <div className="text-[10px] font-mono text-[#a3a3a3] dark:text-[#737373]">
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
                    <div className={`p-4 border ${online ? 'border-[#76B900]/30 bg-[#76B900]/5' : 'border-[#d4d4d4] bg-[#ffffff] dark:border-[#404040] dark:bg-[#0a0a0a]'}`}>
                      <div className="flex items-center gap-3">
                        <span className={`w-2 h-2 flex-shrink-0 ${online ? 'bg-[#76B900] nvidia-pulse' : 'bg-[#a3a3a3]'}`}
                          style={{ clipPath: 'polygon(50% 0%, 100% 50%, 50% 100%, 0% 50%)' }} />
                        <div className="flex-1">
                          <div className={`text-sm font-mono font-bold ${online ? 'text-[#76B900]' : 'text-[#a3a3a3]'}`}>
                            Ollama {online ? 'RUNNING' : 'NOT DETECTED'}
                          </div>
                          <div className="text-[10px] font-mono text-[#a3a3a3] dark:text-[#737373]">
                            {online
                              ? `${ollamaProvider?.models_available ?? 0} model(s) installed at localhost:11434`
                              : 'Install local AI without sudo: nvh studio --install rootless-ollama -y && nvhive-ollama-serve'}
                          </div>
                        </div>
                        {online && ollamaProvider?.latency_ms != null && (
                          <div className="text-right">
                            <div className="text-sm font-mono font-bold text-[#0a0a0a] dark:text-[#fafafa]">{ollamaProvider.latency_ms}ms</div>
                            <div className="text-[10px] font-mono text-[#a3a3a3] dark:text-[#737373]">latency</div>
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
                          isInstalled ? 'border-[#76B900]/30 bg-[#76B900]/5' : 'border-[#e5e5e5] bg-[#ffffff] dark:border-[#262626] dark:bg-[#0a0a0a]'
                        }`}>
                          <div className="flex items-start gap-3">
                            <span className={`w-1.5 h-1.5 mt-1.5 flex-shrink-0 ${
                              isInstalled ? 'bg-[#76B900]' : 'bg-[#a3a3a3]'
                            }`} style={{ clipPath: 'polygon(50% 0%, 100% 50%, 50% 100%, 0% 50%)' }} />
                            <div className="flex-1 min-w-0">
                              <div className="flex items-center gap-2 flex-wrap">
                                <span className="text-xs font-mono font-bold text-[#0a0a0a] dark:text-[#fafafa]">{rec.model}</span>
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
                              <div className="text-[10px] font-mono text-[#a3a3a3] mt-0.5 dark:text-[#737373]">{rec.reason}</div>
                              {!isInstalled && (
                                <div className="mt-1.5 inline-flex bg-[#ffffff] border border-[#e5e5e5] px-2 py-1 dark:bg-[#0a0a0a] dark:border-[#262626]">
                                  <code className="text-[10px] font-mono text-[#a3a3a3] dark:text-[#737373]">ollama pull {rec.model}</code>
                                </div>
                              )}
                            </div>
                            {rec.vram_required_gb > 0 && (
                              <div className="text-right flex-shrink-0">
                                <div className="text-xs font-mono font-bold text-[#525252] dark:text-[#a3a3a3]">{rec.vram_required_gb} GB</div>
                                <div className="text-[10px] font-mono text-[#a3a3a3] dark:text-[#737373]">VRAM</div>
                              </div>
                            )}
                          </div>
                        </div>
                      );
                    })}
                  </div>
                ) : (
                  <div className="card p-6 text-center">
                    <div className="text-xs font-mono text-[#a3a3a3] dark:text-[#737373]">No recommendations available</div>
                  </div>
                )}
              </div>

              {/* Ollama Optimizations */}
              {gpuRecs?.optimizations && gpuInfo && gpuInfo.gpus.length > 0 && (
                <div>
                  <div className="flex items-center gap-2 mb-3">
                    <div className="section-label">Ollama Optimizations</div>
                    <div className="flex-1 h-px bg-[#e5e5e5]" />
                    <span className="text-[10px] font-mono text-[#a3a3a3] dark:text-[#737373]">{gpuRecs.optimizations.architecture}</span>
                  </div>
                  <div className="card p-4 space-y-3">
                    <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
                      {[
                        { label: 'Flash Attn', value: gpuRecs.optimizations.flash_attention ? 'ENABLED' : 'N/A', ok: gpuRecs.optimizations.flash_attention },
                        { label: 'Parallelism', value: `${gpuRecs.optimizations.num_parallel}x`, ok: true },
                        { label: 'Context', value: `${(gpuRecs.optimizations.recommended_ctx / 1024).toFixed(0)}K`, ok: true },
                        { label: 'Quantization', value: gpuRecs.optimizations.recommended_quant, ok: true },
                      ].map(item => (
                        <div key={item.label} className="bg-[#ffffff] border border-[#e5e5e5] p-3 text-center dark:bg-[#0a0a0a] dark:border-[#262626]">
                          <div className={`text-sm font-bold font-mono ${item.ok ? 'text-[#76B900]' : 'text-[#a3a3a3]'}`}>{item.value}</div>
                          <div className="text-[10px] font-mono text-[#a3a3a3] mt-0.5 uppercase dark:text-[#737373]">{item.label}</div>
                        </div>
                      ))}
                    </div>
                    <div className="space-y-1">
                      {gpuRecs.optimizations.notes.map((note, i) => (
                        <div key={i} className="text-[10px] font-mono text-[#a3a3a3] dark:text-[#737373]">· {note}</div>
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
              <svg className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-[#a3a3a3] dark:text-[#737373]" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
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
                    <div className="h-3 bg-[#f5f5f5] w-1/4 dark:bg-[#141414]" />
                    <div className="h-3 bg-[#f5f5f5] w-1/6 dark:bg-[#141414]" />
                    <div className="ml-auto h-3 bg-[#f5f5f5] w-1/6 dark:bg-[#141414]" />
                  </div>
                </div>
              ))}
            </div>
          ) : (cloudModels.length === 0 && localModels.length === 0) ? (
            <div className="card p-8 text-center">
              <div className="text-2xl mb-3 text-[#333333] dark:text-[#525252]">◎</div>
              <div className="text-xs font-mono text-[#a3a3a3] uppercase dark:text-[#737373]">
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
                      <span className="text-[10px] font-mono text-[#a3a3a3] dark:text-[#737373]">{cloudModels.length} available</span>
                    </div>
                  )}
                  <div className="card overflow-hidden">
                    <div className="overflow-x-auto">
                      <table className="w-full text-sm">
                        <thead>
                          <tr className="border-b border-[#e5e5e5] bg-[#ffffff] dark:bg-[#0a0a0a] dark:border-[#262626]">
                            <th className="text-left py-2 px-4 font-mono text-[#a3a3a3] text-[10px] uppercase tracking-wider dark:text-[#737373]">Model</th>
                            <th className="text-left py-2 px-4 font-mono text-[#a3a3a3] text-[10px] uppercase tracking-wider dark:text-[#737373]">Provider</th>
                            <th className="text-right py-2 px-4 font-mono text-[#a3a3a3] text-[10px] uppercase tracking-wider hidden md:table-cell dark:text-[#737373]">Context</th>
                            <th className="text-right py-2 px-4 font-mono text-[#a3a3a3] text-[10px] uppercase tracking-wider hidden lg:table-cell dark:text-[#737373]">Input</th>
                            <th className="text-right py-2 px-4 font-mono text-[#a3a3a3] text-[10px] uppercase tracking-wider hidden lg:table-cell dark:text-[#737373]">Output</th>
                            <th className="py-2 px-4 font-mono text-[#a3a3a3] text-[10px] uppercase tracking-wider hidden xl:table-cell dark:text-[#737373]">Capabilities</th>
                            <th className="text-center py-2 px-4 font-mono text-[#a3a3a3] text-[10px] uppercase tracking-wider dark:text-[#737373]">Status</th>
                          </tr>
                        </thead>
                        <tbody>
                          {cloudModels.map(m => (
                            <ModelRow key={`${m.provider}-${m.model_id}`} m={m} tableMode />
                          ))}
                        </tbody>
                      </table>
                    </div>
                    <div className="px-4 py-2 border-t border-[#e5e5e5] dark:border-[#262626]">
                      <span className="text-[10px] font-mono text-[#a3a3a3] dark:text-[#737373]">{cloudModels.length} models</span>
                    </div>
                  </div>
                </>
              )}
            </div>
          )}
        </div>
      )}
      </div>
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
              <div className="font-mono text-xs text-[#0a0a0a] dark:text-[#fafafa]">{m.model_id}</div>
              {m.display_name && m.display_name !== m.model_id && (
                <div className="text-[10px] font-mono text-[#a3a3a3] mt-0.5 dark:text-[#737373]">{m.display_name}</div>
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
    <tr className="border-b border-[#e5e5e5] hover:bg-[#f5f5f5] transition-colors dark:border-[#262626] dark:hover:bg-[#1f1f1f]">
      <td className="py-3 px-4">
        <div className="font-mono text-xs text-[#0a0a0a] dark:text-[#fafafa]">{m.model_id}</div>
        {m.display_name && m.display_name !== m.model_id && (
          <div className="text-[10px] font-mono text-[#a3a3a3] mt-0.5 dark:text-[#737373]">{m.display_name}</div>
        )}
      </td>
      <td className="py-3 px-4">
        <span className="text-[10px] font-mono px-2 py-1 bg-[#e5e5e5] text-[#737373] uppercase dark:text-[#a3a3a3]">
          {m.provider}
        </span>
      </td>
      <td className="py-3 px-4 text-right hidden md:table-cell">
        <span className="font-mono text-[10px] text-[#737373] dark:text-[#a3a3a3]">
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
    <span className="text-[10px] font-mono px-1.5 py-0.5 bg-[#f5f5f5] text-[#a3a3a3] border border-[#e5e5e5] dark:bg-[#141414] dark:border-[#262626] dark:text-[#737373]">
      {label.toUpperCase()}
    </span>
  );
}
