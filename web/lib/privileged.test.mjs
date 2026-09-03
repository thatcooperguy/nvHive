// Regression tests for lib/privileged.ts — the presentation rules behind the
// red approval card for `privileged` Wizard tools (WizardChat.tsx ToolCard).
//
// Runs on Node's built-in runner with native type stripping, like gpu.test.mjs:
//
//     cd web && node --test lib/privileged.test.mjs
//
// .mjs so tsc / next build leave it alone; Node imports the .ts module by
// explicit extension.

import { test } from 'node:test';
import assert from 'node:assert/strict';

import {
  NEEDS_TERMINAL_HINT,
  PRIVILEGED_BADGE,
  PRIVILEGED_CLASS,
  PRIVILEGED_COLOR,
  PRIVILEGED_DISABLED_NOTE,
  PRIVILEGED_RUN_LABEL,
  PRIVILEGED_SUDO_NOTE,
  cardChrome,
  historyToolOutcome,
  isPendingStatus,
  isPrivilegedCall,
  isPrivilegedDisabled,
  needsTerminal,
  planLines,
  planNotes,
  terminalCommand,
  unwrapToolResult,
} from './privileged.ts';

/** A catalog entry with only the fields the helpers read. */
function schema(safety_class, extra = {}) {
  return {
    name: 'apt_install',
    description: 'Install packages with apt',
    parameters: {},
    summary_template: '',
    safety_class,
    ...extra,
  };
}

const STATUSES = ['idle', 'running', 'ok', 'error', 'awaiting-confirm', 'needs-terminal', 'disabled', 'dismissed'];
const PENDING = ['idle', 'awaiting-confirm'];
const SETTLED = STATUSES.filter(s => !PENDING.includes(s));

// ─── isPrivilegedCall ─────────────────────────────────────────────────────────

test('isPrivilegedCall: the catalog class or the execute result opts in', () => {
  assert.equal(isPrivilegedCall(schema('privileged')), true);
  assert.equal(isPrivilegedCall(schema('privileged'), undefined), true);
  // The server has spoken even though the catalog (older build) has not.
  assert.equal(isPrivilegedCall(schema('confirm'), { privileged: true }), true);
  assert.equal(isPrivilegedCall(undefined, { privileged: true }), true);
  assert.equal(isPrivilegedCall(undefined, { safety_class: 'privileged' }), true);
});

test('isPrivilegedCall: auto, confirm, unknown and missing are not privileged', () => {
  assert.equal(isPrivilegedCall(schema('auto')), false);
  assert.equal(isPrivilegedCall(schema('confirm')), false);
  assert.equal(isPrivilegedCall(schema('weird-new-class')), false);
  assert.equal(isPrivilegedCall(undefined), false);
  assert.equal(isPrivilegedCall(null, null), false);
  assert.equal(isPrivilegedCall(schema('confirm'), { privileged: false }), false);
  // Only the exact string counts — no case folding, no prefixes.
  assert.equal(isPrivilegedCall(schema('Privileged')), false);
  assert.equal(isPrivilegedCall(schema('privileged ')), false);
});

test('isPrivilegedCall: the catalog class is not overridden by privileged:false', () => {
  // Red stays red: a result that omits or negates the flag cannot downgrade a
  // tool the catalog declared privileged.
  assert.equal(isPrivilegedCall(schema('privileged'), { privileged: false }), true);
  assert.equal(isPrivilegedCall(schema('privileged'), { ok: true }), true);
});

// ─── needsTerminal / terminalCommand ─────────────────────────────────────────

test('needsTerminal: only the explicit flag; ok/error say nothing about it', () => {
  assert.equal(needsTerminal({ needs_terminal: true, command: 'sudo apt-get install -y tailscale' }), true);
  assert.equal(needsTerminal({ needs_terminal: true }), true);
  assert.equal(needsTerminal({ needs_terminal: false }), false);
  assert.equal(needsTerminal({ ok: false, error: 'boom' }), false);
  assert.equal(needsTerminal(undefined), false);
  assert.equal(needsTerminal(null), false);
});

test('needsTerminal: the hand-off is reported as ok=true or ok=false — both count', () => {
  // can_sudo=false + in_sudo_group=true (stock DGX OS) is the common path, and
  // the card must never render it as a failure either way.
  assert.equal(needsTerminal({ ok: true, needs_terminal: true }), true);
  assert.equal(needsTerminal({ ok: false, needs_terminal: true }), true);
  assert.equal(needsTerminal({ needs_terminal: 'yes' }), false);
});

test('terminalCommand: the trimmed command, or empty when the result carries none', () => {
  assert.equal(terminalCommand({ needs_terminal: true, command: '  sudo systemctl enable --now docker \n' }), 'sudo systemctl enable --now docker');
  assert.equal(terminalCommand({ needs_terminal: true }), '');
  assert.equal(terminalCommand({ needs_terminal: true, command: 42 }), '');
  assert.equal(terminalCommand({ command: '   ' }), '');
  assert.equal(terminalCommand(undefined), '');
  assert.equal(terminalCommand(null), '');
});

// ─── isPrivilegedDisabled ─────────────────────────────────────────────────────

test('isPrivilegedDisabled: catalog enabled=false or result disabled=true', () => {
  assert.equal(isPrivilegedDisabled(schema('privileged', { enabled: false })), true);
  assert.equal(isPrivilegedDisabled(schema('privileged'), { disabled: true }), true);
  assert.equal(isPrivilegedDisabled(undefined, { disabled: true }), true);
});

test('isPrivilegedDisabled: absent flags mean enabled (older builds send neither)', () => {
  assert.equal(isPrivilegedDisabled(schema('privileged')), false);
  assert.equal(isPrivilegedDisabled(schema('privileged', { enabled: true }), { disabled: false }), false);
  assert.equal(isPrivilegedDisabled(undefined, undefined), false);
  assert.equal(isPrivilegedDisabled(null, null), false);
  assert.equal(isPrivilegedDisabled(schema('privileged'), undefined, undefined), false);
});

test('isPrivilegedDisabled: the list-level privileged_enabled flag only reaches privileged cards', () => {
  assert.equal(isPrivilegedDisabled(schema('privileged'), undefined, false), true);
  assert.equal(isPrivilegedDisabled(schema('privileged'), undefined, true), false);
  // The privileged kill switch never disables a confirm- or auto-class card.
  assert.equal(isPrivilegedDisabled(schema('confirm'), undefined, false), false);
  assert.equal(isPrivilegedDisabled(schema('auto'), undefined, false), false);
  assert.equal(isPrivilegedDisabled(schema('weird-new-class'), undefined, false), false);
});

test('isPrivilegedDisabled: an execute refusal outranks an enabled-looking catalog', () => {
  assert.equal(isPrivilegedDisabled(schema('privileged', { enabled: true }), { disabled: true }, true), true);
});

// ─── planLines ────────────────────────────────────────────────────────────────

test('planLines: strings only, blanks dropped, anything else → []', () => {
  assert.deepEqual(
    planLines({ plan: ['sudo apt-get update', '  ', 'sudo apt-get install -y tailscale', 7, null] }),
    ['sudo apt-get update', 'sudo apt-get install -y tailscale'],
  );
  assert.deepEqual(planLines({ plan: [] }), []);
  assert.deepEqual(planLines({ plan: 'sudo apt-get update' }), []);
  assert.deepEqual(planLines({}), []);
  assert.deepEqual(planLines(undefined), []);
  assert.deepEqual(planLines(null), []);
});

/** The server's plan object (`Plan.to_dict()`), as it rides on a surfaced call. */
const SERVER_PLAN = {
  ok: true,
  setting: 'enable_ufw_tailscale_only',
  title: 'Firewall: deny incoming except over Tailscale',
  commands: ['sudo ufw default deny incoming', 'sudo ufw allow in on tailscale0', 'sudo ufw --force enable'],
  sudo: true,
  changes: 'Sets ufw to deny incoming, allows tailscale0, then enables the firewall.',
  undo: ['sudo ufw delete allow in on tailscale0', 'sudo ufw disable'],
  notes: ['Check `tailscale status` first.'],
  warning: 'If you are connected over the LAN this disconnects you.',
};

test('planLines: reads the server\'s plan object off a surfaced call or an execute result', () => {
  assert.deepEqual(planLines({ name: 'system_settings_apply', arguments: {}, plan: SERVER_PLAN }), SERVER_PLAN.commands);
  assert.deepEqual(planLines({ ok: false, needs_confirmation: true, plan: SERVER_PLAN }), SERVER_PLAN.commands);
  // A refused plan has no commands to show.
  assert.deepEqual(planLines({ plan: { ok: false, error: 'BLOCKED: system shutdown', commands: [] } }), []);
  assert.deepEqual(planLines({ plan: { ok: true } }), []);
  assert.deepEqual(planLines({ plan: null }), []);
});

test('planLines: never reads the model\'s arguments — only a `plan` key counts', () => {
  // A prompt-injected "plan" inside the call's arguments is just another
  // argument: the card must not print it as the commands being approved.
  const call = {
    name: 'apt_install',
    arguments: { packages: ['linux-generic-hwe-24.04'], plan: ['sudo apt-get install -y htop'] },
  };
  assert.deepEqual(planLines(call), []);
  assert.deepEqual(planLines(call.arguments), ['sudo apt-get install -y htop'], 'the helper is shape-blind: the caller must never hand it arguments');
  assert.deepEqual(planLines({ package: 'ollama' }), []);
});

test('planLines: a malformed plan degrades to empty instead of printing junk', () => {
  assert.deepEqual(planLines({ plan: [{ cmd: 'sudo reboot' }, 42, ''] }), []);
  assert.deepEqual(planLines({ plan: ['sudo apt-get update', undefined] }), ['sudo apt-get update']);
  assert.deepEqual(planLines({ plan: { commands: 'sudo reboot' } }), []);
  assert.deepEqual(planLines({ plan: { commands: [{ cmd: 'sudo reboot' }, ' sudo apt-get update '] } }), ['sudo apt-get update']);
});

// ─── planNotes ────────────────────────────────────────────────────────────────

test('planNotes: changes, undo, warning and notes come off the server plan; refusal carries its error', () => {
  const notes = planNotes({ plan: SERVER_PLAN });
  assert.equal(notes.changes, SERVER_PLAN.changes);
  assert.deepEqual(notes.undo, SERVER_PLAN.undo);
  assert.equal(notes.warning, SERVER_PLAN.warning);
  assert.deepEqual(notes.notes, SERVER_PLAN.notes);
  assert.equal(notes.error, '');
  const refused = planNotes({ plan: { ok: false, error: 'refusing to install nvidia-driver-580-open: DGX OS', commands: [] } });
  assert.equal(refused.error, 'refusing to install nvidia-driver-580-open: DGX OS');
  // `error` on an ok plan is not a refusal.
  assert.equal(planNotes({ plan: { ok: true, commands: [], error: 'stale' } }).error, '');
});

test('planNotes: a bare command list, no plan or junk gives empty notes', () => {
  const empty = { changes: '', undo: [], warning: '', notes: [], error: '' };
  assert.deepEqual(planNotes({ plan: ['sudo apt-get update'] }), empty);
  assert.deepEqual(planNotes({}), empty);
  assert.deepEqual(planNotes(undefined), empty);
  assert.deepEqual(planNotes(null), empty);
  assert.deepEqual(planNotes({ plan: { changes: 7, undo: 'x', warning: null, notes: [3] } }), empty);
});

// ─── unwrapToolResult ─────────────────────────────────────────────────────────

test('unwrapToolResult: an ok envelope with a dict result yields the nested handler answer', () => {
  const handoff = { ok: false, needs_terminal: true, command: 'sudo systemctl enable --now ssh', hint: 'run it yourself' };
  const envelope = { ok: true, result: handoff, tool: 'system_settings_apply', safety_class: 'privileged' };
  assert.equal(unwrapToolResult(envelope), handoff);
  // …so the hand-off is seen where the envelope's `ok: true` would have hidden it.
  assert.equal(needsTerminal(unwrapToolResult(envelope)), true);
  assert.equal(needsTerminal(envelope), false);
  const refusal = { ok: false, denied: true, error: 'BLOCKED: system shutdown' };
  assert.equal(unwrapToolResult({ ok: true, result: refusal }).ok, false);
  const receipt = { ok: true, applied: true, summary: 'Enable and start the OpenSSH server', steps: [] };
  assert.equal(unwrapToolResult({ ok: true, result: receipt }).summary, receipt.summary);
});

test('unwrapToolResult: cards, refusals and failures pass through unchanged', () => {
  const card = { ok: false, needs_confirmation: true, privileged: true, plan: SERVER_PLAN, approval_token: 'abc.1' };
  assert.equal(unwrapToolResult(card), card);
  const disabled = { ok: false, disabled: true, error: 'privileged tools are disabled (NVH_ALLOW_PRIVILEGED=0)' };
  assert.equal(unwrapToolResult(disabled), disabled);
  const noToken = { ok: false, approval_required: true, error: 'privileged call needs the approval token from its card' };
  assert.equal(unwrapToolResult(noToken), noToken);
  const thrown = { ok: false, error: 'kaboom', tool: 'boom' };
  assert.equal(unwrapToolResult(thrown), thrown);
  // ok with no dict result: nothing to unwrap.
  const bare = { ok: true, tool: 'refresh_models' };
  assert.equal(unwrapToolResult(bare), bare);
  const listResult = { ok: true, result: ['a'] };
  assert.equal(unwrapToolResult(listResult), listResult);
  assert.deepEqual(unwrapToolResult(undefined), {});
  assert.deepEqual(unwrapToolResult(null), {});
});

// ─── historyToolOutcome ───────────────────────────────────────────────────────

test('historyToolOutcome: pending and running cards produce nothing', () => {
  for (const s of ['idle', 'awaiting-confirm', 'running', undefined]) {
    assert.equal(historyToolOutcome('apt_install', s, 'x'), null, String(s));
  }
});

test('historyToolOutcome: ok is true only for a clean run; the hand-off names the exact command', () => {
  assert.deepEqual(historyToolOutcome('apt_install', 'ok', 'Install htop with apt-get'), {
    name: 'apt_install', ok: true, summary: 'Install htop with apt-get',
  });
  assert.deepEqual(historyToolOutcome('apt_install', 'ok', '  '), { name: 'apt_install', ok: true, summary: 'ran' });
  const handoff = historyToolOutcome('system_settings_apply', 'needs-terminal', 'Run this yourself', {
    command: '  sudo systemctl enable --now ssh ',
  });
  assert.deepEqual(handoff, {
    name: 'system_settings_apply', ok: false, summary: 'needs a terminal: sudo systemctl enable --now ssh',
  });
  // The prefix is what the server's prompt tells the model to look for.
  assert.match(handoff.summary, /^needs a terminal: /);
  assert.equal(historyToolOutcome('x', 'needs-terminal', undefined, undefined).summary, 'needs a terminal: (no command was returned)');
  assert.deepEqual(historyToolOutcome('apt_install', 'error', 'Tool failed: `sudo -n apt-get …` exited 100'), {
    name: 'apt_install', ok: false, summary: 'Tool failed: `sudo -n apt-get …` exited 100',
  });
  assert.equal(historyToolOutcome('apt_install', 'error', undefined).summary, 'failed');
  assert.deepEqual(historyToolOutcome('apt_install', 'dismissed', 'anything'), {
    name: 'apt_install', ok: false, summary: 'skipped by the user; not run',
  });
  assert.deepEqual(historyToolOutcome('apt_install', 'disabled', 'anything'), {
    name: 'apt_install', ok: false, summary: PRIVILEGED_DISABLED_NOTE,
  });
});

test('historyToolOutcome: the summary is cut to 300 characters', () => {
  const long = historyToolOutcome('apt_install', 'ok', 'y'.repeat(1000));
  assert.equal(long.summary.length, 300);
  const longCommand = historyToolOutcome('x', 'needs-terminal', undefined, { command: 'sudo apt-mark hold ' + 'p'.repeat(1000) });
  assert.equal(longCommand.summary.length, 300);
  assert.match(longCommand.summary, /^needs a terminal: sudo apt-mark hold /);
});

// ─── isPendingStatus ──────────────────────────────────────────────────────────

test('isPendingStatus: idle and awaiting-confirm need a click; everything else is settled', () => {
  for (const s of PENDING) assert.equal(isPendingStatus(s), true, s);
  for (const s of SETTLED) assert.equal(isPendingStatus(s), false, s);
  assert.equal(isPendingStatus(undefined), false);
});

// ─── cardChrome ───────────────────────────────────────────────────────────────

test('cardChrome: privileged wears red, the PRIVILEGED badge and "Approve and run" in every status', () => {
  for (const status of STATUSES) {
    const c = cardChrome('privileged', status);
    assert.equal(c.privileged, true, status);
    assert.equal(c.borderColor, PRIVILEGED_COLOR, status);
    assert.equal(c.classBadge, PRIVILEGED_BADGE, status);
    assert.equal(c.runLabel, PRIVILEGED_RUN_LABEL, status);
    assert.equal(c.clickable, PENDING.includes(status), status);
  }
  assert.equal(cardChrome('privileged', 'idle').badge, 'Needs your approval');
  assert.equal(cardChrome('privileged', 'idle').badgeColor, PRIVILEGED_COLOR);
  assert.equal(cardChrome('privileged', 'awaiting-confirm').badge, 'Needs your approval');
});

test('cardChrome: auto, confirm, an unknown class and a missing entry all get the same confirm card', () => {
  const reference = cardChrome('confirm', 'idle');
  assert.equal(reference.privileged, false);
  assert.equal(reference.borderColor, null);
  assert.equal(reference.classBadge, null);
  assert.equal(reference.runLabel, 'Run');
  assert.equal(reference.badge, 'Click to run');
  for (const cls of ['auto', 'weird-new-class', undefined, null, '']) {
    for (const status of STATUSES) {
      assert.deepEqual(cardChrome(cls, status), cardChrome('confirm', status), `${cls}/${status}`);
    }
  }
  assert.equal(cardChrome('confirm', 'awaiting-confirm').badge, 'Needs confirmation');
});

test('cardChrome: every pending card is clickable — auto included, the server already ran what it wanted to', () => {
  for (const cls of ['auto', 'confirm', 'privileged', 'unknown', undefined]) {
    for (const status of PENDING) assert.equal(cardChrome(cls, status).clickable, true, `${cls}/${status}`);
    for (const status of SETTLED) assert.equal(cardChrome(cls, status).clickable, false, `${cls}/${status}`);
  }
});

test('cardChrome: status wins over class for the badge text', () => {
  assert.equal(cardChrome('privileged', 'ok').badge, '✓ Done');
  assert.equal(cardChrome('privileged', 'running').badge, 'Running...');
  assert.equal(cardChrome('privileged', 'dismissed').badge, 'Skipped');
  assert.equal(cardChrome('privileged', 'error').badge, '✗ Failed');
});

test('cardChrome: needs-terminal is amber, never the red "✗ Failed"', () => {
  const handoff = cardChrome('privileged', 'needs-terminal');
  assert.equal(handoff.badge, 'Run it in a terminal');
  assert.equal(handoff.badgeColor, '#d97706');
  assert.notEqual(handoff.badgeColor, PRIVILEGED_COLOR);
  assert.notEqual(handoff.badge, cardChrome('privileged', 'error').badge);
  // Settled: the user finishes it in a terminal, so no Run button.
  assert.equal(handoff.clickable, false);
});

test('cardChrome: disabled mutes the badge, drops the buttons and keeps the red border', () => {
  const off = cardChrome('privileged', 'disabled');
  assert.equal(off.badge, 'Disabled');
  assert.equal(off.badgeColor, 'var(--text-muted)');
  assert.equal(off.borderColor, PRIVILEGED_COLOR);
  assert.equal(off.clickable, false);
});

test('cardChrome: a genuine failure is red for every class', () => {
  assert.equal(cardChrome('confirm', 'error').badgeColor, PRIVILEGED_COLOR);
  assert.equal(cardChrome('privileged', 'error').badgeColor, PRIVILEGED_COLOR);
});

// ─── Copy that the card renders verbatim ─────────────────────────────────────

test('the fixed strings say what they must, and name the env var they name', () => {
  assert.equal(PRIVILEGED_CLASS, 'privileged');
  assert.equal(PRIVILEGED_SUDO_NOTE, 'runs with sudo on this machine');
  // The refusal names the variable the owner has to change (invariant 12).
  assert.match(PRIVILEGED_DISABLED_NOTE, /NVH_ALLOW_PRIVILEGED=0/);
  assert.match(NEEDS_TERMINAL_HINT, /terminal/);
  // No password vocabulary that suggests nvHive would take one.
  assert.doesNotMatch(NEEDS_TERMINAL_HINT, /enter your password (here|below)/i);
});
