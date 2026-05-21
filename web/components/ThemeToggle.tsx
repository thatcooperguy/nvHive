'use client';

/**
 * Theme toggle — cycles light → dark → system.
 *
 * State is persisted to localStorage under `nvh_theme` and applied by toggling
 * the `.dark` class on <html>. System preference is read from
 * prefers-color-scheme and watched for changes when mode === 'system'.
 *
 * Render this anywhere in the layout; it positions itself.
 */

import { useEffect, useState } from 'react';

type ThemeMode = 'light' | 'dark' | 'system';

const STORAGE_KEY = 'nvh_theme';

function readSystemDark(): boolean {
  if (typeof window === 'undefined') return false;
  return window.matchMedia('(prefers-color-scheme: dark)').matches;
}

function applyTheme(mode: ThemeMode) {
  if (typeof document === 'undefined') return;
  const root = document.documentElement;
  const isDark = mode === 'dark' || (mode === 'system' && readSystemDark());
  root.classList.toggle('dark', isDark);
}

export default function ThemeToggle({ compact = false }: { compact?: boolean }) {
  const [mode, setMode] = useState<ThemeMode>('system');
  const [mounted, setMounted] = useState(false);

  // Hydrate from localStorage on mount.
  useEffect(() => {
    setMounted(true);
    try {
      const stored = window.localStorage.getItem(STORAGE_KEY) as ThemeMode | null;
      const initial: ThemeMode = stored === 'light' || stored === 'dark' || stored === 'system' ? stored : 'system';
      setMode(initial);
      applyTheme(initial);
    } catch {
      applyTheme('system');
    }
  }, []);

  // Re-apply whenever mode changes.
  useEffect(() => {
    if (!mounted) return;
    applyTheme(mode);
    try {
      window.localStorage.setItem(STORAGE_KEY, mode);
    } catch {
      // Storage may be blocked in hardened browser profiles.
    }
  }, [mode, mounted]);

  // When mode === 'system', track OS-level changes live.
  useEffect(() => {
    if (mode !== 'system' || typeof window === 'undefined') return;
    const mq = window.matchMedia('(prefers-color-scheme: dark)');
    const handler = () => applyTheme('system');
    mq.addEventListener?.('change', handler);
    return () => mq.removeEventListener?.('change', handler);
  }, [mode]);

  const cycle = () => {
    setMode(prev => (prev === 'light' ? 'dark' : prev === 'dark' ? 'system' : 'light'));
  };

  const label = mode === 'light' ? 'Light' : mode === 'dark' ? 'Dark' : 'System';
  const icon = mode === 'light' ? (
    <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.6}>
      <path strokeLinecap="round" strokeLinejoin="round" d="M12 3v2.25m6.364.386l-1.591 1.591M21 12h-2.25m-.386 6.364l-1.591-1.591M12 18.75V21m-4.773-4.227l-1.591 1.591M5.25 12H3m4.227-4.773L5.636 5.636M15.75 12a3.75 3.75 0 11-7.5 0 3.75 3.75 0 017.5 0z" />
    </svg>
  ) : mode === 'dark' ? (
    <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.6}>
      <path strokeLinecap="round" strokeLinejoin="round" d="M21.752 15.002A9.72 9.72 0 0118 15.75c-5.385 0-9.75-4.365-9.75-9.75 0-1.33.266-2.597.748-3.752A9.753 9.753 0 003 11.25C3 16.635 7.365 21 12.75 21a9.753 9.753 0 009.002-5.998z" />
    </svg>
  ) : (
    <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.6}>
      <path strokeLinecap="round" strokeLinejoin="round" d="M9 17.25v1.007a3 3 0 01-.879 2.122L7.5 21h9l-.621-.621A3 3 0 0115 18.257V17.25m6-12V15a2.25 2.25 0 01-2.25 2.25H5.25A2.25 2.25 0 013 15V5.25m18 0A2.25 2.25 0 0018.75 3H5.25A2.25 2.25 0 003 5.25m18 0V12a2.25 2.25 0 01-2.25 2.25H5.25A2.25 2.25 0 013 12V5.25" />
    </svg>
  );

  // Don't render until mounted so SSR markup matches client.
  if (!mounted) {
    return (
      <button
        type="button"
        className="inline-flex items-center gap-1.5 text-[10px] font-mono text-[#737373] dark:text-[#a3a3a3]"
        aria-label="Theme"
        disabled
      >
        <span className="h-4 w-4" />
        {!compact && <span>Theme</span>}
      </button>
    );
  }

  return (
    <button
      type="button"
      onClick={cycle}
      className="inline-flex items-center gap-1.5 rounded-md px-2 py-1 text-[10px] font-mono text-[#737373] hover:bg-[var(--bg-subtle)] hover:text-[var(--text-primary)] transition-colors dark:text-[#a3a3a3]"
      aria-label={`Theme: ${label} (click to cycle)`}
      title={`Theme: ${label} — click to cycle Light/Dark/System`}
    >
      {icon}
      {!compact && <span>{label}</span>}
    </button>
  );
}
