'use client';

import { useEffect, useState, useCallback } from 'react';
import { getAnalytics } from '@/lib/api';
import type { AnalyticsData } from '@/lib/api';

// ─── Stat Card ──────────────────────────────────────────────────────────────

function StatCard({ label, value, sub }: { label: string; value: string | number; sub?: string }) {
  return (
    <div className="nvidia-corner border border-[#333333] bg-[#111111] p-4 relative overflow-hidden">
      <div className="absolute top-0 left-0 w-full h-px bg-gradient-to-r from-[#76B900]/60 to-transparent" />
      <div className="text-[9px] font-mono text-[#555555] uppercase tracking-[0.15em] mb-1">{label}</div>
      <div className="text-2xl font-bold text-white font-mono">{value}</div>
      {sub && <div className="text-[10px] font-mono text-[#444444] mt-1">{sub}</div>}
    </div>
  );
}

// ─── Bar (inline horizontal) ────────────────────────────────────────────────

function CostBar({ provider, cost, maxCost }: { provider: string; cost: number; maxCost: number }) {
  const pct = maxCost > 0 ? (cost / maxCost) * 100 : 0;
  return (
    <div className="flex items-center gap-3 py-1.5">
      <span className="text-xs font-mono text-[#888888] w-24 text-right truncate">{provider}</span>
      <div className="flex-1 bg-[#1a1a1a] h-5 relative overflow-hidden">
        <div
          className="h-full bg-[#76B900]/70 transition-all duration-500"
          style={{ width: `${Math.max(pct, 1)}%` }}
        />
        <span className="absolute right-2 top-1/2 -translate-y-1/2 text-[10px] font-mono text-[#aaaaaa]">
          ${cost.toFixed(4)}
        </span>
      </div>
    </div>
  );
}

// ─── Main Page ──────────────────────────────────────────────────────────────

export default function AnalyticsPage() {
  const [data, setData] = useState<AnalyticsData | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const loadData = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const result = await getAnalytics();
      setData(result);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load analytics');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    loadData();
  }, [loadData]);

  const totalQueries = data ? data.free_queries + data.paid_queries : 0;
  const freeRatio = totalQueries > 0 ? ((data!.free_queries / totalQueries) * 100).toFixed(1) : '0';

  const costEntries = data
    ? Object.entries(data.cost_by_provider).map(([p, c]) => ({ provider: p, cost: parseFloat(c) }))
    : [];
  const maxCost = Math.max(...costEntries.map(e => e.cost), 0.0001);

  return (
    <div className="p-6 space-y-6 max-w-7xl mx-auto">
      {/* Header */}
      <div className="nvidia-corner relative border border-[#333333] bg-[#111111] p-5 overflow-hidden">
        <div className="absolute top-0 left-0 right-0 h-px bg-gradient-to-r from-transparent via-[#76B900] to-transparent" />
        <div className="relative flex items-center justify-between">
          <div>
            <div className="text-[10px] font-mono text-[#76B900] tracking-[0.2em] uppercase mb-0.5">Usage Insights</div>
            <h1 className="text-2xl font-bold text-white">Analytics</h1>
            <p className="text-xs font-mono text-[#555555] mt-1">
              Query volume, cost breakdown, and provider performance
            </p>
          </div>
          <button
            onClick={loadData}
            disabled={loading}
            className="px-3 py-1.5 text-xs font-mono border border-[#333333] text-[#888888] hover:text-[#76B900] hover:border-[#76B900]/40 transition-colors disabled:opacity-40"
          >
            {loading ? 'Loading...' : 'Refresh'}
          </button>
        </div>
      </div>

      {error && (
        <div className="border border-[#ef4444]/30 bg-[#ef4444]/5 p-4">
          <span className="text-xs font-mono text-[#ef4444]">{error}</span>
        </div>
      )}

      {loading && !data && (
        <div className="flex items-center justify-center py-20">
          <div className="text-xs font-mono text-[#444444] animate-pulse uppercase tracking-widest">Loading analytics...</div>
        </div>
      )}

      {data && (
        <>
          {/* ── Query volume cards ──────────────────────────────────── */}
          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
            <StatCard label="Queries Today" value={data.queries_today} />
            <StatCard label="This Week" value={data.queries_this_week} />
            <StatCard label="This Month" value={data.queries_this_month} />
            <StatCard
              label="Free / Paid Ratio"
              value={`${freeRatio}%`}
              sub={`${data.free_queries} free / ${data.paid_queries} paid`}
            />
          </div>

          {/* ── Cost breakdown + Queries per provider ─────────────── */}
          <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
            {/* Cost by provider bar chart */}
            <div className="nvidia-corner border border-[#333333] bg-[#111111] p-5 relative overflow-hidden">
              <div className="absolute top-0 left-0 w-full h-px bg-gradient-to-r from-[#76B900]/40 to-transparent" />
              <div className="text-[10px] font-mono text-[#76B900] tracking-[0.15em] uppercase mb-3">Cost by Provider (This Month)</div>
              {costEntries.length === 0 ? (
                <div className="text-xs font-mono text-[#333333] py-4 text-center">No cost data yet</div>
              ) : (
                <div className="space-y-1">
                  {costEntries
                    .sort((a, b) => b.cost - a.cost)
                    .map(e => (
                      <CostBar key={e.provider} provider={e.provider} cost={e.cost} maxCost={maxCost} />
                    ))}
                </div>
              )}
            </div>

            {/* Queries per provider table */}
            <div className="nvidia-corner border border-[#333333] bg-[#111111] p-5 relative overflow-hidden">
              <div className="absolute top-0 left-0 w-full h-px bg-gradient-to-r from-[#76B900]/40 to-transparent" />
              <div className="text-[10px] font-mono text-[#76B900] tracking-[0.15em] uppercase mb-3">Queries by Provider (This Month)</div>
              <table className="w-full text-xs font-mono">
                <thead>
                  <tr className="text-[#555555] text-left border-b border-[#222222]">
                    <th className="pb-2 font-medium">Provider</th>
                    <th className="pb-2 font-medium text-right">Queries</th>
                    <th className="pb-2 font-medium text-right">Avg Latency</th>
                    <th className="pb-2 font-medium text-right">Cost</th>
                  </tr>
                </thead>
                <tbody>
                  {Object.keys(data.queries_by_provider).length === 0 ? (
                    <tr>
                      <td colSpan={4} className="text-center text-[#333333] py-4">No query data yet</td>
                    </tr>
                  ) : (
                    Object.entries(data.queries_by_provider)
                      .sort(([, a], [, b]) => b - a)
                      .map(([provider, count]) => (
                        <tr key={provider} className="border-b border-[#1a1a1a] hover:bg-[#1a1a1a] transition-colors">
                          <td className="py-2 text-[#cccccc]">{provider}</td>
                          <td className="py-2 text-right text-[#888888]">{count}</td>
                          <td className="py-2 text-right text-[#888888]">
                            {data.latency_by_provider[provider]
                              ? `${data.latency_by_provider[provider].toFixed(0)}ms`
                              : '--'}
                          </td>
                          <td className="py-2 text-right text-[#888888]">
                            ${parseFloat(data.cost_by_provider[provider] || '0').toFixed(4)}
                          </td>
                        </tr>
                      ))
                  )}
                </tbody>
              </table>
            </div>
          </div>

          {/* ── Most used models + Savings ────────────────────────── */}
          <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
            {/* Most used models */}
            <div className="nvidia-corner border border-[#333333] bg-[#111111] p-5 relative overflow-hidden">
              <div className="absolute top-0 left-0 w-full h-px bg-gradient-to-r from-[#76B900]/40 to-transparent" />
              <div className="text-[10px] font-mono text-[#76B900] tracking-[0.15em] uppercase mb-3">Most Used Models</div>
              <table className="w-full text-xs font-mono">
                <thead>
                  <tr className="text-[#555555] text-left border-b border-[#222222]">
                    <th className="pb-2 font-medium">Model</th>
                    <th className="pb-2 font-medium">Provider</th>
                    <th className="pb-2 font-medium text-right">Queries</th>
                  </tr>
                </thead>
                <tbody>
                  {data.most_used_models.length === 0 ? (
                    <tr>
                      <td colSpan={3} className="text-center text-[#333333] py-4">No model data yet</td>
                    </tr>
                  ) : (
                    data.most_used_models.map((m, i) => (
                      <tr key={i} className="border-b border-[#1a1a1a] hover:bg-[#1a1a1a] transition-colors">
                        <td className="py-2 text-[#cccccc] truncate max-w-[200px]">{m.model || '(default)'}</td>
                        <td className="py-2 text-[#666666]">{m.provider}</td>
                        <td className="py-2 text-right text-[#888888]">{m.count}</td>
                      </tr>
                    ))
                  )}
                </tbody>
              </table>
            </div>

            {/* Savings from local models */}
            <div className="nvidia-corner border border-[#333333] bg-[#111111] p-5 relative overflow-hidden">
              <div className="absolute top-0 left-0 w-full h-px bg-gradient-to-r from-[#76B900]/40 to-transparent" />
              <div className="text-[10px] font-mono text-[#76B900] tracking-[0.15em] uppercase mb-3">Savings from Local Models</div>
              <div className="space-y-4">
                <div className="grid grid-cols-2 gap-4">
                  <div>
                    <div className="text-[9px] font-mono text-[#555555] uppercase tracking-[0.1em] mb-1">Total Saved</div>
                    <div className="text-xl font-bold text-[#76B900] font-mono">
                      ${parseFloat(data.savings.total_savings).toFixed(4)}
                    </div>
                  </div>
                  <div>
                    <div className="text-[9px] font-mono text-[#555555] uppercase tracking-[0.1em] mb-1">Savings Rate</div>
                    <div className="text-xl font-bold text-[#76B900] font-mono">
                      {data.savings.savings_pct.toFixed(1)}%
                    </div>
                  </div>
                </div>

                <div className="border-t border-[#222222] pt-3 space-y-2">
                  <div className="flex justify-between text-xs font-mono">
                    <span className="text-[#555555]">Local queries</span>
                    <span className="text-[#888888]">{data.savings.local_queries}</span>
                  </div>
                  <div className="flex justify-between text-xs font-mono">
                    <span className="text-[#555555]">Cloud queries</span>
                    <span className="text-[#888888]">{data.savings.cloud_queries}</span>
                  </div>
                  <div className="flex justify-between text-xs font-mono">
                    <span className="text-[#555555]">Est. cloud cost (if no local)</span>
                    <span className="text-[#888888]">${parseFloat(data.savings.estimated_cloud_cost).toFixed(4)}</span>
                  </div>
                </div>

                {/* Savings bar */}
                <div>
                  <div className="text-[9px] font-mono text-[#444444] mb-1">Local vs Cloud Usage</div>
                  <div className="w-full h-4 bg-[#1a1a1a] flex overflow-hidden">
                    {totalQueries > 0 && (
                      <>
                        <div
                          className="h-full bg-[#76B900]/60 transition-all duration-500"
                          style={{ width: `${(data.savings.local_queries / totalQueries) * 100}%` }}
                          title={`Local: ${data.savings.local_queries}`}
                        />
                        <div
                          className="h-full bg-[#555555]/40 transition-all duration-500"
                          style={{ width: `${(data.savings.cloud_queries / totalQueries) * 100}%` }}
                          title={`Cloud: ${data.savings.cloud_queries}`}
                        />
                      </>
                    )}
                  </div>
                  <div className="flex justify-between text-[9px] font-mono text-[#333333] mt-1">
                    <span>Local</span>
                    <span>Cloud</span>
                  </div>
                </div>
              </div>
            </div>
          </div>
        </>
      )}
    </div>
  );
}
