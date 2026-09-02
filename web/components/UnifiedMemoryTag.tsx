/**
 * GPU memory tags — the small pills and one-liners shown next to a GPU row's
 * memory figure, plus the payload helpers that decide which one applies.
 *
 * UnifiedMemoryTag — compact "unified" pill shown next to a VRAM figure when
 * GPUDevice.unified_memory is true. On GB10 / DGX Spark the number is a
 * CPU/GPU-shared LPDDR5x pool, not dedicated VRAM, and the OS lives in it too;
 * the tag keeps "128 GB" from reading as 128 GB of headroom. Renders nothing
 * for discrete GPUs so existing layouts are untouched.
 *
 * MemoryUnreadableTag — replaces the "used / total GB" figure when the driver
 * enumerated a GPU but reported no memory pool (GPUDevice.memory_unreadable).
 * The API keeps such rows at 0 MiB so the platform classifier still sees the
 * GPU's name; rendering that as "0 GB VRAM" would present an unknown as a
 * figure. GpuBlockedSummary is the matching one-line explanation, shown once
 * under a GPU list when the detection status is 'blocked' — or when one row is
 * unreadable beside sized ones, since the API names that row only in `summary`.
 *
 * The payload helpers (primaryGpu, isMemoryUnreadable, gpuStatusOf, …) live in
 * lib/gpu.ts so they run under `node --test` without JSX; they are re-exported
 * here so pages keep one import for everything about GPU memory tags.
 */

import type { GPUDevice } from '@/lib/types';
import { unreadableGpus } from '@/lib/gpu';

export {
  describeUnreadableGpus,
  gpuStatusOf,
  isGpuReady,
  isMemoryUnreadable,
  isUnifiedMemoryPool,
  primaryGpu,
  unreadableGpus,
} from '@/lib/gpu';

interface Props {
  show?: boolean;
  className?: string;
}

export default function UnifiedMemoryTag({ show, className = '' }: Props) {
  if (!show) return null;
  return (
    <span
      className={`inline-block rounded border px-1 align-middle text-[9px] font-mono uppercase tracking-[0.12em] ${className}`.trim()}
      style={{ borderColor: 'var(--border)', color: 'var(--text-muted)' }}
      title="Unified memory: one pool shared by the CPU, GPU and OS (GB10 / DGX Spark)."
    >
      unified
    </span>
  );
}

interface MemoryUnreadableTagProps {
  /** The payload's one-line summary; shown as the tooltip so the reason is one hover away. */
  summary?: string | null;
  className?: string;
}

/** Muted "memory unreadable" in place of a used/total figure the driver never reported. */
export function MemoryUnreadableTag({ summary, className = '' }: MemoryUnreadableTagProps) {
  return (
    <span
      className={`font-mono ${className}`.trim()}
      style={{ color: 'var(--text-muted)' }}
      title={summary || 'The driver listed this GPU but reported no memory figures.'}
    >
      memory unreadable
    </span>
  );
}

interface GpuBlockedSummaryProps {
  status?: string | null;
  summary?: string | null;
  /**
   * The payload rows. When given, the summary also renders for a partial
   * failure — status 'ready' on the sized rows while another row's memory is
   * unreadable — because the API names that row only in `summary`. Omitted, or
   * with every row sized, nothing changes.
   */
  gpus?: GPUDevice[] | null;
  className?: string;
}

/**
 * The payload summary, rendered once under a GPU list when detection is
 * 'blocked' (no row sized) or when `gpus` holds an unreadable row beside sized
 * ones (a 'ready' machine with one bad GPU). Nothing otherwise.
 */
export function GpuBlockedSummary({ status, summary, gpus, className = '' }: GpuBlockedSummaryProps) {
  const partial = unreadableGpus(gpus).length > 0;
  if ((status !== 'blocked' && !partial) || !summary) return null;
  return (
    <div
      className={`text-[10px] font-mono leading-snug ${className}`.trim()}
      style={{ color: 'var(--text-muted)' }}
    >
      {summary}
    </div>
  );
}
