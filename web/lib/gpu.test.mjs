// Regression tests for lib/gpu.ts — the primary-GPU rule the UI shares with
// nvh/utils/gpu.py (_primary_row / _unsized_rows / detect_gpu_status).
//
// Runs on Node's built-in runner with native type stripping (Node >= 22.6 with
// --experimental-strip-types, unflagged from 23.6 / 24); no test framework in web/:
//
//     cd web && node --test lib/gpu.test.mjs
//
// This file is .mjs so tsc (include: **/*.ts, **/*.tsx) and `next build` leave
// it alone, while Node can import the .ts module by explicit extension.

import { test } from 'node:test';
import assert from 'node:assert/strict';

import {
  describeUnreadableGpus,
  gpuStatusOf,
  isGpuReady,
  isMemoryUnreadable,
  isUnifiedMemoryPool,
  primaryGpu,
  unreadableGpus,
} from './gpu.ts';

/** A GPUDevice row with only the fields the helpers read; `vram_mb` 0 marks the API's unreadable shape. */
function row(index, vram_mb, extra = {}) {
  const unreadable = vram_mb <= 0;
  return {
    index,
    name: `NVIDIA A100 80GB PCIe`,
    vram_mb,
    vram_gb: Math.round(vram_mb / 1024),
    memory_unreadable: unreadable,
    memory_used_mb: 0,
    memory_free_mb: unreadable ? 0 : vram_mb,
    utilization_pct: 0,
    driver_version: '560.35',
    cuda_version: '12.6',
    ...extra,
  };
}

const SIZED_0 = row(0, 81920);
const SIZED_1 = row(1, 81920);
const UNREADABLE_0 = row(0, 0);
const UNREADABLE_1 = row(1, 0);
const UNREADABLE_2 = row(2, 0);

// ─── primaryGpu — the E1 mechanism ────────────────────────────────────────────

test('primaryGpu: no payload / no rows → null', () => {
  assert.equal(primaryGpu(null), null);
  assert.equal(primaryGpu(undefined), null);
  assert.equal(primaryGpu([]), null);
});

test('primaryGpu: single row is gpus[0] whether sized or unreadable (byte-identical single-GPU rendering)', () => {
  assert.equal(primaryGpu([SIZED_0]), SIZED_0);
  // A lone unreadable GPU still names its architecture — it stays primary, as in _primary_row.
  assert.equal(primaryGpu([UNREADABLE_0]), UNREADABLE_0);
});

test('primaryGpu: GPU 0 unreadable, GPU 1 sized → GPU 1 (E1: the row the API sizes against)', () => {
  assert.equal(primaryGpu([UNREADABLE_0, SIZED_1]), SIZED_1);
});

test('primaryGpu: GPU 0 sized, GPU 1 unreadable → GPU 0 (order among sized rows is preserved)', () => {
  assert.equal(primaryGpu([SIZED_0, UNREADABLE_1]), SIZED_0);
  assert.equal(primaryGpu([SIZED_0, SIZED_1]), SIZED_0);
});

test('primaryGpu: all rows unreadable → first row (same fallback as _primary_row)', () => {
  assert.equal(primaryGpu([UNREADABLE_0, UNREADABLE_1]), UNREADABLE_0);
});

test('primaryGpu: payloads without memory_unreadable fall back to vram_mb <= 0', () => {
  const legacyUnreadable = { index: 0, vram_mb: 0 };
  const legacySized = { index: 1, vram_mb: 24576 };
  assert.equal(primaryGpu([legacyUnreadable, legacySized]), legacySized);
});

// ─── unreadableGpus / describeUnreadableGpus ─────────────────────────────────

test('unreadableGpus: returns the unsized rows in order, [] when none', () => {
  assert.deepEqual(unreadableGpus([SIZED_0, SIZED_1]), []);
  assert.deepEqual(unreadableGpus([UNREADABLE_0, SIZED_1, UNREADABLE_2]), [UNREADABLE_0, UNREADABLE_2]);
  assert.deepEqual(unreadableGpus(null), []);
});

test("describeUnreadableGpus: '' when every row is sized, so single-GPU strings are untouched", () => {
  assert.equal(describeUnreadableGpus([SIZED_0]), '');
  assert.equal(describeUnreadableGpus([SIZED_0, SIZED_1]), '');
  assert.equal(describeUnreadableGpus(undefined), '');
});

test('describeUnreadableGpus: names the bad rows by driver index', () => {
  assert.equal(describeUnreadableGpus([UNREADABLE_0, SIZED_1]), 'GPU 0 memory unreadable');
  assert.equal(describeUnreadableGpus([UNREADABLE_0, SIZED_1, UNREADABLE_2]), 'GPUs 0, 2 memory unreadable');
});

// ─── isGpuReady — the mission-row state ───────────────────────────────────────

test("isGpuReady: trusts the API's status — 'ready' with an unreadable GPU 0 is still ready", () => {
  const partial = { status: 'ready', gpus: [UNREADABLE_0, SIZED_1], summary: '1 of 2 GPUs ready' };
  assert.equal(isGpuReady(partial), true);
  assert.equal(isGpuReady({ status: 'blocked', gpus: [UNREADABLE_0] }), false);
  assert.equal(isGpuReady({ status: 'not-detected', gpus: [] }), false);
  // detection.status is honoured when the top-level field is absent.
  assert.equal(isGpuReady({ detection: { status: 'ready' }, gpus: [UNREADABLE_0, SIZED_1] }), true);
});

test('isGpuReady: without a status, applies the API rule (some row sized) to the rows', () => {
  assert.equal(isGpuReady({ gpus: [UNREADABLE_0, SIZED_1] }), true);
  assert.equal(isGpuReady({ gpus: [SIZED_0] }), true);
  assert.equal(isGpuReady({ gpus: [UNREADABLE_0] }), false);
  assert.equal(isGpuReady({ gpus: [] }), false);
  assert.equal(isGpuReady(null), false);
});

// ─── moved helpers keep their behaviour ──────────────────────────────────────

test('isMemoryUnreadable: flag wins, then vram_mb <= 0', () => {
  assert.equal(isMemoryUnreadable({ memory_unreadable: true, vram_mb: 8192 }), true);
  assert.equal(isMemoryUnreadable({ memory_unreadable: false, vram_mb: 0 }), false);
  assert.equal(isMemoryUnreadable({ vram_mb: 0 }), true);
  assert.equal(isMemoryUnreadable({ vram_mb: 8192 }), false);
});

test('gpuStatusOf: top-level status, then detection.status, else undefined', () => {
  assert.equal(gpuStatusOf({ status: 'ready', detection: { status: 'blocked' } }), 'ready');
  assert.equal(gpuStatusOf({ detection: { status: 'blocked' } }), 'blocked');
  assert.equal(gpuStatusOf({}), undefined);
  assert.equal(gpuStatusOf(null), undefined);
});

test('isUnifiedMemoryPool: system_ram flag first, then any unified row', () => {
  assert.equal(isUnifiedMemoryPool({ system_ram: { unified_memory: true }, gpus: [] }), true);
  assert.equal(isUnifiedMemoryPool({ gpus: [row(0, 131072, { unified_memory: true })] }), true);
  assert.equal(isUnifiedMemoryPool({ gpus: [SIZED_0] }), false);
  assert.equal(isUnifiedMemoryPool(null), false);
});
