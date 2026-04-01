'use client';

import { useEffect, useState } from 'react';
import { getBudgetStatus } from '@/lib/api';
import type { BudgetStatus } from '@/lib/types';

function formatUSD(value: string): string {
  const n = parseFloat(value);
  if (isNaN(n)) return '$—';
  return n < 0.01 ? `$${n.toFixed(4)}` : `$${n.toFixed(2)}`;
}

function pct(spend: string, limit: string): number {
  const s = parseFloat(spend);
  const l = parseFloat(limit);
  if (!l || isNaN(s) || isNaN(l)) return 0;
  return Math.min(100, (s / l) * 100);
}

function StatusBar({ value, label, spend, limit }: {
  value: number;
  label: string;
  spend: string;
  limit: string;
}) {
  const color =
    value >= 90 ? '#ef4444' :
    value >= 70 ? '#f59e0b' :
    '#22c55e';

  return (
    <div>
      <div className="flex items-center justify-between mb-1.5">
        <span className="text-xs text-[#94a3b8]">{label}</span>
        <span className="text-xs font-mono text-[#e2e8f0]">
          {formatUSD(spend)} / {formatUSD(limit)}
        </span>
      </div>
      <div className="progress-bar">
        <div
          className="progress-fill"
          style={{ width: `${value}%`, backgroundColor: color }}
        />
      </div>
      <div className="text-right mt-0.5">
        <span className="text-xs font-mono" style={{ color }}>{value.toFixed(1)}%</span>
      </div>
    </div>
  );
}

interface Props {
  className?: string;
}

export default function BudgetWidget({ className = '' }: Props) {
  const [budget, setBudget] = useState<BudgetStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let mounted = true;
    const load = async () => {
      try {
        const data = await getBudgetStatus();
        if (mounted) { setBudget(data); setError(null); }
      } catch (err) {
        if (mounted) setError(err instanceof Error ? err.message : 'Failed to load');
      } finally {
        if (mounted) setLoading(false);
      }
    };
    load();
    const id = setInterval(load, 60_000);
    return () => { mounted = false; clearInterval(id); };
  }, []);

  return (
    <div className={`card p-5 ${className}`}>
      <div className="flex items-center justify-between mb-4">
        <div className="flex items-center gap-2">
          <svg className="w-4 h-4 text-[#f59e0b]" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
            <path strokeLinecap="round" strokeLinejoin="round"
              d="M12 6v12m-3-2.818l.879.659c1.171.879 3.07.879 4.242 0 1.172-.879 1.172-2.303 0-3.182C13.536 12.219 12.768 12 12 12c-.725 0-1.45-.22-2.003-.659-1.106-.879-1.106-2.303 0-3.182s2.9-.879 4.006 0l.415.33M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
          </svg>
          <span className="font-semibold text-sm text-[#e2e8f0]">Budget</span>
        </div>
        {loading && (
          <div className="w-3 h-3 border-2 border-[#3b82f6]/30 border-t-[#3b82f6] rounded-full animate-spin" />
        )}
      </div>

      {error ? (
        <div className="text-xs text-[#ef4444] font-mono">{error}</div>
      ) : loading ? (
        <div className="space-y-4">
          {[1, 2].map(i => (
            <div key={i} className="space-y-1.5">
              <div className="h-3 bg-[#1a1a2e] rounded animate-pulse" />
              <div className="h-1.5 bg-[#1a1a2e] rounded animate-pulse" />
            </div>
          ))}
        </div>
      ) : budget ? (
        <div className="space-y-4">
          <StatusBar
            label="Daily"
            spend={budget.daily_spend}
            limit={budget.daily_limit}
            value={pct(budget.daily_spend, budget.daily_limit)}
          />
          <StatusBar
            label="Monthly"
            spend={budget.monthly_spend}
            limit={budget.monthly_limit}
            value={pct(budget.monthly_spend, budget.monthly_limit)}
          />

          <div className="grid grid-cols-2 gap-2 pt-1">
            <div className="bg-[#0a0a0a] rounded-lg p-2.5 text-center">
              <div className="font-mono text-sm font-bold text-[#e2e8f0]">
                {budget.daily_queries}
              </div>
              <div className="text-xs text-[#475569] mt-0.5">Today</div>
            </div>
            <div className="bg-[#0a0a0a] rounded-lg p-2.5 text-center">
              <div className="font-mono text-sm font-bold text-[#e2e8f0]">
                {budget.monthly_queries}
              </div>
              <div className="text-xs text-[#475569] mt-0.5">This month</div>
            </div>
          </div>

          {Object.keys(budget.by_provider).length > 0 && (
            <div>
              <div className="section-label mb-2">By Provider</div>
              <div className="space-y-1">
                {Object.entries(budget.by_provider).map(([provider, cost]) => (
                  <div key={provider} className="flex items-center justify-between">
                    <span className="text-xs text-[#94a3b8] capitalize">{provider}</span>
                    <span className="text-xs font-mono text-[#e2e8f0]">{formatUSD(cost)}</span>
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>
      ) : null}
    </div>
  );
}
