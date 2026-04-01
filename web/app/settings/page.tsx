'use client';

import { useState, useEffect } from 'react';
import { getCacheStats, clearCache, getBudgetStatus } from '@/lib/api';
import type { CacheStats, BudgetStatus } from '@/lib/types';

const STORAGE_KEY = 'council_settings';

interface AppSettings {
  apiUrl: string;
  defaultProvider: string;
  defaultModel: string;
  defaultTemperature: number;
  defaultMaxTokens: number;
  streamingEnabled: boolean;
  outputFormat: 'plain' | 'markdown';
  dailyBudgetLimit: string;
  monthlyBudgetLimit: string;
  defaultCouncilStrategy: string;
  defaultNumAgents: number;
  quorumThreshold: number;
  synthesizeByDefault: boolean;
  theme: 'dark' | 'light';
}

const DEFAULT_SETTINGS: AppSettings = {
  apiUrl: 'http://localhost:8000',
  defaultProvider: '',
  defaultModel: '',
  defaultTemperature: 0.7,
  defaultMaxTokens: 1024,
  streamingEnabled: true,
  outputFormat: 'plain',
  dailyBudgetLimit: '5.00',
  monthlyBudgetLimit: '50.00',
  defaultCouncilStrategy: 'weighted',
  defaultNumAgents: 3,
  quorumThreshold: 0.5,
  synthesizeByDefault: true,
  theme: 'dark',
};

function loadSettings(): AppSettings {
  if (typeof window === 'undefined') return DEFAULT_SETTINGS;
  try {
    const saved = localStorage.getItem(STORAGE_KEY);
    if (!saved) return DEFAULT_SETTINGS;
    return { ...DEFAULT_SETTINGS, ...JSON.parse(saved) };
  } catch {
    return DEFAULT_SETTINGS;
  }
}

function Toggle({ value, onChange }: { value: boolean; onChange: (v: boolean) => void }) {
  return (
    <button
      type="button"
      onClick={() => onChange(!value)}
      className={`relative w-11 h-6 transition-colors ${value ? 'bg-[#76B900]' : 'bg-[#222222] border border-[#333333]'}`}
    >
      <span className={`absolute top-1 w-4 h-4 bg-white shadow transition-transform ${value ? 'translate-x-6' : 'translate-x-1'}`} />
    </button>
  );
}

function SectionCard({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="card p-5 space-y-4 nvidia-corner relative">
      <h2 className="font-mono font-bold text-white text-xs uppercase tracking-widest border-b border-[#222222] pb-3 flex items-center gap-2">
        <span className="w-1 h-4 bg-[#76B900] inline-block" />
        {title}
      </h2>
      {children}
    </div>
  );
}

function SettingRow({
  label,
  description,
  children,
}: {
  label: string;
  description?: string;
  children: React.ReactNode;
}) {
  return (
    <div className="flex items-center justify-between gap-4">
      <div className="flex-1 min-w-0">
        <div className="text-sm font-mono text-[#999999]">{label}</div>
        {description && <div className="text-[10px] font-mono text-[#555555] mt-0.5">{description}</div>}
      </div>
      <div className="flex-shrink-0">{children}</div>
    </div>
  );
}

export default function SettingsPage() {
  const [settings, setSettings] = useState<AppSettings>(DEFAULT_SETTINGS);
  const [saved, setSaved] = useState(false);
  const [cacheStats, setCacheStats] = useState<CacheStats | null>(null);
  const [budgetStatus, setBudgetStatus] = useState<BudgetStatus | null>(null);
  const [cacheClearing, setCacheClearing] = useState(false);
  const [cacheCleared, setCacheCleared] = useState<number | null>(null);

  useEffect(() => {
    setSettings(loadSettings());
    getCacheStats().then(setCacheStats).catch(() => {});
    getBudgetStatus().then(setBudgetStatus).catch(() => {});
  }, []);

  const update = <K extends keyof AppSettings>(key: K, value: AppSettings[K]) => {
    setSettings(prev => ({ ...prev, [key]: value }));
    setSaved(false);
  };

  const handleSave = () => {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(settings));
    setSaved(true);
    setTimeout(() => setSaved(false), 3000);
  };

  const handleClearCache = async (provider?: string) => {
    setCacheClearing(true);
    setCacheCleared(null);
    try {
      const result = await clearCache(provider);
      setCacheCleared(result.cleared);
      const stats = await getCacheStats();
      setCacheStats(stats);
    } catch {
      // ignore
    } finally {
      setCacheClearing(false);
    }
  };

  const handleReset = () => {
    setSettings(DEFAULT_SETTINGS);
    setSaved(false);
  };

  return (
    <div className="p-6 space-y-6 max-w-3xl mx-auto">
      {/* Header */}
      <div className="nvidia-corner relative border border-[#333333] bg-[#111111] p-5 overflow-hidden">
        <div className="absolute top-0 left-0 right-0 h-px bg-gradient-to-r from-transparent via-[#76B900] to-transparent" />
        <div className="relative flex items-center justify-between">
          <div>
            <div className="text-[10px] font-mono text-[#76B900] tracking-[0.2em] uppercase mb-0.5">Configuration</div>
            <h1 className="text-2xl font-bold text-white">Settings</h1>
            <p className="text-xs font-mono text-[#555555] mt-1">Configure Hive platform preferences</p>
          </div>
          <div className="flex gap-2">
            <button onClick={handleReset} className="btn-ghost px-4 py-2 text-xs font-mono uppercase tracking-wider">
              Reset
            </button>
            <button
              onClick={handleSave}
              className={`px-4 py-2 text-xs font-mono uppercase tracking-wider font-bold transition-all ${
                saved
                  ? 'bg-[#76B900]/20 text-[#76B900] border border-[#76B900]/40'
                  : 'btn-primary'
              }`}
            >
              {saved ? (
                <span className="flex items-center gap-1.5">
                  <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                    <path strokeLinecap="round" strokeLinejoin="round" d="M4.5 12.75l6 6 9-13.5" />
                  </svg>
                  SAVED
                </span>
              ) : 'SAVE SETTINGS'}
            </button>
          </div>
        </div>
      </div>

      {/* General */}
      <SectionCard title="General">
        <div>
          <label className="block text-[10px] font-mono text-[#666666] mb-1.5 uppercase tracking-wider">API Server URL</label>
          <input
            type="text"
            value={settings.apiUrl}
            onChange={e => update('apiUrl', e.target.value)}
            className="input-base w-full px-3 py-2.5 text-sm font-mono"
            placeholder="http://localhost:8000"
          />
          <div className="text-[10px] font-mono text-[#444444] mt-1">
            The Hive API server address. Changes require a page reload.
          </div>
        </div>

        <div className="grid grid-cols-2 gap-4">
          <div>
            <label className="block text-[10px] font-mono text-[#666666] mb-1.5 uppercase tracking-wider">Default Advisor</label>
            <input
              type="text"
              value={settings.defaultProvider}
              onChange={e => update('defaultProvider', e.target.value)}
              className="input-base w-full px-3 py-2.5 text-sm font-mono"
              placeholder="ollama"
            />
          </div>
          <div>
            <label className="block text-[10px] font-mono text-[#666666] mb-1.5 uppercase tracking-wider">Default Model</label>
            <input
              type="text"
              value={settings.defaultModel}
              onChange={e => update('defaultModel', e.target.value)}
              className="input-base w-full px-3 py-2.5 text-sm font-mono"
              placeholder="ollama/nemotron-mini"
            />
          </div>
        </div>

        <div>
          <div className="flex items-center justify-between mb-1.5">
            <label className="text-[10px] font-mono text-[#666666] uppercase tracking-wider">Default Temperature</label>
            <span className="text-xs font-mono text-[#76B900]">{settings.defaultTemperature.toFixed(2)}</span>
          </div>
          <input
            type="range" min="0" max="2" step="0.05"
            value={settings.defaultTemperature}
            onChange={e => update('defaultTemperature', parseFloat(e.target.value))}
            className="w-full"
          />
        </div>

        <div>
          <label className="block text-[10px] font-mono text-[#666666] mb-1.5 uppercase tracking-wider">Default Max Tokens</label>
          <input
            type="number" min="1" max="32000"
            value={settings.defaultMaxTokens}
            onChange={e => update('defaultMaxTokens', parseInt(e.target.value) || 1024)}
            className="input-base w-full px-3 py-2.5 text-sm font-mono"
          />
        </div>

        <SettingRow label="Streaming" description="Stream responses token by token">
          <Toggle value={settings.streamingEnabled} onChange={v => update('streamingEnabled', v)} />
        </SettingRow>

        <div>
          <label className="block text-[10px] font-mono text-[#666666] mb-1.5 uppercase tracking-wider">Output Format</label>
          <div className="flex gap-0 border border-[#333333]">
            {(['plain', 'markdown'] as const).map(fmt => (
              <button
                key={fmt}
                type="button"
                onClick={() => update('outputFormat', fmt)}
                className={`flex-1 py-2 text-xs font-mono uppercase tracking-wider transition-all ${
                  settings.outputFormat === fmt
                    ? 'bg-[#76B900] text-black font-bold'
                    : 'text-[#555555] hover:text-[#999999] hover:bg-[#1a1a1a]'
                }`}
              >
                {fmt}
              </button>
            ))}
          </div>
        </div>
      </SectionCard>

      {/* NVIDIA / Local AI section */}
      <SectionCard title="NVIDIA Local AI">
        <div className="bg-[#76B900]/5 border border-[#76B900]/20 p-3">
          <div className="text-[10px] font-mono text-[#76B900] mb-2 uppercase tracking-wider">Recommended Configuration</div>
          <div className="text-[10px] font-mono text-[#666666] space-y-1">
            <div>Default Provider: <span className="text-white">ollama</span></div>
            <div>Default Model: <span className="text-[#76B900]">ollama/nemotron-mini</span></div>
            <div>Ollama URL: <span className="text-white">http://localhost:11434</span></div>
          </div>
          <button
            type="button"
            onClick={() => {
              update('defaultProvider', 'ollama');
              update('defaultModel', 'ollama/nemotron-mini');
            }}
            className="mt-2 btn-primary px-3 py-1 text-[10px] font-mono uppercase tracking-wider"
          >
            Apply NVIDIA Defaults
          </button>
        </div>
        <div className="text-[10px] font-mono text-[#444444]">
          NVIDIA Nemotron runs locally on your GPU via Ollama. Zero cost, full privacy.
          Run: <span className="text-[#76B900]">ollama pull nemotron-mini</span>
        </div>
      </SectionCard>

      {/* Budget */}
      <SectionCard title="Budget Limits">
        {budgetStatus && (
          <div className="bg-[#0a0a0a] border border-[#222222] p-3 mb-2">
            <div className="section-label mb-2">Current Usage</div>
            <div className="grid grid-cols-2 gap-3">
              {[
                { label: 'Daily Spend', value: `$${parseFloat(budgetStatus.daily_spend).toFixed(4)}` },
                { label: 'Monthly Spend', value: `$${parseFloat(budgetStatus.monthly_spend).toFixed(4)}` },
                { label: 'Daily Queries', value: `${budgetStatus.daily_queries}` },
                { label: 'Monthly Queries', value: `${budgetStatus.monthly_queries}` },
              ].map(({ label, value }) => (
                <div key={label}>
                  <div className="text-[10px] font-mono text-[#444444] uppercase">{label}</div>
                  <div className="text-sm font-mono text-white">{value}</div>
                </div>
              ))}
            </div>
          </div>
        )}

        <div className="grid grid-cols-2 gap-4">
          <div>
            <label className="block text-[10px] font-mono text-[#666666] mb-1.5 uppercase tracking-wider">Daily Limit (USD)</label>
            <div className="relative">
              <span className="absolute left-3 top-1/2 -translate-y-1/2 text-[#555555] text-sm font-mono">$</span>
              <input
                type="number" min="0" step="0.01"
                value={settings.dailyBudgetLimit}
                onChange={e => update('dailyBudgetLimit', e.target.value)}
                className="input-base w-full pl-7 pr-3 py-2.5 text-sm font-mono"
              />
            </div>
          </div>
          <div>
            <label className="block text-[10px] font-mono text-[#666666] mb-1.5 uppercase tracking-wider">Monthly Limit (USD)</label>
            <div className="relative">
              <span className="absolute left-3 top-1/2 -translate-y-1/2 text-[#555555] text-sm font-mono">$</span>
              <input
                type="number" min="0" step="0.01"
                value={settings.monthlyBudgetLimit}
                onChange={e => update('monthlyBudgetLimit', e.target.value)}
                className="input-base w-full pl-7 pr-3 py-2.5 text-sm font-mono"
              />
            </div>
          </div>
        </div>
        <div className="text-[10px] font-mono text-[#444444]">
          Stored locally. For server-side enforcement, configure limits in the Hive config file.
          Using Nemotron locally = $0.00 always.
        </div>
      </SectionCard>

      {/* Hive defaults */}
      <SectionCard title="Hive Settings">
        <div>
          <label className="block text-[10px] font-mono text-[#666666] mb-1.5 uppercase tracking-wider">Default Strategy</label>
          <select
            value={settings.defaultCouncilStrategy}
            onChange={e => update('defaultCouncilStrategy', e.target.value)}
            className="input-base w-full px-3 py-2.5 text-sm font-mono"
          >
            <option value="weighted">Weighted — responses weighted by configured weights</option>
            <option value="unanimous">Unanimous — all members must agree</option>
            <option value="majority">Majority — more than half must agree</option>
          </select>
        </div>

        <div>
          <div className="flex items-center justify-between mb-1.5">
            <label className="text-[10px] font-mono text-[#666666] uppercase tracking-wider">Default Num Agents</label>
            <span className="text-xs font-mono text-[#76B900]">{settings.defaultNumAgents}</span>
          </div>
          <input
            type="range" min="2" max="8" step="1"
            value={settings.defaultNumAgents}
            onChange={e => update('defaultNumAgents', parseInt(e.target.value))}
            className="w-full"
          />
          <div className="flex justify-between mt-1">
            <span className="text-[10px] font-mono text-[#444444]">2</span>
            <span className="text-[10px] font-mono text-[#444444]">8</span>
          </div>
        </div>

        <div>
          <div className="flex items-center justify-between mb-1.5">
            <label className="text-[10px] font-mono text-[#666666] uppercase tracking-wider">Quorum Threshold</label>
            <span className="text-xs font-mono text-[#76B900]">{(settings.quorumThreshold * 100).toFixed(0)}%</span>
          </div>
          <input
            type="range" min="0" max="1" step="0.05"
            value={settings.quorumThreshold}
            onChange={e => update('quorumThreshold', parseFloat(e.target.value))}
            className="w-full"
          />
        </div>

        <SettingRow label="Synthesize by Default" description="Automatically generate a synthesized response">
          <Toggle value={settings.synthesizeByDefault} onChange={v => update('synthesizeByDefault', v)} />
        </SettingRow>
      </SectionCard>

      {/* Cache management */}
      <SectionCard title="Cache Management">
        {cacheStats && (
          <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 mb-2">
            {[
              { label: 'Hits', value: `${cacheStats.hits}`, color: '#76B900' },
              { label: 'Misses', value: `${cacheStats.misses}`, color: '#ef4444' },
              { label: 'Size', value: `${cacheStats.size}/${cacheStats.max_size}`, color: '#999999' },
              { label: 'Hit Rate', value: `${(cacheStats.hit_rate * 100).toFixed(0)}%`, color: '#76B900' },
            ].map(({ label, value, color }) => (
              <div key={label} className="bg-[#0a0a0a] border border-[#222222] p-3 text-center">
                <div className="font-mono font-bold text-sm" style={{ color }}>{value}</div>
                <div className="text-[10px] font-mono text-[#444444] mt-0.5 uppercase">{label}</div>
              </div>
            ))}
          </div>
        )}

        {cacheCleared !== null && (
          <div className="bg-[#76B900]/5 border border-[#76B900]/20 px-3 py-2 text-xs font-mono text-[#76B900]">
            CLEARED {cacheCleared} cache entr{cacheCleared === 1 ? 'y' : 'ies'}
          </div>
        )}

        <div className="flex gap-2">
          <button
            onClick={() => handleClearCache()}
            disabled={cacheClearing}
            className="btn-secondary flex-1 py-2 text-xs font-mono uppercase tracking-wider flex items-center justify-center gap-2"
          >
            {cacheClearing ? (
              <svg className="animate-spin w-4 h-4" viewBox="0 0 24 24" fill="none">
                <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
                <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
              </svg>
            ) : (
              <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
                <path strokeLinecap="round" strokeLinejoin="round"
                  d="M14.74 9l-.346 9m-4.788 0L9.26 9m9.968-3.21c.342.052.682.107 1.022.166m-1.022-.165L18.16 19.673a2.25 2.25 0 01-2.244 2.077H8.084a2.25 2.25 0 01-2.244-2.077L4.772 5.79m14.456 0a48.108 48.108 0 00-3.478-.397m-12 .562c.34-.059.68-.114 1.022-.165m0 0a48.11 48.11 0 013.478-.397m7.5 0v-.916c0-1.18-.91-2.164-2.09-2.201a51.964 51.964 0 00-3.32 0c-1.18.037-2.09 1.022-2.09 2.201v.916m7.5 0a48.667 48.667 0 00-7.5 0" />
              </svg>
            )}
            Clear All Cache
          </button>
        </div>
      </SectionCard>

      {/* Data reset */}
      <SectionCard title="Data">
        <div>
          <div className="text-sm font-mono text-[#999999] mb-1">Reset All Settings</div>
          <div className="text-[10px] font-mono text-[#444444] mb-3">
            Clear all locally stored settings and return to defaults.
            This does not affect the API server configuration.
          </div>
          <button
            onClick={() => {
              if (typeof window !== 'undefined') {
                localStorage.removeItem(STORAGE_KEY);
                localStorage.removeItem('council_recent_queries');
                setSettings(DEFAULT_SETTINGS);
                setSaved(false);
              }
            }}
            className="px-4 py-2 border border-[#ef4444]/30 text-[#ef4444] text-xs font-mono uppercase tracking-wider hover:bg-[#ef4444]/10 transition-all"
          >
            Reset to Defaults & Clear History
          </button>
        </div>
      </SectionCard>

      {/* Version info */}
      <div className="text-center py-4 border-t border-[#1a1a1a]">
        <div className="text-[10px] font-mono text-[#333333]">COUNCIL AI COMMAND CENTER · v0.2.0</div>
        <div className="text-[10px] font-mono text-[#2a2a2a] mt-1">NVIDIA Nemotron · Next.js 14 · Tailwind CSS</div>
      </div>
    </div>
  );
}
