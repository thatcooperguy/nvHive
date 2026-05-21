'use client';

import { useState } from 'react';
import type { ProviderHealth } from '@/lib/types';
import { testProvider } from '@/lib/api';

interface Props {
  provider: ProviderHealth;
  onRefresh?: () => void;
}

const PROVIDER_ICONS: Record<string, string> = {
  openai: 'OA',
  anthropic: 'AN',
  google: 'GO',
  groq: 'GQ',
  ollama: 'OL',
  mistral: 'MI',
  cohere: 'CO',
  together: 'TG',
};

const PROVIDER_COLORS: Record<string, string> = {
  openai: '#16a34a',
  anthropic: '#d97706',
  google: '#2563eb',
  groq: '#7c3aed',
  ollama: '#0891b2',
  mistral: '#dc2626',
  cohere: '#10b981',
  together: '#ea580c',
};

function getProviderColor(name: string): string {
  const key = name.toLowerCase();
  for (const [k, v] of Object.entries(PROVIDER_COLORS)) {
    if (key.includes(k)) return v;
  }
  return '#737373';
}

function getProviderIcon(name: string): string {
  const key = name.toLowerCase();
  for (const [k, v] of Object.entries(PROVIDER_ICONS)) {
    if (key.includes(k)) return v;
  }
  return name.slice(0, 2).toUpperCase();
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
      className="card p-5 flex flex-col gap-4 hover:border-[#d4d4d4] transition-all duration-200"
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
            <div className="font-semibold text-[#0a0a0a] capitalize dark:text-[#fafafa]">
              {provider.name}
            </div>
            <div className="text-xs text-[#737373] mt-0.5 dark:text-[#a3a3a3]">
              {provider.models_available} model{provider.models_available !== 1 ? 's' : ''}
            </div>
          </div>
        </div>

        {/* Status badge */}
        <div
          className={`flex items-center gap-1.5 px-2.5 py-1 rounded-full text-xs font-medium ${
            displayHealth.healthy
              ? 'bg-[#16a34a]/10 text-[#16a34a] border border-[#16a34a]/20'
              : 'bg-[#dc2626]/10 text-[#dc2626] border border-[#dc2626]/20'
          }`}
        >
          <span
            className={`w-1.5 h-1.5 rounded-full ${
              displayHealth.healthy ? 'bg-[#16a34a]' : 'bg-[#dc2626]'
            }`}
          />
          {displayHealth.healthy ? 'Healthy' : 'Down'}
        </div>
      </div>

      {/* Metrics */}
      <div className="grid grid-cols-2 gap-3">
        <div className="bg-[#ffffff] rounded-lg p-3 dark:bg-[#0a0a0a]">
          <div className="text-xs text-[#737373] mb-1 dark:text-[#a3a3a3]">Latency</div>
          <div className="font-mono text-sm font-semibold text-[#0a0a0a] dark:text-[#fafafa]">
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
                    displayHealth.latency_ms < 500 ? '#16a34a' :
                    displayHealth.latency_ms < 1500 ? '#d97706' : '#dc2626',
                }}
              />
            </div>
          )}
        </div>

        <div className="bg-[#ffffff] rounded-lg p-3 dark:bg-[#0a0a0a]">
          <div className="text-xs text-[#737373] mb-1 dark:text-[#a3a3a3]">Models</div>
          <div className="font-mono text-sm font-semibold text-[#0a0a0a] dark:text-[#fafafa]">
            {provider.models_available}
          </div>
          <div className="text-xs text-[#737373] mt-1 dark:text-[#a3a3a3]">available</div>
        </div>
      </div>

      {/* Error message */}
      {displayHealth.error && (
        <div className="bg-[#dc2626]/5 border border-[#dc2626]/20 rounded-lg px-3 py-2">
          <div className="text-xs text-[#dc2626] font-mono break-words">
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
