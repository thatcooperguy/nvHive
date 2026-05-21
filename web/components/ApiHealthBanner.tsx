'use client';

import { useEffect, useRef, useState } from 'react';

/**
 * Global banner that surfaces "the backend API isn't reachable."
 *
 * Why this exists: on first run on a cold cloud VM, the WebUI process can
 * come up before the API process finishes its imports. Every panel fetch
 * (providers, setup helper, GPU info, system check, …) silently fails and
 * the user sees an empty page with no clue that the *API*, not the UI, is
 * the missing piece. This banner names the gap.
 *
 * Boot-grace state machine (added 2026-05): the original banner went RED
 * after 3 failures, no matter what. On a fresh `nvh webui` boot the API
 * needs 5-15s to finish importing FastAPI + initializing the engine, so
 * the banner would scream "API offline" during a perfectly normal startup.
 * Now we distinguish "still booting" (amber, soft copy) from "was healthy,
 * went away" (red, alarming copy):
 *
 *   - `unknown`  — no probes have returned yet.
 *   - `starting` — 3+ failures BUT we're still inside the 20s boot-grace
 *                  window AND we have never seen a successful probe. Amber.
 *   - `ok`       — at least one successful probe. Any later failure burst
 *                  promotes us straight to `down`, never back to `starting`.
 *   - `down`     — 3+ consecutive failures AFTER an `ok`, OR 3+ failures
 *                  after the boot-grace window expired. Red.
 *
 * Escape hatch: a user can set `localStorage.nvh-banner-no-grace = "1"` to
 * disable the boot-grace window entirely and get the legacy red-on-failure
 * behavior. Useful when debugging "did the API actually crash on boot?".
 *
 * Behavior:
 *   - Polls `/v1/health` every 4s while down/starting, every 30s while ok.
 *   - First two failures are silent regardless of state.
 *   - Auto-dismisses when the API recovers.
 *   - User can dismiss for the rest of the session — but a dismissed banner
 *     comes back if the API goes down AGAIN.
 */

const API_BASE: string =
  typeof window !== 'undefined' && (window as unknown as { __HIVE_API_URL__?: string }).__HIVE_API_URL__
    ? (window as unknown as { __HIVE_API_URL__: string }).__HIVE_API_URL__
    : 'http://localhost:8000';

const BOOT_GRACE_MS = 20_000;

type Status = 'unknown' | 'starting' | 'ok' | 'down';

const readNoGraceFlag = (): boolean => {
  if (typeof window === 'undefined') return false;
  try {
    return window.localStorage.getItem('nvh-banner-no-grace') === '1';
  } catch {
    return false;
  }
};

export default function ApiHealthBanner() {
  const [status, setStatus] = useState<Status>('unknown');
  const [consecutiveFailures, setConsecutiveFailures] = useState(0);
  const [dismissed, setDismissed] = useState(false);
  const [retrying, setRetrying] = useState(false);

  // Captured once, in the mount effect. We deliberately do NOT reset this
  // on re-render so the "starting" window is anchored to component mount.
  // (Initialized to 0 here; set to Date.now() inside the effect to keep the
  // render pure — see react-hooks/purity.)
  const mountedAtRef = useRef<number>(0);
  // Sticky: once we've seen the API healthy, we never go back to "starting"
  // for the rest of this session — a later failure burst means the API
  // *was* up and went away, which is the real-problem case.
  const everOkRef = useRef<boolean>(false);
  // Read once on mount so flipping the flag mid-session doesn't surprise.
  // Initialized in the effect for the same purity reason as mountedAtRef.
  const noGraceRef = useRef<boolean>(false);

  useEffect(() => {
    mountedAtRef.current = Date.now();
    noGraceRef.current = readNoGraceFlag();
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
        everOkRef.current = true;
        setStatus('ok');
        setConsecutiveFailures(0);
      } else {
        const withinGrace =
          !noGraceRef.current &&
          !everOkRef.current &&
          Date.now() - mountedAtRef.current < BOOT_GRACE_MS;
        setStatus(withinGrace ? 'starting' : 'down');
        setConsecutiveFailures(n => n + 1);
      }
      // Poll fast while down/starting, slow while ok.
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
        everOkRef.current = true;
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
  if (status !== 'down' && status !== 'starting') return null;
  if (consecutiveFailures < 3) return null;
  if (dismissed) return null;

  const isStarting = status === 'starting';

  // Amber for "still booting" (informational), red for "was up, went away"
  // (alarming). Same layout, different palette + copy.
  const palette = isStarting
    ? {
        bg: '#78350f', // amber-900
        border: '#92400e', // amber-800
        text: '#fef3c7', // amber-100
        chipBg: '#92400e',
        chipLabel: 'Starting up…',
      }
    : {
        bg: '#7f1d1d', // red-900
        border: '#991b1b', // red-800
        text: '#fee2e2', // red-100
        chipBg: '#991b1b',
        chipLabel: 'API offline',
      };

  return (
    <div
      className="fixed left-0 right-0 z-[60] border-b"
      style={{
        top: '32px', // sits under the 32px top status bar
        background: palette.bg,
        borderColor: palette.border,
        color: palette.text,
      }}
      role="status"
      aria-live="polite"
    >
      <div className="mx-auto max-w-5xl px-4 py-2 flex flex-wrap items-center gap-3 text-[12px]">
        <span
          className="font-mono uppercase tracking-wider text-[10px] px-2 py-0.5"
          style={{ background: palette.chipBg }}
        >
          {palette.chipLabel}
        </span>
        {isStarting ? (
          <span className="flex-1 min-w-[200px]">
            Starting up… The nvHive backend at{' '}
            <code className="font-mono">{API_BASE}</code> is finishing its
            initial boot. This usually takes 5-15 seconds on a fresh install.
          </span>
        ) : (
          <>
            <span className="flex-1 min-w-[200px]">
              The nvHive backend at <code className="font-mono">{API_BASE}</code>{' '}
              isn&apos;t responding. The WebUI is up but every panel will be empty until
              the API recovers.
            </span>
            <span className="font-mono text-[11px] opacity-90">
              Run{' '}
              <code className="px-1.5 py-0.5" style={{ background: palette.chipBg }}>
                nvh serve
              </code>{' '}
              in a terminal, or check{' '}
              <code className="px-1.5 py-0.5" style={{ background: palette.chipBg }}>
                ~/.nvh/logs/api-server.log
              </code>{' '}
              for the error.
            </span>
          </>
        )}
        <button
          type="button"
          onClick={() => void retry()}
          disabled={retrying}
          className="font-mono uppercase tracking-wider text-[10px] px-3 py-1 disabled:opacity-50"
          style={{ background: palette.text, color: palette.bg }}
        >
          {retrying ? 'Retrying…' : 'Retry now'}
        </button>
        <button
          type="button"
          onClick={() => setDismissed(true)}
          aria-label="Dismiss this banner for now"
          className="font-mono uppercase tracking-wider text-[10px] border px-2 py-1"
          style={{ borderColor: `${palette.text}66` }}
        >
          Dismiss
        </button>
      </div>
    </div>
  );
}
