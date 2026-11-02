// Regression tests for lib/schedules.ts — the pure helpers behind /schedules
// (app/schedules/page.tsx): interval parsing and formatting, the Next / Last /
// Last cost labels, the runner status line and the run-log outcome reader.
//
// Runs on Node's built-in runner with native type stripping, like cost.test.mjs:
//
//     cd web && node --test lib/schedules.test.mjs
//
// .mjs so tsc / next build leave it alone; Node imports the .ts modules by
// explicit extension. lib/schedules.ts has no runtime import of its own (Node
// cannot resolve an extensionless `./cost`), so the cost helpers take the real
// `costPill` from here, exactly as the page passes it.

import { test } from 'node:test';
import assert from 'node:assert/strict';

import { COST_UNKNOWN_TEXT, costPill } from './cost.ts';
import {
  DISPLAY_NAME_CHARS,
  EVERY_MAX_CHARS,
  EVERY_PLACEHOLDER,
  answerPreview,
  compactDuration,
  deferredToolNames,
  formatInterval,
  lastCostPill,
  lastStatusLabel,
  lastStatusTone,
  nextRunLabel,
  outcomeFromEvent,
  relativeAgo,
  runOutcomeOf,
  runnerLabel,
  runnerTone,
  scheduleDisplayName,
  validateEvery,
} from './schedules.ts';

// ─── validateEvery / formatInterval ──────────────────────────────────────────

test('validateEvery accepts the server syntax and hands back seconds plus the normalized text', () => {
  assert.deepEqual(validateEvery('30m'), { ok: true, seconds: 1800, normalized: '30m' });
  assert.deepEqual(validateEvery('1h'), { ok: true, seconds: 3600, normalized: '1h' });
  assert.deepEqual(validateEvery('1d'), { ok: true, seconds: 86400, normalized: '1d' });
  assert.deepEqual(validateEvery('45s'), { ok: true, seconds: 45, normalized: '45s' });
  // Whitespace and case are normalized so the server (case-sensitive regex) accepts what we send.
  assert.deepEqual(validateEvery('  2H '), { ok: true, seconds: 7200, normalized: '2h' });
});

test('validateEvery refuses what parse_interval refuses, with a hint', () => {
  for (const bad of ['', '   ', 'hourly', '1 h', '1.5h', '0m', '-1h', '1w', '30', 'm']) {
    const result = validateEvery(bad);
    assert.equal(result.ok, false, `expected ${JSON.stringify(bad)} to be refused`);
    assert.match(result.error, /30m|positive|too long|how often/i);
  }
  assert.equal(validateEvery(null).ok, false);
  assert.equal(validateEvery(undefined).ok, false);
  // The route caps the field; a longer string is refused before the request.
  const tooLong = `${'9'.repeat(EVERY_MAX_CHARS)}m`;
  assert.equal(validateEvery(tooLong).ok, false);
  assert.match(validateEvery(tooLong).error, /too long/);
  assert.equal(EVERY_PLACEHOLDER, '30m | 1h | 1d');
});

test('formatInterval is the inverse of parse_interval and picks the largest exact unit', () => {
  assert.equal(formatInterval(1800), '30m');
  assert.equal(formatInterval(3600), '1h');
  assert.equal(formatInterval(86400), '1d');
  assert.equal(formatInterval(5400), '90m');
  assert.equal(formatInterval(172800), '2d');
  assert.equal(formatInterval(90), '90s');
  assert.equal(formatInterval(0), '');
  assert.equal(formatInterval(-60), '');
  assert.equal(formatInterval(Number.NaN), '');
  assert.equal(formatInterval(null), '');
  assert.equal(formatInterval(undefined), '');
  // Round-trips through the validator.
  for (const text of ['30m', '1h', '1d', '90m', '45s']) {
    const parsed = validateEvery(text);
    assert.equal(parsed.ok, true);
    assert.equal(formatInterval(parsed.seconds), text);
  }
});

// ─── nextRunLabel / compactDuration / relativeAgo ────────────────────────────

test('nextRunLabel reads epoch seconds against Date.now() milliseconds', () => {
  const nowMs = 1_800_000_000_000; // an arbitrary instant
  const nowS = nowMs / 1000;
  assert.equal(nextRunLabel(nowS + 42 * 60, nowMs), 'in 42m');
  assert.equal(nextRunLabel(nowS + 42, nowMs), 'in 42s');
  assert.equal(nextRunLabel(nowS + 3 * 3600 + 5 * 60, nowMs), 'in 3h 5m');
  assert.equal(nextRunLabel(nowS + 2 * 86400, nowMs), 'in 2d');
  assert.equal(nextRunLabel(nowS, nowMs), 'due now');
  assert.equal(nextRunLabel(nowS - 600, nowMs), 'due now');
  // A disabled schedule is paused whatever its timestamp.
  assert.equal(nextRunLabel(nowS + 600, nowMs, false), 'paused');
  assert.equal(nextRunLabel(nowS - 600, nowMs, false), 'paused');
  // Nothing to read.
  assert.equal(nextRunLabel(0, nowMs), 'not scheduled');
  assert.equal(nextRunLabel(null, nowMs), 'not scheduled');
  assert.equal(nextRunLabel(undefined, nowMs), 'not scheduled');
  assert.equal(nextRunLabel(Number.NaN, nowMs), 'not scheduled');
});

test('compactDuration keeps two units at most', () => {
  assert.equal(compactDuration(0), '0s');
  assert.equal(compactDuration(59_000), '59s');
  assert.equal(compactDuration(60_000), '1m');
  assert.equal(compactDuration(3_600_000), '1h');
  assert.equal(compactDuration(3_660_000), '1h 1m');
  assert.equal(compactDuration(86_400_000), '1d');
  assert.equal(compactDuration(90_000_000), '1d 1h');
  assert.equal(compactDuration(-5), '0s');
});

test('relativeAgo buckets a timestamp and tolerates bad input', () => {
  const nowMs = Date.parse('2026-11-02T12:00:00Z');
  assert.equal(relativeAgo('2026-11-02T11:59:30Z', nowMs), 'just now');
  assert.equal(relativeAgo('2026-11-02T11:48:00Z', nowMs), '12m ago');
  assert.equal(relativeAgo('2026-11-02T09:00:00Z', nowMs), '3h ago');
  assert.equal(relativeAgo('2026-10-31T12:00:00Z', nowMs), '2d ago');
  assert.equal(relativeAgo('2026-10-01T12:00:00Z', nowMs), '2026-10-01');
  // Python's isoformat with microseconds and an explicit offset parses too
  // (30m 59.9s ago floors to 30m).
  assert.equal(relativeAgo('2026-11-02T11:29:00.123456+00:00', nowMs), '30m ago');
  // Slight clock skew (server ahead of the browser) is "just now", not negative.
  assert.equal(relativeAgo('2026-11-02T12:00:20Z', nowMs), 'just now');
  assert.equal(relativeAgo('', nowMs), '');
  assert.equal(relativeAgo(null, nowMs), '');
  assert.equal(relativeAgo('not a date', nowMs), '');
});

// ─── Last status / last cost ─────────────────────────────────────────────────

test('lastStatusLabel maps the five stored statuses and the live "running" state', () => {
  assert.equal(lastStatusLabel('ok'), 'ran');
  assert.equal(lastStatusLabel('deferred'), 'needs your approval');
  assert.equal(lastStatusLabel('skipped'), 'skipped (budget)');
  assert.equal(lastStatusLabel('error'), 'error');
  assert.equal(lastStatusLabel('error', 'no model answered the scheduled turn'), 'error: no model answered the scheduled turn');
  assert.equal(lastStatusLabel(''), 'never ran');
  assert.equal(lastStatusLabel(null), 'never ran');
  assert.equal(lastStatusLabel(undefined), 'never ran');
  assert.equal(lastStatusLabel('running'), 'running');
  // An unknown future status is shown as-is rather than hidden.
  assert.equal(lastStatusLabel('paused-by-admin'), 'paused-by-admin');

  assert.equal(lastStatusTone('ok'), 'green');
  assert.equal(lastStatusTone('deferred'), 'amber');
  assert.equal(lastStatusTone('skipped'), 'amber');
  assert.equal(lastStatusTone('running'), 'amber');
  assert.equal(lastStatusTone('error'), 'red');
  assert.equal(lastStatusTone(''), 'gray');
});

test('lastCostPill shows nothing for a never-run schedule and the three states otherwise', () => {
  // null cost beside a null verdict: never ran (or no figure) — NOTHING, not "cost unknown".
  assert.equal(lastCostPill({ last_cost_usd: null, last_cost_known: null }, costPill), null);
  // The verdict alone says the model had no price.
  assert.deepEqual(
    { ...lastCostPill({ last_cost_usd: null, last_cost_known: false }, costPill), title: undefined },
    { state: 'unknown', text: COST_UNKNOWN_TEXT, title: undefined },
  );
  assert.equal(lastCostPill({ last_cost_usd: 0, last_cost_known: false }, costPill).text, COST_UNKNOWN_TEXT);
  // A priced run.
  assert.deepEqual(lastCostPill({ last_cost_usd: 0.0123, last_cost_known: true }, costPill), { state: 'priced', text: '$0.0123' });
  // A priced zero (a local model) is $0.0000, never hidden.
  assert.deepEqual(lastCostPill({ last_cost_usd: 0, last_cost_known: true }, costPill), { state: 'zero', text: '$0.0000' });
  // The pill function is injected: the page passes costPill, a test may pass a probe.
  const calls = [];
  lastCostPill({ last_cost_usd: 0.5, last_cost_known: true }, (cost, known) => { calls.push([cost, known]); return null; });
  assert.deepEqual(calls, [[0.5, true]]);
});

// ─── Runner ──────────────────────────────────────────────────────────────────

test('runnerLabel names who runs schedules, or how to start a runner', () => {
  const base = { active: true, pid: 4242, lock_path: '/x/state/scheduler.lock', heartbeat: 'h', started_at: 's', stale: false, unreadable: false, poll_s: 60, tools: [], history: 20 };
  assert.equal(runnerLabel({ ...base, runner: 'api' }), 'the API server is running schedules');
  assert.equal(runnerLabel({ ...base, runner: 'cli' }), 'nvh schedule start is running schedules in a terminal');
  assert.equal(runnerLabel({ ...base, runner: '' }), 'a runner holds the schedule lock');
  assert.equal(runnerLabel({ ...base, active: false, runner: '' }), 'no runner: start `nvh serve` or `nvh schedule start`');
  assert.equal(runnerLabel({ ...base, active: false, stale: true, runner: '' }), 'no runner (stale lock): start `nvh serve` or `nvh schedule start`');
  // An unreadable lock is counted active by the server; it still wins the label.
  assert.equal(runnerLabel({ ...base, unreadable: true, runner: '' }), 'runner state unreadable');
  // The flag is optional in the type (older servers): absent means readable.
  assert.equal(runnerLabel({ active: true, runner: 'api', stale: false }), 'the API server is running schedules');
  assert.equal(runnerLabel(null), 'runner state unknown');
  assert.equal(runnerLabel(undefined), 'runner state unknown');

  assert.equal(runnerTone({ ...base, runner: 'api' }), 'green');
  assert.equal(runnerTone({ ...base, active: false, stale: true }), 'amber');
  assert.equal(runnerTone({ ...base, active: false, stale: false }), 'gray');
  assert.equal(runnerTone({ ...base, unreadable: true }), 'red');
  assert.equal(runnerTone(null), 'gray');
});

// ─── Names and previews ──────────────────────────────────────────────────────

test('scheduleDisplayName prefers the name, then the prompt head, then a placeholder', () => {
  assert.equal(scheduleDisplayName({ name: 'Morning status', prompt: 'Summarise overnight GPU usage' }), 'Morning status');
  assert.equal(scheduleDisplayName({ name: '  ', prompt: 'Summarise overnight GPU usage' }), 'Summarise overnight GPU usage');
  const long = 'word '.repeat(30).trim();
  const shown = scheduleDisplayName({ name: '', prompt: long });
  assert.ok(shown.endsWith('…'));
  assert.ok(shown.length <= DISPLAY_NAME_CHARS);
  assert.equal(scheduleDisplayName({ name: '', prompt: 'line one\n\n   line   two' }), 'line one line two');
  assert.equal(scheduleDisplayName({ name: '', prompt: '' }), 'untitled schedule');
});

test('answerPreview keeps the first lines and marks the cut', () => {
  assert.equal(answerPreview(''), '');
  assert.equal(answerPreview(null), '');
  assert.equal(answerPreview('one\n\ntwo\nthree'), 'one\ntwo\nthree');
  assert.equal(answerPreview('one\ntwo\nthree\nfour'), 'one\ntwo\nthree…');
  assert.equal(answerPreview('a'.repeat(300), 3, 10), `${'a'.repeat(9)}…`);
  assert.equal(answerPreview('x\r\ny'), 'x\ny');
});

test('deferredToolNames lists distinct names and ignores junk', () => {
  assert.deepEqual(
    deferredToolNames([{ name: 'system_settings_apply', privileged: true }, { name: 'remember' }, { name: 'remember' }, { nope: 1 }, null, 'x']),
    ['system_settings_apply', 'remember'],
  );
  assert.deepEqual(deferredToolNames(undefined), []);
  assert.deepEqual(deferredToolNames('not a list'), []);
});

// ─── Run outcomes ────────────────────────────────────────────────────────────

const completeOk = {
  event: 'complete', status: 'complete', outcome: 'ok', schedule_id: 's1', conversation_id: 'c1',
  message: 'All quiet overnight.', answer: 'All quiet overnight.\nNo alerts.', provider: 'nvidia', model: 'm',
  cost_usd: 0.0021, cost_known: true, latency_ms: 1200, deferred: [],
};

test('outcomeFromEvent settles on complete and error, not on a step', () => {
  assert.deepEqual(outcomeFromEvent(completeOk, '2026-11-02T06:00:00Z'), {
    outcome: 'ok', message: 'All quiet overnight.', answer: 'All quiet overnight.\nNo alerts.', conversationId: 'c1',
    costUsd: 0.0021, costKnown: true, deferredNames: [], settledAt: '2026-11-02T06:00:00Z',
  });
  const deferred = outcomeFromEvent({ ...completeOk, outcome: 'deferred', deferred: [{ name: 'system_settings_apply', privileged: true, unattended: true }] });
  assert.equal(deferred.outcome, 'deferred');
  assert.deepEqual(deferred.deferredNames, ['system_settings_apply']);
  // Skipped: no conversation, null cost and verdict.
  const skipped = outcomeFromEvent({ event: 'complete', status: 'complete', outcome: 'skipped', message: 'Skipped: daily budget exceeded', answer: '', deferred: [], cost_usd: null, cost_known: null });
  assert.equal(skipped.outcome, 'skipped');
  assert.equal(skipped.conversationId, '');
  assert.equal(skipped.costUsd, null);
  assert.equal(skipped.costKnown, null);
  // Error (also how a mode:do refusal arrives).
  const failed = outcomeFromEvent({ event: 'error', status: 'failed', message: "mode 'do' runs tools unattended and cannot show an approval card" });
  assert.equal(failed.outcome, 'error');
  assert.match(failed.message, /^mode 'do'/);
  assert.equal(failed.costUsd, null);
  // A step is not a settlement.
  assert.equal(outcomeFromEvent({ event: 'step', status: 'running', message: 'Running Morning status (mode ask)' }), null);
  // A complete without an outcome (older runner) reads as ok.
  assert.equal(outcomeFromEvent({ event: 'complete', status: 'complete', message: 'Done' }).outcome, 'ok');
});

function job(status, events, extra = {}) {
  return {
    id: 'j1', kind: 'schedule-run', title: 't', status, message: 'job message', progress: 100,
    request: { schedule_id: 's1' }, storage_home: '/x', created_at: '2026-11-02T05:59:00Z', updated_at: '2026-11-02T06:00:00Z',
    started_at: '2026-11-02T05:59:01Z', completed_at: '2026-11-02T06:00:00Z', event_count: events.length, cancel_requested: false,
    events_path: '/x/jobs/j1.jsonl',
    recent_events: events.map((payload, index) => ({
      job_id: 'j1', sequence: index + 1, timestamp: `2026-11-02T05:59:0${index}Z`,
      event: payload.event, status: payload.status, message: payload.message, payload,
    })),
    ...extra,
  };
}

test('runOutcomeOf reads the newest settling event out of recent_events', () => {
  const ok = runOutcomeOf(job('complete', [
    { event: 'step', status: 'running', message: 'Running Morning status (mode ask)', schedule_id: 's1' },
    completeOk,
  ]));
  assert.equal(ok.outcome, 'ok');
  assert.equal(ok.conversationId, 'c1');
  assert.equal(ok.costUsd, 0.0021);
  assert.equal(ok.settledAt, '2026-11-02T05:59:01Z');
  // The job's own fields are not consulted while an event settles it.
  assert.equal(ok.message, 'All quiet overnight.');

  const failed = runOutcomeOf(job('failed', [{ event: 'error', status: 'failed', message: 'boom', schedule_id: 's1' }]));
  assert.equal(failed.outcome, 'error');
  assert.equal(failed.message, 'boom');
});

test('runOutcomeOf falls back to the job status when the tail holds no settling event', () => {
  // Events pruned or unavailable: a complete job is ok, a failed one is error.
  const bare = runOutcomeOf(job('complete', []));
  assert.equal(bare.outcome, 'ok');
  assert.equal(bare.message, 'job message');
  assert.equal(bare.settledAt, '2026-11-02T06:00:00Z');
  for (const status of ['failed', 'canceled', 'interrupted']) {
    assert.equal(runOutcomeOf(job(status, [])).outcome, 'error', status);
  }
  // Still going.
  const running = runOutcomeOf(job('running', [{ event: 'step', status: 'running', message: 'Running…' }], { completed_at: null }));
  assert.equal(running.outcome, 'running');
  assert.equal(running.settledAt, '');
  assert.equal(runOutcomeOf({ status: 'queued', message: '' }).outcome, 'running');
  // The pill the page renders from it: nothing for a run without a figure.
  assert.equal(costPill(running.costUsd, running.costKnown ?? undefined), null);
});
