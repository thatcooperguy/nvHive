'use client';

import { useMemo, useState, useEffect, useRef } from 'react';
import type { ProviderHealth, QueryMode } from '@/lib/types';

interface Props {
  onSubmit: (params: {
    prompt: string;
    mode: QueryMode;
    provider?: string;
    model?: string;
    systemPrompt?: string;
    temperature: number;
    maxTokens: number;
    stream: boolean;
  }) => void;
  loading?: boolean;
  /** Full provider health data — used to sort healthy advisors first
   *  and show a connection indicator. Falling back to a string[] keeps
   *  older callers working. */
  providers?: ProviderHealth[] | string[];
  models?: Array<{ model_id: string; provider: string; display_name: string }>;
  defaultMode?: QueryMode;
  showModeToggle?: boolean;
}

export default function QueryInput({
  onSubmit,
  loading = false,
  providers = [],
  models = [],
  defaultMode = 'simple',
  showModeToggle = true,
}: Props) {
  const [prompt, setPrompt] = useState('');
  const [mode, setMode] = useState<QueryMode>(defaultMode);
  const [provider, setProvider] = useState('');
  const [model, setModel] = useState('');
  const [temperature, setTemperature] = useState(0.7);
  const [maxTokens, setMaxTokens] = useState(1024);
  const [stream, setStream] = useState(true);
  const [showAdvanced, setShowAdvanced] = useState(false);
  const [systemPrompt, setSystemPrompt] = useState('');
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  // Auto-resize textarea
  useEffect(() => {
    const el = textareaRef.current;
    if (!el) return;
    el.style.height = 'auto';
    el.style.height = `${Math.min(el.scrollHeight, 300)}px`;
  }, [prompt]);

  // Normalize the providers prop into ProviderHealth records and sort
  // healthy ones first so connected advisors surface at the top of the
  // dropdown. Stable order within each health bucket (by latency, then
  // by name) so the same advisor doesn't jump around between renders.
  const sortedProviders: ProviderHealth[] = useMemo(() => {
    if (!providers || providers.length === 0) return [];
    const normalized: ProviderHealth[] = (providers as Array<ProviderHealth | string>).map(p =>
      typeof p === 'string'
        ? { name: p, healthy: true, latency_ms: null, models_available: 0, error: null }
        : p
    );
    return normalized.slice().sort((a, b) => {
      if (a.healthy !== b.healthy) return a.healthy ? -1 : 1;
      const la = a.latency_ms ?? Number.POSITIVE_INFINITY;
      const lb = b.latency_ms ?? Number.POSITIVE_INFINITY;
      if (la !== lb) return la - lb;
      return a.name.localeCompare(b.name);
    });
  }, [providers]);

  const healthyCount = sortedProviders.filter(p => p.healthy).length;

  // Filter models by selected provider
  const filteredModels = provider
    ? models.filter(m => m.provider === provider)
    : models;

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    if (!prompt.trim() || loading) return;
    onSubmit({
      prompt: prompt.trim(),
      mode,
      provider: provider || undefined,
      model: model || undefined,
      systemPrompt: systemPrompt || undefined,
      temperature,
      maxTokens,
      stream,
    });
  };

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) {
      handleSubmit(e as unknown as React.FormEvent);
    }
  };

  const MODES: { id: QueryMode; label: string; desc: string; color: string }[] = [
    { id: 'simple', label: 'Simple', desc: 'Single provider', color: '#3b82f6' },
    { id: 'council', label: 'Convene', desc: 'Multi-LLM orchestration', color: '#a855f7' },
    { id: 'compare', label: 'Poll', desc: 'Side-by-side advisors', color: '#22c55e' },
  ];

  return (
    <form onSubmit={handleSubmit} className="space-y-4">
      {/* Mode toggle */}
      {showModeToggle && (
        <div className="flex gap-2">
          {MODES.map(m => (
            <button
              key={m.id}
              type="button"
              onClick={() => setMode(m.id)}
              className={`flex-1 py-2.5 px-3 rounded-xl border text-sm font-medium transition-all duration-150 ${
                mode === m.id
                  ? 'border-opacity-50 text-white'
                  : 'border-[#2a2a3e] text-[#475569] hover:text-[#94a3b8] hover:border-[#3a3a5e]'
              }`}
              style={mode === m.id ? {
                backgroundColor: `${m.color}15`,
                borderColor: `${m.color}50`,
                color: m.color,
              } : {}}
            >
              <div className="font-semibold">{m.label}</div>
              <div className="text-xs opacity-70 mt-0.5">{m.desc}</div>
            </button>
          ))}
        </div>
      )}

      {/* Prompt textarea */}
      <div className="relative">
        <textarea
          ref={textareaRef}
          value={prompt}
          onChange={e => setPrompt(e.target.value)}
          onKeyDown={handleKeyDown}
          placeholder="Ask anything... (⌘+Enter to submit)"
          rows={3}
          className="input-base w-full px-4 py-3 resize-none text-sm leading-relaxed"
          style={{ minHeight: '80px' }}
          disabled={loading}
        />
        <div className="absolute bottom-3 right-3 text-xs text-[#333] font-mono">
          {prompt.length > 0 && `${prompt.length}`}
        </div>
      </div>

      {/* Provider / Model row */}
      <div className="grid grid-cols-2 gap-3">
        <div>
          <label className="block text-xs text-[#475569] mb-1.5 flex items-center justify-between">
            <span>Advisor</span>
            {sortedProviders.length > 0 && (
              <span className="font-mono text-[10px] text-[#475569]">
                <span className="text-[#22c55e]">●</span> {healthyCount} online
                {healthyCount < sortedProviders.length && (
                  <> / <span className="text-[#475569]">{sortedProviders.length - healthyCount} offline</span></>
                )}
              </span>
            )}
          </label>
          <select
            value={provider}
            onChange={e => { setProvider(e.target.value); setModel(''); }}
            className="input-base w-full px-3 py-2 text-sm font-mono"
            disabled={loading}
          >
            <option value="">Auto (best available)</option>
            {healthyCount > 0 && (
              <optgroup label="● Connected">
                {sortedProviders.filter(p => p.healthy).map(p => (
                  <option key={p.name} value={p.name}>
                    {p.name}{p.latency_ms != null ? `  ·  ${p.latency_ms}ms` : ''}
                  </option>
                ))}
              </optgroup>
            )}
            {healthyCount < sortedProviders.length && (
              <optgroup label="○ Offline">
                {sortedProviders.filter(p => !p.healthy).map(p => (
                  <option key={p.name} value={p.name}>
                    {p.name}  ·  offline
                  </option>
                ))}
              </optgroup>
            )}
          </select>
        </div>
        <div>
          <label className="block text-xs text-[#475569] mb-1.5">Model</label>
          <select
            value={model}
            onChange={e => setModel(e.target.value)}
            className="input-base w-full px-3 py-2 text-sm"
            disabled={loading}
          >
            <option value="">Default</option>
            {filteredModels.map(m => (
              <option key={m.model_id} value={m.model_id}>{m.display_name || m.model_id}</option>
            ))}
          </select>
        </div>
      </div>

      {/* Advanced toggle */}
      <button
        type="button"
        onClick={() => setShowAdvanced(!showAdvanced)}
        className="flex items-center gap-2 text-xs text-[#475569] hover:text-[#94a3b8] transition-colors"
      >
        <svg
          className={`w-3.5 h-3.5 transition-transform ${showAdvanced ? 'rotate-90' : ''}`}
          fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}
        >
          <path strokeLinecap="round" strokeLinejoin="round" d="M8.25 4.5l7.5 7.5-7.5 7.5" />
        </svg>
        Advanced settings
      </button>

      {showAdvanced && (
        <div className="space-y-4 bg-[#0d0d1a] border border-[#2a2a3e] rounded-xl p-4 animate-fade-in">
          {/* Temperature */}
          <div>
            <div className="flex items-center justify-between mb-2">
              <label className="text-xs text-[#94a3b8]">Temperature</label>
              <span className="text-xs font-mono text-[#e2e8f0]">{temperature.toFixed(2)}</span>
            </div>
            <input
              type="range"
              min="0"
              max="2"
              step="0.05"
              value={temperature}
              onChange={e => setTemperature(parseFloat(e.target.value))}
              className="w-full h-1.5 rounded-full appearance-none cursor-pointer bg-[#1a1a2e]"
              style={{
                background: `linear-gradient(to right, #3b82f6 ${(temperature / 2) * 100}%, #1a1a2e ${(temperature / 2) * 100}%)`
              }}
            />
            <div className="flex justify-between mt-1">
              <span className="text-xs text-[#475569]">Precise</span>
              <span className="text-xs text-[#475569]">Creative</span>
            </div>
          </div>

          {/* Max tokens */}
          <div>
            <label className="block text-xs text-[#94a3b8] mb-1.5">Max Tokens</label>
            <input
              type="number"
              min="1"
              max="32000"
              value={maxTokens}
              onChange={e => setMaxTokens(parseInt(e.target.value) || 1024)}
              className="input-base w-full px-3 py-2 text-sm font-mono"
            />
          </div>

          {/* System prompt */}
          <div>
            <label className="block text-xs text-[#94a3b8] mb-1.5">System Prompt</label>
            <textarea
              value={systemPrompt}
              onChange={e => setSystemPrompt(e.target.value)}
              placeholder="Optional system prompt..."
              rows={3}
              className="input-base w-full px-3 py-2 text-sm resize-none"
            />
          </div>

          {/* Streaming toggle */}
          <div className="flex items-center justify-between">
            <label className="text-xs text-[#94a3b8]">Streaming</label>
            <button
              type="button"
              onClick={() => setStream(!stream)}
              className={`relative w-10 h-5 rounded-full transition-colors ${
                stream ? 'bg-[#3b82f6]' : 'bg-[#2a2a3e]'
              }`}
            >
              <span
                className={`absolute top-0.5 w-4 h-4 bg-white rounded-full shadow transition-transform ${
                  stream ? 'translate-x-5' : 'translate-x-0.5'
                }`}
              />
            </button>
          </div>
        </div>
      )}

      {/* Submit */}
      <button
        type="submit"
        disabled={!prompt.trim() || loading}
        className="btn-primary w-full py-3 text-sm font-semibold flex items-center justify-center gap-2"
      >
        {loading ? (
          <>
            <svg className="animate-spin w-4 h-4" viewBox="0 0 24 24" fill="none">
              <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
              <path className="opacity-75" fill="currentColor"
                d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z" />
            </svg>
            Processing...
          </>
        ) : (
          <>
            <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
              <path strokeLinecap="round" strokeLinejoin="round"
                d="M6 12L3.269 3.126A59.768 59.768 0 0121.485 12 59.77 59.77 0 013.27 20.876L5.999 12zm0 0h7.5" />
            </svg>
            {mode === 'council' ? 'Convene' : mode === 'compare' ? 'Poll' : 'Send'}
          </>
        )}
      </button>
    </form>
  );
}
