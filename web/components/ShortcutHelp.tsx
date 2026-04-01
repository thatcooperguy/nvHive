'use client';

import { useEffect, useRef } from 'react';

// ─── Data ──────────────────────────────────────────────────────────────────────

const SHORTCUT_GROUPS = [
  {
    title: 'General',
    rows: [
      { keys: ['Ctrl', 'K'],       desc: 'Command palette' },
      { keys: ['Ctrl', 'N'],       desc: 'New conversation' },
      { keys: ['Ctrl', 'B'],       desc: 'Toggle sidebar' },
      { keys: ['Ctrl', '/'],       desc: 'This help overlay' },
      { keys: ['Escape'],          desc: 'Close modal / stop generation' },
    ],
  },
  {
    title: 'Chat',
    rows: [
      { keys: ['Ctrl', 'Enter'],   desc: 'Send message' },
      { keys: ['Ctrl', 'Shift', 'C'], desc: 'Copy last response' },
      { keys: ['Ctrl', '1'],       desc: 'Ask mode (single advisor)' },
      { keys: ['Ctrl', '2'],       desc: 'Convene mode (council)' },
      { keys: ['Ctrl', '3'],       desc: 'Poll mode (compare)' },
    ],
  },
];

// ─── Types ─────────────────────────────────────────────────────────────────────

interface ShortcutHelpProps {
  open: boolean;
  onClose: () => void;
}

// ─── Component ─────────────────────────────────────────────────────────────────

export default function ShortcutHelp({ open, onClose }: ShortcutHelpProps) {
  const dialogRef = useRef<HTMLDivElement>(null);

  // Close on Escape
  useEffect(() => {
    if (!open) return;
    const handler = (e: KeyboardEvent) => {
      if (e.key === 'Escape') { e.preventDefault(); onClose(); }
    };
    document.addEventListener('keydown', handler);
    return () => document.removeEventListener('keydown', handler);
  }, [open, onClose]);

  // Focus the dialog for accessibility
  useEffect(() => {
    if (open) {
      const t = setTimeout(() => dialogRef.current?.focus(), 30);
      return () => clearTimeout(t);
    }
  }, [open]);

  if (!open) return null;

  return (
    <div
      className="shortcut-help-backdrop"
      onClick={onClose}
      role="dialog"
      aria-modal="true"
      aria-label="Keyboard shortcuts"
    >
      <div
        ref={dialogRef}
        className="shortcut-help-modal"
        onClick={e => e.stopPropagation()}
        tabIndex={-1}
      >
        {/* Header */}
        <div className="shortcut-help-header">
          <div className="flex items-center gap-2">
            <span className="text-[#76B900] text-sm font-mono">?</span>
            <h2 className="shortcut-help-title">Keyboard Shortcuts</h2>
          </div>
          <button
            onClick={onClose}
            className="shortcut-help-close"
            aria-label="Close keyboard shortcuts"
          >
            <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
              <path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" />
            </svg>
          </button>
        </div>

        {/* Body */}
        <div className="shortcut-help-body">
          {SHORTCUT_GROUPS.map(group => (
            <div key={group.title} className="shortcut-help-group">
              <div className="shortcut-help-group-title">{group.title}</div>
              <div className="shortcut-help-rows">
                {group.rows.map((row, i) => (
                  <div key={i} className="shortcut-help-row">
                    <span className="shortcut-help-row-desc">{row.desc}</span>
                    <span className="shortcut-help-keys">
                      {row.keys.map((k, ki) => (
                        <kbd key={ki} className="kbd-badge">{k}</kbd>
                      ))}
                    </span>
                  </div>
                ))}
              </div>
            </div>
          ))}
        </div>

        {/* Footer */}
        <div className="shortcut-help-footer">
          <button
            onClick={onClose}
            className="shortcut-help-got-it"
            autoFocus
          >
            Got it
          </button>
        </div>
      </div>
    </div>
  );
}
