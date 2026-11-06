// Regression tests for lib/sessionCost.ts — the session cost pill the top
// bars render (components/SessionCostPill.tsx, mounted on `/` and through
// components/SessionAgePill.tsx in LayoutShell) from
// `GET /v1/budget/status?since=<tab start>` (0.46 design §7, owner decision O4).
//
// Runs on Node's built-in runner with native type stripping (Node >= 22.6 with
// --experimental-strip-types, unflagged from 23.6 / 24); no test framework in web/:
//
//     cd web && node --test lib/sessionCost.test.mjs
//
// This file is .mjs so tsc (include: **/*.ts, **/*.tsx) and `next build` leave
// it alone, while Node can import the .ts modules by explicit extension.

import { test } from 'node:test';
import assert from 'node:assert/strict';

import { COST_UNKNOWN_TEXT } from './cost.ts';
import {
  SESSION_COST_DESKTOP_TITLE,
  SESSION_COST_INCONCLUSIVE_TITLE,
  SESSION_COST_LEDGER_TITLE,
  SESSION_COST_RATE_COMMAND,
  SESSION_COST_UNKNOWN_TEXT,
  SESSION_COST_WARN_MIN_AGE_MS,
  SESSION_START_KEY,
  desktopCostUsd,
  formatHourlyRate,
  formatSessionAge,
  formatSessionCost,
  sessionCost,
  sessionCostFromStatus,
  sessionCostInput,
  sessionCostNumber,
} from './sessionCost.ts';

const HOUR = 3_600_000;
const MINUTE = 60_000;
const AGE_4H12M = 4 * HOUR + 12 * MINUTE;

/** An owned workstation with a known $0.42 of cloud spend since the tab opened. */
function owned(overrides = {}) {
  return {
    ageMs: AGE_4H12M,
    hourlyRate: null,
    cloudSinceTab: 0.42,
    cloudKnown: true,
    localOnly: false,
    isOwned: true,
    isCloud: false,
    deviceClass: 'workstation',
    ...overrides,
  };
}

/** A rented cloud desktop, same ledger. */
function rented(overrides = {}) {
  return owned({ isOwned: false, isCloud: true, deviceClass: 'cloud-vm', ...overrides });
}

test('the tab anchor key is the one the pre-0.46 SessionAgePill wrote', () => {
  assert.equal(SESSION_START_KEY, 'nvh_session_started_at');
});

test('formatSessionAge keeps the old SessionAgePill output', () => {
  assert.equal(formatSessionAge(AGE_4H12M), '4h 12m');
  assert.equal(formatSessionAge(HOUR + 5 * MINUTE), '1h 05m');
  assert.equal(formatSessionAge(12 * MINUTE + 5_000), '12m 05s');
  assert.equal(formatSessionAge(0), '0m 00s');
  assert.equal(formatSessionAge(-5_000), '0m 00s');
  assert.equal(formatSessionAge(Number.NaN), '0m 00s');
});

test('formatSessionCost: cents from a cent up, four decimals below, $0.0000 for zero', () => {
  assert.equal(formatSessionCost(3.3684), '3.37');
  assert.equal(formatSessionCost(0.42), '0.42');
  assert.equal(formatSessionCost(0.0042), '0.0042');
  assert.equal(formatSessionCost(0), '0.0000');
  assert.equal(formatSessionCost(-1), '0.0000');
  assert.equal(formatSessionCost(Number.NaN), '0.0000');
});

test('sessionCostNumber reads the decimal strings the API sends and rejects junk', () => {
  assert.equal(sessionCostNumber('0.42'), 0.42);
  assert.equal(sessionCostNumber('0'), 0);
  assert.equal(sessionCostNumber(2.1), 2.1);
  assert.equal(sessionCostNumber(''), null);
  assert.equal(sessionCostNumber(null), null);
  assert.equal(sessionCostNumber(undefined), null);
  assert.equal(sessionCostNumber('abc'), null);
  assert.equal(sessionCostNumber(-0.5), null);
  assert.equal(sessionCostNumber(true), null);
});

test('desktopCostUsd is the rate times the tab age in hours', () => {
  assert.equal(desktopCostUsd(0.8, 4.2 * HOUR).toFixed(2), '3.36');
  assert.equal(desktopCostUsd(2.1, 0), 0);
  assert.equal(desktopCostUsd(2.1, -HOUR), 0);
});

test('owned device: cloud spend only, no desktop term, no rate prompt', () => {
  const pill = sessionCost(owned());
  assert.equal(pill.text, 'Session 4h 12m · cloud $0.42');
  assert.equal(pill.state, 'cloud');
  assert.doesNotMatch(pill.text, /desktop/);
  assert.doesNotMatch(pill.text, /hourly rate/);
  assert.doesNotMatch(pill.title, /inconclusive/);
  assert.match(pill.title, /All cloud-model spend on this machine since this tab opened/);
});

test('rented desktop without a rate prompts for one and the title names the command', () => {
  const pill = sessionCost(rented());
  assert.equal(pill.text, 'Session 4h 12m · set your hourly rate · cloud $0.42');
  assert.equal(pill.state, 'metered');
  assert.match(pill.title, /nvh config set budget\.hourly_rate_usd 2\.10/);
  assert.equal(SESSION_COST_RATE_COMMAND, 'nvh config set budget.hourly_rate_usd 2.10');
});

test('rented desktop with a rate shows rate × tab age and says so', () => {
  const pill = sessionCost(rented({ hourlyRate: 0.8 }));
  assert.equal(pill.text, 'Session 4h 12m · desktop ≈ $3.36 · cloud $0.42');
  assert.equal(pill.state, 'metered');
  assert.match(pill.title, /time since this tab opened/);
  assert.match(pill.title, /not the rental's total/);
  assert.ok(pill.title.includes(SESSION_COST_DESKTOP_TITLE));
  // The desktop term is per tab, so a fresh tab on an old rental is small.
  assert.equal(sessionCost(rented({ hourlyRate: 0.8, ageMs: 3 * MINUTE })).text, 'Session 3m 00s · desktop ≈ $0.04 · cloud $0.42');
});

test('an unpriced cloud model makes the figure a floor: ≥ plus "cost unknown"', () => {
  assert.equal(SESSION_COST_UNKNOWN_TEXT, COST_UNKNOWN_TEXT);
  const pill = sessionCost(owned({ cloudKnown: false }));
  assert.equal(pill.text, 'Session 4h 12m · cloud ≥ $0.42 · cost unknown');
  assert.equal(pill.state, 'cloud');
  assert.match(pill.title, /no catalog price/);
  // Zero with an unpriced pair is still a floor, not "free" and not "local only".
  assert.equal(sessionCost(owned({ cloudKnown: false, cloudSinceTab: 0 })).text, 'Session 4h 12m · cloud ≥ $0.0000 · cost unknown');
});

test('zero spend with a cloud pair renders cloud $0.0000, never "local only"', () => {
  const pill = sessionCost(owned({ cloudSinceTab: 0, cloudKnown: true, localOnly: false }));
  assert.equal(pill.text, 'Session 4h 12m · cloud $0.0000');
  assert.equal(pill.state, 'cloud');
  assert.doesNotMatch(pill.text, /local only/);
});

test('"local only" requires the server verdict, and keeps the desktop term when a rate is set', () => {
  const pill = sessionCost(owned({ cloudSinceTab: 0, localOnly: true }));
  assert.equal(pill.text, 'Session 4h 12m · local only');
  assert.equal(pill.state, 'local-only');
  assert.match(pill.title, /local provider/);
  const metered = sessionCost(rented({ hourlyRate: 0.8, cloudSinceTab: 0, localOnly: true }));
  assert.equal(metered.text, 'Session 4h 12m · desktop ≈ $3.36 · local only');
  assert.equal(metered.state, 'metered');
});

test('a tab in which nothing has run yet is not "local only" (the server verdict is vacuous with no pairs)', () => {
  const platform = { device_class: 'workstation', device_label: 'Workstation', is_cloud: false, cloud_provider: null, is_owned: true };
  const base = { daily_spend: '0', daily_limit: '0', monthly_spend: '0', monthly_limit: '0', daily_queries: 0, monthly_queries: 0, by_provider: {} };
  // The exact zero-rows payload: `_pairs_local_only([])` is True, by_provider is empty.
  const fresh = {
    ...base,
    hourly_rate_usd: null,
    session: { since: 1_700_000_000_000, spend_usd: '0', cost_known: true, local_only: true, by_provider: {}, unavailable: false },
    platform,
  };
  assert.equal(sessionCostInput(fresh, 4_000).localOnly, false);
  const pill = sessionCostFromStatus(fresh, 4_000);
  assert.equal(pill.text, 'Session 0m 04s · cloud $0.0000');
  assert.equal(pill.state, 'cloud');
  assert.doesNotMatch(pill.title, /local provider/);
  // On a rental the desktop term stays and the verdict is still not "local only".
  const rental = { ...fresh, hourly_rate_usd: '0.80', platform: { ...platform, is_cloud: true, is_owned: false, device_class: 'cloud-vm' } };
  assert.equal(sessionCostFromStatus(rental, 4_000).text, 'Session 0m 04s · desktop ≈ $0.0009 · cloud $0.0000');
  // One counted local pair (a $0 ollama group) is the real verdict.
  const local = { ...fresh, session: { ...fresh.session, by_provider: { ollama: '0' } } };
  assert.equal(sessionCostInput(local, 4_000).localOnly, true);
  assert.equal(sessionCostFromStatus(local, 4_000).text, 'Session 0m 04s · local only');
  // A session block without by_provider at all cannot prove a pair: plain cloud zero.
  const noGroups = { ...fresh.session };
  delete noGroups.by_provider;
  assert.equal(sessionCostInput({ ...fresh, session: noGroups }, 4_000).localOnly, false);
});

test('amber only when the tab is at least 15 minutes old and cloud per hour beats the rate', () => {
  assert.equal(SESSION_COST_WARN_MIN_AGE_MS, 15 * MINUTE);
  // $5 in ten minutes is $30/h against a $1/h desktop — but the tab is too young.
  const young = sessionCost(rented({ hourlyRate: 1, ageMs: 10 * MINUTE, cloudSinceTab: 5 }));
  assert.equal(young.state, 'metered');
  assert.doesNotMatch(young.title, /above your hourly desktop rate/);
  // Same spend at twenty minutes: $15/h > $1/h → amber.
  const over = sessionCost(rented({ hourlyRate: 1, ageMs: 20 * MINUTE, cloudSinceTab: 5 }));
  assert.equal(over.state, 'warn');
  assert.match(over.title, /above your hourly desktop rate/);
  // Exactly fifteen minutes counts as old enough.
  assert.equal(sessionCost(rented({ hourlyRate: 1, ageMs: 15 * MINUTE, cloudSinceTab: 5 })).state, 'warn');
  // Under the rate: $0.10 over twenty minutes is $0.30/h < $1/h.
  assert.equal(sessionCost(rented({ hourlyRate: 1, ageMs: 20 * MINUTE, cloudSinceTab: 0.1 })).state, 'metered');
  // A floor above the rate is a sure overrun even with an unpriced pair.
  assert.equal(sessionCost(rented({ hourlyRate: 1, ageMs: 20 * MINUTE, cloudSinceTab: 5, cloudKnown: false })).state, 'warn');
  // No rate → nothing to compare against → never amber.
  assert.equal(sessionCost(rented({ ageMs: 20 * MINUTE, cloudSinceTab: 5 })).state, 'metered');
  // "local only" has no cloud spend to compare.
  assert.equal(sessionCost(rented({ hourlyRate: 1, ageMs: 20 * MINUTE, cloudSinceTab: 0, localOnly: true })).state, 'metered');
});

test('a rate of exactly 0 shows the desktop term but never turns amber', () => {
  // `nvh config set budget.hourly_rate_usd 0` reaches the pill as "0", not null:
  // "my desktop is free" must not read as "every cloud call beats my rate".
  const pill = sessionCost(owned({ hourlyRate: 0, ageMs: 16 * MINUTE, cloudSinceTab: 0.002 }));
  assert.equal(pill.text, 'Session 16m 00s · desktop ≈ $0.0000 · cloud $0.0020');
  assert.equal(pill.state, 'metered');
  assert.doesNotMatch(pill.title, /above your hourly desktop rate/);
  assert.equal(sessionCost(rented({ hourlyRate: 0, ageMs: 20 * MINUTE, cloudSinceTab: 5 })).state, 'metered');
  // Through the API adapter too.
  const status = {
    daily_spend: '0', daily_limit: '0', monthly_spend: '0', monthly_limit: '0', daily_queries: 0, monthly_queries: 0, by_provider: {},
    hourly_rate_usd: '0',
    session: { since: 1_700_000_000_000, spend_usd: '0.002', cost_known: true, local_only: false, by_provider: { openrouter: '0.002' }, unavailable: false },
    platform: { device_class: 'workstation', device_label: 'Workstation', is_cloud: false, cloud_provider: null, is_owned: true },
  };
  assert.equal(sessionCostInput(status, 16 * MINUTE).hourlyRate, 0);
  assert.equal(sessionCostFromStatus(status, 16 * MINUTE).state, 'metered');
});

test('a configured rate shows the desktop term on an owned device too (O4: the rate wins)', () => {
  const pill = sessionCost(owned({ hourlyRate: 2.1 }));
  assert.equal(pill.text, 'Session 4h 12m · desktop ≈ $8.82 · cloud $0.42');
  assert.equal(pill.state, 'metered');
  // And the amber rule applies there as well.
  assert.equal(sessionCost(owned({ hourlyRate: 0.01, cloudSinceTab: 5 })).state, 'warn');
});

test('inconclusive detection uses the owned copy and adds the caveat to the title', () => {
  for (const input of [
    owned({ isOwned: false, isCloud: false, deviceClass: 'unknown' }),
    owned({ isOwned: null, isCloud: null, deviceClass: null }),
    owned({ isOwned: true, isCloud: false, deviceClass: 'unknown' }),
  ]) {
    const pill = sessionCost(input);
    assert.equal(pill.text, 'Session 4h 12m · cloud $0.42');
    assert.equal(pill.state, 'cloud');
    assert.ok(pill.title.includes(SESSION_COST_INCONCLUSIVE_TITLE));
    assert.match(pill.title, /set budget\.hourly_rate_usd if this machine is metered/);
  }
  // A rate still wins over inconclusive detection.
  const rated = sessionCost(owned({ isOwned: null, isCloud: null, deviceClass: null, hourlyRate: 2.1 }));
  assert.match(rated.text, /desktop ≈ \$8\.82/);
  assert.match(rated.title, /inconclusive/);
});

test('an unreadable ledger says so and keeps the desktop term', () => {
  const pill = sessionCost(owned({ unavailable: true }));
  assert.equal(pill.text, 'Session 4h 12m · spend unavailable');
  assert.equal(pill.state, 'unknown');
  assert.match(pill.title, /could not be read/);
  assert.equal(sessionCost(rented({ unavailable: true, hourlyRate: 0.8 })).text, 'Session 4h 12m · desktop ≈ $3.36 · spend unavailable');
});

test('no session ledger at all degrades to the plain age', () => {
  const pill = sessionCost(owned({ cloudSinceTab: null }));
  assert.equal(pill.text, 'Session 4h 12m');
  assert.equal(pill.state, 'unknown');
  assert.match(pill.title, /only the age is shown/);
});

test('every title states what the cloud figure covers', () => {
  for (const input of [
    owned(),
    rented(),
    rented({ hourlyRate: 0.8 }),
    owned({ cloudKnown: false }),
    owned({ localOnly: true }),
    owned({ unavailable: true }),
    owned({ cloudSinceTab: null }),
    rented({ hourlyRate: 1, ageMs: 20 * MINUTE, cloudSinceTab: 5 }),
  ]) {
    assert.ok(sessionCost(input).title.includes(SESSION_COST_LEDGER_TITLE), JSON.stringify(input));
  }
});

test('sessionCostInput reads the 0.46 budget-status payload', () => {
  const status = {
    daily_spend: '1.00', daily_limit: '5', monthly_spend: '3', monthly_limit: '50',
    daily_queries: 4, monthly_queries: 9, by_provider: { openai: '1.00' },
    hourly_rate_usd: '0.80',
    daily_cost_known: true,
    session: { since: 1_700_000_000_000, spend_usd: '0.42', cost_known: true, local_only: false, by_provider: { openai: '0.42' }, unavailable: false },
    platform: { device_class: 'cloud-vm', device_label: 'Cloud VM', is_cloud: true, cloud_provider: 'brev', is_owned: false },
  };
  assert.deepEqual(sessionCostInput(status, AGE_4H12M), {
    ageMs: AGE_4H12M,
    hourlyRate: 0.8,
    cloudSinceTab: 0.42,
    cloudKnown: true,
    localOnly: false,
    isOwned: false,
    isCloud: true,
    deviceClass: 'cloud-vm',
    unavailable: false,
  });
  assert.equal(sessionCostFromStatus(status, AGE_4H12M).text, 'Session 4h 12m · desktop ≈ $3.36 · cloud $0.42');
  // The owner set no rate: null, not a guess.
  assert.equal(sessionCostInput({ ...status, hourly_rate_usd: null }, AGE_4H12M).hourlyRate, null);
  // No platform block → detection unknown.
  const noPlatform = sessionCostInput({ ...status, platform: null }, AGE_4H12M);
  assert.equal(noPlatform.isOwned, null);
  assert.equal(noPlatform.isCloud, null);
  assert.equal(noPlatform.deviceClass, null);
});

test('sessionCostInput: the fetch fallback and an older server show the plain age, an unreadable ledger says so', () => {
  // lib/api.ts getBudgetStatus → budgetStatusFallback() when the API is down.
  const fallback = {
    daily_spend: '0', daily_limit: '0', monthly_spend: '0', monthly_limit: '0',
    daily_queries: 0, monthly_queries: 0, by_provider: {}, unavailable: true, session: null, platform: null,
  };
  const offline = sessionCostInput(fallback, AGE_4H12M);
  assert.equal(offline.cloudSinceTab, null);
  assert.equal(offline.unavailable, false);
  assert.equal(sessionCostFromStatus(fallback, AGE_4H12M).text, 'Session 4h 12m');
  // An older server: no session key at all.
  const legacy = { daily_spend: '0', daily_limit: '0', monthly_spend: '0', monthly_limit: '0', daily_queries: 0, monthly_queries: 0, by_provider: {} };
  assert.equal(sessionCostFromStatus(legacy, AGE_4H12M).text, 'Session 4h 12m');
  // Nothing loaded yet.
  assert.equal(sessionCostFromStatus(null, AGE_4H12M).text, 'Session 4h 12m');
  assert.equal(sessionCostFromStatus(undefined, 0).text, 'Session 0m 00s');
  // The server answered but could not read the ledger.
  const unreadable = {
    ...legacy,
    unavailable: true,
    session: { since: 1_700_000_000_000, spend_usd: '0', cost_known: true, local_only: false, by_provider: {}, unavailable: true },
    platform: { device_class: 'workstation', device_label: 'Workstation', is_cloud: false, cloud_provider: null, is_owned: true },
  };
  assert.equal(sessionCostFromStatus(unreadable, AGE_4H12M).text, 'Session 4h 12m · spend unavailable');
});

test('formatHourlyRate: the settings page value', () => {
  assert.equal(formatHourlyRate('2.10'), '$2.10 / hour');
  assert.equal(formatHourlyRate('0.8'), '$0.80 / hour');
  assert.equal(formatHourlyRate(null), 'not set');
  assert.equal(formatHourlyRate(''), 'not set');
  assert.equal(formatHourlyRate(undefined), 'not reported');
  // A string the client cannot parse is shown as the server sent it.
  assert.equal(formatHourlyRate('two'), 'two / hour');
});

test('server `pairs` separates an idle tab from a tab whose only call failed locally', () => {
  const platform = { device_class: 'workstation', device_label: 'Workstation', is_cloud: false, cloud_provider: null, is_owned: true };
  const status = (session) => ({
    daily_spend: '0', daily_limit: '0', monthly_spend: '0', monthly_limit: '0', daily_queries: 0, monthly_queries: 0, by_provider: {},
    hourly_rate_usd: null, platform,
    session: { since: 1_700_000_000_000, spend_usd: '0', cost_known: true, local_only: true, by_provider: {}, unavailable: false, ...session },
  });
  // Idle tab: nothing counted — the plain age (cloud $0.0000), never "local only".
  const idle = status({ pairs: 0, cost_known: null });
  assert.equal(sessionCostInput(idle, 4_000).localOnly, false);
  assert.equal(sessionCostFromStatus(idle, 4_000).text, 'Session 0m 04s · cloud $0.0000');
  // A null cost_known with nothing counted is not an unpriced floor.
  assert.equal(sessionCostInput(idle, 4_000).cloudKnown, true);
  // One failed local pair: by_provider (successes) is empty, pairs is 1 — local only.
  const failedLocal = status({ pairs: 1 });
  assert.equal(sessionCostInput(failedLocal, 4_000).localOnly, true);
  assert.equal(sessionCostFromStatus(failedLocal, 4_000).text, 'Session 0m 04s · local only');
  // pairs wins over a by_provider group: a counted pair with local_only false stays cloud.
  assert.equal(sessionCostInput(status({ pairs: 2, local_only: false }), 4_000).localOnly, false);
  // pairs 0 beats a stray by_provider group.
  assert.equal(sessionCostInput(status({ pairs: 0, by_provider: { ollama: '0' } }), 4_000).localOnly, false);
  // Absent pairs (an older API): the by_provider inference, as before.
  assert.equal(sessionCostInput(status({}), 4_000).localOnly, false);
  assert.equal(sessionCostInput(status({ by_provider: { ollama: '0' } }), 4_000).localOnly, true);
});
