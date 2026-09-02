'use client';

import { useState, useEffect } from 'react';
import PageHeader from '@/components/PageHeader';
import ThemeToggle from '@/components/ThemeToggle';
import { getCacheStats, clearCache } from '@/lib/api';
import { LEGACY_CHATS_KEY } from '@/lib/importLocalChats';
import { NVHIVE_VERSION } from '@/lib/version';
import type { CacheStats } from '@/lib/types';

// Browser-only keys this page is allowed to clear. Chat history lives on the
// API server and is managed from the sidebar; model defaults and budget
// limits live in config.yaml.
const BROWSER_KEYS = [
  'council_settings',        // pre-0.42 write-only preferences
  'council_recent_queries',  // pre-0.42 /query history
  LEGACY_CHATS_KEY,          // pre-0.42 chat store (normally already imported)
  'hive_cmd_palette_recents',
];

function SectionCard({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="card p-5 space-y-4 nvidia-corner relative">
      <h2 className="font-mono font-bold text-[#0a0a0a] text-xs uppercase tracking-widest border-b border-[#e5e5e5] pb-3 flex items-center gap-2 dark:border-[#262626] dark:text-[#fafafa]">
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
        <div className="text-sm font-mono text-[#525252] dark:text-[#a3a3a3]">{label}</div>
        {description && <div className="text-[10px] font-mono text-[#a3a3a3] mt-0.5 dark:text-[#737373]">{description}</div>}
      </div>
      <div className="flex-shrink-0">{children}</div>
    </div>
  );
}

export default function SettingsPage() {
  const [cacheStats, setCacheStats] = useState<CacheStats | null>(null);
  const [cacheClearing, setCacheClearing] = useState(false);
  const [cacheCleared, setCacheCleared] = useState<number | null>(null);
  const [browserCleared, setBrowserCleared] = useState(false);

  useEffect(() => {
    getCacheStats().then(setCacheStats).catch(() => {});
  }, []);

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

  const handleClearBrowserData = () => {
    try {
      for (const key of BROWSER_KEYS) localStorage.removeItem(key);
    } catch {
      // Storage may be blocked in hardened browser profiles.
    }
    setBrowserCleared(true);
    setTimeout(() => setBrowserCleared(false), 3000);
  };

  return (
    <div>
      <PageHeader
        eyebrow="Workspace"
        title="Preferences"
        subtitle="Theme, response cache, and browser data. Model defaults and budget limits live in config.yaml (nvh config)."
      />
      <div className="p-6 space-y-6 max-w-3xl mx-auto">

      <SectionCard title="Theme">
        <SettingRow label="Appearance" description="Light, dark, or follow the system setting. Applies immediately on every page.">
          <ThemeToggle />
        </SettingRow>
      </SectionCard>

      <SectionCard title="Cache">
        {cacheStats && (
          <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 mb-2">
            {[
              { label: 'Hits', value: `${cacheStats.hits}`, color: '#76B900' },
              { label: 'Misses', value: `${cacheStats.misses}`, color: '#dc2626' },
              { label: 'Size', value: `${cacheStats.size}/${cacheStats.max_size}`, color: '#525252' },
              { label: 'Hit Rate', value: `${(cacheStats.hit_rate * 100).toFixed(0)}%`, color: '#76B900' },
            ].map(({ label, value, color }) => (
              <div key={label} className="bg-[#ffffff] border border-[#e5e5e5] p-3 text-center dark:bg-[#0a0a0a] dark:border-[#262626]">
                <div className="font-mono font-bold text-sm" style={{ color }}>{value}</div>
                <div className="text-[10px] font-mono text-[#a3a3a3] mt-0.5 uppercase dark:text-[#737373]">{label}</div>
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

      <SectionCard title="Data">
        <div>
          <div className="text-sm font-mono text-[#525252] mb-1 dark:text-[#a3a3a3]">Chat history</div>
          <div className="text-[10px] font-mono text-[#a3a3a3] dark:text-[#737373]">
            Conversations are stored by the API server ($NVH_HOME/state/nvhive.db).
            Rename, pin, export, or delete them from the sidebar on any page.
          </div>
        </div>
        <div>
          <div className="text-sm font-mono text-[#525252] mb-1 dark:text-[#a3a3a3]">Clear browser data</div>
          <div className="text-[10px] font-mono text-[#a3a3a3] mb-3 dark:text-[#737373]">
            Removes command-palette recents and any preferences left behind by
            versions before 0.42. Server configuration is not affected.
          </div>
          <button
            onClick={handleClearBrowserData}
            className={`px-4 py-2 text-xs font-mono uppercase tracking-wider border transition-all ${
              browserCleared
                ? 'border-[#76B900]/40 bg-[#76B900]/10 text-[#76B900]'
                : 'border-[#dc2626]/30 text-[#dc2626] hover:bg-[#dc2626]/10'
            }`}
          >
            {browserCleared ? 'Cleared' : 'Clear Browser Data'}
          </button>
        </div>
      </SectionCard>

      {/* Version info */}
      <div className="text-center py-4 border-t border-[#e5e5e5] dark:border-[#262626]">
        <div className="text-[10px] font-mono text-[#404040] dark:text-[#d4d4d4]">NVHIVE - v{NVHIVE_VERSION}</div>
        <div className="text-[10px] font-mono text-[#a3a3a3] mt-1 dark:text-[#737373]">NVIDIA Nemotron - Next.js - Tailwind CSS</div>
      </div>
      </div>
    </div>
  );
}
