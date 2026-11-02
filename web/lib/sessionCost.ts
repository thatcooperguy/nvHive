/**
 * Session cost pill (0.46 design §7): one pure decision for the text, title
 * and tone of the top-bar pill that pairs the age of this browser tab with
 * what cloud models cost since it opened and, on a metered desktop, what the
 * desktop itself cost over the same span.
 *
 * Inputs come from `GET /v1/budget/status?since=<tab start, epoch ms>`
 * (lib/api.ts `getBudgetStatus(since)`): `session.spend_usd` / `cost_known` /
 * `local_only`, `hourly_rate_usd` and the `platform` block. The rules:
 *
 *   rented desktop (platform.is_cloud)
 *     "Session 4h 12m · desktop ≈ $3.37 · cloud $0.42" — the desktop term is
 *     hourly_rate_usd × time since THIS TAB opened (sessionStorage resets in a
 *     new tab), never the rental's total. Without a rate: "· set your hourly
 *     rate" and the title names the CLI command.
 *   owned device (platform.is_owned)
 *     "Session 4h 12m · cloud $0.42", or "· local only" ONLY when the server
 *     says every (model, provider) pair since the tab opened was local — never
 *     because the sum is zero (an unpriced cloud model logs $0). An unpriced
 *     pair makes the figure a floor: "cloud ≥ $0.42 · cost unknown".
 *   a configured rate always shows the desktop term (owner decision O4);
 *     detection only decides the default copy.
 *   inconclusive detection (device_class "unknown", or no platform block)
 *     owned copy plus a title caveat naming budget.hourly_rate_usd.
 *   amber ('warn') only when the tab is at least 15 minutes old AND cloud
 *     spend per tab-hour exceeds the configured rate.
 *
 * Import-free at runtime on purpose: tsconfig has no
 * `allowImportingTsExtensions` (a `./cost.ts` import does not type-check) and
 * Node cannot resolve an extensionless `./cost` (the .mjs test could not load
 * it). The formatting therefore mirrors lib/cost.ts — "$0.0000" for a priced
 * zero, "cost unknown" for a missing price — and sessionCost.test.mjs imports
 * both modules to pin the shared literal.
 */

import type { BudgetStatus } from './types';

/** sessionStorage key for the tab's start (epoch ms); the same anchor the
 * pre-0.46 SessionAgePill used, so a tab open across the upgrade keeps its age. */
export const SESSION_START_KEY = 'nvh_session_started_at';

/** Below this age the "cloud per hour vs desktop rate" verdict is noise. */
export const SESSION_COST_WARN_MIN_AGE_MS = 15 * 60_000;

/** The one way to set the rate: there is no write route (0.47 follow-up). */
export const SESSION_COST_RATE_COMMAND = 'nvh config set budget.hourly_rate_usd 2.10';

/** Same literal as lib/cost.ts `COST_UNKNOWN_TEXT` (pinned by the test). */
export const SESSION_COST_UNKNOWN_TEXT = 'cost unknown';

export const SESSION_COST_LEDGER_TITLE =
  'All cloud-model spend on this machine since this tab opened (CLI, other tabs, proxy clients and scheduled runs count).';
export const SESSION_COST_DESKTOP_TITLE =
  "Desktop ≈ the rate you set × time since this tab opened; a new tab starts at zero — not the rental's total.";
export const SESSION_COST_RATE_PROMPT_TITLE =
  `No hourly rate is set: run \`${SESSION_COST_RATE_COMMAND}\` (your rental's hourly price) and the pill estimates the desktop cost since this tab opened.`;
export const SESSION_COST_UNKNOWN_TITLE =
  'At least one model that answered since this tab opened has no catalog price, so the cloud figure is a floor (≥), not the total.';
export const SESSION_COST_LOCAL_ONLY_TITLE =
  'Every model counted since this tab opened ran on a local provider.';
export const SESSION_COST_WARN_TITLE =
  'Cloud spend per hour since this tab opened is above your hourly desktop rate.';
export const SESSION_COST_INCONCLUSIVE_TITLE =
  'Device detection was inconclusive; set budget.hourly_rate_usd if this machine is metered.';
export const SESSION_COST_UNAVAILABLE_TITLE =
  'Spend unavailable: the cost ledger could not be read; the age still counts from when this tab opened.';
export const SESSION_COST_AGE_ONLY_TITLE =
  'Spend since this tab opened is not available from the API right now (offline, or a server without the session ledger); only the age is shown.';

export type SessionCostState = 'local-only' | 'cloud' | 'metered' | 'warn' | 'unknown';

export interface SessionCostInput {
  /** Milliseconds since this tab opened (the sessionStorage anchor). */
  ageMs: number;
  /** `budget.hourly_rate_usd` as a number; null when none is set or the API did not report one. */
  hourlyRate: number | null;
  /** `session.spend_usd` as a number; null when the API returned no session ledger at all. */
  cloudSinceTab: number | null;
  /** `session.cost_known`: false = a counted model has no catalog price, so the spend is a floor. */
  cloudKnown: boolean;
  /** `session.local_only` AND at least one pair was counted: true only when
   * EVERY counted (model, provider) pair is a local provider — never for a
   * tab in which nothing has run yet (the server's verdict is vacuous then). */
  localOnly: boolean;
  /** `platform.is_owned`; null when the server sent no platform block. */
  isOwned: boolean | null;
  /** `platform.is_cloud`; null when the server sent no platform block. */
  isCloud: boolean | null;
  /** `platform.device_class`; "unknown" marks inconclusive detection. */
  deviceClass: string | null;
  /** The server answered but the ledger could not be read (`unavailable` flags). */
  unavailable?: boolean;
}

export interface SessionCostPill {
  /** The pill's visible text, terms joined by " · ". */
  text: string;
  /** Hover explanation; always states what the cloud figure covers. */
  title: string;
  state: SessionCostState;
}

/** "4h 12m" from an hour up, "12m 05s" below — the pre-0.46 SessionAgePill output. */
export function formatSessionAge(ms: number): string {
  const totalSec = Math.max(0, Math.floor((Number.isFinite(ms) ? ms : 0) / 1000));
  const hours = Math.floor(totalSec / 3600);
  const minutes = Math.floor((totalSec % 3600) / 60);
  if (hours > 0) return `${hours}h ${minutes.toString().padStart(2, '0')}m`;
  const seconds = totalSec % 60;
  return `${minutes}m ${seconds.toString().padStart(2, '0')}s`;
}

/** Session precision: cents from a cent up, four decimals below (so a priced
 * zero reads "$0.0000", as lib/cost.ts renders it). */
export function formatSessionCost(n: number): string {
  const v = Number.isFinite(n) && n > 0 ? n : 0;
  return v >= 0.01 ? v.toFixed(2) : v.toFixed(4);
}

/** A finite, non-negative number from a number or decimal string (the API
 * sends `spend_usd` and `hourly_rate_usd` as strings); null otherwise. */
export function sessionCostNumber(value: unknown): number | null {
  if (value === null || value === undefined || value === '' || typeof value === 'boolean') return null;
  const n = typeof value === 'number' ? value : Number(value);
  return Number.isFinite(n) && n >= 0 ? n : null;
}

/** The desktop term: the configured hourly rate × the tab's age in hours. */
export function desktopCostUsd(hourlyRate: number, ageMs: number): number {
  const hours = Math.max(0, ageMs) / 3_600_000;
  return Math.max(0, hourlyRate) * hours;
}

/** The settings page's read-only value: "$2.10 / hour", "not set" (the owner
 * set none) or "not reported" (offline, or a server without the field). */
export function formatHourlyRate(value: string | null | undefined): string {
  if (value === undefined) return 'not reported';
  if (value === null || value === '') return 'not set';
  const n = sessionCostNumber(value);
  return n === null ? `${value} / hour` : `$${n.toFixed(2)} / hour`;
}

export function sessionCost(input: SessionCostInput): SessionCostPill {
  const ageMs = Number.isFinite(input.ageMs) && input.ageMs > 0 ? input.ageMs : 0;
  const rate =
    input.hourlyRate !== null && Number.isFinite(input.hourlyRate) && input.hourlyRate >= 0
      ? input.hourlyRate
      : null;
  const rented = input.isCloud === true;
  const owned = !rented && input.isOwned === true && input.deviceClass !== 'unknown';
  const inconclusive = !rented && !owned;

  const parts: string[] = [`Session ${formatSessionAge(ageMs)}`];
  const titles: string[] = [];

  // Desktop term: a configured rate always wins (O4); a rented desktop
  // without one gets the prompt.
  const showDesktop = rented || rate !== null;
  if (rate !== null) {
    parts.push(`desktop ≈ $${formatSessionCost(desktopCostUsd(rate, ageMs))}`);
    titles.push(SESSION_COST_DESKTOP_TITLE);
  } else if (rented) {
    parts.push('set your hourly rate');
    titles.push(SESSION_COST_RATE_PROMPT_TITLE);
  }

  titles.push(SESSION_COST_LEDGER_TITLE);

  let state: SessionCostState;
  if (input.unavailable) {
    parts.push('spend unavailable');
    titles.push(SESSION_COST_UNAVAILABLE_TITLE);
    state = 'unknown';
  } else if (input.cloudSinceTab === null) {
    titles.push(SESSION_COST_AGE_ONLY_TITLE);
    state = 'unknown';
  } else if (input.localOnly) {
    parts.push('local only');
    titles.push(SESSION_COST_LOCAL_ONLY_TITLE);
    state = showDesktop ? 'metered' : 'local-only';
  } else {
    const floor = Math.max(0, Number.isFinite(input.cloudSinceTab) ? input.cloudSinceTab : 0);
    if (input.cloudKnown === false) {
      parts.push(`cloud ≥ $${formatSessionCost(floor)} · ${SESSION_COST_UNKNOWN_TEXT}`);
      titles.push(SESSION_COST_UNKNOWN_TITLE);
    } else {
      parts.push(`cloud $${formatSessionCost(floor)}`);
    }
    state = showDesktop ? 'metered' : 'cloud';
    // A rate of exactly 0 ("my desktop is free") has nothing to compare
    // against — any cloud spend would beat it — so it never turns amber.
    if (rate !== null && rate > 0 && ageMs >= SESSION_COST_WARN_MIN_AGE_MS) {
      const perHour = floor / (ageMs / 3_600_000);
      if (perHour > rate) {
        state = 'warn';
        titles.push(SESSION_COST_WARN_TITLE);
      }
    }
  }

  if (inconclusive) titles.push(SESSION_COST_INCONCLUSIVE_TITLE);

  return { text: parts.join(' · '), title: titles.join(' '), state };
}

/** Did the ledger count at least one (model, provider) pair since the cut? */
function hasCountedPairs(byProvider: Record<string, string> | null | undefined): boolean {
  return Boolean(byProvider) && Object.keys(byProvider as Record<string, string>).length > 0;
}

/**
 * `GET /v1/budget/status?since=` → `SessionCostInput`. No status yet, or a
 * payload without a `session` block (the fetch fallback, an older server) →
 * `cloudSinceTab: null`, so the pill shows the plain age; a session the
 * server flagged unreadable → `unavailable`.
 */
export function sessionCostInput(status: BudgetStatus | null | undefined, ageMs: number): SessionCostInput {
  const session = status?.session ?? null;
  const platform = status?.platform ?? null;
  return {
    ageMs,
    hourlyRate: sessionCostNumber(status?.hourly_rate_usd),
    cloudSinceTab: session ? (sessionCostNumber(session.spend_usd) ?? 0) : null,
    cloudKnown: session ? session.cost_known !== false : true,
    // The server's verdict is vacuously true when no pair was counted
    // (`_pairs_local_only([])`): a tab in which nothing has run yet is not
    // "local only" — it has no cloud spend, which `cloud $0.0000` says.
    localOnly: session ? session.local_only === true && hasCountedPairs(session.by_provider) : false,
    isOwned: platform ? platform.is_owned === true : null,
    isCloud: platform ? platform.is_cloud === true : null,
    deviceClass: platform ? platform.device_class || 'unknown' : null,
    unavailable: session !== null && (session.unavailable === true || status?.unavailable === true),
  };
}

export function sessionCostFromStatus(status: BudgetStatus | null | undefined, ageMs: number): SessionCostPill {
  return sessionCost(sessionCostInput(status, ageMs));
}
