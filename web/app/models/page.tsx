'use client';

/**
 * Model Manager — the in-app model browser/downloader (roadmap critical).
 *
 * Unifies backend surfaces that already existed but had no home:
 *   - GET /v1/setup/model-fit  → fit-ranked catalog for the detected GPU
 *   - GET /v1/ollama/models    → installed models with on-disk sizes
 *   - POST /v1/ollama/pull     → SSE download progress
 *   - DELETE /v1/ollama/models → reclaim disk
 *
 * The wedge: nvHive knows the rig's VRAM, so every catalog row shows a
 * "fits your GPU" verdict + disk estimate before you download — the
 * LM Studio / Jan experience, but on the rented GPU desktop nvHive
 * provisioned rootlessly.
 */

import { useCallback, useEffect, useState } from 'react';
import PageHeader from '@/components/PageHeader';
import {
  deleteModel,
  getInstalledModels,
  getModelFit,
  pullModelStream,
  type InstalledModel,
  type ModelFitEntry,
} from '@/lib/api';

function fmtDisk(gb: number | null | undefined): string {
  if (gb === null || gb === undefined) return '—';
  return gb >= 1 ? `${gb.toFixed(1)} GB` : `${Math.round(gb * 1024)} MB`;
}

interface PullState {
  percent: number | null;
  status: string;
  error: string | null;
  abort: () => void;
}

export default function ModelsPage() {
  const [catalog, setCatalog] = useState<ModelFitEntry[]>([]);
  const [installed, setInstalled] = useState<InstalledModel[]>([]);
  const [vram, setVram] = useState<number | null>(null);
  const [freeGb, setFreeGb] = useState<number | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [pulls, setPulls] = useState<Record<string, PullState>>({});
  const [busy, setBusy] = useState<Record<string, boolean>>({});

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const [fit, inst] = await Promise.all([getModelFit(), getInstalledModels().catch(() => ({ models: [], count: 0 }))]);
      const models = fit.models ?? fit.ranked ?? [];
      setCatalog(models);
      setVram(fit.detected_vram_gb ?? fit.vram_gb ?? null);
      setFreeGb(fit.free_gb ?? null);
      setInstalled(inst.models);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const startPull = useCallback((name: string) => {
    const abort = pullModelStream(name, {
      onProgress: (p) =>
        setPulls(prev => ({
          ...prev,
          [name]: { percent: p.percent ?? null, status: p.status, error: null, abort: prev[name]?.abort ?? (() => {}) },
        })),
      onComplete: () => {
        setPulls(prev => {
          const next = { ...prev };
          delete next[name];
          return next;
        });
        void load();
      },
      onError: (message) =>
        setPulls(prev => ({
          ...prev,
          [name]: { percent: prev[name]?.percent ?? null, status: 'error', error: message, abort: prev[name]?.abort ?? (() => {}) },
        })),
    });
    setPulls(prev => ({ ...prev, [name]: { percent: null, status: 'starting', error: null, abort } }));
  }, [load]);

  const cancelPull = useCallback((name: string) => {
    setPulls(prev => {
      prev[name]?.abort();
      const next = { ...prev };
      delete next[name];
      return next;
    });
  }, []);

  const remove = useCallback(async (name: string) => {
    if (!window.confirm(`Delete ${name} and reclaim its disk?`)) return;
    setBusy(prev => ({ ...prev, [name]: true }));
    try {
      await deleteModel(name);
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(prev => ({ ...prev, [name]: false }));
    }
  }, [load]);

  const installedNames = new Set(installed.map(m => m.name.replace(/:latest$/, '')));
  const notInstalled = catalog.filter(
    m => !m.installed && !installedNames.has((m.install_target ?? '').replace(/:latest$/, '')),
  );

  const subtitle = loading
    ? 'Loading catalog…'
    : `Detected GPU: ${vram ? `${vram} GB VRAM` : 'unknown'}${freeGb != null ? ` · ${freeGb.toFixed(0)} GB free disk` : ''}`;

  return (
    <div>
      <PageHeader
        eyebrow="Local AI"
        title="Model Manager"
        subtitle={subtitle}
        trailing={
          <button
            type="button"
            onClick={() => void load()}
            className="rounded border border-[#e5e5e5] px-3 py-2 text-xs font-mono text-[#525252] hover:text-[#0a0a0a] dark:border-[#262626] dark:text-[#a3a3a3] dark:hover:text-[#fafafa]"
          >
            Refresh
          </button>
        }
      />
      <div className="mx-auto max-w-5xl space-y-8 p-6">
        <p className="text-xs leading-relaxed text-[#525252] dark:text-[#a3a3a3]">
          Browse and install local models. Each catalog row shows whether it fits your detected
          VRAM and how much disk it needs before you download. Models live in Ollama under{' '}
          <code className="rounded bg-[#f5f5f5] px-1 py-0.5 font-mono text-[10px] text-[#737373] dark:bg-[#141414] dark:text-[#a3a3a3]">$NVH_HOME/models</code>,
          so they survive reconnects. CLI: <code className="font-mono">nvh models list --all</code>.
        </p>

        {error && (
          <div className="rounded border border-[#dc2626] bg-[rgba(220,38,38,0.08)] px-3 py-2 text-xs font-mono text-[#dc2626]">
            {error}
          </div>
        )}

        {/* Installed */}
        <section>
          <h2 className="mb-3 flex items-baseline gap-2 font-mono text-xs font-bold uppercase tracking-wider text-[#525252] dark:text-[#a3a3a3]">
            Installed
            <span className="text-[10px] font-normal text-[#a3a3a3] dark:text-[#737373]">{installed.length}</span>
          </h2>
          {installed.length === 0 ? (
            <p className="text-xs font-mono text-[#a3a3a3] dark:text-[#737373]">
              No local models yet — install one from the catalog below.
            </p>
          ) : (
            <div className="space-y-2">
              {installed.map(m => (
                <div key={m.name} className="flex items-center gap-3 rounded border border-[#e5e5e5] bg-white px-4 py-3 text-sm dark:border-[#262626] dark:bg-[#0a0a0a]">
                  <span className="h-2 w-2 rotate-45 bg-[#76B900]" aria-hidden />
                  <span className="font-mono font-bold text-[#0a0a0a] dark:text-[#fafafa]">{m.name}</span>
                  <span className="font-mono text-xs text-[#737373] dark:text-[#a3a3a3]">{fmtDisk(m.size_gb)}</span>
                  <button
                    type="button"
                    onClick={() => void remove(m.name)}
                    disabled={busy[m.name]}
                    className="ml-auto rounded border border-[#e5e5e5] px-2 py-1 text-[10px] font-mono uppercase tracking-wider text-[#737373] hover:border-[#dc2626] hover:text-[#dc2626] disabled:opacity-40 dark:border-[#262626] dark:text-[#a3a3a3]"
                  >
                    {busy[m.name] ? 'Removing…' : 'Remove'}
                  </button>
                </div>
              ))}
            </div>
          )}
        </section>

        {/* Catalog */}
        <section>
          <h2 className="mb-3 flex items-baseline gap-2 font-mono text-xs font-bold uppercase tracking-wider text-[#525252] dark:text-[#a3a3a3]">
            Catalog
            <span className="text-[10px] font-normal text-[#a3a3a3] dark:text-[#737373]">{notInstalled.length}</span>
          </h2>
          {loading ? (
            <div className="space-y-2">
              {[0, 1, 2, 3].map(i => (
                <div key={i} className="h-16 animate-pulse rounded border border-[#e5e5e5] bg-[#fafafa] dark:border-[#262626] dark:bg-[#141414]" />
              ))}
            </div>
          ) : (
            <div className="space-y-2">
              {notInstalled.map(m => {
                const pull = pulls[m.install_target];
                const fits = m.fits_vram !== false;
                return (
                  <div key={m.id} className="rounded border border-[#e5e5e5] bg-white px-4 py-3 dark:border-[#262626] dark:bg-[#0a0a0a]">
                    <div className="flex items-center gap-3">
                      <span className="font-mono text-sm font-bold text-[#0a0a0a] dark:text-[#fafafa]">{m.title}</span>
                      <span className="font-mono text-[10px] text-[#a3a3a3] dark:text-[#737373]">{m.install_target}</span>
                      {m.use_case_label && (
                        <span className="text-[10px] font-mono uppercase tracking-wider text-[#a3a3a3] dark:text-[#737373]">{m.use_case_label}</span>
                      )}
                      {m.recommended && (
                        <span className="rounded-sm bg-[#76B900]/10 px-1.5 py-0.5 text-[9px] font-mono uppercase tracking-wider text-[#5a9100]">recommended</span>
                      )}
                      <span
                        className={`rounded-sm px-1.5 py-0.5 text-[9px] font-mono uppercase tracking-wider ${
                          fits
                            ? 'bg-[#76B900]/10 text-[#5a9100]'
                            : 'bg-[#d97706]/10 text-[#b45309] dark:text-[#f59e0b]'
                        }`}
                        title={fits ? 'Fits your detected VRAM' : 'May exceed your detected VRAM'}
                      >
                        {fits ? 'fits GPU' : 'tight fit'}
                      </span>
                      <span className="font-mono text-xs text-[#737373] dark:text-[#a3a3a3]">{fmtDisk(m.estimated_disk_gb)}</span>
                      <div className="ml-auto">
                        {pull ? (
                          <button
                            type="button"
                            onClick={() => cancelPull(m.install_target)}
                            className="rounded border border-[#e5e5e5] px-2 py-1 text-[10px] font-mono uppercase tracking-wider text-[#737373] hover:border-[#dc2626] hover:text-[#dc2626] dark:border-[#262626] dark:text-[#a3a3a3]"
                          >
                            Cancel
                          </button>
                        ) : (
                          <button
                            type="button"
                            onClick={() => startPull(m.install_target)}
                            className="rounded bg-[#76B900] px-3 py-1.5 text-[10px] font-mono font-bold uppercase tracking-wider text-black hover:bg-[#5a9100]"
                          >
                            Install
                          </button>
                        )}
                      </div>
                    </div>
                    {pull && (
                      <div className="mt-2">
                        {pull.error ? (
                          <p className="font-mono text-[10px] text-[#dc2626]">{pull.error}</p>
                        ) : (
                          <>
                            <div className="h-1.5 w-full overflow-hidden rounded-full bg-[#e5e5e5] dark:bg-[#262626]">
                              <div
                                className="h-full bg-[#76B900] transition-all"
                                style={{ width: `${pull.percent ?? 5}%` }}
                              />
                            </div>
                            <p className="mt-1 font-mono text-[10px] text-[#737373] dark:text-[#a3a3a3]">
                              {pull.percent != null ? `${pull.percent}%` : pull.status}
                            </p>
                          </>
                        )}
                      </div>
                    )}
                  </div>
                );
              })}
            </div>
          )}
        </section>
      </div>
    </div>
  );
}
