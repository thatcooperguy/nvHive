// Regression tests for lib/apiShapes.ts — the pure helpers behind lib/api.ts
// (0.46 Wave C): the budget-status payload normalised with every new field
// passed through, the offline fallback that admits it is one, the `since`
// query guard, and the 409 "run already in flight" body read back out of the
// Error message `apiFetch` throws.
//
// Runs on Node's built-in runner with native type stripping (Node >= 22.6 with
// --experimental-strip-types, unflagged from 23.6 / 24); no test framework in web/:
//
//     cd web && node --test lib/apiShapes.test.mjs
//
// This file is .mjs so tsc (include: **/*.ts, **/*.tsx) and `next build` leave
// it alone, while Node can import the .ts module by explicit extension.
// api.ts itself is not importable here (extensionless relative imports), which
// is why the helpers live in their own alias-free module.

import { test } from 'node:test';
import assert from 'node:assert/strict';

import {
  BUDGET_SINCE_FLOOR_MS,
  SCHEDULE_RUN_IN_FLIGHT_TEXT,
  budgetStatusFallback,
  budgetStatusQuery,
  normalizeBudgetStatus,
  scheduleRunConflictFromMessage,
} from './apiShapes.ts';

// ─── budgetStatusQuery ───────────────────────────────────────────────────────

test('no since → no query string; a Date.now()-scale since → ?since=<integer ms>', () => {
  assert.equal(budgetStatusQuery(), '');
  assert.equal(budgetStatusQuery(undefined), '');
  assert.equal(budgetStatusQuery(1_762_000_000_000), '?since=1762000000000');
  // A fractional timestamp is floored, never sent with a decimal point.
  assert.equal(budgetStatusQuery(1_762_000_000_000.7), '?since=1762000000000');
});

test('a seconds-scale or non-finite since throws before any request is made', () => {
  // The server answers 422 for this; the client refuses earlier and louder.
  assert.throws(() => budgetStatusQuery(1_762_000_000), RangeError);
  assert.throws(() => budgetStatusQuery(BUDGET_SINCE_FLOOR_MS - 1), RangeError);
  assert.throws(() => budgetStatusQuery(Number.NaN), RangeError);
  assert.throws(() => budgetStatusQuery(Number.POSITIVE_INFINITY), RangeError);
  // The floor itself is accepted (2001-09-09 in milliseconds).
  assert.equal(budgetStatusQuery(BUDGET_SINCE_FLOOR_MS), `?since=${BUDGET_SINCE_FLOOR_MS}`);
});

// ─── normalizeBudgetStatus ───────────────────────────────────────────────────

const LEGACY_PAYLOAD = {
  daily_spend: '0.42',
  daily_limit: '10.00',
  monthly_spend: '3.10',
  monthly_limit: '100.00',
  daily_queries: 7,
  monthly_queries: 41,
  by_provider: { ollama: '0', groq: '0.42' },
};

test('a pre-0.46 payload normalises exactly as before, with no 0.46 keys invented', () => {
  const status = normalizeBudgetStatus(LEGACY_PAYLOAD);
  assert.deepEqual(status, LEGACY_PAYLOAD);
  // Absent means "this server does not say" — distinct from null ("not set").
  assert.equal('hourly_rate_usd' in status, false);
  assert.equal('session' in status, false);
  assert.equal('platform' in status, false);
  assert.equal('unavailable' in status, false);
});

test('the 0.46 fields pass through: rate string, null rate, daily verdict, platform, session', () => {
  const status = normalizeBudgetStatus({
    ...LEGACY_PAYLOAD,
    unavailable: false,
    hourly_rate_usd: '2.10',
    daily_cost_known: false,
    platform: {
      device_class: 'workstation',
      device_label: 'Linux workstation',
      is_cloud: false,
      cloud_provider: null,
      is_owned: true,
    },
    session: {
      since: 1_762_000_000_000,
      spend_usd: '0.0210',
      cost_known: true,
      local_only: false,
      by_provider: { groq: '0.0210' },
      unavailable: false,
    },
  });
  assert.equal(status.unavailable, false);
  assert.equal(status.hourly_rate_usd, '2.10');
  assert.equal(status.daily_cost_known, false);
  assert.deepEqual(status.platform, {
    device_class: 'workstation',
    device_label: 'Linux workstation',
    is_cloud: false,
    cloud_provider: null,
    is_owned: true,
  });
  assert.deepEqual(status.session, {
    since: 1_762_000_000_000,
    spend_usd: '0.0210',
    cost_known: true,
    local_only: false,
    by_provider: { groq: '0.0210' },
    unavailable: false,
  });

  // The owner set no rate: null survives (it is not "unknown").
  const unset = normalizeBudgetStatus({ ...LEGACY_PAYLOAD, hourly_rate_usd: null });
  assert.equal(unset.hourly_rate_usd, null);
  // A numeric rate from some other writer is still a decimal string to the UI.
  assert.equal(normalizeBudgetStatus({ ...LEGACY_PAYLOAD, hourly_rate_usd: 2.1 }).hourly_rate_usd, '2.1');
});

test('session and platform null from the server stay null; a session fallback keeps its flag', () => {
  const noSince = normalizeBudgetStatus({ ...LEGACY_PAYLOAD, session: null, platform: null });
  assert.equal(noSince.session, null);
  assert.equal(noSince.platform, null);

  const ledgerDown = normalizeBudgetStatus({
    ...LEGACY_PAYLOAD,
    session: { since: 1_762_000_000_000, spend_usd: '0', cost_known: false, local_only: false, by_provider: {}, unavailable: true },
  });
  assert.equal(ledgerDown.session?.unavailable, true);
  assert.equal(ledgerDown.session?.local_only, false);
});

test('loose shapes coerce: numeric spends become strings, missing counts zero, odd provider maps clean up', () => {
  const status = normalizeBudgetStatus({
    daily_spend: 0.5,
    by_provider: { ollama: 0, groq: null },
    session: { since: '1762000000000', spend_usd: 0.25, cost_known: 'yes', by_provider: { groq: 0.25 } },
    platform: { device_class: '', is_cloud: 'true' },
  });
  assert.equal(status.daily_spend, '0.5');
  assert.equal(status.daily_limit, '0');
  assert.equal(status.daily_queries, 0);
  assert.deepEqual(status.by_provider, { ollama: '0' });
  assert.deepEqual(status.session, {
    since: 1_762_000_000_000,
    spend_usd: '0.25',
    cost_known: false, // only a real boolean true counts as a verdict
    local_only: false,
    by_provider: { groq: '0.25' },
    unavailable: false,
  });
  assert.deepEqual(status.platform, {
    device_class: 'unknown',
    device_label: '',
    is_cloud: false,
    cloud_provider: null,
    is_owned: false,
  });
  // Garbage in → the zero ledger, not a throw.
  assert.equal(normalizeBudgetStatus(null).daily_spend, '0');
  assert.equal(normalizeBudgetStatus('nope').monthly_queries, 0);
});

// ─── budgetStatusFallback ────────────────────────────────────────────────────

test('the offline fallback is a zero ledger that says so', () => {
  const fallback = budgetStatusFallback();
  assert.equal(fallback.unavailable, true);
  assert.equal(fallback.session, null);
  assert.equal(fallback.platform, null);
  assert.equal(fallback.daily_spend, '0');
  assert.deepEqual(fallback.by_provider, {});
  // The rate is unknown, not "not set": a pill must not say "set your hourly rate" offline.
  assert.equal('hourly_rate_usd' in fallback, false);
});

// ─── scheduleRunConflictFromMessage ──────────────────────────────────────────

const DETAIL = {
  message: 'a run of schedule abc is already in flight (job 20261102093000-1a2b3c4d)',
  job_id: '20261102093000-1a2b3c4d',
  schedule_id: 'abc',
};

test('a bare JSON detail (what apiFetch puts on a 409) parses into the conflict', () => {
  assert.deepEqual(scheduleRunConflictFromMessage(JSON.stringify(DETAIL), 'abc'), DETAIL);
});

test('the request-id suffix apiFetch appends does not break the parse', () => {
  const withIds = `${JSON.stringify(DETAIL)} (request 0f3e2d1c, error 9a8b7c6d)`;
  assert.deepEqual(scheduleRunConflictFromMessage(withIds, 'abc'), DETAIL);
  const requestOnly = `${JSON.stringify(DETAIL)} (request 0f3e2d1c)`;
  assert.deepEqual(scheduleRunConflictFromMessage(requestOnly, 'abc'), DETAIL);
});

test('a message containing braces inside its text still parses as a whole object', () => {
  const braces = { ...DETAIL, message: 'in flight {since} the last poll}' };
  assert.deepEqual(scheduleRunConflictFromMessage(`${JSON.stringify(braces)} (request x)`, 'abc'), braces);
});

test('a non-JSON message is kept verbatim with no job id; the caller supplies the schedule id', () => {
  assert.deepEqual(scheduleRunConflictFromMessage('HTTP 409', 'abc'), {
    message: 'HTTP 409',
    job_id: '',
    schedule_id: 'abc',
  });
  assert.deepEqual(scheduleRunConflictFromMessage('', 'abc'), {
    message: SCHEDULE_RUN_IN_FLIGHT_TEXT,
    job_id: '',
    schedule_id: 'abc',
  });
  // A JSON detail without the expected keys still yields a usable conflict.
  assert.deepEqual(scheduleRunConflictFromMessage('{"detail":"busy"}', 'abc'), {
    message: SCHEDULE_RUN_IN_FLIGHT_TEXT,
    job_id: '',
    schedule_id: 'abc',
  });
  // A JSON array or scalar is not a detail object.
  assert.equal(scheduleRunConflictFromMessage('[1,2]', 'abc').schedule_id, 'abc');
});
