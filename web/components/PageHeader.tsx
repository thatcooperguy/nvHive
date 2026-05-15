'use client';

/**
 * PageHeader — shared header used by every route. Standardizes the chip /
 * title / subtitle styling so the product feels coherent across /wizard,
 * /vault, /settings, /providers, /system, /analytics, /integrations, etc.
 *
 * Drop this at the top of a page's main content (under LayoutShell's top
 * bar). Eyebrow chip is optional; trailing slot accepts a right-aligned
 * action set (buttons, refresh control, hardware widget, …).
 */

import type { ReactNode } from 'react';

interface Props {
  eyebrow?: string;
  title: ReactNode;
  subtitle?: ReactNode;
  trailing?: ReactNode;
  accent?: string;
}

export default function PageHeader({
  eyebrow,
  title,
  subtitle,
  trailing,
  accent = '#76B900',
}: Props) {
  return (
    <header
      className="border-b px-4 py-3"
      style={{ borderColor: 'var(--border)', background: 'var(--bg-secondary)' }}
    >
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0 flex-1">
          {eyebrow && (
            <div className="flex items-center gap-2">
              <span className="h-2 w-2 rounded-full" style={{ background: accent }} />
              <span
                className="text-[10px] font-mono font-bold uppercase tracking-[0.18em]"
                style={{ color: accent }}
              >
                {eyebrow}
              </span>
            </div>
          )}
          <div
            className="mt-1 text-base font-semibold tracking-tight"
            style={{ color: 'var(--text-primary)' }}
          >
            {title}
          </div>
          {subtitle && (
            <div className="mt-0.5 text-xs" style={{ color: 'var(--text-secondary)' }}>
              {subtitle}
            </div>
          )}
        </div>
        {trailing && <div className="flex-shrink-0">{trailing}</div>}
      </div>
    </header>
  );
}
