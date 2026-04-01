'use client';

import {
  createContext,
  useCallback,
  useContext,
  useState,
  type ReactNode,
} from 'react';
import type { ShortcutAction } from '@/components/KeyboardShortcuts';

// ─── Context ───────────────────────────────────────────────────────────────────

export type ModalState = 'none' | 'command-palette' | 'shortcut-help';

interface UIShellContextValue {
  modal: ModalState;
  openCommandPalette: () => void;
  openShortcutHelp: () => void;
  closeModal: () => void;
  /** Dispatch a shortcut action from any component. */
  dispatchShortcut: (action: ShortcutAction) => void;
  /** Register a handler to be called when specific shortcut actions fire */
  onShortcut: (action: ShortcutAction, handler: () => void) => () => void;
}

const UIShellContext = createContext<UIShellContextValue | null>(null);

export function useUIShell(): UIShellContextValue {
  const ctx = useContext(UIShellContext);
  if (!ctx) throw new Error('useUIShell must be used inside UIShellProvider');
  return ctx;
}

// ─── Provider ──────────────────────────────────────────────────────────────────

// Simple pub/sub for shortcut actions
type Handler = () => void;
const listeners: Partial<Record<ShortcutAction, Set<Handler>>> = {};

function subscribe(action: ShortcutAction, handler: Handler): () => void {
  if (!listeners[action]) listeners[action] = new Set();
  listeners[action]!.add(handler);
  return () => listeners[action]!.delete(handler);
}

function publish(action: ShortcutAction) {
  listeners[action]?.forEach(fn => fn());
}

export function UIShellProvider({ children }: { children: ReactNode }) {
  const [modal, setModal] = useState<ModalState>('none');

  const openCommandPalette = useCallback(() => setModal('command-palette'), []);
  const openShortcutHelp   = useCallback(() => setModal('shortcut-help'), []);
  const closeModal         = useCallback(() => setModal('none'), []);

  const dispatchShortcut = useCallback((action: ShortcutAction) => {
    switch (action) {
      case 'open-command-palette':
        setModal('command-palette');
        break;
      case 'show-shortcuts':
        setModal('shortcut-help');
        break;
      case 'stop-generation':
        publish(action);
        break;
      default:
        publish(action);
    }
  }, []);

  const onShortcut = useCallback((action: ShortcutAction, handler: Handler) => {
    return subscribe(action, handler);
  }, []);

  return (
    <UIShellContext.Provider
      value={{ modal, openCommandPalette, openShortcutHelp, closeModal, dispatchShortcut, onShortcut }}
    >
      {children}
    </UIShellContext.Provider>
  );
}
