'use client';

import { useState } from 'react';
import type { ProviderHealth } from '@/lib/types';
import { testProvider } from '@/lib/api';

interface Props {
  provider: ProviderHealth;
  onRefresh?: () => void;
}

const PROVIDER_ICONS: Record<string, string> = {
  openai: '⬡',
  anthropic: '◈',
  google: '◉',
  groq: '▲',
  ollama: '◎',
  mistral: '❖',
  cohere: '⬟',
  together: '⊕',
};

const PROVIDER_COLORS: Record<string, string> = {
  openai: '#22c55e',
  anthropic: '#f59e0b',
  google: '#3b82f6',
  groq: '#a855f7',
  ollama: '#06b6d4',
  mistral: '#ef4444',
  cohere: '#10b981',
  together: '#f97316',
};

function getProviderColor(name: string): string {
  const key = name.toLowerCase();
  for (const [k, v] of Object.entries(PROVIDER_COLORS)) {
    if (key.includes(k)) return v;
  }
  return '#475569';
}

function getProviderIcon(name: string): string {
  const key = name.toLowerCase();
  for (const [k, v] of Object.entries(PROVIDER_ICONS)) {
    if (key.includes(k)) return v;
  }
  return '◆';
}

export default function ProviderCard({ provider, onRefresh }: Props) {
  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState<ProviderHealth | null>(null);

  const handleTest = async () => {
    setTesting(true);
    setTestResult(null);
    try {
      const result = await testProvider(provider.name);
      setTestResult(result);
    } catch {
      setTestResult({ ...provider, healthy: false, error: 'Test failed' });
    } finally {
      setTesting(false);
      onRefresh?.();
    }
  };

  const displayHealth = testResult ?? provider;
  const accentColor = getProviderColor(provider.name);
  const icon = getProviderIcon(provider.name);

  return (
    <div
      className="card p-5 flex flex-col gap-4 hover:border-[#3a3a5e] transition-all duration-200"
      style={{ borderTopColor: accentColor, borderTopWidth: '2px' }}
    >
      {/* Header */}
      <div className="flex items-start justify-between">
        <div className="flex items-center gap-3">
          <div
            className="w-10 h-10 rounded-xl flex items-center justify-center text-xl font-mono"
            style={{ backgroundColor: `${accentColor}18`, color: accentColor }}
          >
            {icon}
          </div>
          <div>
            <div className="font-semibold text-[#e2e8f0] capitalize">
              {provider.name}
            </div>
            <div className="text-xs text-[#475569] mt-0.5">
              {provider.models_available} model{provider.models_available !== 1 ? 's' : ''}
            </div>
          </div>
        </div>

        {/* Status badge */}
        <div
          className={`flex items-center gap-1.5 px-2.5 py-1 rounded-full text-xs font-medium ${
            displayHealth.healthy
              ? 'bg-[#22c55e]/10 text-[#22c55e] border border-[#22c55e]/20'
              : 'bg-[#ef4444]/10 text-[#ef4444] border border-[#ef4444]/20'
          }`}
        >
          <span
            className={`w-1.5 h-1.5 rounded-full ${
              displayHealth.healthy ? 'bg-[#22c55e]' : 'bg-[#ef4444]'
            }`}
          />
          {displayHealth.healthy ? 'Healthy' : 'Down'}
        </div>
      </div>

      {/* Metrics */}
      <div className="grid grid-cols-2 gap-3">
        <div className="bg-[#0a0a0a] rounded-lg p-3">
          <div className="text-xs text-[#475569] mb-1">Latency</div>
          <div className="font-mono text-sm font-semibold text-[#e2e8f0]">
            {displayHealth.latency_ms != null
              ? `${Math.round(displayHealth.latency_ms)}ms`
              : '—'}
          </div>
          {displayHealth.latency_ms != null && (
            <div className="mt-1.5 progress-bar">
              <div
                className="progress-fill"
                style={{
                  width: `${Math.min(100, (displayHealth.latency_ms / 3000) * 100)}%`,
                  backgroundColor:
                    displayHealth.latency_ms < 500 ? '#22c55e' :
                    displayHealth.latency_ms < 1500 ? '#f59e0b' : '#ef4444',
                }}
              />
            </div>
          )}
        </div>

        <div className="bg-[#0a0a0a] rounded-lg p-3">
          <div className="text-xs text-[#475569] mb-1">Models</div>
          <div className="font-mono text-sm font-semibold text-[#e2e8f0]">
            {provider.models_available}
          </div>
          <div className="text-xs text-[#475569] mt-1">available</div>
        </div>
      </div>

      {/* Error message */}
      {displayHealth.error && (
        <div className="bg-[#ef4444]/5 border border-[#ef4444]/20 rounded-lg px-3 py-2">
          <div className="text-xs text-[#ef4444] font-mono break-words">
            {displayHealth.error}
          </div>
        </div>
      )}

      {/* Test button */}
      <button
        onClick={handleTest}
        disabled={testing}
        className="btn-secondary w-full py-2 text-sm flex items-center justify-center gap-2"
      >
        {testing ? (
          <>
            <svg className="animate-spin w-4 h-4" viewBox="0 0 24 24" fill="none">
              <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
              <path className="opacity-75" fill="currentColor"
                d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z" />
            </svg>
            Testing...
          </>
        ) : (
          <>
            <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
              <path strokeLinecap="round" strokeLinejoin="round"
                d="M9 12.75L11.25 15 15 9.75M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
            </svg>
            Test Connectivity
          </>
        )}
      </button>
    </div>
  );
}
