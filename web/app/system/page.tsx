'use client';

import { useEffect, useState, useCallback } from 'react';
import Link from 'next/link';
import BudgetWidget from '@/components/BudgetWidget';
import ProviderCard from '@/components/ProviderCard';
import { checkHealth, getProviders, getCacheStats, getGPUInfo, getRecommendations } from '@/lib/api';
import type { ProviderHealth, CacheStats, GPUInfo, ModelRecommendation } from '@/lib/types';

export default function SystemPage() {
  const [apiStatus, setApiStatus] = useState<'connected' | 'disconnected' | 'checking'>('checking');
  const [providers, setProviders] = useState<ProviderHealth[]>([]);
  const [cacheStats, setCacheStats] = useState<CacheStats | null>(null);
  const [providersLoading, setProvidersLoading] = useState(true);
  const [gpuInfo, setGpuInfo] = useState<GPUInfo | null>(null);
  const [gpuLoading, setGpuLoading] = useState(true);
  const [topRec, setTopRec] = useState<ModelRecommendation | null>(null);

  const loadProviders = useCallback(async () => {
    setProvidersLoading(true);
    try {
      const data = await getProviders();
      setProviders(data.providers);
    } catch {
      setProviders([]);
    } finally {
      setProvidersLoading(false);
    }
  }, []);

  useEffect(() => {
    let mounted = true;

    const init = async () => {
      try {
        await checkHealth();
        if (mounted) setApiStatus('connected');
      } catch {
        if (mounted) setApiStatus('disconnected');
      }

      await loadProviders();

      try {
        const stats = await getCacheStats();
        if (mounted) setCacheStats(stats);
      } catch {
        // ignore
      }

      setGpuLoading(true);
      try {
        const [gpu, recs] = await Promise.all([getGPUInfo(), getRecommendations()]);
        if (mounted) {
          setGpuInfo(gpu);
          setTopRec(recs.recommendations[0] ?? null);
        }
      } catch {
        // GPU endpoints unavailable
      } finally {
        if (mounted) setGpuLoading(false);
      }
    };

    init();

    const interval = setInterval(() => {
      checkHealth()
        .then(() => mounted && setApiStatus('connected'))
        .catch(() => mounted && setApiStatus('disconnected'));
    }, 30_000);

    return () => {
      mounted = false;
      clearInterval(interval);
    };
  }, [loadProviders]);

  const healthyCount = providers.filter(p => p.healthy).length;

  return (
    <div className="p-6 space-y-6 max-w-7xl mx-auto">
      {/* Page header */}
      <div className="nvidia-corner relative border border-[#333333] bg-[#111111] p-5 overflow-hidden">
        <div className="absolute top-0 left-0 right-0 h-px bg-gradient-to-r from-transparent via-[#76B900] to-transparent" />
        <div className="absolute inset-0 grid-bg opacity-30 pointer-events-none" />
        <div className="relative flex items-center justify-between flex-wrap gap-4">
          <div>
            <div className="text-[10px] font-mono text-[#76B900] tracking-[0.25em] uppercase mb-1">
              System Monitor
            </div>
            <h1 className="text-2xl font-bold text-white tracking-tight">System Status</h1>
            <p className="text-sm text-[#555555] mt-1 font-mono">
              Advisor health, GPU status, budget &amp; cache metrics
            </p>
          </div>
          <div className="flex items-center gap-3">
            <div className={`flex items-center gap-2 px-3 py-1.5 border text-xs font-mono ${
              apiStatus === 'connected'
                ? 'border-[#76B900]/40 bg-[#76B900]/5 text-[#76B900]'
                : apiStatus === 'disconnected'
                ? 'border-[#ef4444]/40 bg-[#ef4444]/5 text-[#ef4444]'
                : 'border-[#333333] text-[#555555]'
            }`}>
              <span
                className={`w-1.5 h-1.5 flex-shrink-0 ${
                  apiStatus === 'connected' ? 'bg-[#76B900] nvidia-pulse' :
                  apiStatus === 'disconnected' ? 'bg-[#ef4444]' :
                  'bg-[#444444] animate-pulse'
                }`}
                style={{ clipPath: 'polygon(50% 0%, 100% 50%, 50% 100%, 0% 50%)' }}
              />
              {apiStatus === 'connected' ? 'API ONLINE' : apiStatus === 'disconnected' ? 'API OFFLINE' : 'CHECKING'}
            </div>
            <Link href="/" className="btn-primary px-4 py-2 text-sm flex items-center gap-2 font-mono tracking-wide">
              <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                <path strokeLinecap="round" strokeLinejoin="round"
                  d="M7.5 8.25h9m-9 3H12m-9.75 1.51c0 1.6 1.123 2.994 2.707 3.227 1.129.166 2.27.293 3.423.379.35.026.67.21.865.501L12 21l2.755-4.133a1.14 1.14 0 01.865-.501 48.172 48.172 0 003.423-.379c1.584-.233 2.707-1.626 2.707-3.228V6.741c0-1.602-1.123-2.995-2.707-3.228A48.394 48.394 0 0012 3c-2.392 0-4.744.175-7.043.513C3.373 3.746 2.25 5.14 2.25 6.741v6.018z" />
              </svg>
              OPEN CHAT
            </Link>
          </div>
        </div>
      </div>

      {/* GPU + stat cards */}
      <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
        <div className="card p-4 col-span-1 nvidia-corner">
          <div className="section-label mb-3">GPU Status</div>

          {gpuLoading ? (
            <div className="space-y-2 animate-pulse">
              <div className="h-10 bg-[#1a1a1a]" />
              <div className="h-3 bg-[#1a1a1a] w-3/4" />
              <div className="h-2 bg-[#1a1a1a]" />
            </div>
          ) : gpuInfo && gpuInfo.gpus.length > 0 ? (
            (() => {
              const g = gpuInfo.gpus[0];
              const usedPct = g.vram_mb > 0 ? Math.round((g.memory_used_mb / g.vram_mb) * 100) : 0;
              const barColor = usedPct > 90 ? '#ef4444' : usedPct > 70 ? '#f59e0b' : '#76B900';
              return (
                <div>
                  <div className="flex items-center gap-3 mb-3">
                    <div className="w-10 h-10 bg-[#76B900]/10 border border-[#76B900]/30 flex items-center justify-center flex-shrink-0">
                      <svg className="w-5 h-5 text-[#76B900]" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
                        <path strokeLinecap="round" strokeLinejoin="round"
                          d="M9 3H5a2 2 0 00-2 2v4m6-6h10a2 2 0 012 2v4M9 3v18m0 0h10a2 2 0 002-2V9M9 21H5a2 2 0 01-2-2V9m0 0h18" />
                      </svg>
                    </div>
                    <div className="min-w-0">
                      <div className="text-sm font-bold text-white truncate">{g.name}</div>
                      <div className="text-xs font-mono text-[#76B900]">GPU DETECTED</div>
                    </div>
                  </div>
                  <div className="space-y-1.5">
                    <div className="flex items-center justify-between text-[10px] font-mono">
                      <span className="text-[#555555]">VRAM</span>
                      <span className="text-[#999999]">
                        {(g.memory_used_mb / 1024).toFixed(1)} / {g.vram_gb} GB ({usedPct}%)
                      </span>
                    </div>
                    <div className="progress-bar">
                      <div className="progress-fill transition-all" style={{ width: `${usedPct}%`, backgroundColor: barColor }} />
                    </div>
                    <div className="flex items-center justify-between text-[10px] font-mono text-[#444444]">
                      <span>Util {g.utilization_pct}%</span>
                      <span>CUDA {g.cuda_version}</span>
                    </div>
                    <div className="text-[10px] font-mono text-[#444444]">driver {g.driver_version}</div>
                  </div>
                  {topRec && (
                    <div className="mt-3 border-t border-[#1a1a1a] pt-2">
                      <div className="flex items-center justify-between">
                        <span className="text-[10px] font-mono text-[#555555] uppercase">Recommended</span>
                        <span className="text-[10px] font-mono px-1.5 py-0.5 bg-[#76B900]/10 text-[#76B900] uppercase">{topRec.tier}</span>
                      </div>
                      <div className="text-xs font-mono font-bold text-white mt-0.5">{topRec.model}</div>
                    </div>
                  )}
                </div>
              );
            })()
          ) : (
            <div>
              <div className="flex items-center gap-3 mb-3">
                <div className="w-10 h-10 bg-[#222222] border border-[#333333] flex items-center justify-center flex-shrink-0">
                  <svg className="w-5 h-5 text-[#555555]" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
                    <path strokeLinecap="round" strokeLinejoin="round"
                      d="M9 3H5a2 2 0 00-2 2v4m6-6h10a2 2 0 012 2v4M9 3v18m0 0h10a2 2 0 002-2V9M9 21H5a2 2 0 01-2-2V9m0 0h18" />
                  </svg>
                </div>
                <div>
                  <div className="text-sm font-bold text-white">CPU Mode</div>
                  <div className="text-xs font-mono text-[#555555]">NO GPU DETECTED</div>
                </div>
              </div>
              <div className="text-[10px] font-mono text-[#444444] leading-relaxed">
                No NVIDIA GPU found. Local models will run on CPU.
              </div>
            </div>
          )}
        </div>

        <div className="col-span-2 grid grid-cols-2 sm:grid-cols-4 gap-4">
          <StatCard
            label="Advisors"
            value={`${healthyCount}/${providers.length}`}
            sub="online"
            color={healthyCount === providers.length && providers.length > 0 ? '#76B900' : '#f59e0b'}
            loading={providersLoading}
            icon="▣"
          />
          <StatCard
            label="Cache Hits"
            value={cacheStats ? `${cacheStats.hits}` : '—'}
            sub={cacheStats ? `${(cacheStats.hit_rate * 100).toFixed(0)}% rate` : undefined}
            color="#76B900"
            icon="⚡"
          />
          <StatCard
            label="Cache Size"
            value={cacheStats ? `${cacheStats.size}/${cacheStats.max_size}` : '—'}
            color="#999999"
            icon="◈"
          />
          <StatCard
            label="API"
            value={apiStatus === 'connected' ? 'ONLINE' : apiStatus === 'disconnected' ? 'OFFLINE' : '...'}
            color={apiStatus === 'connected' ? '#76B900' : '#ef4444'}
            icon="◎"
          />
        </div>
      </div>

      {/* Main grid */}
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
        <div className="lg:col-span-2 space-y-3">
          <div className="flex items-center justify-between">
            <div className="section-label">Advisor Status</div>
            <button
              onClick={loadProviders}
              className="text-[10px] font-mono text-[#444444] hover:text-[#76B900] flex items-center gap-1.5 transition-colors"
            >
              <svg className="w-3 h-3" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                <path strokeLinecap="round" strokeLinejoin="round"
                  d="M16.023 9.348h4.992v-.001M2.985 19.644v-4.992m0 0h4.992m-4.993 0l3.181 3.183a8.25 8.25 0 0013.803-3.7M4.031 9.865a8.25 8.25 0 0113.803-3.7l3.181 3.182m0-4.991v4.99" />
              </svg>
              REFRESH
            </button>
          </div>

          {providersLoading ? (
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
              {[1, 2, 3, 4].map(i => (
                <div key={i} className="card p-5 h-48 animate-pulse">
                  <div className="h-3 bg-[#222222] mb-3 w-1/2" />
                  <div className="h-2 bg-[#1a1a1a] mb-2" />
                  <div className="h-2 bg-[#1a1a1a] w-3/4" />
                </div>
              ))}
            </div>
          ) : providers.length === 0 ? (
            <div className="card p-8 text-center">
              <div className="text-3xl mb-3 text-[#333333]">▣</div>
              <div className="text-[#666666] font-mono text-sm mb-1">NO ADVISORS AVAILABLE</div>
              <div className="text-xs text-[#444444] font-mono mb-4">
                {apiStatus === 'disconnected'
                  ? 'Connect the API server to see advisors'
                  : 'Configure advisors in Settings or start the Hive API'}
              </div>
              <Link href="/setup" className="btn-secondary inline-flex mt-2 px-4 py-2 text-xs font-mono gap-2 items-center">
                RUN SETUP WIZARD
              </Link>
            </div>
          ) : (
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
              {providers.map(p => (
                <ProviderCard key={p.name} provider={p} onRefresh={loadProviders} />
              ))}
            </div>
          )}
        </div>

        <div className="space-y-4">
          <BudgetWidget />

          {/* Quick links */}
          <div className="card p-4">
            <div className="section-label mb-3">Quick Links</div>
            <div className="space-y-1">
              {[
                { href: '/', label: 'Chat Interface', icon: '▶', color: '#76B900' },
                { href: '/providers', label: 'View Advisors', icon: '▲', color: '#76B900' },
                { href: '/setup', label: 'Setup Wizard', icon: '◎', color: '#f59e0b' },
                { href: '/settings', label: 'Settings', icon: '⚙', color: '#555555' },
              ].map(({ href, label, icon, color }) => (
                <Link
                  key={href}
                  href={href}
                  className="flex items-center gap-3 px-3 py-2 hover:bg-[#1a1a1a] border border-transparent hover:border-[#76B900]/20 text-[#666666] hover:text-white transition-all text-xs font-mono"
                >
                  <span style={{ color }}>{icon}</span>
                  {label.toUpperCase()}
                  <svg className="w-3 h-3 ml-auto text-[#333333]" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                    <path strokeLinecap="round" strokeLinejoin="round" d="M8.25 4.5l7.5 7.5-7.5 7.5" />
                  </svg>
                </Link>
              ))}
            </div>
          </div>
        </div>
      </div>

      {/* Footer */}
      <div className="border-t border-[#1a1a1a] pt-4 flex items-center justify-between">
        <div className="text-[10px] font-mono text-[#333333]">COUNCIL AI — SYSTEM MONITOR</div>
        <div className="text-[10px] font-mono text-[#333333]">POWERED BY NVIDIA · LOCAL AI</div>
      </div>
    </div>
  );
}

function StatCard({
  label,
  value,
  sub,
  icon,
  color,
  loading = false,
}: {
  label: string;
  value: string;
  sub?: string;
  icon: string;
  color: string;
  loading?: boolean;
}) {
  return (
    <div className="card p-4">
      <div className="flex items-center justify-between mb-2">
        <span className="text-[10px] font-mono text-[#555555] uppercase tracking-wider">{label}</span>
        <span style={{ color }} className="text-sm">{icon}</span>
      </div>
      {loading ? (
        <div className="h-6 w-16 bg-[#1a1a1a] animate-pulse" />
      ) : (
        <div className="font-mono font-bold text-lg leading-none" style={{ color }}>
          {value}
        </div>
      )}
      {sub && <div className="text-[10px] font-mono text-[#444444] mt-1">{sub}</div>}
    </div>
  );
}
