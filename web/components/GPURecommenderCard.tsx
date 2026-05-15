'use client';

/**
 * GPURecommenderCard — turns the Hardware widget's bare VRAM number into a
 * concrete next action ("Install qwen2.5:14b — fits in your 24GB").
 *
 * Reads /v1/system/recommendations on mount, picks the top non-installed
 * recommendation, and renders a tight CTA. The user clicks Install, which
 * fires the existing studio-pack install endpoint. We don't poll; this is
 * meant as a first-5-minute nudge, not a live monitor.
 */

import { useEffect, useState } from 'react';
import { getRecommendations } from '@/lib/api';
import type { ModelRecommendation } from '@/lib/types';

interface Props {
  /** Optional callback when the user clicks the install CTA. The Setup page
   * already owns the actual install flow; this callback just hands the model
   * name back so the host page can route or trigger. */
  onInstall?: (model: string) => void;
}

export default function GPURecommenderCard({ onInstall }: Props) {
  const [top, setTop] = useState<ModelRecommendation | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const data = await getRecommendations();
        if (cancelled) return;
        const pick = (data.recommendations ?? []).find(r => r.tier !== 'incompatible') ?? null;
        setTop(pick);
      } catch {
        // GPU not detected, no recommendation available — render nothing.
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  if (loading || !top) return null;

  return (
    <div
      className="mt-3 rounded-md border p-3"
      style={{
        background: 'var(--bg-card)',
        borderColor: 'var(--border)',
      }}
    >
      <div className="flex items-baseline justify-between gap-2">
        <div>
          <div
            className="text-[10px] font-mono uppercase tracking-[0.18em]"
            style={{ color: '#76B900' }}
          >
            Recommended for your GPU
          </div>
          <div className="mt-1 font-mono text-sm" style={{ color: 'var(--text-primary)' }}>
            {top.model}
          </div>
          <div className="mt-0.5 text-xs" style={{ color: 'var(--text-secondary)' }}>
            {top.reason} · ~{top.vram_required_gb.toFixed(1)} GB VRAM · tier {top.tier}
          </div>
        </div>
        <button
          type="button"
          onClick={() => onInstall?.(top.model)}
          className="btn-primary px-3 py-1 text-[10px] font-mono"
        >
          Install
        </button>
      </div>
    </div>
  );
}
