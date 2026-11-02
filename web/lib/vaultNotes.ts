/**
 * Pure helpers behind the /vault note list (app/vault/page.tsx) — 0.46 §2.
 *
 * The page loads the newest headers once (`listVaultNotes({ limit })`) and
 * narrows them here, so a chip click or a keystroke in the search box never
 * waits on the API. The rules mirror the server's `list_vault_notes`
 * (nvh/integrations/workspace/vault.py): a category is an exact folder
 * match, a tag is one slug (a leading `#` is tolerated), the search needle is
 * case-insensitive over the title and the tags (the server also reads the
 * opening lines of the body, which a header-only list does not carry), and
 * "memory only" drops `doc` notes — seeded documentation — and keeps every
 * record (`nvh-memory/1` and pre-0.46 `legacy` notes alike).
 *
 * No React, DOM or path alias here: lib/vaultNotes.test.mjs imports this
 * module directly under Node's type stripping.
 */

import type { VaultNote, VaultStatus } from './types';

/** The `schema` of a seeded or frontmatter note: documentation, not a record. */
export const DOC_SCHEMA = 'doc';

/**
 * The folder the API writes to when a save names no category
 * (`POST /v1/vault/memory` defaults to it, and the Wizard's `remember` tool
 * is pinned to it). Used only as the last-resort option when the status
 * carries no `directories` block and no note has been listed yet.
 */
export const DEFAULT_MEMORY_CATEGORY = 'Wizard Memory';

export interface NoteFilters {
  /** Exact vault folder name; '' or undefined means every folder. */
  category?: string;
  /** One tag slug; a leading `#` and letter case are ignored. */
  tag?: string;
  /** Free text matched case-insensitively against the title and the tags. */
  q?: string;
  /** Records only: drop `doc` notes. */
  memoryOnly?: boolean;
}

/** True for a record (`nvh-memory/1`, `legacy`, or any schema that is not `doc`). */
export function isMemoryRecord(note: Pick<VaultNote, 'schema'>): boolean {
  return note.schema !== DOC_SCHEMA;
}

/** One tag the way the server stores it: trimmed, no leading `#`, lower-case. */
export function normalizeTag(tag: string | null | undefined): string {
  return (tag ?? '').trim().replace(/^#+/, '').toLowerCase();
}

/**
 * Narrow a header list the way `GET /v1/vault/notes?category&tag&q&memory_only`
 * would. Order is preserved (the API lists newest first).
 */
export function filterNotes(notes: readonly VaultNote[], filters: NoteFilters = {}): VaultNote[] {
  const category = (filters.category ?? '').trim();
  const tag = normalizeTag(filters.tag);
  const needle = (filters.q ?? '').trim().toLowerCase();
  const memoryOnly = filters.memoryOnly === true;
  return notes.filter(note => {
    if (memoryOnly && !isMemoryRecord(note)) return false;
    if (category && note.category !== category) return false;
    const tags = Array.isArray(note.tags) ? note.tags.map(normalizeTag) : [];
    if (tag && !tags.includes(tag)) return false;
    if (needle) {
      const haystack = `${note.title ?? ''}\n${tags.join(' ')}`.toLowerCase();
      if (!haystack.includes(needle)) return false;
    }
    return true;
  });
}

/**
 * The category options for the chips and the save form, never hand-typed:
 * the vault's folders from `VaultStatus.directories` (the server's own
 * `VAULT_DIRS` roster) in the server's order, then any folder a listed note
 * sits in that the status did not name (an older server without
 * `directories`, or a folder someone created by hand). An empty vault with
 * no status block falls back to the API's default category alone.
 */
export function categoriesFromStatus(
  status: Pick<VaultStatus, 'directories'> | null | undefined,
  notes: readonly Pick<VaultNote, 'category'>[] = [],
): string[] {
  const seen = new Set<string>();
  const out: string[] = [];
  for (const name of Object.keys(status?.directories ?? {})) {
    const trimmed = name.trim();
    if (trimmed && !seen.has(trimmed)) {
      seen.add(trimmed);
      out.push(trimmed);
    }
  }
  const extra: string[] = [];
  for (const note of notes) {
    const name = (note.category ?? '').trim();
    if (name && !seen.has(name)) {
      seen.add(name);
      extra.push(name);
    }
  }
  extra.sort((a, b) => a.localeCompare(b));
  out.push(...extra);
  return out.length > 0 ? out : [DEFAULT_MEMORY_CATEGORY];
}

/** Every tag across the listed notes, normalised, unique and sorted. */
export function tagsFromNotes(notes: readonly Pick<VaultNote, 'tags'>[]): string[] {
  const seen = new Set<string>();
  for (const note of notes) {
    if (!Array.isArray(note.tags)) continue;
    for (const raw of note.tags) {
      const tag = normalizeTag(raw);
      if (tag) seen.add(tag);
    }
  }
  return Array.from(seen).sort((a, b) => a.localeCompare(b));
}

/**
 * The DOM id of a note row, so `/vault?id=<id>` can scroll to it. Ids are
 * 32 hex characters from the server, but anything outside `[A-Za-z0-9_-]`
 * is replaced so a stray value cannot produce an invalid selector.
 */
export function noteAnchorId(id: string): string {
  const safe = (id ?? '').replace(/[^A-Za-z0-9_-]/g, '-');
  return `vault-note-${safe || 'unknown'}`;
}

/**
 * `created` as the list shows it: an ISO-8601 UTC stamp becomes
 * `YYYY-MM-DD HH:MM`; a `doc` note's empty stamp stays empty; anything else
 * is shown verbatim rather than guessed at.
 */
export function createdLabel(created: string | null | undefined): string {
  const raw = (created ?? '').trim();
  if (!raw) return '';
  const match = /^(\d{4}-\d{2}-\d{2})[T ](\d{2}):(\d{2})/.exec(raw);
  if (!match) return raw;
  return `${match[1]} ${match[2]}:${match[3]}`;
}

/** What `DELETE /v1/vault/notes/{id}` reports back, as the page phrases it. */
export function forgetOutcomeMessage(
  title: string,
  result: { deleted: boolean; forgotten: boolean },
): string {
  const name = title.trim() || 'the note';
  if (!result.deleted) return `Could not forget "${name}".`;
  if (!result.forgotten) {
    return `Forgot "${name}" (file removed; its search index entry could not be dropped).`;
  }
  return `Forgot "${name}".`;
}

/**
 * The saved-memory line after the form's save: names the folder, the
 * redaction count when the writer stripped something, and whether the note
 * is already searchable. The caller appends the `/vault?id=` link.
 */
export function savedMemoryMessage(result: {
  title: string;
  category: string;
  redacted?: number;
  indexed?: boolean;
}): string {
  const title = result.title.trim() || 'memory';
  const parts = [`Saved "${title}" to ${result.category}.`];
  const redacted = typeof result.redacted === 'number' ? result.redacted : 0;
  if (redacted > 0) {
    parts.push(`${redacted} secret-looking ${redacted === 1 ? 'span was' : 'spans were'} redacted before the write.`);
  }
  if (result.indexed === false) {
    parts.push('Not yet searchable from the Wizard (the index will catch up).');
  }
  return parts.join(' ');
}
