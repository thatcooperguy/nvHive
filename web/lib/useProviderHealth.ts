'use client';

import { useEffect, useState } from 'react';
import { getProviders } from './api';
import type { ProviderHealth } from './types';

/**
 * Poll `/v1/advisors` on an interval so "online/offline" indicators
 * stay accurate throughout a session without the user refreshing.
 *
 * - Fetches once immediately on mount.
 * - Re-fetches every `intervalMs` (default 30s).
 * - Stops polling when the document is hidden and resumes when it's
 *   visible again, so background tabs don't hammer the API.
 * - On fetch failure, keeps the previous value rather than clearing
 *   it — a transient 500 shouldn't wipe out known-good health data.
 *
 * Returns the latest snapshot plus a `refresh` function callers can
 * invoke manually (e.g. after saving a new API key).
 */
export function useProviderHealth(intervalMs: number = 30_000): {
  providers: ProviderHealth[];
  loading: boolean;
  refresh: () => Promise<void>;
} {
  const [providers, setProviders] = useState<ProviderHealth[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | null = null;

    const fetchOnce = async () => {
      try {
        const data = await getProviders();
        if (!cancelled) {
          setProviders(data.providers);
          setLoading(false);
        }
      } catch {
        // Keep previous value on error — better a slightly stale
        // dropdown than an empty one on a transient network blip.
        if (!cancelled) setLoading(false);
      }
    };

    const schedule = () => {
      if (cancelled) return;
      if (typeof document !== 'undefined' && document.hidden) return;
      timer = setTimeout(async () => {
        await fetchOnce();
        schedule();
      }, intervalMs);
    };

    const onVisibility = () => {
      if (typeof document === 'undefined') return;
      if (document.hidden) {
        if (timer) clearTimeout(timer);
        timer = null;
      } else {
        // Fetch immediately on tab focus — the user just came back,
        // so their view should be fresh.
        fetchOnce().then(schedule);
      }
    };

    fetchOnce().then(schedule);
    if (typeof document !== 'undefined') {
      document.addEventListener('visibilitychange', onVisibility);
    }

    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
      if (typeof document !== 'undefined') {
        document.removeEventListener('visibilitychange', onVisibility);
      }
    };
  }, [intervalMs]);

  const refresh = async () => {
    try {
      const data = await getProviders();
      setProviders(data.providers);
    } catch {
      // ignore
    }
  };

  return { providers, loading, refresh };
}
