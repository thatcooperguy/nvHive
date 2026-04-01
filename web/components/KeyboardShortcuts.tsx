'use client';

import { useEffect, useCallback } from 'react';

// ─── Types ─────────────────────────────────────────────────────────────────────

export type ShortcutAction =
  | 'open-command-palette'
  | 'new-chat'
  | 'toggle-sidebar'
  | 'send-message'
  | 'copy-last-response'
  | 'stop-generation'
  | 'show-shortcuts'
  | 'mode-ask'
  | 'mode-convene'
  | 'mode-poll';

export interface KeyboardShortcutsProps {
  onAction: (action: ShortcutAction) => void;
  /** Pass true while the chat textarea has focus so Ctrl+Enter can be routed correctly */
  inputFocused?: boolean;
  /** Pass true while generation is streaming */
  streaming?: boolean;
}

// ─── Helper ────────────────────────────────────────────────────────────────────

function isMod(e: KeyboardEvent): boolean {
  return e.ctrlKey || e.metaKey;
}

// ─── Component ─────────────────────────────────────────────────────────────────

/**
 * KeyboardShortcuts — registers global hotkeys and fires an `onAction` callback.
 *
 * Render this component once near the root of the app (inside LayoutShell or
 * the chat page).  It renders nothing to the DOM; it only attaches/removes
 * event listeners.
 */
export default function KeyboardShortcuts({ onAction, inputFocused = false, streaming = false }: KeyboardShortcutsProps) {
  const handler = useCallback(
    (e: KeyboardEvent) => {
      const tag = (e.target as HTMLElement | null)?.tagName?.toLowerCase();
      const inText = tag === 'textarea' || tag === 'input';

      // Ctrl/Cmd+K — Command palette (always)
      if (isMod(e) && e.key === 'k') {
        e.preventDefault();
        onAction('open-command-palette');
        return;
      }

      // Ctrl/Cmd+N — New conversation
      if (isMod(e) && e.key === 'n') {
        e.preventDefault();
        onAction('new-chat');
        return;
      }

      // Ctrl/Cmd+B — Toggle sidebar
      if (isMod(e) && e.key === 'b') {
        // NOTE: Sidebar.tsx also handles this. We fire the action here too so
        // the command palette and page can react. The sidebar handles its own
        // collapsed state via its own listener; that is fine — double-fire is
        // idempotent for the sidebar.
        e.preventDefault();
        onAction('toggle-sidebar');
        return;
      }

      // Ctrl+/ — Show shortcut help (must not be in textarea, or be explicit)
      if (isMod(e) && e.key === '/') {
        e.preventDefault();
        onAction('show-shortcuts');
        return;
      }

      // Ctrl+Shift+C — Copy last response
      if (isMod(e) && e.shiftKey && e.key === 'C') {
        e.preventDefault();
        onAction('copy-last-response');
        return;
      }

      // Ctrl+Enter — Send message (only when input is focused)
      if (isMod(e) && e.key === 'Enter' && inText) {
        // ChatInput already handles this itself. We only relay if the page
        // needs to know (e.g. for future use). Don't double-submit — let the
        // textarea's own onKeyDown handle it.
        onAction('send-message');
        return;
      }

      // Escape — close modals OR stop generation
      if (e.key === 'Escape' && !inText) {
        // Don't preventDefault here — let modal Escape propagate naturally.
        if (streaming) {
          onAction('stop-generation');
        }
        return;
      }

      // Ctrl+1/2/3 — Switch modes (only when not in text field)
      if (isMod(e) && !inText) {
        if (e.key === '1') { e.preventDefault(); onAction('mode-ask'); return; }
        if (e.key === '2') { e.preventDefault(); onAction('mode-convene'); return; }
        if (e.key === '3') { e.preventDefault(); onAction('mode-poll'); return; }
      }
    },
    [onAction, streaming]
  );

  useEffect(() => {
    document.addEventListener('keydown', handler);
    return () => document.removeEventListener('keydown', handler);
  }, [handler]);

  return null;
}
