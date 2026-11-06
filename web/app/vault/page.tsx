'use client';

/**
 * /vault — the Markdown memory vault: status, the save form, and (0.46 §2)
 * the note list behind the Wizard's `remember` card.
 *
 * The list loads the newest headers once (`GET /v1/vault/notes`) and narrows
 * them client-side with the pure helpers in lib/vaultNotes.ts: category
 * chips derived from the server's `directories` block (never hand-typed),
 * tag chips from the listed notes, a search box and a Memory toggle that
 * keeps records only (`?memory=1` turns it on). A row expands to fetch its
 * body lazily; Forget confirms, then `DELETE /v1/vault/notes/{id}` removes
 * the file and its search index entry. `/vault?id=<id>` — the link a
 * `remember` card and the save form hand out — scrolls to that note and
 * rings it for a few seconds.
 *
 * `useSearchParams()` needs a Suspense boundary for Next's static prerender,
 * so the default export wraps the page body exactly as app/wizard/page.tsx
 * does.
 */

import Link from 'next/link';
import { useSearchParams } from 'next/navigation';
import { Suspense, useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { ReactNode } from 'react';
import PageHeader from '@/components/PageHeader';
import {
  deleteVaultNote,
  getVaultNote,
  getVaultStatus,
  initVault,
  installObsidian,
  listVaultNotes,
  saveVaultMemory,
} from '@/lib/api';
import type { VaultNote, VaultNotesResult, VaultStatus } from '@/lib/types';
import {
  DEFAULT_MEMORY_CATEGORY,
  categoriesFromStatus,
  createdLabel,
  filterNotes,
  forgetOutcomeMessage,
  isMemoryRecord,
  noteAnchorId,
  savedMemoryMessage,
  tagsFromNotes,
} from '@/lib/vaultNotes';

/** Headers the list asks for in one go; the server caps the query at its own maximum. */
const NOTES_PAGE_LIMIT = 200;
/** How long the deep-linked note keeps its ring. */
const HIGHLIGHT_MS = 3000;

function statusLabel(status: VaultStatus | null): string {
  if (!status) return 'checking';
  if (!status.initialized) return 'needs setup';
  if (status.obsidian.installed) return 'vault + obsidian';
  return 'vault ready';
}

function StatCard({ label, value, tone = 'green' }: { label: string; value: string | number; tone?: 'green' | 'amber' | 'gray' }) {
  const toneClass = tone === 'amber' ? 'text-[#d97706]' : tone === 'gray' ? 'text-[#737373]' : 'text-[#76B900]';
  return (
    <div className="border border-[#e5e5e5] bg-white p-4 dark:bg-[#0a0a0a] dark:border-[#262626]">
      <div className="text-[10px] font-mono uppercase tracking-[0.18em] text-[#737373] dark:text-[#a3a3a3]">{label}</div>
      <div className={`mt-2 font-mono text-lg font-bold ${toneClass}`}>{value}</div>
    </div>
  );
}

function InlineMessage({ message, error }: { message: ReactNode | null; error?: boolean }) {
  if (!message) return null;
  return (
    <div
      role={error ? 'alert' : 'status'}
      className={`border px-4 py-3 text-sm ${
        error
          ? 'border-red-200 bg-red-50 text-red-700 dark:border-red-900/60 dark:bg-red-950/30 dark:text-red-300'
          : 'border-[#76B900]/30 bg-[#76B900]/5 text-[#447000] dark:text-[#9ad13a]'
      }`}
    >
      {message}
    </div>
  );
}

/** A filter chip: a real button with a pressed state, in the setup page's chip style. */
function FilterChip({ label, active, onClick, title }: { label: string; active: boolean; onClick: () => void; title?: string }) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-pressed={active}
      title={title}
      className={`text-[10px] font-mono border px-2 py-0.5 transition-colors ${
        active
          ? 'text-[#0a0a0a] bg-[#76B900] border-[#76B900] dark:text-[#0a0a0a]'
          : 'text-[#525252] bg-[#f5f5f5] border-[#e5e5e5] hover:border-[#76B900] dark:bg-[#141414] dark:border-[#262626] dark:text-[#a3a3a3]'
      }`}
    >
      {label}
    </button>
  );
}

function Badge({ children, tone = 'gray' }: { children: ReactNode; tone?: 'gray' | 'green' | 'amber' }) {
  const toneClass =
    tone === 'green'
      ? 'text-[#447000] border-[#76B900]/40 bg-[#76B900]/10 dark:text-[#9ad13a]'
      : tone === 'amber'
        ? 'text-[#b45309] border-[#d97706]/40 bg-[#d97706]/10 dark:text-[#fbbf24]'
        : 'text-[#737373] border-[#e5e5e5] bg-[#fafafa] dark:bg-[#141414] dark:border-[#262626] dark:text-[#a3a3a3]';
  return <span className={`text-[9px] font-mono uppercase tracking-wider border px-1.5 py-0.5 ${toneClass}`}>{children}</span>;
}

type ExpandedBody =
  | { state: 'loading' }
  | { state: 'ok'; body: string; path: string }
  | { state: 'error'; error: string };

export default function VaultPage() {
  return (
    <Suspense fallback={null}>
      <VaultPageBody />
    </Suspense>
  );
}

function VaultPageBody() {
  const searchParams = useSearchParams();
  const deepLinkId = searchParams?.get('id') ?? null;
  const memoryParam = searchParams?.get('memory');

  const [status, setStatus] = useState<VaultStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [working, setWorking] = useState<string | null>(null);
  const [message, setMessage] = useState<ReactNode | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [title, setTitle] = useState('');
  const [category, setCategory] = useState(DEFAULT_MEMORY_CATEGORY);
  const [body, setBody] = useState('');
  const [tags, setTags] = useState('');

  // ── Note list ──
  const [notes, setNotes] = useState<VaultNote[]>([]);
  const [notesMeta, setNotesMeta] = useState<Pick<VaultNotesResult, 'count' | 'limit'> | null>(null);
  const [notesLoading, setNotesLoading] = useState(true);
  const [notesError, setNotesError] = useState<string | null>(null);
  const [filterCategory, setFilterCategory] = useState('');
  const [filterTag, setFilterTag] = useState('');
  const [query, setQuery] = useState('');
  const [memoryOnly, setMemoryOnly] = useState(memoryParam === '1' || memoryParam === 'true');
  const [expanded, setExpanded] = useState<Record<string, ExpandedBody>>({});
  const [forgetting, setForgetting] = useState<string | null>(null);
  const [highlightId, setHighlightId] = useState<string | null>(null);
  const [missingId, setMissingId] = useState<string | null>(null);
  const lookedUpRef = useRef<Set<string>>(new Set());

  const loadNotes = useCallback(async () => {
    setNotesLoading(true);
    setNotesError(null);
    try {
      const result = await listVaultNotes({ limit: NOTES_PAGE_LIMIT });
      setNotes(Array.isArray(result.notes) ? result.notes : []);
      setNotesMeta({ count: result.count, limit: result.limit });
    } catch (err) {
      setNotesError(err instanceof Error ? err.message : 'Could not load the vault notes');
    } finally {
      setNotesLoading(false);
    }
  }, []);

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setStatus(await getVaultStatus());
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not load vault status');
    } finally {
      setLoading(false);
    }
    await loadNotes();
  }, [loadNotes]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const seedCount = useMemo(() => {
    if (!status?.seeded_files) return 0;
    return Object.keys(status.seeded_files).length;
  }, [status]);

  const categories = useMemo(() => categoriesFromStatus(status, notes), [status, notes]);
  const saveCategories = useMemo(
    () => (categories.includes(category) ? categories : [category, ...categories]),
    [categories, category],
  );
  const allTags = useMemo(() => tagsFromNotes(notes), [notes]);
  const visibleNotes = useMemo(
    () => filterNotes(notes, { category: filterCategory, tag: filterTag, q: query, memoryOnly }),
    [notes, filterCategory, filterTag, query, memoryOnly],
  );
  const recordCount = useMemo(() => notes.filter(isMemoryRecord).length, [notes]);
  const filtersActive = Boolean(filterCategory || filterTag || query.trim() || memoryOnly);
  // Flips once when the deep-linked note is on screen, so the ring and the
  // scroll fire once per id rather than on every list change (a Forget
  // elsewhere). Presence is judged over the VISIBLE rows: a note the Memory
  // toggle or a chip hides has no element to scroll to.
  const deepLinkLoaded = useMemo(
    () => Boolean(deepLinkId) && notes.some(note => note.id === deepLinkId),
    [deepLinkId, notes],
  );
  const deepLinkVisible = useMemo(
    () => Boolean(deepLinkId) && visibleNotes.some(note => note.id === deepLinkId),
    [deepLinkId, visibleNotes],
  );
  const revealedRef = useRef<string | null>(null);

  // A deep-linked note that is loaded but hidden — a `doc` note behind
  // `?memory=1`, a record outside the active folder or tag chip, a title the
  // search box does not match — is revealed by clearing the filters, once
  // per id so the user can narrow the list again afterwards.
  useEffect(() => {
    if (!deepLinkId || !deepLinkLoaded || deepLinkVisible) return;
    if (revealedRef.current === deepLinkId) return;
    revealedRef.current = deepLinkId;
    setFilterCategory('');
    setFilterTag('');
    setQuery('');
    setMemoryOnly(false);
  }, [deepLinkId, deepLinkLoaded, deepLinkVisible]);

  // ── ?id= deep link: scroll to and ring the note once it is on screen. ──
  useEffect(() => {
    if (!deepLinkId || !deepLinkVisible) return;
    setHighlightId(deepLinkId);
    setMissingId(null);
    const frame = window.requestAnimationFrame(() => {
      document.getElementById(noteAnchorId(deepLinkId))?.scrollIntoView({ block: 'center', behavior: 'smooth' });
    });
    const timer = window.setTimeout(() => setHighlightId(current => (current === deepLinkId ? null : current)), HIGHLIGHT_MS);
    return () => {
      window.cancelAnimationFrame(frame);
      window.clearTimeout(timer);
    };
  }, [deepLinkId, deepLinkVisible]);

  // A deep-linked note outside the loaded page is fetched on its own and
  // placed at the top, body included; the reveal effect above then clears
  // any filter that would hide it.
  useEffect(() => {
    if (!deepLinkId || notesLoading) return;
    if (notes.some(note => note.id === deepLinkId)) return;
    if (lookedUpRef.current.has(deepLinkId)) return;
    lookedUpRef.current.add(deepLinkId);
    let cancelled = false;
    void getVaultNote(deepLinkId)
      .then(full => {
        if (cancelled) return;
        const { body: fullBody, ...header } = full;
        setNotes(prev => (prev.some(note => note.id === full.id) ? prev : [header as VaultNote, ...prev]));
        setExpanded(prev => ({ ...prev, [full.id]: { state: 'ok', body: fullBody ?? '', path: full.path } }));
      })
      .catch(() => {
        if (!cancelled) setMissingId(deepLinkId);
      });
    return () => {
      cancelled = true;
    };
  }, [deepLinkId, notes, notesLoading]);

  const handleInit = async () => {
    setWorking('vault');
    setMessage(null);
    setError(null);
    try {
      const next = await initVault();
      setStatus(next);
      const count = Object.keys(next.seeded_files ?? {}).length;
      setMessage(count ? `Vault initialized with ${count} starter notes.` : 'Vault is already initialized.');
      await loadNotes();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Vault initialization failed');
    } finally {
      setWorking(null);
    }
  };

  const handleInstallObsidian = async () => {
    setWorking('obsidian');
    setMessage(null);
    setError(null);
    try {
      const obsidian = await installObsidian();
      const next = await getVaultStatus();
      setStatus({ ...next, obsidian });
      setMessage(obsidian.message || (obsidian.installed ? 'Obsidian is ready for this vault.' : obsidian.reason));
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Obsidian install failed');
    } finally {
      setWorking(null);
    }
  };

  const handleSaveMemory = async () => {
    if (!title.trim() || !body.trim()) {
      setError('Add a title and memory note first.');
      return;
    }
    setWorking('memory');
    setMessage(null);
    setError(null);
    try {
      const saved = await saveVaultMemory({
        title,
        body,
        category,
        tags: tags.split(',').map(tag => tag.trim()).filter(Boolean),
        source: 'web',
      });
      const text = savedMemoryMessage(saved);
      setMessage(
        saved.id ? (
          <span>
            {text}{' '}
            <Link href={`/vault?id=${encodeURIComponent(saved.id)}`} className="font-semibold underline underline-offset-2 hover:text-[#76B900]">
              Open the note
            </Link>
          </span>
        ) : (
          text
        ),
      );
      setTitle('');
      setBody('');
      setTags('');
      setStatus(await getVaultStatus());
      await loadNotes();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not save memory');
    } finally {
      setWorking(null);
    }
  };

  const toggleExpanded = async (note: VaultNote) => {
    const current = expanded[note.id];
    if (current) {
      setExpanded(prev => {
        const next = { ...prev };
        delete next[note.id];
        return next;
      });
      return;
    }
    setExpanded(prev => ({ ...prev, [note.id]: { state: 'loading' } }));
    try {
      const full = await getVaultNote(note.id);
      setExpanded(prev => ({ ...prev, [note.id]: { state: 'ok', body: full.body ?? '', path: full.path } }));
    } catch (err) {
      setExpanded(prev => ({
        ...prev,
        [note.id]: { state: 'error', error: err instanceof Error ? err.message : 'Could not load the note' },
      }));
    }
  };

  const handleForget = async (note: VaultNote) => {
    const name = note.title.trim() || note.id;
    if (typeof window !== 'undefined' && !window.confirm(`Forget "${name}"? The Markdown file is removed from the vault along with its search index entry.`)) {
      return;
    }
    setForgetting(note.id);
    setMessage(null);
    setError(null);
    try {
      const result = await deleteVaultNote(note.id);
      setNotes(prev => prev.filter(item => item.id !== note.id));
      setExpanded(prev => {
        const next = { ...prev };
        delete next[note.id];
        return next;
      });
      setNotesMeta(prev => (prev ? { ...prev, count: Math.max(0, prev.count - 1) } : prev));
      setMessage(forgetOutcomeMessage(note.title, result));
      try {
        setStatus(await getVaultStatus());
      } catch {
        // The counts refresh on the next load; the forget itself succeeded.
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not forget the note');
    } finally {
      setForgetting(null);
    }
  };

  const clearFilters = () => {
    setFilterCategory('');
    setFilterTag('');
    setQuery('');
    setMemoryOnly(false);
  };

  return (
    <main>
      <PageHeader
        eyebrow="Memory"
        title="nvHive Vault"
        subtitle="Plain Markdown memory for this rootless workspace. Obsidian can open it, but nvHive owns the durable vault."
        trailing={
          <div className="flex gap-2">
            <button onClick={refresh} disabled={loading || Boolean(working)} className="btn-secondary px-4 py-2 text-xs font-mono uppercase tracking-wider">
              Refresh
            </button>
            <button onClick={handleInit} disabled={working === 'vault'} className="btn-primary px-4 py-2 text-xs font-mono uppercase tracking-wider">
              {working === 'vault' ? 'Initializing' : 'Initialize Vault'}
            </button>
            <button onClick={handleInstallObsidian} disabled={working === 'obsidian'} className="btn-secondary px-4 py-2 text-xs font-mono uppercase tracking-wider">
              {working === 'obsidian' ? 'Installing' : 'Install Obsidian'}
            </button>
          </div>
        }
      />
      <div className="p-6 space-y-6 max-w-6xl mx-auto">

      <InlineMessage message={message} />
      <InlineMessage message={error} error />

      <section className="grid gap-4 md:grid-cols-5">
        <StatCard label="Vault" value={statusLabel(status)} tone={status?.initialized ? 'green' : 'amber'} />
        <StatCard label="Notes" value={status?.markdown_files ?? (loading ? '...' : 0)} />
        <StatCard
          label="Memory"
          value={typeof status?.memory_files === 'number' ? status.memory_files : (loading ? '...' : recordCount)}
        />
        <StatCard label="Seeded" value={seedCount} tone="gray" />
        <StatCard label="Obsidian" value={status?.obsidian.installed ? 'ready' : 'optional'} tone={status?.obsidian.installed ? 'green' : 'gray'} />
      </section>

      <section className="grid gap-6 lg:grid-cols-[1.2fr_0.8fr]">
        <div className="card-flat p-5 space-y-4">
          <div>
            <div className="section-label">Add Memory</div>
            <h2 className="text-xl font-bold mt-1">Capture a decision, fix, or test result</h2>
            <p className="text-sm text-[#737373] mt-1 dark:text-[#a3a3a3]">
              Use this for durable product memory that the wizard and support flow can read later.
            </p>
          </div>
          <div className="grid gap-3 md:grid-cols-[1fr_220px]">
            <input
              value={title}
              onChange={event => setTitle(event.target.value)}
              className="input-base px-3 py-2 text-sm"
              placeholder="Memory title"
              aria-label="Memory title"
            />
            <select
              value={category}
              onChange={event => setCategory(event.target.value)}
              className="input-base px-3 py-2 text-sm"
              aria-label="Vault folder"
            >
              {saveCategories.map(item => <option key={item}>{item}</option>)}
            </select>
          </div>
          <textarea
            value={body}
            onChange={event => setBody(event.target.value)}
            className="input-base min-h-[180px] w-full px-3 py-2 text-sm"
            placeholder="What should AI Wizard remember?"
            aria-label="Memory note"
          />
          <div className="grid gap-3 md:grid-cols-[1fr_180px]">
            <input
              value={tags}
              onChange={event => setTags(event.target.value)}
              className="input-base px-3 py-2 text-sm"
              placeholder="comma,separated,tags"
              aria-label="Tags, comma separated"
            />
            <button onClick={handleSaveMemory} disabled={working === 'memory'} className="btn-primary px-4 py-2 text-xs font-mono uppercase tracking-wider">
              {working === 'memory' ? 'Saving' : 'Save Memory'}
            </button>
          </div>
        </div>

        <div className="card-flat p-5 space-y-4">
          <div>
            <div className="section-label">Rootless Paths</div>
            <h2 className="text-xl font-bold mt-1">Open the same memory anywhere</h2>
          </div>
          <div className="space-y-3 text-sm">
            <div>
              <div className="text-[10px] font-mono uppercase tracking-wider text-[#737373] dark:text-[#a3a3a3]">Vault folder</div>
              <code className="block mt-1 break-all border border-[#e5e5e5] bg-[#fafafa] px-3 py-2 text-xs dark:bg-[#141414] dark:border-[#262626]">{status?.vault_dir ?? 'loading'}</code>
            </div>
            <div>
              <div className="text-[10px] font-mono uppercase tracking-wider text-[#737373] dark:text-[#a3a3a3]">Obsidian command</div>
              <code className="block mt-1 break-all border border-[#e5e5e5] bg-[#fafafa] px-3 py-2 text-xs dark:bg-[#141414] dark:border-[#262626]">{status?.obsidian.open_command ?? 'install optional viewer'}</code>
            </div>
          </div>
          <div className="border border-[#76B900]/30 bg-[#76B900]/5 p-3 text-xs text-[#447000] dark:text-[#9ad13a]">
            Vault notes are not a secrets manager. API keys stay in rootless config, not in Markdown memory.
          </div>
        </div>
      </section>

      <section className="card-flat p-5 space-y-4" aria-labelledby="vault-notes-heading">
        <div className="flex flex-wrap items-start justify-between gap-4">
          <div>
            <div className="section-label">Notes</div>
            <h2 id="vault-notes-heading" className="text-xl font-bold mt-1">Everything the vault remembers</h2>
            <p className="text-sm text-[#737373] mt-1 dark:text-[#a3a3a3]">
              Records the Wizard, the REPL or this page saved, plus the seeded documentation. Forget removes a note and its search index entry; nothing here leaves the device.
            </p>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <button
              type="button"
              role="switch"
              aria-checked={memoryOnly}
              onClick={() => setMemoryOnly(value => !value)}
              className={`flex items-center gap-2 border px-3 py-1.5 text-[10px] font-mono uppercase tracking-wider transition-colors ${
                memoryOnly
                  ? 'border-[#76B900] bg-[#76B900]/10 text-[#447000] dark:text-[#9ad13a]'
                  : 'border-[#e5e5e5] bg-white text-[#525252] hover:border-[#76B900] dark:bg-[#0a0a0a] dark:border-[#262626] dark:text-[#a3a3a3]'
              }`}
              title="Show records only (notes the Wizard, the REPL or this page saved); hides the seeded documentation"
            >
              <span className={`h-2 w-2 rounded-full ${memoryOnly ? 'bg-[#76B900]' : 'bg-[#a3a3a3] dark:bg-[#525252]'}`} aria-hidden="true" />
              Memory {memoryOnly ? 'on' : 'off'}
            </button>
            <button
              type="button"
              onClick={() => void loadNotes()}
              disabled={notesLoading}
              className="btn-secondary px-3 py-1.5 text-[10px] font-mono uppercase tracking-wider"
            >
              {notesLoading ? 'Loading' : 'Reload notes'}
            </button>
          </div>
        </div>

        <div className="grid gap-3 md:grid-cols-[1fr_auto] md:items-start">
          <input
            type="search"
            value={query}
            onChange={event => setQuery(event.target.value)}
            className="input-base px-3 py-2 text-sm"
            placeholder="Search titles and tags"
            aria-label="Search notes by title or tag"
          />
          {filtersActive && (
            <button type="button" onClick={clearFilters} className="btn-ghost px-3 py-2 text-[10px] font-mono uppercase tracking-wider">
              Clear filters
            </button>
          )}
        </div>

        <div className="space-y-2">
          <div className="flex flex-wrap items-center gap-1.5">
            <span className="text-[10px] font-mono uppercase tracking-wider text-[#737373] dark:text-[#a3a3a3] mr-1">Folder</span>
            <FilterChip label="All" active={filterCategory === ''} onClick={() => setFilterCategory('')} />
            {categories.map(name => (
              <FilterChip
                key={name}
                label={name}
                active={filterCategory === name}
                onClick={() => setFilterCategory(current => (current === name ? '' : name))}
              />
            ))}
          </div>
          {allTags.length > 0 && (
            <div className="flex flex-wrap items-center gap-1.5">
              <span className="text-[10px] font-mono uppercase tracking-wider text-[#737373] dark:text-[#a3a3a3] mr-1">Tag</span>
              {allTags.map(tag => (
                <FilterChip
                  key={tag}
                  label={`#${tag}`}
                  active={filterTag === tag}
                  onClick={() => setFilterTag(current => (current === tag ? '' : tag))}
                />
              ))}
            </div>
          )}
        </div>

        <InlineMessage message={notesError} error />
        {missingId && (
          <InlineMessage message={`No note with id ${missingId} is in the vault (it may have been forgotten).`} error />
        )}

        {notesLoading && notes.length === 0 ? (
          <div className="border border-dashed border-[#e5e5e5] p-6 text-center text-sm text-[#737373] dark:border-[#262626] dark:text-[#a3a3a3]">
            Loading notes… they appear here in a moment, then pick one to open.
          </div>
        ) : notes.length === 0 ? (
          <div className="border border-dashed border-[#e5e5e5] p-6 text-center text-sm text-[#737373] dark:border-[#262626] dark:text-[#a3a3a3]">
            {notesError
              ? 'The note list could not be loaded. Start the API from the console at the top, then press Reload notes.'
              : status?.initialized
                ? 'No notes yet. Save a note from the chat with /save, or add one above.'
                : 'No notes yet. Press Initialize Vault at the top, then save a note from the chat with /save.'}
          </div>
        ) : visibleNotes.length === 0 ? (
          <div className="border border-dashed border-[#e5e5e5] p-6 text-center text-sm text-[#737373] dark:border-[#262626] dark:text-[#a3a3a3]">
            {memoryOnly && !filterCategory && !filterTag && !query.trim()
              ? 'No memory records yet. Only seeded documentation is in the vault; turn Memory off to see it.'
              : 'No notes match these filters. Clear the search or filters to see every note.'}
          </div>
        ) : (
          <ul className="divide-y divide-[#e5e5e5] border border-[#e5e5e5] dark:divide-[#262626] dark:border-[#262626]">
            {visibleNotes.map(note => {
              const open = expanded[note.id];
              const highlighted = highlightId === note.id;
              const created = createdLabel(note.created);
              return (
                <li
                  key={note.id}
                  id={noteAnchorId(note.id)}
                  className={`bg-white p-4 transition-shadow dark:bg-[#0a0a0a] ${
                    highlighted ? 'ring-2 ring-[#76B900] ring-inset' : ''
                  }`}
                >
                  <div className="flex flex-wrap items-start justify-between gap-3">
                    <div className="min-w-0 flex-1">
                      <button
                        type="button"
                        onClick={() => void toggleExpanded(note)}
                        aria-expanded={Boolean(open)}
                        className="text-left font-bold text-[#0a0a0a] hover:text-[#76B900] dark:text-[#fafafa]"
                      >
                        {note.title || '(untitled note)'}
                      </button>
                      <div className="mt-1.5 flex flex-wrap items-center gap-1.5 text-[10px] font-mono text-[#737373] dark:text-[#a3a3a3]">
                        {note.category && <Badge tone="green">{note.category}</Badge>}
                        {!isMemoryRecord(note) ? <Badge>doc</Badge> : note.schema === 'legacy' ? <Badge tone="amber">legacy</Badge> : null}
                        {note.source && <Badge>{note.source}</Badge>}
                        {created && <span title={note.created}>{created}</span>}
                      </div>
                      {Array.isArray(note.tags) && note.tags.length > 0 && (
                        <div className="mt-2 flex flex-wrap gap-1">
                          {note.tags.map(tag => (
                            <button
                              key={tag}
                              type="button"
                              onClick={() => setFilterTag(current => (current === tag ? '' : tag))}
                              aria-pressed={filterTag === tag}
                              className={`text-[9px] font-mono border px-1.5 py-0.5 transition-colors ${
                                filterTag === tag
                                  ? 'text-[#0a0a0a] bg-[#76B900] border-[#76B900]'
                                  : 'text-[#a3a3a3] bg-[#f5f5f5] border-[#e5e5e5] hover:border-[#76B900] dark:bg-[#141414] dark:border-[#262626] dark:text-[#737373]'
                              }`}
                              title={`Filter by #${tag}`}
                            >
                              #{tag}
                            </button>
                          ))}
                        </div>
                      )}
                    </div>
                    <div className="flex shrink-0 items-center gap-2">
                      <button
                        type="button"
                        onClick={() => void toggleExpanded(note)}
                        aria-expanded={Boolean(open)}
                        className="btn-secondary px-3 py-1.5 text-[10px] font-mono uppercase tracking-wider"
                      >
                        {open ? 'Hide' : 'Read'}
                      </button>
                      <button
                        type="button"
                        onClick={() => void handleForget(note)}
                        disabled={forgetting === note.id}
                        className="border border-red-200 bg-white px-3 py-1.5 text-[10px] font-mono uppercase tracking-wider text-red-700 transition-colors hover:border-red-500 disabled:cursor-not-allowed disabled:opacity-40 dark:border-red-900/60 dark:bg-[#0a0a0a] dark:text-red-300"
                      >
                        {forgetting === note.id ? 'Forgetting' : 'Forget'}
                      </button>
                    </div>
                  </div>
                  {open && (
                    <div className="mt-3 border-t border-[#e5e5e5] pt-3 dark:border-[#262626]">
                      {open.state === 'loading' && (
                        <div className="text-xs text-[#737373] dark:text-[#a3a3a3]">Loading the note… it opens here in a moment.</div>
                      )}
                      {open.state === 'error' && (
                        <div className="text-xs text-red-700 dark:text-red-300">{open.error} Close the note and open it again.</div>
                      )}
                      {open.state === 'ok' && (
                        <>
                          <pre className="whitespace-pre-wrap break-words font-sans text-sm text-[#525252] dark:text-[#d4d4d4]">
                            {open.body.trim() || '(empty note)'}
                          </pre>
                          <code className="mt-3 block break-all border border-[#e5e5e5] bg-[#fafafa] px-3 py-2 text-[10px] text-[#737373] dark:bg-[#141414] dark:border-[#262626] dark:text-[#a3a3a3]">
                            {open.path}
                          </code>
                        </>
                      )}
                    </div>
                  )}
                </li>
              );
            })}
          </ul>
        )}

        {notesMeta && notes.length > 0 && (
          <div className="text-[10px] font-mono text-[#737373] dark:text-[#a3a3a3]">
            {visibleNotes.length === notes.length
              ? `${notes.length} ${notes.length === 1 ? 'note' : 'notes'} listed`
              : `${visibleNotes.length} of ${notes.length} listed notes shown`}
            {notesMeta.count >= notesMeta.limit ? ` (the newest ${notesMeta.limit}; older notes stay on disk)` : ''}
          </div>
        )}
      </section>

      <section className="card-flat p-5">
        <div className="flex items-start justify-between gap-4">
          <div>
            <div className="section-label">Conflicts</div>
            <h2 className="text-xl font-bold mt-1">Tradeoffs AI Wizard should explain</h2>
          </div>
        </div>
        <div className="mt-4 grid gap-3 md:grid-cols-2">
          {(status?.conflicts ?? []).map(conflict => (
            <div key={conflict.title} className="border border-[#e5e5e5] bg-white p-4 dark:bg-[#0a0a0a] dark:border-[#262626]">
              <h3 className="font-bold text-[#0a0a0a] dark:text-[#fafafa]">{conflict.title}</h3>
              <p className="mt-1 text-sm text-[#525252] dark:text-[#a3a3a3]">{conflict.summary}</p>
            </div>
          ))}
        </div>
      </section>
      </div>
    </main>
  );
}
