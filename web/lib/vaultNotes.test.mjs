// Regression tests for lib/vaultNotes.ts — the pure helpers behind the
// /vault note list (app/vault/page.tsx, 0.46 §2): the client-side filter
// that mirrors GET /v1/vault/notes, the category roster derived from the
// server's `directories` block (never hand-typed), the tag roster, the
// deep-link anchor id and the two message formatters.
//
// Runs on Node's built-in runner with native type stripping (Node >= 22.6 with
// --experimental-strip-types, unflagged from 23.6 / 24); no test framework in web/:
//
//     cd web && node --test lib/vaultNotes.test.mjs
//
// This file is .mjs so tsc (include: **/*.ts, **/*.tsx) and `next build` leave
// it alone, while Node can import the .ts module by explicit extension.

import { test } from 'node:test';
import assert from 'node:assert/strict';

import {
  DEFAULT_MEMORY_CATEGORY,
  DOC_SCHEMA,
  categoriesFromStatus,
  createdLabel,
  filterNotes,
  forgetOutcomeMessage,
  isMemoryRecord,
  noteAnchorId,
  normalizeTag,
  savedMemoryMessage,
  tagsFromNotes,
} from './vaultNotes.ts';

function note(overrides = {}) {
  return {
    id: 'a'.repeat(32),
    schema: 'nvh-memory/1',
    title: 'ComfyUI lives on the second NVMe',
    created: '2026-11-01T12:00:00Z',
    category: 'Wizard Memory',
    tags: ['wizard', 'comfyui'],
    source: 'wizard',
    path: '/home/me/.nvh/vault/Wizard Memory/2026-11-01T120000Z-comfyui.md',
    ...overrides,
  };
}

const RECORD = note();
const LEGACY = note({ id: 'b'.repeat(32), schema: 'legacy', title: 'Old pilot note', category: 'Projects', tags: ['pilot'], source: 'import', created: '2026-01-05T08:30:00Z' });
const DOC = note({ id: 'c'.repeat(32), schema: DOC_SCHEMA, title: 'System overview', category: 'System', tags: ['moc'], source: '', created: '' });
const DECISION = note({ id: 'd'.repeat(32), title: 'Keep Py3.11', category: 'Decisions', tags: ['Decision', '#audit'], source: 'audit', created: '2026-10-20T09:00:00Z' });
const ALL = [RECORD, LEGACY, DOC, DECISION];

test('isMemoryRecord keeps both record schemas and drops seeded docs', () => {
  assert.equal(isMemoryRecord(RECORD), true);
  assert.equal(isMemoryRecord(LEGACY), true);
  assert.equal(isMemoryRecord(DOC), false);
  // An unfamiliar future schema is still a record — only `doc` is documentation.
  assert.equal(isMemoryRecord({ schema: 'nvh-memory/2' }), true);
});

test('normalizeTag strips the hash, whitespace and case like the server does', () => {
  assert.equal(normalizeTag(' #Wizard '), 'wizard');
  assert.equal(normalizeTag('##two'), 'two');
  assert.equal(normalizeTag(undefined), '');
  assert.equal(normalizeTag(null), '');
});

test('filterNotes with no filters returns every note in the server order', () => {
  assert.deepEqual(filterNotes(ALL), ALL);
  assert.deepEqual(filterNotes(ALL, {}), ALL);
  assert.deepEqual(filterNotes(ALL, { category: '', tag: '', q: '', memoryOnly: false }), ALL);
});

test('memoryOnly drops doc notes and keeps nvh-memory/1 and legacy records', () => {
  assert.deepEqual(filterNotes(ALL, { memoryOnly: true }).map(n => n.id), [RECORD.id, LEGACY.id, DECISION.id]);
});

test('category is an exact folder match', () => {
  assert.deepEqual(filterNotes(ALL, { category: 'Decisions' }).map(n => n.id), [DECISION.id]);
  assert.deepEqual(filterNotes(ALL, { category: 'decisions' }), []);
  assert.deepEqual(filterNotes(ALL, { category: 'Wizard Memory' }).map(n => n.id), [RECORD.id]);
});

test('tag filter tolerates a leading hash and case on both sides', () => {
  assert.deepEqual(filterNotes(ALL, { tag: '#COMFYUI' }).map(n => n.id), [RECORD.id]);
  assert.deepEqual(filterNotes(ALL, { tag: 'decision' }).map(n => n.id), [DECISION.id]);
  assert.deepEqual(filterNotes(ALL, { tag: 'audit' }).map(n => n.id), [DECISION.id]);
  assert.deepEqual(filterNotes(ALL, { tag: 'nope' }), []);
});

test('q matches the title and the tags case-insensitively', () => {
  assert.deepEqual(filterNotes(ALL, { q: 'nvme' }).map(n => n.id), [RECORD.id]);
  assert.deepEqual(filterNotes(ALL, { q: 'PILOT' }).map(n => n.id), [LEGACY.id]);
  assert.deepEqual(filterNotes(ALL, { q: '  moc ' }).map(n => n.id), [DOC.id]);
  assert.deepEqual(filterNotes(ALL, { q: 'nothing matches this' }), []);
});

test('filters combine with AND', () => {
  assert.deepEqual(filterNotes(ALL, { memoryOnly: true, category: 'System' }), []);
  assert.deepEqual(filterNotes(ALL, { category: 'Wizard Memory', tag: 'wizard', q: 'comfy' }).map(n => n.id), [RECORD.id]);
  assert.deepEqual(filterNotes(ALL, { category: 'Wizard Memory', tag: 'pilot' }), []);
  assert.deepEqual(filterNotes(ALL, { category: 'Projects', tag: 'pilot' }).map(n => n.id), [LEGACY.id]);
});

test('filterNotes survives a note with no tags array', () => {
  const bare = note({ id: 'e'.repeat(32), tags: undefined });
  assert.deepEqual(filterNotes([bare], { q: 'nvme' }), [bare]);
  assert.deepEqual(filterNotes([bare], { tag: 'wizard' }), []);
});

test('categoriesFromStatus uses the server directories in their own order', () => {
  const status = { directories: { System: '/v/System', Models: '/v/Models', 'Wizard Memory': '/v/Wizard Memory' } };
  assert.deepEqual(categoriesFromStatus(status), ['System', 'Models', 'Wizard Memory']);
});

test('categoriesFromStatus appends folders only the notes know, sorted, without duplicates', () => {
  const status = { directories: { 'Wizard Memory': '/v/Wizard Memory' } };
  const notes = [DECISION, DOC, RECORD, note({ category: 'Zettel' }), note({ category: '' })];
  assert.deepEqual(categoriesFromStatus(status, notes), ['Wizard Memory', 'Decisions', 'System', 'Zettel']);
});

test('categoriesFromStatus falls back to the API default only when nothing else is known', () => {
  assert.deepEqual(categoriesFromStatus(null), [DEFAULT_MEMORY_CATEGORY]);
  assert.deepEqual(categoriesFromStatus(undefined, []), [DEFAULT_MEMORY_CATEGORY]);
  assert.deepEqual(categoriesFromStatus({ directories: {} }, [DECISION]), ['Decisions']);
  assert.equal(DEFAULT_MEMORY_CATEGORY, 'Wizard Memory');
});

test('tagsFromNotes is normalised, unique and sorted', () => {
  assert.deepEqual(tagsFromNotes(ALL), ['audit', 'comfyui', 'decision', 'moc', 'pilot', 'wizard']);
  assert.deepEqual(tagsFromNotes([]), []);
  assert.deepEqual(tagsFromNotes([note({ tags: undefined }), note({ tags: ['', ' #A ', 'a'] })]), ['a']);
});

test('noteAnchorId is stable and selector-safe', () => {
  assert.equal(noteAnchorId('a'.repeat(32)), `vault-note-${'a'.repeat(32)}`);
  assert.equal(noteAnchorId('with space/and.dots'), 'vault-note-with-space-and-dots');
  assert.equal(noteAnchorId(''), 'vault-note-unknown');
});

test('createdLabel trims an ISO stamp to the minute and leaves odd values alone', () => {
  assert.equal(createdLabel('2026-11-01T12:00:00Z'), '2026-11-01 12:00');
  assert.equal(createdLabel('2026-11-01 07:05:59'), '2026-11-01 07:05');
  assert.equal(createdLabel(''), '');
  assert.equal(createdLabel(undefined), '');
  assert.equal(createdLabel('yesterday'), 'yesterday');
});

test('forgetOutcomeMessage reports a dropped index entry in plain words', () => {
  assert.equal(forgetOutcomeMessage('Old pilot note', { deleted: true, forgotten: true }), 'Forgot "Old pilot note".');
  assert.equal(
    forgetOutcomeMessage('Old pilot note', { deleted: true, forgotten: false }),
    'Forgot "Old pilot note" (file removed; its search index entry could not be dropped).',
  );
  assert.equal(forgetOutcomeMessage('', { deleted: false, forgotten: false }), 'Could not forget "the note".');
});

test('savedMemoryMessage names the folder, the redactions and a lagging index', () => {
  assert.equal(savedMemoryMessage({ title: 'A', category: 'Wizard Memory' }), 'Saved "A" to Wizard Memory.');
  assert.equal(
    savedMemoryMessage({ title: 'A', category: 'Decisions', redacted: 1, indexed: true }),
    'Saved "A" to Decisions. 1 secret-looking span was redacted before the write.',
  );
  assert.equal(
    savedMemoryMessage({ title: ' ', category: 'Wizard Memory', redacted: 2, indexed: false }),
    'Saved "memory" to Wizard Memory. 2 secret-looking spans were redacted before the write. Not yet searchable from the Wizard (the index will catch up).',
  );
});
