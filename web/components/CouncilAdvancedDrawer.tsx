'use client';

import { useEffect, useState } from 'react';
import { getAgentPresets } from '@/lib/api';
import type { AgentPreset, CouncilRequest, ProviderHealth } from '@/lib/types';

export interface CouncilOptions {
  autoAgents: boolean;
  preset: string;
  numAgents: number;
  /** '' = server default (config.council.strategy). */
  strategy: string;
  synthesize: boolean;
  /** Per-provider weight override; absent = configured default. */
  weights: Record<string, number>;
}

export const DEFAULT_COUNCIL_OPTIONS: CouncilOptions = {
  autoAgents: false,
  preset: '',
  numAgents: 3,
  strategy: '',
  synthesize: true,
  weights: {},
};

const STRATEGIES: { value: string; label: string }[] = [
  { value: '', label: 'Default' },
  { value: 'weighted_consensus', label: 'Weighted consensus' },
  { value: 'majority_vote', label: 'Majority vote' },
  { value: 'best_of', label: 'Best of' },
];

export function councilRequestOptions(opts: CouncilOptions): Omit<CouncilRequest, 'prompt'> {
  return {
    auto_agents: opts.autoAgents,
    preset: opts.preset || undefined,
    num_agents: opts.autoAgents || opts.preset ? opts.numAgents : undefined,
    strategy: opts.strategy || undefined,
    synthesize: opts.synthesize,
    weights: Object.keys(opts.weights).length > 0 ? opts.weights : undefined,
  };
}

function changedCount(opts: CouncilOptions): number {
  let n = 0;
  if (opts.autoAgents) n++;
  if (opts.preset) n++;
  if (opts.strategy) n++;
  if (!opts.synthesize) n++;
  if (Object.keys(opts.weights).length > 0) n++;
  return n;
}

function Toggle({ value, onChange, disabled }: { value: boolean; onChange: (v: boolean) => void; disabled?: boolean }) {
  return (
    <button
      type="button"
      disabled={disabled}
      onClick={() => onChange(!value)}
      className={`relative w-10 h-5 flex-shrink-0 transition-colors disabled:opacity-50 ${
        value ? 'bg-[#76B900]' : 'bg-[#e5e5e5] border border-[#d4d4d4] dark:bg-[#3f3f46] dark:border-[#525252]'
      }`}
    >
      <span className={`absolute top-0.5 w-4 h-4 bg-white shadow transition-transform ${value ? 'translate-x-5' : 'translate-x-0.5'}`} />
    </button>
  );
}

interface Props {
  options: CouncilOptions;
  onChange: (next: CouncilOptions) => void;
  providerHealth: ProviderHealth[];
  disabled?: boolean;
}

/** Collapsible council settings shown at the top of the council panel. */
export default function CouncilAdvancedDrawer({ options, onChange, providerHealth, disabled = false }: Props) {
  const [open, setOpen] = useState(false);
  const [presets, setPresets] = useState<AgentPreset[]>([]);

  useEffect(() => {
    let mounted = true;
    getAgentPresets()
      .then(r => { if (mounted) setPresets(r.presets); })
      .catch(() => {});
    return () => { mounted = false; };
  }, []);

  const set = <K extends keyof CouncilOptions>(key: K, value: CouncilOptions[K]) =>
    onChange({ ...options, [key]: value });

  const changed = changedCount(options);
  const sortedProviders = [...providerHealth]
    .sort((a, b) => {
      if (a.healthy !== b.healthy) return a.healthy ? -1 : 1;
      const la = a.latency_ms ?? Number.POSITIVE_INFINITY;
      const lb = b.latency_ms ?? Number.POSITIVE_INFINITY;
      if (la !== lb) return la - lb;
      return a.name.localeCompare(b.name);
    })
    .slice(0, 6);

  return (
    <div className="border-b border-[#e5e5e5] dark:border-[#262626]">
      <div className="flex items-center hover:bg-[#f5f5f5] dark:hover:bg-[#141414] transition-colors">
        <button
          type="button"
          onClick={() => setOpen(prev => !prev)}
          className="flex-1 flex items-center gap-2 px-4 py-2.5 text-left"
        >
          <svg
            className={`w-3 h-3 text-[#737373] transition-transform ${open ? 'rotate-90' : ''}`}
            fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}
          >
            <path strokeLinecap="round" strokeLinejoin="round" d="M8.25 4.5l7.5 7.5-7.5 7.5" />
          </svg>
          <span className="text-[10px] font-mono uppercase tracking-wider text-[#525252] dark:text-[#a3a3a3]">Advanced</span>
          {changed > 0 && (
            <span className="ml-auto text-[9px] font-mono px-1.5 py-0.5 bg-[#76B900]/10 text-[#5a9100] border border-[#76B900]/30">
              {changed} set
            </span>
          )}
        </button>
        {changed > 0 && open && (
          <button
            type="button"
            disabled={disabled}
            onClick={() => onChange(DEFAULT_COUNCIL_OPTIONS)}
            className="px-3 py-2.5 text-[9px] font-mono uppercase text-[#a3a3a3] hover:text-[#dc2626] disabled:opacity-50"
          >
            Reset
          </button>
        )}
      </div>

      {open && (
        <div className="px-4 pb-4 space-y-4">
          <div className="flex items-center justify-between gap-3">
            <div>
              <div className="text-xs font-mono text-[#525252] dark:text-[#a3a3a3]">Pick members automatically</div>
              <div className="text-[10px] font-mono text-[#a3a3a3] dark:text-[#737373]">Generate expert personas from the prompt</div>
            </div>
            <Toggle value={options.autoAgents} onChange={v => set('autoAgents', v)} disabled={disabled} />
          </div>

          {presets.length > 0 && (
            <div>
              <label className="block text-[10px] font-mono text-[#737373] mb-1.5 uppercase tracking-wider dark:text-[#a3a3a3]">Preset</label>
              <select
                value={options.preset}
                onChange={e => set('preset', e.target.value)}
                disabled={disabled}
                className="input-base w-full px-3 py-2 text-sm"
              >
                <option value="">None</option>
                {presets.map(p => (
                  <option key={p.name} value={p.name}>{p.name}</option>
                ))}
              </select>
              {options.preset && (
                <div className="mt-1.5 text-[10px] font-mono text-[#a3a3a3] dark:text-[#737373]">
                  {presets.find(p => p.name === options.preset)?.description}
                </div>
              )}
            </div>
          )}

          <div className={options.autoAgents || options.preset ? '' : 'opacity-50'}>
            <div className="flex items-center justify-between mb-1.5">
              <label className="text-[10px] font-mono text-[#737373] uppercase tracking-wider dark:text-[#a3a3a3]">Members</label>
              <span className="text-xs font-mono text-[#76B900]">{options.numAgents}</span>
            </div>
            <input
              type="range" min="2" max="8" step="1"
              value={options.numAgents}
              disabled={disabled || !(options.autoAgents || options.preset)}
              onChange={e => set('numAgents', parseInt(e.target.value))}
              className="w-full"
            />
            <div className="text-[10px] font-mono text-[#a3a3a3] mt-1 dark:text-[#737373]">
              Applies when members are generated (auto or preset); otherwise the configured advisors run.
            </div>
          </div>

          <div>
            <label className="block text-[10px] font-mono text-[#737373] mb-1.5 uppercase tracking-wider dark:text-[#a3a3a3]">Strategy</label>
            <select
              value={options.strategy}
              onChange={e => set('strategy', e.target.value)}
              disabled={disabled}
              className="input-base w-full px-3 py-2 text-sm"
            >
              {STRATEGIES.map(s => (
                <option key={s.value} value={s.value}>{s.label}</option>
              ))}
            </select>
          </div>

          <div className="flex items-center justify-between gap-3">
            <div>
              <div className="text-xs font-mono text-[#525252] dark:text-[#a3a3a3]">Synthesize</div>
              <div className="text-[10px] font-mono text-[#a3a3a3] dark:text-[#737373]">Merge member answers into one reply</div>
            </div>
            <Toggle value={options.synthesize} onChange={v => set('synthesize', v)} disabled={disabled} />
          </div>

          {sortedProviders.length > 0 && (
            <div className="space-y-2">
              <div className="flex items-center justify-between">
                <span className="text-[10px] font-mono text-[#737373] uppercase tracking-wider dark:text-[#a3a3a3]">Weights</span>
                <span className="font-mono text-[9px] text-[#737373] dark:text-[#a3a3a3]">
                  <span className="text-[#16a34a]">●</span>{' '}
                  {providerHealth.filter(p => p.healthy).length} online
                </span>
              </div>
              <div className="text-[10px] font-mono text-[#a3a3a3] dark:text-[#737373]">0 = configured default</div>
              {sortedProviders.map(p => (
                <div key={p.name} className={p.healthy ? '' : 'opacity-60'}>
                  <div className="flex items-center justify-between mb-0.5">
                    <span className="text-[10px] font-mono uppercase flex items-center gap-1.5">
                      <span className={p.healthy ? 'text-[#16a34a]' : 'text-[#737373]'}>{p.healthy ? 'OK' : 'OFF'}</span>
                      <span className={p.healthy ? 'text-[#525252] dark:text-[#d4d4d4]' : 'text-[#a3a3a3]'}>{p.name}</span>
                    </span>
                    <span className="text-xs font-mono text-[#76B900]">{(options.weights[p.name] ?? 0).toFixed(2)}</span>
                  </div>
                  <input
                    type="range" min="0" max="2" step="0.05"
                    value={options.weights[p.name] ?? 0}
                    disabled={disabled}
                    onChange={e => {
                      const v = parseFloat(e.target.value);
                      const next = { ...options.weights };
                      if (v === 0) delete next[p.name]; else next[p.name] = v;
                      set('weights', next);
                    }}
                    className="w-full"
                  />
                </div>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
