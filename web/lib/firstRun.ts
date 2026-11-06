// Pure rules behind the Wizard's empty state: the three first-run lines (the
// local model, the starters, where keys and packs live) derived from the
// machine's state. No DOM, no `@/lib/api`; components pass the status in.

export interface FirstRunStatus {
  /** A local model that is installed and usable, or null. */
  localModel: string | null;
  /** A running pull of a local model, or null. */
  pulling: { model: string; bytes: number } | null;
  /** True when a GPU was detected. */
  gpu: boolean;
  /** Provider keys configured; reserved for callers that know it. */
  keyCount: number;
  /** Starter prompts, e.g. each agent profile's `starter`. */
  starters: string[];
}

export interface FirstRunLines {
  model: string;
  starters: string[];
  later: string;
}

export const FALLBACK_STARTERS: readonly string[] = [
  'What can this machine run?',
  'Check my services',
  'Summarise a file',
];

export const FIRST_RUN_LATER = 'Keys, ports and packs live in Settings → Get started';

/** First three non-empty starters, trimmed and de-duplicated; the fallback list when none. */
export function pickStarters(starters: readonly string[]): string[] {
  const out: string[] = [];
  for (const raw of starters) {
    const s = typeof raw === 'string' ? raw.trim() : '';
    if (s && !out.includes(s)) out.push(s);
    if (out.length === 3) break;
  }
  return out.length > 0 ? out : [...FALLBACK_STARTERS];
}

function gigabytes(bytes: number): string {
  const n = Number.isFinite(bytes) && bytes > 0 ? bytes : 0;
  return (n / 1e9).toFixed(1);
}

export function firstRunLines(status: FirstRunStatus): FirstRunLines {
  let model: string;
  if (status.localModel) {
    model = status.gpu
      ? `${status.localModel} is ready on your GPU`
      : `${status.localModel} is ready`;
  } else if (status.pulling) {
    // A size the job does not carry is left out rather than printed as 0.0 GB.
    const size = status.pulling.bytes > 0 ? ` (${gigabytes(status.pulling.bytes)} GB)` : '';
    model = `Pulling ${status.pulling.model}${size} — you can ask the free cloud tier meanwhile`;
  } else if (!status.gpu) {
    model = 'No GPU here: the free cloud tier answers; add a key in Settings → Connections for more';
  } else {
    model = 'No local model yet — pick one in Models, or ask the free cloud tier';
  }
  return { model, starters: pickStarters(status.starters), later: FIRST_RUN_LATER };
}
