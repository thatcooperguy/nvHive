'use client';

/**
 * HardwareWidget — live GPU + persistent storage status, shown prominently.
 *
 * Two variants:
 *  - `compact`: one-line status pill for the sidebar foot / top bar.
 *  - `hero`: full card for landing/empty states — name, VRAM bar, utilization
 *     gauge, persistent storage path + free space.
 *
 * Polls /v1/system/gpu and /v1/system/storage every 5s by default. Designed so
 * an ephemeral cloud Linux GPU session feels real and concrete — the user sees
 * "yes, I have a GPU, and my workspace is on persistent storage" at a glance.
 */

import { useEffect, useState } from 'react';
import { getGPUInfo, getStorageStatus } from '@/lib/api';
import type { GPUDevice, StorageStatus } from '@/lib/types';

interface HardwareSnapshot {
  gpu: GPUDevice | null;
  storage: StorageStatus | null;
  detection_summary: string;
  gpu_detected: boolean;
}

function shortGpuName(name: string): string {
  return name
    .replace(/^NVIDIA GeForce /, '')
    .replace(/^NVIDIA /, '')
    .replace(/ Laptop GPU$/, '')
    .trim() || 'GPU';
}

function utilColor(pct: number): string {
  if (pct >= 90) return '#dc2626';
  if (pct >= 70) return '#d97706';
  return '#76B900';
}

function gbDisplay(gb: number | null | undefined): string {
  if (gb === null || gb === undefined) return '—';
  if (gb >= 100) return `${Math.round(gb)} GB`;
  if (gb >= 10) return `${gb.toFixed(0)} GB`;
  return `${gb.toFixed(1)} GB`;
}

function useHardwareSnapshot(refreshMs = 5000): HardwareSnapshot | null {
  const [snap, setSnap] = useState<HardwareSnapshot | null>(null);

  useEffect(() => {
    let cancelled = false;

    const tick = async () => {
      try {
        const [gpuInfo, storage] = await Promise.all([
          getGPUInfo().catch(() => null),
          getStorageStatus().catch(() => null),
        ]);
        if (cancelled) return;
        const gpu = gpuInfo && gpuInfo.gpus.length > 0 ? gpuInfo.gpus[0] : null;
        setSnap({
          gpu,
          storage: storage ?? null,
          detection_summary: gpuInfo?.summary ?? '',
          gpu_detected: !!gpu,
        });
      } catch {
        // Swallow — the widget should gracefully no-op when offline.
      }
    };

    tick();
    const interval = setInterval(tick, refreshMs);
    return () => {
      cancelled = true;
      clearInterval(interval);
    };
  }, [refreshMs]);

  return snap;
}

// ─── Compact variant — for sidebars / top bars ───────────────────────────────

export function HardwareWidgetCompact() {
  const snap = useHardwareSnapshot(5000);

  if (!snap) {
    // Reserve space so the layout doesn't reflow when data lands.
    return <div className="h-5 w-32" aria-hidden />;
  }

  if (!snap.gpu_detected) {
    return (
      <div
        className="flex items-center gap-1.5 rounded-md border px-2 py-0.5 text-[10px] font-mono"
        style={{
          background: 'var(--bg-card)',
          borderColor: 'var(--border)',
          color: 'var(--text-muted)',
        }}
        title={snap.detection_summary || 'No NVIDIA GPU detected on this session.'}
      >
        <span className="h-1.5 w-1.5 rounded-full bg-[#737373]" />
        <span>CPU only</span>
      </div>
    );
  }

  const gpu = snap.gpu!;
  const pct = Math.round(gpu.utilization_pct);
  const color = utilColor(pct);
  const usedGb = Math.round(gpu.memory_used_mb / 1024 * 10) / 10;
  const totalGb = gpu.vram_gb;

  return (
    <div
      className="flex items-center gap-2 rounded-md border px-2 py-0.5 text-[10px] font-mono"
      style={{
        background: 'var(--bg-card)',
        borderColor: 'var(--border)',
      }}
      title={`${gpu.name} — ${pct}% util, ${usedGb} / ${totalGb} GB VRAM`}
    >
      <span className="nvidia-pulse h-1.5 w-1.5 flex-shrink-0 rounded-full" style={{ background: color }} />
      <span style={{ color: 'var(--text-secondary)' }}>{shortGpuName(gpu.name)}</span>
      <span style={{ color }}>{pct}%</span>
      <span style={{ color: 'var(--text-muted)' }}>
        {usedGb}/{totalGb} GB
      </span>
    </div>
  );
}

// ─── Hero variant — for landing pages / empty states ─────────────────────────

export function HardwareWidgetHero() {
  const snap = useHardwareSnapshot(5000);

  if (!snap) {
    return (
      <div
        className="rounded-lg border p-4"
        style={{ background: 'var(--bg-card)', borderColor: 'var(--border)' }}
      >
        <div className="h-3 w-32 animate-pulse rounded" style={{ background: 'var(--bg-subtle)' }} />
        <div className="mt-2 h-6 w-48 animate-pulse rounded" style={{ background: 'var(--bg-subtle)' }} />
      </div>
    );
  }

  return (
    <div
      className="rounded-lg border p-4"
      style={{ background: 'var(--bg-card)', borderColor: 'var(--border)' }}
    >
      <div className="flex items-center justify-between gap-2">
        <div className="flex items-center gap-2 text-[10px] font-mono uppercase tracking-[0.16em]" style={{ color: 'var(--text-muted)' }}>
          {snap.gpu_detected && (
            <span className="nvidia-pulse h-2 w-2 flex-shrink-0 rounded-full" style={{ background: utilColor(snap.gpu?.utilization_pct ?? 0) }} />
          )}
          <span>Your hardware</span>
        </div>
        {snap.gpu?.architecture && (
          <span className="text-[9px] font-mono uppercase tracking-[0.14em]" style={{ color: 'var(--text-faint)' }}>
            {snap.gpu.architecture}
          </span>
        )}
      </div>

      {snap.gpu_detected ? (
        <>
          <div className="mt-2 text-lg font-semibold tracking-tight" style={{ color: 'var(--text-primary)' }}>
            {shortGpuName(snap.gpu!.name)}
          </div>
          <div className="mt-3 grid grid-cols-2 gap-3">
            <UtilizationGauge value={snap.gpu!.utilization_pct} />
            <VramBar used={snap.gpu!.memory_used_mb / 1024} total={snap.gpu!.vram_gb} />
          </div>
        </>
      ) : (
        <div className="mt-2 text-sm leading-snug" style={{ color: 'var(--text-secondary)' }}>
          No NVIDIA GPU detected on this session — you can still chat, route to cloud
          providers, and run a CPU-only local model. Re-attach a GPU instance to unlock
          full local AI.
        </div>
      )}

      {snap.storage && (
        <div className="mt-4 border-t pt-3" style={{ borderColor: 'var(--border-subtle)' }}>
          <div className="flex items-baseline justify-between gap-2 text-[10px] font-mono">
            <span className="uppercase tracking-[0.14em]" style={{ color: 'var(--text-muted)' }}>
              Persistent workspace
            </span>
            <span style={{ color: 'var(--text-faint)' }} title={snap.storage.layout.home}>
              {snap.storage.exists ? 'OK' : 'Not initialized'}
            </span>
          </div>
          <div className="mt-1 truncate text-xs" style={{ color: 'var(--text-secondary)' }} title={snap.storage.layout.home}>
            {snap.storage.layout.home || '—'}
          </div>
          <div className="mt-1 flex items-center gap-2 text-[10px] font-mono" style={{ color: 'var(--text-muted)' }}>
            <span>
              {gbDisplay(snap.storage.free_gb)} free of {gbDisplay(snap.storage.total_gb)}
            </span>
            {!snap.storage.ok && snap.storage.warnings.length > 0 && (
              <span style={{ color: '#d97706' }} title={snap.storage.warnings.join('\n')}>
                ⚠ {snap.storage.warnings.length} warning{snap.storage.warnings.length > 1 ? 's' : ''}
              </span>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

// ─── Subcomponents ───────────────────────────────────────────────────────────

function UtilizationGauge({ value }: { value: number }) {
  const pct = Math.max(0, Math.min(100, Math.round(value)));
  const color = utilColor(pct);
  return (
    <div>
      <div className="text-[10px] font-mono uppercase tracking-[0.14em]" style={{ color: 'var(--text-muted)' }}>
        Utilization
      </div>
      <div className="mt-1 flex items-baseline gap-1.5">
        <span className="text-xl font-semibold tabular-nums" style={{ color }}>
          {pct}
        </span>
        <span className="text-xs font-mono" style={{ color: 'var(--text-muted)' }}>%</span>
      </div>
      <div className="mt-1 h-1 overflow-hidden rounded-full" style={{ background: 'var(--bg-subtle)' }}>
        <div
          className="h-full transition-all duration-500"
          style={{ width: `${pct}%`, background: color }}
        />
      </div>
    </div>
  );
}

function VramBar({ used, total }: { used: number; total: number }) {
  const usedRounded = Math.round(used * 10) / 10;
  const totalRounded = Math.round(total * 10) / 10;
  const pct = total > 0 ? Math.max(0, Math.min(100, (used / total) * 100)) : 0;
  const color = utilColor(pct);
  return (
    <div>
      <div className="text-[10px] font-mono uppercase tracking-[0.14em]" style={{ color: 'var(--text-muted)' }}>
        VRAM
      </div>
      <div className="mt-1 flex items-baseline gap-1.5">
        <span className="text-xl font-semibold tabular-nums" style={{ color: 'var(--text-primary)' }}>
          {usedRounded}
        </span>
        <span className="text-xs font-mono" style={{ color: 'var(--text-muted)' }}>
          / {totalRounded} GB
        </span>
      </div>
      <div className="mt-1 h-1 overflow-hidden rounded-full" style={{ background: 'var(--bg-subtle)' }}>
        <div
          className="h-full transition-all duration-500"
          style={{ width: `${pct}%`, background: color }}
        />
      </div>
    </div>
  );
}

// Default export for convenience — hero shape.
export default HardwareWidgetHero;
