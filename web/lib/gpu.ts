/**
 * GPU payload helpers — pure functions over the /v1/system/gpu payload that
 * decide what the UI says about a machine's GPUs. No JSX here so the module
 * runs under plain `node --test lib/gpu.test.mjs`; the rendering pieces
 * (UnifiedMemoryTag, MemoryUnreadableTag, GpuBlockedSummary) live in
 * components/UnifiedMemoryTag.tsx, which re-exports these for convenience.
 *
 * Mirrors nvh/utils/gpu.py: `primaryGpu` is `_primary_row`, `unreadableGpus`
 * is `_unsized_rows`, and `isGpuReady` is the `detect_gpu_status` verdict —
 * 'ready' when at least one row has a readable memory pool. The API sizes
 * models against the first *sized* row, so every UI decision about "the GPU"
 * must key off that row too, never `gpus[0]`: when GPU 0 is the unreadable one
 * and GPU 1 is sized, the API is 'ready' on GPU 1 while gpus[0] reads
 * "memory unreadable".
 */

import type { GPUDevice, SystemRAM } from './types';

/** The two fields the readability predicate needs; lets tests pass minimal rows. */
type MemoryRow = Pick<GPUDevice, 'memory_unreadable' | 'vram_mb'>;

/**
 * True when the machine's memory is one CPU/GPU-shared pool (GB10 / DGX Spark).
 * Reads the system_ram flag first (the API sets it when any GPU is unified) and
 * falls back to the per-device flag so payloads that predate it still classify
 * right. Pages use this to stop advertising "N GB usable for CPU offload" —
 * on a unified pool there is no second pool to offload into.
 */
export function isUnifiedMemoryPool(
  info: { gpus?: GPUDevice[] | null; system_ram?: Partial<SystemRAM> | null } | null | undefined,
): boolean {
  if (!info) return false;
  if (info.system_ram?.unified_memory) return true;
  return (info.gpus ?? []).some(g => g.unified_memory === true);
}

/**
 * Detection verdict for a /v1/system/gpu payload. Prefers the top-level
 * `status` the API emits and falls back to `detection.status` for builds that
 * predate it; both carry the same value. `undefined` when neither is present.
 */
export function gpuStatusOf(
  info: { status?: string | null; detection?: { status?: string | null } | null } | null | undefined,
): string | undefined {
  return info?.status ?? info?.detection?.status ?? undefined;
}

/**
 * True when a GPU row is visible but its memory pool could not be read. Trusts
 * the API's `memory_unreadable` flag when present; otherwise applies the same
 * predicate the API uses to mark a row unsized (vram_mb <= 0), so payloads
 * that predate the flag still stop rendering "0 GB". A readable row always
 * has vram_mb > 0, so this never changes how discrete or unified rows render.
 */
export function isMemoryUnreadable(g: MemoryRow): boolean {
  if (typeof g.memory_unreadable === 'boolean') return g.memory_unreadable;
  return !(g.vram_mb > 0);
}

/**
 * The row every primary-GPU decision keys on — the API's `_primary_row`: the
 * first row with a readable memory pool, else the first row (a lone unreadable
 * GPU still has a real name and architecture), else null. Identical to
 * `gpus[0]` whenever there is a single row, so single-GPU rendering is
 * unchanged; differs exactly when GPU 0 is unreadable and a later row is
 * sized — the case where the API is 'ready' and sizing against that later row.
 */
export function primaryGpu<G extends MemoryRow>(gpus: G[] | null | undefined): G | null {
  const rows = gpus ?? [];
  return rows.find(g => !isMemoryUnreadable(g)) ?? rows[0] ?? null;
}

/** Rows whose memory pool could not be read — the API's `_unsized_rows`. Nothing may budget against them. */
export function unreadableGpus<G extends MemoryRow>(gpus: G[] | null | undefined): G[] {
  return (gpus ?? []).filter(g => isMemoryUnreadable(g));
}

/**
 * Short clause naming the unreadable rows for a one-line status —
 * "GPU 0 memory unreadable" / "GPUs 0, 2 memory unreadable" — or '' when every
 * row is sized, so single-GPU strings are untouched. Indices are the driver's
 * `index`, the same number the API's `summary` and issues use.
 */
export function describeUnreadableGpus(
  gpus: Array<MemoryRow & Pick<GPUDevice, 'index'>> | null | undefined,
): string {
  const rows = unreadableGpus(gpus);
  if (rows.length === 0) return '';
  const noun = rows.length === 1 ? 'GPU' : 'GPUs';
  return `${noun} ${rows.map(g => g.index).join(', ')} memory unreadable`;
}

/**
 * The detection verdict as a boolean. Trusts the API's status when the payload
 * carries one ('ready' iff at least one row is sized — a secondary unreadable
 * row does not make the machine un-ready, the API still sizes against the
 * primary row) and applies that same rule to the rows for payloads that
 * predate the field. False for a missing payload or an empty GPU list.
 */
export function isGpuReady(
  info: {
    gpus?: GPUDevice[] | null;
    status?: string | null;
    detection?: { status?: string | null } | null;
  } | null | undefined,
): boolean {
  if (!info) return false;
  const status = gpuStatusOf(info);
  if (status !== undefined) return status === 'ready';
  const primary = primaryGpu(info.gpus);
  return primary !== null && !isMemoryUnreadable(primary);
}
