'use client';

import { useEffect, useRef, useState, useCallback } from 'react';

// ─── Types ─────────────────────────────────────────────────────────────────────

export type PaletteActionId =
  | 'new-chat'
  | 'search-conversations'
  | 'mode-ask'
  | 'mode-convene'
  | 'mode-poll'
  | 'mode-throwdown'
  | 'model-nemotron-small'
  | 'model-gpt4o'
  | 'model-claude-sonnet'
  | 'nav-system'
  | 'nav-providers'
  | 'nav-analytics'
  | 'nav-integrations'
  | 'nav-settings'
  | 'toggle-sidebar'
  | 'show-shortcuts';

export interface PaletteAction {
  id: PaletteActionId | string;
  label: string;
  category: string;
  shortcut?: string;
  icon?: string;
}

interface CommandPaletteProps {
  open: boolean;
  onClose: () => void;
  onAction: (id: string) => void;
  models?: Array<{ model_id: string; display_name: string; provider: string }>;
}

// ─── Static actions ────────────────────────────────────────────────────────────

const STATIC_ACTIONS: PaletteAction[] = [
  // Conversations
  { id: 'new-chat',              label: 'New Chat',                 category: 'Conversations', shortcut: 'Ctrl+N',       icon: '+' },
  { id: 'search-conversations',  label: 'Search Conversations',     category: 'Conversations', shortcut: 'Ctrl+Shift+F', icon: '⌕' },

  // Commands / modes
  { id: 'mode-ask',       label: 'Ask — Single Advisor',      category: 'Commands', shortcut: 'Ctrl+1', icon: '▶' },
  { id: 'mode-convene',   label: 'Convene — Council',          category: 'Commands', shortcut: 'Ctrl+2', icon: '◈' },
  { id: 'mode-poll',      label: 'Poll — Compare Models',      category: 'Commands', shortcut: 'Ctrl+3', icon: '▣' },
  { id: 'mode-throwdown', label: 'Throwdown — Deep Analysis',  category: 'Commands',                    icon: '⚡' },

  // Models
  { id: 'model-nemotron-small', label: 'Switch to Nemotron Small', category: 'Models', icon: '▷' },
  { id: 'model-gpt4o',          label: 'Switch to GPT-4o',          category: 'Models', icon: '▷' },
  { id: 'model-claude-sonnet',  label: 'Switch to Claude Sonnet',   category: 'Models', icon: '▷' },

  // Navigation
  { id: 'nav-system',       label: 'Go to System',       category: 'Navigation', icon: '⊞' },
  { id: 'nav-providers',    label: 'Go to Providers',    category: 'Navigation', icon: '⊟' },
  { id: 'nav-integrations', label: 'Go to Integrations', category: 'Navigation', icon: '⊡' },
  { id: 'nav-analytics',    label: 'Go to Analytics',    category: 'Navigation', icon: '▤' },
  { id: 'nav-settings',     label: 'Go to Settings',     category: 'Navigation', icon: '⚙' },

  // Settings
  { id: 'toggle-sidebar',  label: 'Toggle Sidebar',           category: 'Settings', shortcut: 'Ctrl+B', icon: '◧' },
  { id: 'show-shortcuts',  label: 'Show Keyboard Shortcuts',  category: 'Settings', shortcut: 'Ctrl+/', icon: '?' },
];

const CATEGORY_ORDER = ['Conversations', 'Commands', 'Models', 'Navigation', 'Settings'];

// ─── Fuzzy match ───────────────────────────────────────────────────────────────

function fuzzyMatch(needle: string, haystack: string): boolean {
  if (!needle) return true;
  const n = needle.toLowerCase();
  const h = haystack.toLowerCase();
  if (h.includes(n)) return true;
  let ni = 0;
  for (let hi = 0; hi < h.length && ni < n.length; hi++) {
    if (h[hi] === n[ni]) ni++;
  }
  return ni === n.length;
}

function fuzzyScore(needle: string, haystack: string): number {
  if (!needle) return 0;
  const n = needle.toLowerCase();
  const h = haystack.toLowerCase();
  if (h.startsWith(n)) return 3;
  if (h.includes(n)) return 2;
  return 1;
}

// ─── Recent-used helpers ───────────────────────────────────────────────────────

const RECENTS_KEY = 'hive_cmd_palette_recents';
const MAX_RECENTS = 5;

function loadRecents(): string[] {
  if (typeof window === 'undefined') return [];
  try {
    return JSON.parse(localStorage.getItem(RECENTS_KEY) ?? '[]');
  } catch {
    return [];
  }
}

function saveRecent(id: string) {
  if (typeof window === 'undefined') return;
  const existing = loadRecents().filter(r => r !== id);
  const updated = [id, ...existing].slice(0, MAX_RECENTS);
  try {
    localStorage.setItem(RECENTS_KEY, JSON.stringify(updated));
  } catch {
    // ignore quota
  }
}

// ─── ShortcutBadge ─────────────────────────────────────────────────────────────

function ShortcutBadge({ shortcut }: { shortcut: string }) {
  const parts = shortcut.split('+');
  return (
    <span className="flex items-center gap-0.5 ml-auto flex-shrink-0">
      {parts.map((p, i) => (
        <kbd key={i} className="kbd-badge">{p}</kbd>
      ))}
    </span>
  );
}

// ─── CommandPalette ────────────────────────────────────────────────────────────

export default function CommandPalette({ open, onClose, onAction, models = [] }: CommandPaletteProps) {
  const [query, setQuery] = useState('');
  const [activeIndex, setActiveIndex] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);
  const listRef = useRef<HTMLDivElement>(null);
  const [recents, setRecents] = useState<string[]>([]);

  // Load recents when palette opens
  useEffect(() => {
    if (open) {
      setRecents(loadRecents());
      setQuery('');
      setActiveIndex(0);
    }
  }, [open]);

  // Focus input when open
  useEffect(() => {
    if (open) {
      const t = setTimeout(() => inputRef.current?.focus(), 30);
      return () => clearTimeout(t);
    }
  }, [open]);

  // Build dynamic model actions from live model list
  const dynamicModelActions: PaletteAction[] = models.map(m => ({
    id: `model-switch:${m.model_id}`,
    label: `Switch to ${m.display_name}`,
    category: 'Models',
    icon: '▷',
  }));

  // Merge static + dynamic, dedup models section
  const staticModelIds = new Set(['model-nemotron-small', 'model-gpt4o', 'model-claude-sonnet']);
  const extraModelActions = dynamicModelActions.filter(a => {
    const mid = a.id.replace('model-switch:', '');
    return !['nemotron', 'gpt-4o', 'claude-sonnet'].some(k => mid.includes(k));
  });
  const allActions = [...STATIC_ACTIONS, ...extraModelActions];

  // Filter & sort
  const filtered: PaletteAction[] = query.trim()
    ? allActions
        .filter(a => fuzzyMatch(query, a.label) || fuzzyMatch(query, a.category))
        .sort((a, b) => fuzzyScore(query, b.label) - fuzzyScore(query, a.label))
    : allActions;

  // When no query: prepend recent items at top
  const recentActions = !query.trim()
    ? recents
        .map(id => allActions.find(a => a.id === id))
        .filter(Boolean) as PaletteAction[]
    : [];

  // Group by category (skip if there's a query — flat list)
  const grouped: { category: string; items: PaletteAction[] }[] = [];
  if (!query.trim() && recentActions.length > 0) {
    grouped.push({ category: 'Recent', items: recentActions });
  }
  if (query.trim()) {
    grouped.push({ category: 'Results', items: filtered });
  } else {
    const byCategory: Record<string, PaletteAction[]> = {};
    for (const action of filtered) {
      if (!byCategory[action.category]) byCategory[action.category] = [];
      byCategory[action.category].push(action);
    }
    for (const cat of CATEGORY_ORDER) {
      if (byCategory[cat]?.length) {
        grouped.push({ category: cat, items: byCategory[cat] });
      }
    }
  }

  // Flat list for keyboard nav
  const flatItems = grouped.flatMap(g => g.items);
  const clampedIndex = Math.min(activeIndex, Math.max(0, flatItems.length - 1));

  const handleSelect = useCallback((action: PaletteAction) => {
    saveRecent(action.id);
    setRecents(loadRecents());
    onAction(action.id);
    onClose();
  }, [onAction, onClose]);

  // Keyboard handler
  useEffect(() => {
    if (!open) return;
    const handler = (e: KeyboardEvent) => {
      if (e.key === 'Escape') { e.preventDefault(); onClose(); return; }
      if (e.key === 'ArrowDown') {
        e.preventDefault();
        setActiveIndex(i => Math.min(i + 1, flatItems.length - 1));
        return;
      }
      if (e.key === 'ArrowUp') {
        e.preventDefault();
        setActiveIndex(i => Math.max(i - 1, 0));
        return;
      }
      if (e.key === 'Enter') {
        e.preventDefault();
        const item = flatItems[clampedIndex];
        if (item) handleSelect(item);
        return;
      }
    };
    document.addEventListener('keydown', handler);
    return () => document.removeEventListener('keydown', handler);
  }, [open, flatItems, clampedIndex, handleSelect, onClose]);

  // Scroll active item into view
  useEffect(() => {
    if (!listRef.current) return;
    const el = listRef.current.querySelector(`[data-palette-index="${clampedIndex}"]`) as HTMLElement | null;
    el?.scrollIntoView({ block: 'nearest' });
  }, [clampedIndex]);

  // Reset active index when query or results change
  useEffect(() => { setActiveIndex(0); }, [query]);

  if (!open) return null;

  let flatIdx = 0;

  return (
    <div className="cmd-palette-backdrop" onClick={onClose} aria-modal="true" role="dialog" aria-label="Command palette">
      <div className="cmd-palette-container" onClick={e => e.stopPropagation()}>
        {/* Search bar */}
        <div className="cmd-palette-search-row">
          <svg className="cmd-palette-search-icon" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
            <path strokeLinecap="round" strokeLinejoin="round"
              d="M21 21l-5.197-5.197m0 0A7.5 7.5 0 105.196 5.196a7.5 7.5 0 0010.607 10.607z" />
          </svg>
          <input
            ref={inputRef}
            type="text"
            value={query}
            onChange={e => setQuery(e.target.value)}
            placeholder="Search commands, models, conversations..."
            className="cmd-palette-input"
            autoComplete="off"
            spellCheck={false}
          />
          <kbd className="kbd-badge" style={{ fontSize: '10px', padding: '2px 6px' }}>Esc</kbd>
        </div>

        {/* Results */}
        <div className="cmd-palette-list" ref={listRef}>
          {flatItems.length === 0 && (
            <div className="cmd-palette-empty">No matching commands</div>
          )}
          {grouped.map(group => (
            <div key={group.category}>
              <div className="cmd-palette-category-header">{group.category}</div>
              {group.items.map(action => {
                const idx = flatIdx++;
                const isActive = idx === clampedIndex;
                return (
                  <button
                    key={action.id}
                    data-palette-index={idx}
                    className={`cmd-palette-item${isActive ? ' cmd-palette-item-active' : ''}`}
                    onClick={() => handleSelect(action)}
                    onMouseEnter={() => setActiveIndex(idx)}
                  >
                    {action.icon && (
                      <span className="cmd-palette-item-icon">{action.icon}</span>
                    )}
                    <span className="cmd-palette-item-label">{action.label}</span>
                    {action.shortcut && <ShortcutBadge shortcut={action.shortcut} />}
                  </button>
                );
              })}
            </div>
          ))}
        </div>

        {/* Footer hint */}
        <div className="cmd-palette-footer">
          <span><kbd className="kbd-badge">↑↓</kbd> navigate</span>
          <span><kbd className="kbd-badge">Enter</kbd> select</span>
          <span><kbd className="kbd-badge">Esc</kbd> close</span>
        </div>
      </div>
    </div>
  );
}
