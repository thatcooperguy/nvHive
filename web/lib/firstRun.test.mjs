// Tests for lib/firstRun.ts — the three first-run lines of the Wizard's empty
// state (components/chat/EmptyState.tsx).
//
//     cd web && node --test lib/firstRun.test.mjs

import { test } from 'node:test';
import assert from 'node:assert/strict';

import { firstRunLines } from './firstRun.ts';

const base = { localModel: null, pulling: null, gpu: true, keyCount: 0, starters: [] };

test('a local model on a GPU is ready on your GPU', () => {
  assert.equal(
    firstRunLines({ ...base, localModel: 'llama3.2:3b' }).model,
    'llama3.2:3b is ready on your GPU',
  );
});

test('a local model without a GPU is just ready', () => {
  assert.equal(
    firstRunLines({ ...base, localModel: 'qwen2.5:0.5b', gpu: false }).model,
    'qwen2.5:0.5b is ready',
  );
});

test('a pull in flight names the model and its size in GB', () => {
  assert.equal(
    firstRunLines({ ...base, pulling: { model: 'llama3.1:8b', bytes: 4_920_000_000 } }).model,
    'Pulling llama3.1:8b (4.9 GB) — you can ask the free cloud tier meanwhile',
  );
});

test('a pull with an unknown size leaves the size out', () => {
  assert.equal(
    firstRunLines({ ...base, pulling: { model: 'llama3.1:8b', bytes: 0 } }).model,
    'Pulling llama3.1:8b — you can ask the free cloud tier meanwhile',
  );
});

test('no model and no GPU points at the free cloud tier and Connections', () => {
  assert.equal(
    firstRunLines({ ...base, gpu: false }).model,
    'No GPU here: the free cloud tier answers; add a key in Settings → Connections for more',
  );
});

test('no model on a GPU box points at Models', () => {
  assert.equal(
    firstRunLines(base).model,
    'No local model yet — pick one in Models, or ask the free cloud tier',
  );
});

test('a ready model wins over a pull', () => {
  const lines = firstRunLines({ ...base, localModel: 'a', pulling: { model: 'b', bytes: 1e9 } });
  assert.equal(lines.model, 'a is ready on your GPU');
});

test('starters: first three, trimmed, de-duplicated, blanks dropped', () => {
  const lines = firstRunLines({
    ...base,
    starters: ['  one ', '', 'one', '   ', 'two', 'three', 'four'],
  });
  assert.deepEqual(lines.starters, ['one', 'two', 'three']);
});

test('starters fall back to the built-in three when none is usable', () => {
  const expected = ['What can this machine run?', 'Check my services', 'Summarise a file'];
  assert.deepEqual(firstRunLines(base).starters, expected);
  assert.deepEqual(firstRunLines({ ...base, starters: ['', '  '] }).starters, expected);
});

test('one starter is kept as is (no padding from the fallback)', () => {
  assert.deepEqual(firstRunLines({ ...base, starters: ['Only one'] }).starters, ['Only one']);
});

test('the third line says where keys, ports and packs live', () => {
  assert.equal(firstRunLines(base).later, 'Keys, ports and packs live in Settings → Get started');
});
