/**
 * Pure shape helpers behind lib/api.ts (0.46 Wave C) — no fetch, no DOM, no
 * React, no `@/` alias — so they run under plain
 * `node --test lib/apiShapes.test.mjs`; api.ts itself cannot be imported by
 * Node (its extensionless relative imports), which is why these live apart.
 *
 * Two jobs:
 *  - the budget-status payload (`GET /v1/budget/status[?since=]`) normalised
 *    into `BudgetStatus` with every 0.46 field passed through, plus the
 *    offline fallback that says it is one (`unavailable: true`) instead of
 *    posing as a zero ledger;
 *  - the 409 body of `POST /v1/schedules/{id}/run` read back out of the Error
 *    message `apiFetch` throws (`JSON.stringify(detail)` plus an optional
 *    ` (request <id>, error <id>)` suffix).
 */

import type { BudgetPlatform, BudgetSession, BudgetStatus, ScheduleRunConflict } from './types';

/**
 * The smallest `since` that reads as epoch milliseconds (10^12 ms is
 * 2001-09-09); anything below is almost surely epoch seconds, which the
 * server refuses with a 422. Mirrors `_SINCE_FLOOR_MS` in nvh/api/server.py.
 */
export const BUDGET_SINCE_FLOOR_MS = 1_000_000_000_000;

/**
 * The query string for `GET /v1/budget/status`: '' without `since`,
 * `?since=<integer ms>` with it. A non-finite or seconds-scale `since` is a
 * programming error and throws a `RangeError` before any request is made.
 */
export function budgetStatusQuery(since?: number): string {
  if (since === undefined) return '';
  if (typeof since !== 'number' || !Number.isFinite(since) || since < BUDGET_SINCE_FLOOR_MS) {
    throw new RangeError('getBudgetStatus: since must be epoch milliseconds (Date.now() scale)');
  }
  return `?since=${Math.floor(since)}`;
}

function record(value: unknown): Record<string, unknown> | null {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

/** `{provider: "0.42"}` with every value as the decimal string the server sends. */
function stringMap(value: unknown): Record<string, string> {
  const source = record(value);
  if (!source) return {};
  const out: Record<string, string> = {};
  for (const [key, entry] of Object.entries(source)) {
    if (entry === null || entry === undefined) continue;
    out[key] = String(entry);
  }
  return out;
}

function budgetSession(value: unknown): BudgetSession | null {
  const s = record(value);
  if (!s) return null;
  return {
    since: Number(s.since ?? 0),
    spend_usd: String(s.spend_usd ?? '0'),
    cost_known: s.cost_known === true,
    local_only: s.local_only === true,
    by_provider: stringMap(s.by_provider),
    unavailable: s.unavailable === true,
  };
}

function budgetPlatform(value: unknown): BudgetPlatform | null {
  const p = record(value);
  if (!p) return null;
  return {
    device_class: typeof p.device_class === 'string' && p.device_class ? p.device_class : 'unknown',
    device_label: typeof p.device_label === 'string' ? p.device_label : '',
    is_cloud: p.is_cloud === true,
    cloud_provider: typeof p.cloud_provider === 'string' && p.cloud_provider ? p.cloud_provider : null,
    is_owned: p.is_owned === true,
  };
}

/**
 * The `/v1/budget/status` payload as `BudgetStatus`. The pre-0.46 fields are
 * coerced exactly as before (decimal strings, numeric counts); the 0.46
 * fields are passed through when the server sent them and left absent when
 * it did not, so a caller can tell an older server (`hourly_rate_usd`
 * undefined) from an owner who set no rate (`null`). `session` and
 * `platform` are `null` when the server sent `null` (no `since`; detection
 * failed) and absent on an older server.
 */
export function normalizeBudgetStatus(raw: unknown): BudgetStatus {
  const r = record(raw) ?? {};
  const out: BudgetStatus = {
    daily_spend: String(r.daily_spend ?? '0'),
    daily_limit: String(r.daily_limit ?? '0'),
    monthly_spend: String(r.monthly_spend ?? '0'),
    monthly_limit: String(r.monthly_limit ?? '0'),
    daily_queries: Number(r.daily_queries ?? 0),
    monthly_queries: Number(r.monthly_queries ?? 0),
    by_provider: stringMap(r.by_provider),
  };
  if (typeof r.unavailable === 'boolean') out.unavailable = r.unavailable;
  if ('hourly_rate_usd' in r) {
    const rate = r.hourly_rate_usd;
    out.hourly_rate_usd = typeof rate === 'string' ? rate : typeof rate === 'number' ? String(rate) : null;
  }
  if (typeof r.daily_cost_known === 'boolean') out.daily_cost_known = r.daily_cost_known;
  if ('session' in r) out.session = budgetSession(r.session);
  if ('platform' in r) out.platform = budgetPlatform(r.platform);
  return out;
}

/**
 * What `getBudgetStatus` resolves to when the fetch itself failed: the zero
 * ledger the widgets always tolerated, flagged `unavailable: true`, with
 * `session` and `platform` `null` and the rate unknown (absent, not `null`),
 * so the session pill shows its fallback copy instead of "$0 · local only".
 */
export function budgetStatusFallback(): BudgetStatus {
  return {
    daily_spend: '0',
    daily_limit: '0',
    monthly_spend: '0',
    monthly_limit: '0',
    daily_queries: 0,
    monthly_queries: 0,
    by_provider: {},
    unavailable: true,
    session: null,
    platform: null,
  };
}

/** Shown when a 409 arrived without a readable `message`. */
export const SCHEDULE_RUN_IN_FLIGHT_TEXT = 'a run of this schedule is already in flight';

/**
 * The 409 `detail` of `POST /v1/schedules/{id}/run` — `{message, job_id,
 * schedule_id}` — read back from the message `apiFetch` put on its Error:
 * `JSON.stringify(detail)`, optionally followed by ` (request <id>, error
 * <id>)`. The object is the span from the first `{` to the last `}` (the
 * suffix holds neither). A message that does not parse is kept verbatim as
 * the conflict's `message` with an empty `job_id`, so nothing is lost.
 */
export function scheduleRunConflictFromMessage(message: string, scheduleId: string): ScheduleRunConflict {
  const text = typeof message === 'string' ? message : String(message ?? '');
  const start = text.indexOf('{');
  const end = text.lastIndexOf('}');
  if (start !== -1 && end > start) {
    try {
      const parsed = record(JSON.parse(text.slice(start, end + 1)));
      if (parsed) {
        return {
          message:
            typeof parsed.message === 'string' && parsed.message.trim()
              ? parsed.message.trim()
              : SCHEDULE_RUN_IN_FLIGHT_TEXT,
          job_id: typeof parsed.job_id === 'string' ? parsed.job_id : '',
          schedule_id:
            typeof parsed.schedule_id === 'string' && parsed.schedule_id ? parsed.schedule_id : scheduleId,
        };
      }
    } catch {
      // not JSON — fall through to the verbatim message
    }
  }
  return { message: text.trim() || SCHEDULE_RUN_IN_FLIGHT_TEXT, job_id: '', schedule_id: scheduleId };
}
