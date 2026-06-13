'use client';

import { usePathname } from 'next/navigation';
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

// Matches SystemConsole's PROBE_TIMEOUT_MS (and debug/report's probeService
// default). 2026-06-10 audit: this banner still aborted at 3000ms after PR
// #81 tuned the other two probe surfaces to 8000ms for cold cloud rigs
// (real-rig photo 2026-05-22: a healthy /v1/health took 4694ms during
// engine warmup). With the stale 3s abort the banner went red "API
// offline" while the SystemConsole chip — probing the same endpoint with
// 8s tolerance — showed green "API up". All probe surfaces must share the
// same tolerance.
const PROBE_TIMEOUT_MS = 8_000;

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
  // Chat (/) and setup (/setup*) have no 32px topbar (LayoutShell renders
  // them as self-contained surfaces), so the SystemConsole sits at top:0
  // there and we slot in right under its 24px collapsed bar. Mirror the
  // console's bareSurface test so the two stay glued together.
  const pathname = usePathname();
  const bareSurface = pathname === '/' || (pathname?.startsWith('/setup') ?? false);
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
        const t = setTimeout(() => ctl.abort(), PROBE_TIMEOUT_MS);
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
      // Same bounded timeout as the probe loop (2026-06-10 audit) — an
      // un-aborted fetch against a hung API left the button stuck on
      // "Retrying…" until the browser's own fetch timeout.
      const ctl = new AbortController();
      const t = setTimeout(() => ctl.abort(), PROBE_TIMEOUT_MS);
      const r = await fetch(`${API_BASE}/v1/health`, {
        signal: ctl.signal,
        credentials: 'omit',
        cache: 'no-store',
      });
      clearTimeout(t);
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

  // First-time-user audit 2026-05-22 (Agent C): the previous boot-grace
  // palette was amber. Students read banner COLOR, not text — amber +
  // chip-style copy "Starting up…" reads as "something's wrong" even
  // though the actual message says everything's fine. Switch to brand-
  // green during boot grace so the user sees "things are happening
  // and that's expected." Reserve amber/red for actual degraded /
  // down states (post-grace, where there IS something wrong).
  const palette = isStarting
    ? {
        bg: '#1f3a1f', // brand-green-tinted dark — nvHive accent #76B900
        border: '#3a5c1f', // slightly lighter green border
        text: '#d4f1c2', // pale green-tinted off-white
        chipBg: '#76B900', // nvHive brand green
        chipLabel: 'Warming up',
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
      // Layering contract with SystemConsole (2026-06-10 audit): the
      // banner renders at z-[60], BELOW the console's z-[65]. While the
      // console is collapsed there is no overlap (its 24px bar ends
      // exactly where we start), so the banner is fully visible; when
      // the user expands the console its 280px panel deliberately covers
      // us — the down-state copy below tells them to inspect the API tab,
      // so the tabs must win the stacking fight, not the banner.
      className="fixed left-0 right-0 z-[60] border-b"
      style={{
        // Sits under the SystemConsole's 24px collapsed bar: 32px topbar
        // + 24px console on shell pages; no topbar on chat/setup (see
        // bareSurface above).
        ...(bareSurface ? { top: '24px' } : { top: '56px' }),
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
            Warming up the AI engine — usually 5-15 seconds on a fresh install.
            The dashboard will start showing data the moment it&apos;s ready.
          </span>
        ) : (
          <>
            <span className="flex-1 min-w-[200px]">
              The nvHive backend at <code className="font-mono">{API_BASE}</code>{' '}
              isn&apos;t responding. The WebUI is up but every panel will be empty until
              the API recovers.
            </span>
            <span className="font-mono text-[11px] opacity-90">
              Use{' '}
              <code className="px-1.5 py-0.5" style={{ background: palette.chipBg }}>
                Restart API
              </code>{' '}
              in the System Console above, or inspect the{' '}
              <code className="px-1.5 py-0.5" style={{ background: palette.chipBg }}>
                API
              </code>{' '}
              tab for the underlying error.
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
