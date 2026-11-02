// Regression tests for the pure mascot rules in lib/mascotRules.ts — the two
// decisions the 0.46 question card added to the mascot (lib/mascot.ts calls
// them): which `asking` bubble the pending cards earn, and whether a streamed
// `tool_call` event moves the sprite at all.
//
// Runs on Node's built-in runner with native type stripping, like
// cost.test.mjs:
//
//     cd web && node --test lib/mascot.test.mjs
//
// lib/mascot.ts itself cannot be imported here: it loads the sprite manifest
// JSON and `@/lib/api` at module load (an alias Node does not resolve), which
// is why the rules live in an alias-free module. .mjs so tsc / next build
// leave it alone; Node imports the .ts modules by explicit extension.

import { test } from 'node:test';
import assert from 'node:assert/strict';

import {
  MASCOT_ASKING_PRIVILEGED_LABEL,
  MASCOT_ASKING_QUESTION_LABEL,
  MASCOT_QUESTION_TOOL,
  askingKind,
  toolCallMovesMascot,
} from './mascotRules.ts';
import { QUESTION_TOOL } from './privileged.ts';

test('the asking labels are the fixed sentences docs/MASCOT.md names', () => {
  assert.equal(MASCOT_ASKING_QUESTION_LABEL, 'the Wizard has a question for you');
  assert.equal(MASCOT_ASKING_PRIVILEGED_LABEL, 'the Wizard needs your approval for a privileged change');
  assert.notEqual(MASCOT_ASKING_QUESTION_LABEL, MASCOT_ASKING_PRIVILEGED_LABEL);
});

test('the question tool is spelled the same here and in lib/privileged.ts', () => {
  assert.equal(MASCOT_QUESTION_TOOL, 'ask_user');
  assert.equal(MASCOT_QUESTION_TOOL, QUESTION_TOOL);
});

test('askingKind: every pending card an ask_user → question', () => {
  assert.equal(askingKind(['confirm'], ['ask_user']), 'question');
  assert.equal(askingKind([undefined], ['ask_user']), 'question');
  // Two questions never reach the UI in one turn, but the rule holds anyway.
  assert.equal(askingKind(['confirm', 'confirm'], ['ask_user', 'ask_user']), 'question');
});

test('askingKind: a privileged card wins over a question; anything else is the plain confirmation', () => {
  assert.equal(askingKind(['privileged'], ['system_settings_apply']), 'privileged');
  assert.equal(askingKind(['confirm', 'privileged'], ['ask_user', 'apt_install']), 'privileged');
  assert.equal(askingKind([undefined, 'privileged'], ['ask_user', 'apt_install']), 'privileged');
  // A question beside a confirm sibling is not "only a question".
  assert.equal(askingKind(['confirm', 'confirm'], ['ask_user', 'remember']), 'confirm');
  assert.equal(askingKind(['confirm'], ['remember']), 'confirm');
  assert.equal(askingKind(['weird-new-class'], ['something']), 'confirm');
  // No names (an older caller) can never claim the question wording.
  assert.equal(askingKind(['confirm']), 'confirm');
  assert.equal(askingKind([], []), 'confirm');
  // Exact matches only.
  assert.equal(askingKind(['Privileged'], ['ask_user ']), 'confirm');
  assert.equal(askingKind(['confirm'], ['Ask_User']), 'confirm');
});

test('toolCallMovesMascot: auto or no class moves the sprite; a card-bound class leaves it alone', () => {
  assert.equal(toolCallMovesMascot('auto'), true);
  assert.equal(toolCallMovesMascot(undefined), true);
  assert.equal(toolCallMovesMascot(null), true);
  for (const cls of ['confirm', 'privileged', 'unknown', '', 'Auto', 'auto ', 'something-new']) {
    assert.equal(toolCallMovesMascot(cls), false, JSON.stringify(cls));
  }
});
