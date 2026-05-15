'use client';

/**
 * SessionAgePill — tiny "Session: 4h 12m" indicator in the top bar.
 *
 * For metered cloud-GPU desktop users the *age of the current rental* is
 * the load-bearing fact, so a one-line, always-visible pill answers it
 * without forcing them to leave the page. We anchor the start time in
 * sessionStorage so reloads on the same tab keep counting from the same
 * origin; a new tab starts fresh (matches "session" semantics).
 *
 * The pill is read-only and uses <span> instead of a button so it never
 * accidentally captures keyboard focus.
 */

import { useEffect, useState } from 'react';

const SESSION_START_KEY = 'nvh_session_started_at';

function readOrInitStart(): number {
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

function formatDuration(ms: number): string {
  const totalSec = Math.max(0, Math.floor(ms / 1000));
  const hours = Math.floor(totalSec / 3600);
  const minutes = Math.floor((totalSec % 3600) / 60);
  if (hours > 0) return `${hours}h ${minutes.toString().padStart(2, '0')}m`;
  const seconds = totalSec % 60;
  return `${minutes}m ${seconds.toString().padStart(2, '0')}s`;
}

export default function SessionAgePill() {
  const [age, setAge] = useState<string>('—');

  useEffect(() => {
    const start = readOrInitStart();
    const tick = () => setAge(formatDuration(Date.now() - start));
    tick();
    // 15s is granular enough for a top-bar indicator without churning
    // re-renders. Hours change rarely; minutes shift visually within a
    // perceptible window.
    const handle = window.setInterval(tick, 15_000);
    return () => window.clearInterval(handle);
  }, []);

  return (
    <span
      className="font-mono text-[10px]"
      style={{ color: 'var(--text-muted)' }}
      title="Time since this browser session started. Useful when you're paying by the hour for a GPU desktop."
    >
      Session: {age}
    </span>
  );
}
