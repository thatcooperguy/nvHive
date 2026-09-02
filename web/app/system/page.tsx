'use client';

import { useEffect, useState, useCallback } from 'react';
import Link from 'next/link';
import PageHeader from '@/components/PageHeader';
import BudgetWidget from '@/components/BudgetWidget';
import ProviderCard from '@/components/ProviderCard';
import { checkHealth, getProviders, getCacheStats, getGPUInfo, getRecommendations } from '@/lib/api';
import type { ProviderHealth, CacheStats, GPUInfo, ModelRecommendation } from '@/lib/types';
import { GpuBlockedSummary, MemoryUnreadableTag, gpuStatusOf, isMemoryUnreadable, primaryGpu } from '@/components/UnifiedMemoryTag';

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

    checkHealth()
      .then(() => mounted && setApiStatus('connected'))
      .catch(() => mounted && setApiStatus('disconnected'));

    void loadProviders();

    getCacheStats()
      .then(stats => mounted && setCacheStats(stats))
      .catch(() => mounted && setCacheStats(null));

    setGpuLoading(true);
    Promise.all([getGPUInfo(), getRecommendations()])
      .then(([gpu, recs]) => {
        if (!mounted) return;
        setGpuInfo(gpu);
        setTopRec(recs.recommendations[0] ?? null);
      })
      .catch(() => {
        if (!mounted) {
          return;
        }
        setGpuInfo(null);
        setTopRec(null);
      })
      .finally(() => {
        if (mounted) setGpuLoading(false);
      });

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
  const cacheHitRate = cacheStats && Number.isFinite(cacheStats.hit_rate) ? cacheStats.hit_rate : 0;

  return (
    <div>
      <PageHeader
        eyebrow="Computer Check"
        title="My Computer"
        subtitle="Check whether local AI is ready on this desktop."
        trailing={
          <div className="flex items-center gap-3">
            <div className={`flex items-center gap-2 px-3 py-1.5 border text-xs font-mono ${
              apiStatus === 'connected'
                ? 'border-[#76B900]/40 bg-[#76B900]/5 text-[#76B900]'
                : apiStatus === 'disconnected'
                  ? 'border-[#dc2626]/40 bg-[#dc2626]/5 text-[#dc2626]'
                  : 'border-[#d4d4d4] text-[#a3a3a3]'
            }`}>
              <span
                className={`w-1.5 h-1.5 flex-shrink-0 ${
                  apiStatus === 'connected' ? 'bg-[#76B900] nvidia-pulse' :
                    apiStatus === 'disconnected' ? 'bg-[#dc2626]' :
                      'bg-[#a3a3a3] animate-pulse'
                }`}
                style={{ clipPath: 'polygon(50% 0%, 100% 50%, 50% 100%, 0% 50%)' }}
              />
              {apiStatus === 'connected' ? 'SERVICE RUNNING' : apiStatus === 'disconnected' ? 'SERVICE OFFLINE' : 'CHECKING'}
            </div>
            <Link href="/" className="btn-primary px-4 py-2 text-sm flex items-center gap-2 font-mono tracking-wide">
              <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                <path strokeLinecap="round" strokeLinejoin="round"
                  d="M7.5 8.25h9m-9 3H12m-9.75 1.51c0 1.6 1.123 2.994 2.707 3.227 1.129.166 2.27.293 3.423.379.35.026.67.21.865.501L12 21l2.755-4.133a1.14 1.14 0 01.865-.501 48.172 48.172 0 003.423-.379c1.584-.233 2.707-1.626 2.707-3.228V6.741c0-1.602-1.123-2.995-2.707-3.228A48.394 48.394 0 0012 3c-2.392 0-4.744.175-7.043.513C3.373 3.746 2.25 5.14 2.25 6.741v6.018z" />
              </svg>
              ASK A QUESTION
            </Link>
          </div>
        }
      />
      <div className="p-6 space-y-6 max-w-7xl mx-auto">

      {/* GPU + stat cards */}
      <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
        <div className="card p-4 col-span-1 nvidia-corner">
          <div className="section-label mb-3">GPU Status</div>

          {gpuLoading ? (
            <div className="space-y-2 animate-pulse">
              <div className="h-10 bg-[#f5f5f5] dark:bg-[#141414]" />
              <div className="h-3 bg-[#f5f5f5] w-3/4 dark:bg-[#141414]" />
              <div className="h-2 bg-[#f5f5f5] dark:bg-[#141414]" />
            </div>
          ) : gpuInfo && gpuInfo.gpus.length > 0 ? (
            (() => {
              // The card shows one row: the primary (first sized, else first), the same row the
              // API sizes against — not gpus[0], which on a multi-GPU box may be the unreadable
              // one. The summary line below names any unreadable row that is not shown.
              const g = primaryGpu(gpuInfo.gpus);
              if (!g) return null;
              const usedPct = g.vram_mb > 0 ? Math.round((g.memory_used_mb / g.vram_mb) * 100) : 0;
              const barColor = usedPct > 90 ? '#dc2626' : usedPct > 70 ? '#d97706' : '#76B900';
              // Visible but unsized: the API keeps the row at 0 MiB so the GPU's name is
              // still seen; "0.0 / 0 GB" would present that unknown as a figure.
              const unreadable = isMemoryUnreadable(g);
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
                      <div className="text-sm font-bold text-[#0a0a0a] truncate dark:text-[#fafafa]">{g.name}</div>
                      <div className="text-xs font-mono text-[#76B900]">GPU DETECTED</div>
                    </div>
                  </div>
                  <div className="space-y-1.5">
                    <div className="flex items-center justify-between text-[10px] font-mono">
                      <span className="text-[#a3a3a3] dark:text-[#737373]">VRAM</span>
                      {unreadable ? (
                        <MemoryUnreadableTag summary={gpuInfo.summary} />
                      ) : (
                        <span className="text-[#525252] dark:text-[#a3a3a3]">
                          {(g.memory_used_mb / 1024).toFixed(1)} / {g.vram_gb} GB ({usedPct}%)
                        </span>
                      )}
                    </div>
                    <div className="progress-bar">
                      <div className="progress-fill transition-all" style={{ width: `${usedPct}%`, backgroundColor: barColor }} />
                    </div>
                    <div className="flex items-center justify-between text-[10px] font-mono text-[#a3a3a3] dark:text-[#737373]">
                      <span>Util {g.utilization_pct}%</span>
                      <span>CUDA {g.cuda_version}</span>
                    </div>
                    <div className="text-[10px] font-mono text-[#a3a3a3] dark:text-[#737373]">driver {g.driver_version}</div>
                  </div>
                  {/* Once, under the GPU block: why detection is 'blocked' (a visible GPU with no readable memory
                      pool), or which row is unreadable beside the sized one shown above. */}
                  <GpuBlockedSummary status={gpuStatusOf(gpuInfo)} summary={gpuInfo.summary} gpus={gpuInfo.gpus} className="mt-2" />
                  {topRec && (
                    <div className="mt-3 border-t border-[#e5e5e5] pt-2 dark:border-[#262626]">
                      <div className="flex items-center justify-between">
                        <span className="text-[10px] font-mono text-[#a3a3a3] uppercase dark:text-[#737373]">Recommended</span>
                        <span className="text-[10px] font-mono px-1.5 py-0.5 bg-[#76B900]/10 text-[#76B900] uppercase">{topRec.tier}</span>
                      </div>
                      <div className="text-xs font-mono font-bold text-[#0a0a0a] mt-0.5 dark:text-[#fafafa]">{topRec.model}</div>
                    </div>
                  )}
                </div>
              );
            })()
          ) : (
            <div>
              <div className="flex items-center gap-3 mb-3">
                <div className="w-10 h-10 bg-[#e5e5e5] border border-[#d4d4d4] flex items-center justify-center flex-shrink-0 dark:border-[#404040]">
                  <svg className="w-5 h-5 text-[#a3a3a3] dark:text-[#737373]" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
                    <path strokeLinecap="round" strokeLinejoin="round"
                      d="M9 3H5a2 2 0 00-2 2v4m6-6h10a2 2 0 012 2v4M9 3v18m0 0h10a2 2 0 002-2V9M9 21H5a2 2 0 01-2-2V9m0 0h18" />
                  </svg>
                </div>
                <div>
                  <div className="text-sm font-bold text-[#0a0a0a] dark:text-[#fafafa]">CPU Mode</div>
                  <div className="text-xs font-mono text-[#a3a3a3] dark:text-[#737373]">NO GPU DETECTED</div>
                </div>
              </div>
              <div className="text-[10px] font-mono text-[#a3a3a3] leading-relaxed dark:text-[#737373]">
                No NVIDIA GPU found. Local models will run on CPU.
              </div>
            </div>
          )}
        </div>

        <div className="col-span-2 grid grid-cols-2 sm:grid-cols-4 gap-4">
          <StatCard
            label="AI Connections"
            value={`${healthyCount}/${providers.length}`}
            sub="online"
            color={healthyCount === providers.length && providers.length > 0 ? '#76B900' : '#d97706'}
            loading={providersLoading}
            icon="AI"
          />
          <StatCard
            label="Cache Hits"
            value={cacheStats ? `${cacheStats.hits ?? 0}` : '-'}
            sub={cacheStats ? `${(cacheHitRate * 100).toFixed(0)}% rate` : undefined}
            color="#76B900"
            icon="CACHE"
          />
          <StatCard
            label="Cache Size"
            value={cacheStats ? `${cacheStats.size ?? 0}/${cacheStats.max_size ?? 0}` : '-'}
            color="#525252"
            icon="SIZE"
          />
          <StatCard
            label="Service"
            value={apiStatus === 'connected' ? 'ONLINE' : apiStatus === 'disconnected' ? 'OFFLINE' : '...'}
            color={apiStatus === 'connected' ? '#76B900' : '#dc2626'}
            icon="SVC"
          />
        </div>
      </div>

      {/* Main grid */}
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
        <div className="lg:col-span-2 space-y-3">
          <div className="flex items-center justify-between">
            <div className="section-label">AI Connections</div>
            <button
              onClick={loadProviders}
              className="text-[10px] font-mono text-[#a3a3a3] hover:text-[#76B900] flex items-center gap-1.5 transition-colors dark:text-[#737373]"
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
                  <div className="h-3 bg-[#e5e5e5] mb-3 w-1/2" />
                  <div className="h-2 bg-[#f5f5f5] mb-2 dark:bg-[#141414]" />
                  <div className="h-2 bg-[#f5f5f5] w-3/4 dark:bg-[#141414]" />
                </div>
              ))}
            </div>
          ) : providers.length === 0 ? (
            <div className="card p-8 text-center">
              <div className="text-3xl mb-3 text-[#333333] font-mono dark:text-[#525252]">AI</div>
              <div className="text-[#737373] font-mono text-sm mb-1 dark:text-[#a3a3a3]">NO ADVISORS AVAILABLE</div>
              <div className="text-xs text-[#a3a3a3] font-mono mb-4 dark:text-[#737373]">
                {apiStatus === 'disconnected'
                  ? 'Start the nvHive service to see AI connections'
                  : 'Use setup to install a local model or connect optional cloud providers'}
              </div>
              <Link href="/setup" className="btn-secondary inline-flex mt-2 px-4 py-2 text-xs font-mono gap-2 items-center">
                FIX SETUP
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
                { href: '/', label: 'Chat Interface', icon: 'CHAT', color: '#76B900' },
                { href: '/providers', label: 'View Advisors', icon: 'AI', color: '#76B900' },
                { href: '/setup', label: 'Setup Wizard', icon: 'SETUP', color: '#d97706' },
                { href: '/settings', label: 'Settings', icon: 'PREF', color: '#a3a3a3' },
              ].map(({ href, label, icon, color }) => {
                const displayLabel =
                  href === '/' ? 'ASK A QUESTION' :
                  href === '/providers' ? 'AI CONNECTIONS' :
                  href === '/setup' ? 'FIX SETUP' :
                  href === '/settings' ? 'PREFERENCES' :
                  label.toUpperCase();
                const displayIcon =
                  href === '/' ? 'CHAT' :
                  href === '/providers' ? 'AI' :
                  href === '/setup' ? 'SETUP' :
                  href === '/settings' ? 'PREF' :
                  icon;
                return (
                <Link
                  key={href}
                  href={href}
                  className="flex items-center gap-3 px-3 py-2 hover:bg-[#f5f5f5] border border-transparent hover:border-[#76B900]/20 text-[#737373] hover:text-[#0a0a0a] transition-all text-xs font-mono dark:text-[#a3a3a3] dark:hover:bg-[#1f1f1f]"
                >
                  <span style={{ color }}>{displayIcon}</span>
                  {displayLabel}
                  <svg className="w-3 h-3 ml-auto text-[#333333] dark:text-[#525252]" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                    <path strokeLinecap="round" strokeLinejoin="round" d="M8.25 4.5l7.5 7.5-7.5 7.5" />
                  </svg>
                </Link>
                );
              })}
            </div>
          </div>
        </div>
      </div>

      {/* Footer */}
      <div className="border-t border-[#e5e5e5] pt-4 flex items-center justify-between dark:border-[#262626]">
        <div className="text-[10px] font-mono text-[#333333] dark:text-[#737373]">NVHIVE COMPUTER CHECK</div>
        <div className="text-[10px] font-mono text-[#333333] dark:text-[#737373]">POWERED BY NVIDIA LOCAL AI</div>
      </div>
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
  const displayIcon =
    label === 'AI Connections' ? 'AI' :
    label === 'Cache Hits' ? 'CACHE' :
    label === 'Cache Size' ? 'SIZE' :
    label === 'Service' ? 'SVC' :
    icon;
  const safeValue = value.includes('undefined') || value.includes('NaN')
    ? '-'
    : value;
  const safeSub = sub && (sub.includes('undefined') || sub.includes('NaN'))
    ? undefined
    : sub;

  return (
    <div className="card p-4">
      <div className="flex items-center justify-between mb-2">
        <span className="text-[10px] font-mono text-[#a3a3a3] uppercase tracking-wider dark:text-[#737373]">{label}</span>
        <span style={{ color }} className="text-[10px] font-mono font-bold">{displayIcon}</span>
      </div>
      {loading ? (
        <div className="h-6 w-16 bg-[#f5f5f5] animate-pulse dark:bg-[#141414]" />
      ) : (
        <div className="font-mono font-bold text-lg leading-none" style={{ color }}>
          {safeValue}
        </div>
      )}
      {safeSub && <div className="text-[10px] font-mono text-[#a3a3a3] mt-1 dark:text-[#737373]">{safeSub}</div>}
    </div>
  );
}
