// Regression tests for lib/cost.ts — the three-state cost pill the Wizard
// footer (components/WizardChat.tsx) and the chat cards
// (components/ChatMessage.tsx) render from the API's cost_usd + cost_known.
//
// Runs on Node's built-in runner with native type stripping (Node >= 22.6 with
// --experimental-strip-types, unflagged from 23.6 / 24); no test framework in web/:
//
//     cd web && node --test lib/cost.test.mjs
//
// This file is .mjs so tsc (include: **/*.ts, **/*.tsx) and `next build` leave
// it alone, while Node can import the .ts module by explicit extension.

import { test } from 'node:test';
import assert from 'node:assert/strict';

import {
  COST_UNKNOWN_TEXT,
  COST_UNKNOWN_TITLE,
  costKnownFlag,
  costNumber,
  costPill,
  formatCardCost,
  formatWizardCost,
} from './cost.ts';

test('a priced non-zero cost renders as a dollar amount, as before', () => {
  assert.deepEqual(costPill(0.012345, true), { state: 'priced', text: '$0.0123' });
  assert.deepEqual(costPill('0.012345', true), { state: 'priced', text: '$0.0123' });
  // The Wizard footer keeps its own precision (3 dp from a cent, 5 below).
  assert.deepEqual(costPill(0.012345, true, formatWizardCost), { state: 'priced', text: '$0.012' });
  assert.deepEqual(costPill(0.0005, true, formatWizardCost), { state: 'priced', text: '$0.00050' });
  // The cards keep theirs (4 dp, 5 below a tenth of a cent).
  assert.equal(formatCardCost(0.00012), '0.00012');
  assert.equal(formatCardCost(0.25), '0.2500');
  // No hover text on a price.
  assert.equal(costPill(0.01, true).title, undefined);
});

test('a priced zero cost renders as $0.0000 and is never hidden', () => {
  assert.deepEqual(costPill(0, true), { state: 'zero', text: '$0.0000' });
  assert.deepEqual(costPill('0', true), { state: 'zero', text: '$0.0000' });
  assert.deepEqual(costPill('0.000', true, formatWizardCost), { state: 'zero', text: '$0.0000' });
  // An older API without cost_known: a $0 keeps its historical meaning (priced).
  assert.deepEqual(costPill(0, undefined), { state: 'zero', text: '$0.0000' });
});

test('cost_known=false renders "cost unknown" with a title that explains the missing catalog price', () => {
  const pill = costPill(0, false);
  assert.equal(pill.state, 'unknown');
  assert.equal(pill.text, COST_UNKNOWN_TEXT);
  assert.equal(pill.text, 'cost unknown');
  assert.equal(pill.title, COST_UNKNOWN_TITLE);
  assert.match(pill.title, /no price in the catalog/);
  // The same whether the $0 arrived as a string or not at all.
  assert.deepEqual(costPill('0', false), pill);
  assert.deepEqual(costPill(undefined, false), pill);
  assert.deepEqual(costPill(null, false), pill);
});

test('a non-zero bill is a price even when the catalog has no row', () => {
  // LiteLLM may price a model the YAML catalog does not list; the server
  // already reports such a bill as known, and the pill never contradicts a
  // real number with "unknown".
  assert.deepEqual(costPill(0.002, false), { state: 'priced', text: '$0.0020' });
});

test('no cost and no verdict renders nothing, as before', () => {
  assert.equal(costPill(undefined, undefined), null);
  assert.equal(costPill(null, true), null);
  assert.equal(costPill('', true), null);
  assert.equal(costPill('abc', true), null);
  assert.equal(costPill(-1, true), null);
  assert.equal(costPill(Number.NaN, true), null);
});

test('costNumber keeps a zero (metaNumber drops it) and rejects junk', () => {
  assert.equal(costNumber(0), 0);
  assert.equal(costNumber('0'), 0);
  assert.equal(costNumber('0.5'), 0.5);
  assert.equal(costNumber(''), undefined);
  assert.equal(costNumber(null), undefined);
  assert.equal(costNumber(undefined), undefined);
  assert.equal(costNumber('x'), undefined);
  assert.equal(costNumber(true), undefined);
  assert.equal(costNumber(-0.1), undefined);
  assert.equal(costNumber(Infinity), undefined);
});

test('costKnownFlag only trusts a real boolean', () => {
  assert.equal(costKnownFlag(true), true);
  assert.equal(costKnownFlag(false), false);
  assert.equal(costKnownFlag('false'), undefined);
  assert.equal(costKnownFlag(0), undefined);
  assert.equal(costKnownFlag(undefined), undefined);
  assert.equal(costKnownFlag(null), undefined);
});
