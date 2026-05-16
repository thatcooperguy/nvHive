'use client';

import { useEffect, useState } from 'react';

/**
 * Global banner that surfaces "the backend API isn't reachable."
 *
 * Why this exists: on first run on a cold cloud VM, the WebUI process can
 * come up before the API process finishes its imports. Every panel fetch
 * (providers, setup helper, GPU info, system check, …) silently fails and
 * the user sees an empty page with no clue that the *API*, not the UI, is
 * the missing piece. This banner names the gap.
 *
 * Behavior:
 *   - Polls `/v1/health` every 4s while down, every 30s while ok (light).
 *   - First two failures are silent (gives the API time to finish booting).
 *   - After three consecutive failures, renders a sticky top banner with
 *     concrete next steps (run `nvh serve`, check the log path).
 *   - Auto-dismisses when the API recovers.
 *   - User can dismiss for the rest of the session — but a dismissed banner
 *     comes back if the API goes down AGAIN.
 */

const API_BASE: string =
  typeof window !== 'undefined' && (window as unknown as { __HIVE_API_URL__?: string }).__HIVE_API_URL__
    ? (window as unknown as { __HIVE_API_URL__: string }).__HIVE_API_URL__
    : 'http://localhost:8000';

type Status = 'unknown' | 'ok' | 'down';

export default function ApiHealthBanner() {
  const [status, setStatus] = useState<Status>('unknown');
  const [consecutiveFailures, setConsecutiveFailures] = useState(0);
  const [dismissed, setDismissed] = useState(false);
  const [retrying, setRetrying] = useState(false);

  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | null = null;

    const probe = async (): Promise<boolean> => {
      try {
        const ctl = new AbortController();
        const t = setTimeout(() => ctl.abort(), 3000);
        const r = await fetch(`${API_BASE}/v1/health`, {
          signal: ctl.signal,
          // Don't send cookies — health is anon, and CORS preflights are wasteful.
          credentials: 'omit',
          cache: 'no-store',
        });
        clearTimeout(t);
        return r.ok;
      } catch {
        return false;
      }
    };

    const tick = async () => {
      const ok = await probe();
      if (cancelled) return;
      if (ok) {
        setStatus('ok');
        setConsecutiveFailures(0);
      } else {
        setStatus('down');
        setConsecutiveFailures(n => n + 1);
      }
      // Poll fast while down, slow while ok.
      const next = ok ? 30_000 : 4_000;
      timer = setTimeout(tick, next);
    };
    void tick();

    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
  }, []);

  // Reset the dismissed flag when the API recovers, so a *future* outage
  // brings the banner back rather than staying hidden forever.
  useEffect(() => {
    if (status === 'ok' && dismissed) {
      setDismissed(false);
    }
  }, [status, dismissed]);

  const retry = async () => {
    setRetrying(true);
    try {
      const r = await fetch(`${API_BASE}/v1/health`, {
        credentials: 'omit',
        cache: 'no-store',
      });
      if (r.ok) {
        setStatus('ok');
        setConsecutiveFailures(0);
        setDismissed(false);
      }
    } catch {
      // leave status as-is; tick will keep polling
    } finally {
      setRetrying(false);
    }
  };

  // Render nothing on the happy path or while we're still figuring out
  // whether the API is just slow to wake up.
  if (status !== 'down') return null;
  if (consecutiveFailures < 3) return null;
  if (dismissed) return null;

  return (
    <div
      className="fixed left-0 right-0 z-[60] border-b"
      style={{
        top: '32px', // sits under the 32px top status bar
        background: '#7f1d1d',
        borderColor: '#991b1b',
        color: '#fee2e2',
      }}
      role="status"
      aria-live="polite"
    >
      <div className="mx-auto max-w-5xl px-4 py-2 flex flex-wrap items-center gap-3 text-[12px]">
        <span className="font-mono uppercase tracking-wider text-[10px] bg-[#991b1b] px-2 py-0.5">
          API offline
        </span>
        <span className="flex-1 min-w-[200px]">
          The nvHive backend at <code className="font-mono">{API_BASE}</code>{' '}
          isn&apos;t responding. The WebUI is up but every panel will be empty until
          the API recovers.
        </span>
        <span className="font-mono text-[11px] opacity-90">
          Run <code className="bg-[#991b1b] px-1.5 py-0.5">nvh serve</code> in a
          terminal, or check{' '}
          <code className="bg-[#991b1b] px-1.5 py-0.5">~/.nvh/logs/api-server.log</code>{' '}
          for the error.
        </span>
        <button
          type="button"
          onClick={() => void retry()}
          disabled={retrying}
          className="font-mono uppercase tracking-wider text-[10px] bg-[#fee2e2] text-[#7f1d1d] px-3 py-1 disabled:opacity-50"
        >
          {retrying ? 'Retrying…' : 'Retry now'}
        </button>
        <button
          type="button"
          onClick={() => setDismissed(true)}
          aria-label="Dismiss this banner for now"
          className="font-mono uppercase tracking-wider text-[10px] border border-[#fee2e2]/40 px-2 py-1 hover:bg-[#991b1b]"
        >
          Dismiss
        </button>
      </div>
    </div>
  );
}
