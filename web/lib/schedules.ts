/**
 * Pure helpers behind /schedules (web/app/schedules/page.tsx) — 0.46 §6.
 *
 * The table labels (interval, next run, last status, display name), the
 * `every` validator that mirrors the server's `parse_interval` syntax so the
 * form can refuse early (the server still validates), the runner status line
 * read from `GET /v1/schedules → runner`, and the reader that turns a
 * `schedule-run` job's events into one outcome row for the run log.
 *
 * No React, no DOM, no `@/` alias and no runtime import, so Node's type
 * stripping can run lib/schedules.test.mjs against this file directly:
 * `import type` lines are erased by both Node and tsc, while a runtime
 * `./cost` import would fail under Node (no extension resolution) and
 * `./cost.ts` is refused by tsc (allowImportingTsExtensions is off). The two
 * cost helpers therefore take the pill function (`costPill` from lib/cost.ts)
 * as an argument instead of importing it.
 */

import type { CostPill } from './cost';
import type { InstallJob, InstallJobEvent, Schedule, ScheduleRunEvent, ScheduleRunner } from './types';

/** `costPill` from lib/cost.ts, or anything with its first two parameters. */
export type CostPillFn = (costUsd: unknown, costKnown: boolean | undefined) => CostPill | null;

/** Colour family for a status word; the page maps it to its Tailwind classes. */
export type StatusTone = 'green' | 'amber' | 'red' | 'gray';

// ─── Intervals ───────────────────────────────────────────────────────────────

/** Placeholder for the `every` input: the examples the CLI documents. */
export const EVERY_PLACEHOLDER = '30m | 1h | 1d';

/** The one-line rule behind `validateEvery`'s errors. */
export const EVERY_HINT = 'a whole number followed by s, m, h or d, such as 30m, 1h or 1d';

/** `POST /v1/schedules` caps `every` at this many characters. */
export const EVERY_MAX_CHARS = 16;

const UNIT_SECONDS: Record<string, number> = { s: 1, m: 60, h: 3600, d: 86400 };
// The server's `parse_interval`: ^(\d+)(s|m|h|d)$ after trim.
const EVERY_RE = /^(\d+)([smhd])$/;

export type EveryCheck =
  | { ok: true; seconds: number; normalized: string }
  | { ok: false; error: string };

/**
 * Mirror of the server's `parse_interval`: a positive whole number plus one of
 * `s`, `m`, `h`, `d`. Whitespace is trimmed and the unit lower-cased, so the
 * form can send `normalized` (`1H` → `1h`) and the server accepts it.
 */
export function validateEvery(text: string | null | undefined): EveryCheck {
  const normalized = (text ?? '').trim().toLowerCase();
  if (!normalized) return { ok: false, error: `Say how often: ${EVERY_HINT}.` };
  if (normalized.length > EVERY_MAX_CHARS) {
    return { ok: false, error: `Interval is too long; use ${EVERY_HINT}.` };
  }
  const match = EVERY_RE.exec(normalized);
  if (!match) return { ok: false, error: `Invalid interval "${normalized}": use ${EVERY_HINT}.` };
  const value = Number(match[1]);
  if (!Number.isSafeInteger(value) || value <= 0) {
    return { ok: false, error: 'Interval must be a positive number of seconds, minutes, hours or days.' };
  }
  return { ok: true, seconds: value * UNIT_SECONDS[match[2]], normalized };
}

/**
 * Inverse of `parse_interval`: the largest unit that divides the interval
 * exactly (`1800` → `30m`, `3600` → `1h`, `86400` → `1d`, `5400` → `90m`,
 * `90` → `90s`). '' for a non-positive or non-finite value.
 */
export function formatInterval(seconds: number | null | undefined): string {
  if (typeof seconds !== 'number' || !Number.isFinite(seconds) || seconds <= 0) return '';
  const whole = Math.round(seconds);
  if (whole <= 0) return '';
  for (const [unit, size] of [['d', 86400], ['h', 3600], ['m', 60]] as const) {
    if (whole % size === 0) return `${whole / size}${unit}`;
  }
  return `${whole}s`;
}

/** `42s`, `42m`, `3h 05m` → written as `3h 5m`, `2d 4h`: the coarse remaining time. */
export function compactDuration(ms: number): string {
  const totalSeconds = Math.max(0, Math.round(ms / 1000));
  if (totalSeconds < 60) return `${totalSeconds}s`;
  const totalMinutes = Math.floor(totalSeconds / 60);
  if (totalMinutes < 60) return `${totalMinutes}m`;
  const hours = Math.floor(totalMinutes / 60);
  const minutes = totalMinutes % 60;
  if (hours < 24) return minutes ? `${hours}h ${minutes}m` : `${hours}h`;
  const days = Math.floor(hours / 24);
  const restHours = hours % 24;
  return restHours ? `${days}d ${restHours}h` : `${days}d`;
}

/**
 * The Next column. `next_run` is epoch SECONDS (ScheduledTask.to_dict());
 * `nowMs` is `Date.now()`. A disabled schedule is `paused` whatever its
 * timestamp; a due or overdue one is `due now` (the runner picks it up on its
 * next poll); otherwise `in <duration>`.
 */
export function nextRunLabel(nextRunEpochS: number | null | undefined, nowMs: number, enabled = true): string {
  if (!enabled) return 'paused';
  if (typeof nextRunEpochS !== 'number' || !Number.isFinite(nextRunEpochS) || nextRunEpochS <= 0) {
    return 'not scheduled';
  }
  const diffMs = nextRunEpochS * 1000 - nowMs;
  if (diffMs <= 0) return 'due now';
  return `in ${compactDuration(diffMs)}`;
}

/**
 * `just now`, `12m ago`, `3h ago`, `2d ago`, or the ISO date once a week has
 * passed; '' for a missing or unparseable timestamp. A timestamp slightly in
 * the future (clock skew between server and browser) reads as `just now`.
 */
export function relativeAgo(iso: string | null | undefined, nowMs: number): string {
  if (!iso) return '';
  const then = Date.parse(iso);
  if (!Number.isFinite(then)) return '';
  const diff = nowMs - then;
  if (diff < 45_000) return 'just now';
  if (diff < 3_600_000) return `${Math.floor(diff / 60_000)}m ago`;
  if (diff < 86_400_000) return `${Math.floor(diff / 3_600_000)}h ago`;
  if (diff < 7 * 86_400_000) return `${Math.floor(diff / 86_400_000)}d ago`;
  return new Date(then).toISOString().slice(0, 10);
}

// ─── Status words ────────────────────────────────────────────────────────────

/**
 * The Last column and the run log's status word. `last_status` is '' before
 * the first run; `running` is the live tail's own state, never stored.
 * An `error` carries the recorded message when there is one.
 */
export function lastStatusLabel(status: string | null | undefined, lastError?: string | null): string {
  switch (status || '') {
    case 'ok':
      return 'ran';
    case 'deferred':
      return 'needs your approval';
    case 'skipped':
      return 'skipped (budget)';
    case 'error':
      return lastError ? `error: ${lastError}` : 'error';
    case 'running':
      return 'running';
    case '':
      return 'never ran';
    default:
      return String(status);
  }
}

/** Colour for `lastStatusLabel`'s word. */
export function lastStatusTone(status: string | null | undefined): StatusTone {
  switch (status || '') {
    case 'ok':
      return 'green';
    case 'deferred':
    case 'skipped':
    case 'running':
      return 'amber';
    case 'error':
      return 'red';
    default:
      return 'gray';
  }
}

/**
 * The Last cost column: `null` cost beside a `null` verdict is a schedule that
 * never ran (or recorded no figure) and shows NOTHING — not "cost unknown".
 * Otherwise the three-state pill decides: `last_cost_known === false` is
 * "cost unknown", a priced figure is the price, a priced zero is `$0.0000`.
 */
export function lastCostPill(
  schedule: Pick<Schedule, 'last_cost_usd' | 'last_cost_known'>,
  pill: CostPillFn,
): CostPill | null {
  const cost = schedule.last_cost_usd;
  const known = schedule.last_cost_known;
  if ((cost === null || cost === undefined) && (known === null || known === undefined)) return null;
  return pill(cost, known ?? undefined);
}

// ─── Runner ──────────────────────────────────────────────────────────────────

const START_A_RUNNER = 'start `nvh serve` or `nvh schedule start`';

/**
 * One sentence for `GET /v1/schedules → runner`. `active` means a lock exists
 * with a fresh heartbeat; `runner` says who wrote it ('api' | 'cli', '' when
 * no live lock is held); `stale` is a lock whose heartbeat aged out (the next
 * runner takes it over); `unreadable` is a lock file that exists but could
 * not be parsed (counted as live, nothing else to show).
 */
export function runnerLabel(
  runner: Pick<ScheduleRunner, 'active' | 'runner' | 'stale'> & { unreadable?: boolean } | null | undefined,
): string {
  if (!runner) return 'runner state unknown';
  if (runner.unreadable) return 'runner state unreadable';
  if (runner.active) {
    if (runner.runner === 'api') return 'the API server is running schedules';
    if (runner.runner === 'cli') return 'nvh schedule start is running schedules in a terminal';
    return 'a runner holds the schedule lock';
  }
  return runner.stale ? `no runner (stale lock): ${START_A_RUNNER}` : `no runner: ${START_A_RUNNER}`;
}

/** Colour for `runnerLabel`. */
export function runnerTone(
  runner: Pick<ScheduleRunner, 'active' | 'stale'> & { unreadable?: boolean } | null | undefined,
): StatusTone {
  if (!runner) return 'gray';
  if (runner.unreadable) return 'red';
  if (runner.active) return 'green';
  return runner.stale ? 'amber' : 'gray';
}

// ─── Names and previews ──────────────────────────────────────────────────────

/** How much of an unnamed schedule's prompt stands in for its name. */
export const DISPLAY_NAME_CHARS = 60;

/** The Name column: `name`, else the prompt's first characters, else a placeholder. */
export function scheduleDisplayName(schedule: Pick<Schedule, 'name' | 'prompt'>): string {
  const name = (schedule.name || '').trim();
  if (name) return name;
  const prompt = (schedule.prompt || '').replace(/\s+/g, ' ').trim();
  if (!prompt) return 'untitled schedule';
  if (prompt.length <= DISPLAY_NAME_CHARS) return prompt;
  return `${prompt.slice(0, DISPLAY_NAME_CHARS - 1).trimEnd()}…`;
}

/**
 * The first lines of a run's answer for the log: blank lines dropped, at most
 * `maxLines` lines and `maxChars` characters, an ellipsis when cut.
 */
export function answerPreview(answer: string | null | undefined, maxLines = 3, maxChars = 240): string {
  const lines = (answer || '')
    .split(/\r?\n/)
    .map(line => line.trim())
    .filter(Boolean);
  if (lines.length === 0) return '';
  let text = lines.slice(0, maxLines).join('\n');
  const cut = lines.length > maxLines || text.length > maxChars;
  if (text.length > maxChars) text = text.slice(0, Math.max(0, maxChars - 1)).trimEnd();
  return cut ? `${text}…` : text;
}

/** Distinct tool names from a run's `deferred` list, in order; [] for anything else. */
export function deferredToolNames(deferred: unknown): string[] {
  if (!Array.isArray(deferred)) return [];
  const names: string[] = [];
  for (const item of deferred) {
    const name = item && typeof item === 'object' ? (item as { name?: unknown }).name : undefined;
    if (typeof name === 'string' && name && !names.includes(name)) names.push(name);
  }
  return names;
}

// ─── Run outcomes ────────────────────────────────────────────────────────────

/** `ok` | `deferred` | `skipped` from the `complete` payload, `error` from an
 * error event or a failed job, `running` while neither has arrived. */
export type RunOutcomeKind = 'ok' | 'deferred' | 'skipped' | 'error' | 'running' | string;

export interface RunOutcome {
  outcome: RunOutcomeKind;
  /** The event's message: the answer preview, `Skipped: …`, or the error text. */
  message: string;
  answer: string;
  /** '' on `skipped` and `error` (the runner sends none) and before the run settles. */
  conversationId: string;
  costUsd: number | null;
  /** `null` = no verdict (skipped, error, running); false = no catalog price. */
  costKnown: boolean | null;
  deferredNames: string[];
  /** When the settling event was recorded; '' while running. */
  settledAt: string;
}

const EMPTY_OUTCOME: Omit<RunOutcome, 'outcome' | 'message'> = {
  answer: '',
  conversationId: '',
  costUsd: null,
  costKnown: null,
  deferredNames: [],
  settledAt: '',
};

/**
 * Read one flattened `schedule-run` event (the shape `watchInstallJob` hands
 * out): a `complete` event settles the run with its `outcome`, answer,
 * attribution and cost; an `error` event settles it as `error` with the
 * message (this is also how a `mode: do` refusal arrives). Anything else
 * (`step`) is `null` — the run is still going.
 */
export function outcomeFromEvent(event: ScheduleRunEvent, settledAt = ''): RunOutcome | null {
  if (event.event === 'complete') {
    return {
      outcome: typeof event.outcome === 'string' && event.outcome ? event.outcome : 'ok',
      message: event.message || '',
      answer: typeof event.answer === 'string' ? event.answer : '',
      conversationId: typeof event.conversation_id === 'string' ? event.conversation_id : '',
      costUsd: typeof event.cost_usd === 'number' && Number.isFinite(event.cost_usd) ? event.cost_usd : null,
      costKnown: typeof event.cost_known === 'boolean' ? event.cost_known : null,
      deferredNames: deferredToolNames(event.deferred),
      settledAt,
    };
  }
  if (event.event === 'error') {
    return { ...EMPTY_OUTCOME, outcome: 'error', message: event.message || '', settledAt };
  }
  return null;
}

const TERMINAL_FAILURES = new Set(['failed', 'canceled', 'interrupted']);

/**
 * One run-log row from a job record (`GET /v1/schedules/{id}/runs` → `runs[]`,
 * each with its `recent_events` tail). The newest settling event wins; a job
 * whose tail no longer holds one falls back to the job's own status and
 * message (`complete` → ok, failed/canceled/interrupted → error, else running).
 */
export function runOutcomeOf(
  job: Pick<InstallJob, 'status' | 'message'> & {
    recent_events?: InstallJobEvent[] | null;
    completed_at?: string | null;
  },
): RunOutcome {
  const events = job.recent_events ?? [];
  for (let index = events.length - 1; index >= 0; index -= 1) {
    const record = events[index];
    const flattened: ScheduleRunEvent = {
      event: record.event,
      status: record.status,
      message: record.message,
      ...(record.payload ?? {}),
    };
    const settled = outcomeFromEvent(flattened, record.timestamp || job.completed_at || '');
    if (settled) return settled;
  }
  const settledAt = job.completed_at || '';
  if (job.status === 'complete') return { ...EMPTY_OUTCOME, outcome: 'ok', message: job.message || '', settledAt };
  if (TERMINAL_FAILURES.has(job.status)) {
    return { ...EMPTY_OUTCOME, outcome: 'error', message: job.message || '', settledAt };
  }
  return { ...EMPTY_OUTCOME, outcome: 'running', message: job.message || '' };
}
