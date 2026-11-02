'use client';

/**
 * SessionCostPill — the top-bar "Session 4h 12m · desktop ≈ $3.37 · cloud
 * $0.42" indicator (0.46 design §7). It grew out of SessionAgePill, keeps its
 * `nvh_session_started_at` sessionStorage anchor (reloads on the same tab keep
 * counting; a new tab starts fresh) and adds what cloud models cost since the
 * tab opened — the machine-wide ledger from `GET /v1/budget/status?since=` —
 * plus, on a metered desktop or whenever `budget.hourly_rate_usd` is set, the
 * desktop term `rate × tab age`.
 *
 * The decision itself (text, title, tone) is lib/sessionCost.ts, pure and
 * tested; this file only keeps time, polls and renders. Mounted on `/` in the
 * chat page's own bar and, through components/SessionAgePill.tsx (a
 * re-export), in LayoutShell's bar on every other page.
 *
 * Read-only: a <span>, never a button, so it cannot take keyboard focus. The
 * title carries the explanation. Amber only for the `warn` verdict (cloud per
 * hour above the desktop rate after fifteen minutes); muted otherwise. While
 * the API is unreachable the pill degrades to the plain age.
 */

import { useEffect, useState } from 'react';
import { getBudgetStatus } from '@/lib/api';
import { SESSION_START_KEY, sessionCostFromStatus } from '@/lib/sessionCost';
import type { BudgetStatus } from '@/lib/types';

/** The tab's start (epoch ms) from sessionStorage, written on first read. */
export function readOrInitStart(): number {
  if (typeof window === 'undefined') return Date.now();
  try {
    const stored = window.sessionStorage.getItem(SESSION_START_KEY);
    if (stored) {
      const parsed = Number.parseInt(stored, 10);
      if (Number.isFinite(parsed) && parsed > 0) return parsed;
    }
    const now = Date.now();
    window.sessionStorage.setItem(SESSION_START_KEY, String(now));
    return now;
  } catch {
    return Date.now();
  }
}

/** The age re-renders every 15 s (minutes shift visibly within that window). */
const AGE_TICK_MS = 15_000;
/** The ledger is polled once a minute, like BudgetWidget. */
const LEDGER_POLL_MS = 60_000;

export default function SessionCostPill({ className = '' }: { className?: string }) {
  const [start, setStart] = useState<number | null>(null);
  const [now, setNow] = useState(0);
  const [status, setStatus] = useState<BudgetStatus | null>(null);

  useEffect(() => {
    const anchor = readOrInitStart();
    setStart(anchor);
    const tick = () => setNow(Date.now());
    tick();
    const handle = window.setInterval(tick, AGE_TICK_MS);
    return () => window.clearInterval(handle);
  }, []);

  useEffect(() => {
    if (start === null) return;
    let cancelled = false;
    const load = async () => {
      try {
        // Never throws for a fetch (a failed fetch is the `unavailable`
        // fallback, which the pill renders as the plain age); the only throw
        // is the seconds-scale guard, which a Date.now() anchor never trips.
        const next = await getBudgetStatus(start);
        if (!cancelled) setStatus(next);
      } catch {
        if (!cancelled) setStatus(null);
      }
    };
    void load();
    const handle = window.setInterval(() => void load(), LEDGER_POLL_MS);
    return () => {
      cancelled = true;
      window.clearInterval(handle);
    };
  }, [start]);

  // Before the first client tick (and on the server) the age is unknown;
  // render a stable placeholder so hydration matches.
  if (start === null || now === 0) {
    return (
      <span className={`font-mono text-[10px] whitespace-nowrap ${className}`} style={{ color: 'var(--text-muted)' }}>
        Session —
      </span>
    );
  }

  const pill = sessionCostFromStatus(status, now - start);
  const warn = pill.state === 'warn';

  return (
    <span
      className={`font-mono text-[10px] whitespace-nowrap ${warn ? 'text-[#d97706] dark:text-[#f59e0b]' : ''} ${className}`}
      style={warn ? undefined : { color: 'var(--text-muted)' }}
      title={pill.title}
      data-state={pill.state}
    >
      {pill.text}
    </span>
  );
}
