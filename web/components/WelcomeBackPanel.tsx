'use client';

/**
 * WelcomeBackPanel — shown once on chat-page mount when the Wizard reconnect
 * routine reports auto-repairs or drift. Designed for cloud-renter users
 * landing on a freshly-minted ephemeral GPU session who want a clear answer
 * to "what survived from last time and what changed?"
 *
 * Behavior:
 *  - Hits /v1/wizard/reconnect once on mount.
 *  - Auto-hides when nothing changed AND no repairs ran AND no attention items
 *    (the happy "just keep going" case).
 *  - Shows for as long as the user wants to read it; only the dismiss button
 *    closes it.
 *  - On dismiss, sets sessionStorage so the panel stays closed for the rest
 *    of the tab session — coming back after a real reconnect re-opens it.
 */

import { useEffect, useState } from 'react';
import { wizardReconnect, type WizardReconnectResult } from '@/lib/api';

const DISMISS_KEY = 'nvh_welcome_back_dismissed_for';

function shouldShow(result: WizardReconnectResult): boolean {
  if (result.first_run) return true;
  if (result.changed) return true;
  if (result.auto_repaired.length > 0) return true;
  if (result.needs_attention.length > 0) return true;
  return false;
}

export default function WelcomeBackPanel() {
  const [result, setResult] = useState<WizardReconnectResult | null>(null);
  const [dismissed, setDismissed] = useState(false);

  useEffect(() => {
    let cancelled = false;

    const run = async () => {
      try {
        const payload = await wizardReconnect();
        if (cancelled) return;
        if (!shouldShow(payload)) return;

        // Per-tab dismissal keyed to the reconnect signature so a new
        // reconnect that surfaces new info reopens the panel.
        const signature = [
          payload.first_run ? 'first' : 'rec',
          payload.what_changed.map(c => c.fact).join(',') || 'no-change',
          payload.auto_repaired.length,
          payload.needs_attention.length,
        ].join('|');
        try {
          const stored = window.sessionStorage.getItem(DISMISS_KEY);
          if (stored === signature) {
            setDismissed(true);
          }
        } catch {
          // Hardened browser — fall through.
        }
        setResult({ ...payload, preflight: undefined });

        // Stash the signature for the dismiss handler.
        (payload as unknown as { _signature: string })._signature = signature;
      } catch {
        // Endpoint missing on older nvh-api builds is fine; just don't render.
      }
    };

    void run();
    return () => {
      cancelled = true;
    };
  }, []);

  if (!result || dismissed) return null;

  const handleDismiss = () => {
    setDismissed(true);
    try {
      const sig = (result as unknown as { _signature?: string })._signature;
      if (sig) window.sessionStorage.setItem(DISMISS_KEY, sig);
    } catch {
      // ignore
    }
  };

  return (
    <div
      className="relative rounded-lg border p-4 shadow-sm"
      style={{
        background: 'var(--bg-card)',
        borderColor: 'var(--border-green)',
      }}
      role="status"
      aria-live="polite"
    >
      <button
        type="button"
        onClick={handleDismiss}
        className="absolute right-2 top-2 rounded p-1 text-[#737373] hover:bg-[var(--bg-subtle)] hover:text-[var(--text-primary)]"
        aria-label="Dismiss welcome back panel"
        title="Dismiss"
      >
        <svg className="h-3.5 w-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
          <path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" />
        </svg>
      </button>

      <div className="flex items-center gap-2 text-[10px] font-mono uppercase tracking-[0.18em]" style={{ color: '#76B900' }}>
        <span className="h-1.5 w-1.5 rounded-full bg-[#76B900]" />
        AI Wizard
      </div>
      <div className="mt-2 text-sm font-semibold pr-6" style={{ color: 'var(--text-primary)' }}>
        {result.summary}
      </div>

      {result.what_survived.length > 0 && (
        <div className="mt-3">
          <div className="text-[10px] font-mono uppercase tracking-[0.14em]" style={{ color: 'var(--text-muted)' }}>
            What survived since last session
          </div>
          <ul className="mt-1 grid grid-cols-2 gap-x-3 gap-y-1 text-xs" style={{ color: 'var(--text-secondary)' }}>
            {result.what_survived.slice(0, 6).map(item => (
              <li key={item.label} className="truncate">
                <span style={{ color: 'var(--text-muted)' }}>{item.label}:</span>{' '}
                <span className="font-mono">{item.value}</span>
              </li>
            ))}
          </ul>
        </div>
      )}

      {result.what_changed.length > 0 && (
        <div className="mt-3">
          <div className="text-[10px] font-mono uppercase tracking-[0.14em]" style={{ color: 'var(--text-muted)' }}>
            What changed
          </div>
          <ul className="mt-1 space-y-1 text-xs" style={{ color: 'var(--text-secondary)' }}>
            {result.what_changed.slice(0, 5).map(change => (
              <li key={change.fact}>
                <span className="font-mono" style={{ color: 'var(--text-primary)' }}>{change.label}:</span>{' '}
                <span style={{ color: 'var(--text-faint)' }}>{change.previous}</span>{' '}
                <span style={{ color: 'var(--text-muted)' }}>→</span>{' '}
                <span style={{ color: change.severity === 'warning' ? '#d97706' : 'var(--text-primary)' }}>
                  {change.current}
                </span>
              </li>
            ))}
          </ul>
        </div>
      )}

      {result.auto_repaired.length > 0 && (
        <div className="mt-3">
          <div className="text-[10px] font-mono uppercase tracking-[0.14em]" style={{ color: 'var(--text-muted)' }}>
            I fixed automatically
          </div>
          <ul className="mt-1 space-y-1 text-xs" style={{ color: 'var(--text-secondary)' }}>
            {result.auto_repaired.map(action => (
              <li key={action.id} className="flex items-baseline gap-1.5">
                <span style={{ color: '#76B900' }}>✓</span>
                <span className="font-medium" style={{ color: 'var(--text-primary)' }}>{action.title}</span>
                {action.summary && (
                  <span className="ml-1" style={{ color: 'var(--text-muted)' }}>— {action.summary}</span>
                )}
              </li>
            ))}
          </ul>
        </div>
      )}

      {result.needs_attention.length > 0 && (
        <div className="mt-3 rounded-md border p-2" style={{ background: 'var(--bg-subtle)', borderColor: 'var(--border-bright)' }}>
          <div className="text-[10px] font-mono uppercase tracking-[0.14em]" style={{ color: '#d97706' }}>
            Needs your attention
          </div>
          <ul className="mt-1 space-y-1 text-xs" style={{ color: 'var(--text-secondary)' }}>
            {result.needs_attention.map(action => (
              <li key={action.id}>
                <span className="font-medium" style={{ color: 'var(--text-primary)' }}>{action.title}</span>
                {action.summary && (
                  <span className="ml-1" style={{ color: 'var(--text-muted)' }}>— {action.summary}</span>
                )}
              </li>
            ))}
          </ul>
        </div>
      )}

      <div className="mt-3 text-[10px] font-mono" style={{ color: 'var(--text-faint)' }}>
        Reconnect took {result.elapsed_ms} ms.
      </div>
    </div>
  );
}
