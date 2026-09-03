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
  PLAYBOOK_HANDOFF_PREFIX,
  PLAYBOOK_INSTALL_TOOL,
  PLAYBOOK_LIST_TOOL,
  PLAYBOOK_LOG_LINES_PER_STEP,
  PLAYBOOK_NEEDS_TERMINAL_EXPECTED_NOTE,
  PLAYBOOK_PLAN_TOOL,
  PRIVILEGED_BADGE,
  PRIVILEGED_CLASS,
  PRIVILEGED_COLOR,
  PRIVILEGED_DISABLED_NOTE,
  PRIVILEGED_RUN_LABEL,
  PRIVILEGED_SUDO_NOTE,
  applyPlaybookEvent,
  cardChrome,
  historyToolOutcome,
  initialPlaybookRun,
  isPendingStatus,
  isPrivilegedCall,
  isPrivilegedDisabled,
  jobOutcome,
  manualStepsFromNotes,
  needsTerminal,
  planLines,
  planNotes,
  planNotesWithoutManual,
  playbookCardStatus,
  playbookCatalogue,
  playbookChips,
  playbookHandoffCommand,
  playbookOutcomeLabel,
  playbookPlanExtras,
  playbookProgressLabel,
  playbookRunStart,
  playbookStepIndex,
  playbookStepNumber,
  settlePlaybookRun,
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

// ═══ Spark playbooks ═════════════════════════════════════════════════════════

test('playbook tool names and the hand-off command are fixed strings', () => {
  assert.equal(PLAYBOOK_LIST_TOOL, 'playbook_list');
  assert.equal(PLAYBOOK_PLAN_TOOL, 'playbook_plan');
  assert.equal(PLAYBOOK_INSTALL_TOOL, 'playbook_install');
  assert.equal(PLAYBOOK_HANDOFF_PREFIX, 'nvh playbook install');
  assert.equal(playbookHandoffCommand('ollama'), 'nvh playbook install ollama');
  assert.equal(playbookHandoffCommand('  tailscale \n'), 'nvh playbook install tailscale');
  // A blank id keeps a visible placeholder rather than an incomplete command.
  assert.equal(playbookHandoffCommand(''), 'nvh playbook install <id>');
  assert.equal(playbookHandoffCommand(undefined), 'nvh playbook install <id>');
  assert.equal(playbookHandoffCommand(null), 'nvh playbook install <id>');
  assert.doesNotMatch(PLAYBOOK_NEEDS_TERMINAL_EXPECTED_NOTE, /enter your password/i);
});

// ─── playbookRunStart ─────────────────────────────────────────────────────────

test('playbookRunStart: a non-empty job_id starts a run; defaults fill the rest', () => {
  assert.deepEqual(playbookRunStart({ ok: true, job_id: 'job-1', playbook: 'ollama', steps_total: 4 }), {
    ok: true, job_id: 'job-1', playbook: 'ollama', steps_total: 4,
  });
  assert.deepEqual(playbookRunStart({ job_id: ' job-2 ' }), { ok: true, job_id: 'job-2', playbook: '', steps_total: 0 });
  assert.equal(playbookRunStart({ job_id: 'job-3', steps_total: '5' }).steps_total, 5);
  assert.equal(playbookRunStart({ job_id: 'job-3', steps_total: -1 }).steps_total, 0);
  assert.equal(playbookRunStart({ job_id: 'job-3', steps_total: true }).steps_total, 0);
  assert.equal(playbookRunStart({ ok: false, job_id: 'job-4' }).ok, false);
});

test('playbookRunStart: no job_id means no run — the card settles from the envelope as before', () => {
  assert.equal(playbookRunStart({ ok: true, applied: true, summary: 'Enabled ssh' }), null);
  assert.equal(playbookRunStart({ job_id: '' }), null);
  assert.equal(playbookRunStart({ job_id: 42 }), null);
  assert.equal(playbookRunStart({ ok: false, needs_terminal: true, command: 'sudo apt-get install -y x' }), null);
  assert.equal(playbookRunStart(undefined), null);
  assert.equal(playbookRunStart(null), null);
  // The unwrapped handler answer is what the card reads, and the start rides there.
  const envelope = { ok: true, result: { ok: true, job_id: 'job-9', playbook: 'vllm', steps_total: 3 }, tool: 'playbook_install', safety_class: 'privileged' };
  assert.equal(playbookRunStart(unwrapToolResult(envelope)).job_id, 'job-9');
  assert.equal(playbookRunStart(envelope), null, 'the envelope itself carries no job_id');
});

// ─── playbookCatalogue ────────────────────────────────────────────────────────

const CATALOGUE_ROW = {
  id: 'open-webui',
  title: 'Open WebUI with Ollama',
  category: 'chat',
  summary: 'Install Open WebUI and use Ollama to chat with models on your Spark',
  requires_sudo: true,
  sudo_steps: 1,
  manual_steps: 3,
  estimated_minutes: 20,
  estimated_disk_gb: 32,
  rootless_alternative: null,
  installed: false,
  receipt_path: null,
  source_urls: ['https://github.com/NVIDIA/dgx-spark-playbooks/tree/main/nvidia/open-webui'],
};

test('playbookCatalogue: a bare list or an object carrying `playbooks` yields typed rows; unknown fields ride along', () => {
  const [fromList] = playbookCatalogue([CATALOGUE_ROW]);
  assert.equal(fromList.id, 'open-webui');
  assert.equal(fromList.requires_sudo, true);
  assert.equal(fromList.sudo_steps, 1);
  assert.equal(fromList.manual_steps, 3);
  assert.equal(fromList.estimated_minutes, 20);
  assert.equal(fromList.estimated_disk_gb, 32);
  assert.equal(fromList.rootless_alternative, null);
  assert.equal(fromList.installed, false);
  assert.deepEqual(fromList.source_urls, CATALOGUE_ROW.source_urls, 'unknown fields are kept');
  const [fromObject] = playbookCatalogue({ ok: true, playbooks: [CATALOGUE_ROW], count: 1 });
  assert.deepEqual(fromObject, fromList);
  assert.equal(playbookCatalogue({ items: [CATALOGUE_ROW] }).length, 1);
  assert.equal(playbookCatalogue({ catalogue: [CATALOGUE_ROW] }).length, 1);
});

test('playbookCatalogue: rows without a string id are dropped; fields are coerced with safe defaults', () => {
  const rows = playbookCatalogue([
    { id: 'ollama' },
    { id: '' },
    { id: 7, title: 'seven' },
    { title: 'no id' },
    null,
    'ollama',
    { id: ' vllm ', sudo_steps: '2', manual_steps: -3, estimated_minutes: 'x', estimated_disk_gb: '1.5', rootless_alternative: ' ', installed: 'yes', receipt_path: '' },
  ]);
  assert.deepEqual(rows.map(r => r.id), ['ollama', 'vllm']);
  const [ollama, vllm] = rows;
  assert.equal(ollama.title, 'ollama', 'a missing title falls back to the id');
  assert.equal(ollama.category, '');
  assert.equal(ollama.summary, '');
  assert.equal(ollama.requires_sudo, false);
  assert.equal(ollama.sudo_steps, 0);
  assert.equal(ollama.manual_steps, 0);
  assert.equal(ollama.estimated_minutes, null);
  assert.equal(ollama.estimated_disk_gb, null);
  assert.equal(ollama.rootless_alternative, null);
  assert.equal(ollama.installed, false);
  assert.equal(ollama.receipt_path, null);
  assert.equal(vllm.sudo_steps, 2);
  assert.equal(vllm.requires_sudo, true, 'sudo steps imply requires_sudo even when the flag is missing');
  assert.equal(vllm.manual_steps, 0);
  assert.equal(vllm.estimated_minutes, null);
  assert.equal(vllm.estimated_disk_gb, 1.5);
  assert.equal(vllm.rootless_alternative, null);
  assert.equal(vllm.installed, false, 'only a boolean true counts as installed');
  assert.equal(vllm.receipt_path, null);
});

test('playbookCatalogue: junk gives an empty catalogue, never a throw', () => {
  assert.deepEqual(playbookCatalogue(undefined), []);
  assert.deepEqual(playbookCatalogue(null), []);
  assert.deepEqual(playbookCatalogue('ollama'), []);
  assert.deepEqual(playbookCatalogue(42), []);
  assert.deepEqual(playbookCatalogue({ ok: true }), []);
  assert.deepEqual(playbookCatalogue({ playbooks: 'ollama' }), []);
  assert.deepEqual(playbookCatalogue([]), []);
});

// ─── playbookChips ────────────────────────────────────────────────────────────

test('playbookChips: the sudo chip is always first; manual steps and estimates follow when known', () => {
  assert.deepEqual(playbookChips(CATALOGUE_ROW), ['sudo: 1 step', '3 manual steps', '~20 min', '~32 GB']);
  assert.deepEqual(playbookChips({ requires_sudo: true, sudo_steps: 2, manual_steps: 1, estimated_minutes: 45.4, estimated_disk_gb: 2 }), ['sudo: 2 steps', '1 manual step', '~45 min', '~2 GB']);
  assert.deepEqual(playbookChips({ requires_sudo: false, sudo_steps: 0, manual_steps: 0, estimated_minutes: null, estimated_disk_gb: null }), ['no sudo']);
  // requires_sudo without a count still says sudo (an older server that sends only the flag).
  assert.deepEqual(playbookChips({ requires_sudo: true, sudo_steps: 0, manual_steps: 0, estimated_minutes: 0, estimated_disk_gb: 0 }), ['sudo']);
});

// ─── playbookPlanExtras / manual notes ────────────────────────────────────────

const PLAYBOOK_PLAN = {
  ok: true,
  setting: 'playbook_install',
  title: 'Install the open-webui playbook',
  commands: ['sudo usermod -aG docker alice', 'docker pull ghcr.io/open-webui/open-webui:ollama', 'docker run -d -p 8080:8080 --gpus=all --name open-webui ghcr.io/open-webui/open-webui:ollama'],
  sudo: true,
  changes: 'Adds you to the docker group, pulls the image and starts the container.',
  undo: ['docker stop open-webui', 'docker rm open-webui'],
  notes: ['MANUAL: log out and back in after the docker-group change', 'Image is ~7 GB.', 'MANUAL: create the admin account at http://localhost:8080'],
  warning: 'The cleanup commands delete all Open WebUI data.',
  id: 'open-webui',
  manual_steps: ['log out and back in after the docker-group change', 'create the admin account at http://localhost:8080'],
  verify: [['docker', 'ps', '--filter', 'name=^open-webui$'], 'curl -sI http://localhost:8080'],
  estimates: { minutes: 20, disk_gb: 32 },
  needs_terminal_expected: true,
};

test('playbookPlanExtras: the plan_dict extras come off the same `plan` key planLines reads', () => {
  const call = { name: 'playbook_install', arguments: { id: 'open-webui' }, plan: PLAYBOOK_PLAN };
  const extras = playbookPlanExtras(call);
  assert.equal(extras.id, 'open-webui');
  assert.deepEqual(extras.manualSteps, PLAYBOOK_PLAN.manual_steps);
  assert.deepEqual(extras.verify, ['docker ps --filter name=^open-webui$', 'curl -sI http://localhost:8080'], 'argv lists and strings both render');
  assert.equal(extras.estimatedMinutes, 20);
  assert.equal(extras.estimatedDiskGb, 32);
  assert.equal(extras.needsTerminalExpected, true);
  // The generic readers still work on the same plan.
  assert.deepEqual(planLines(call), PLAYBOOK_PLAN.commands);
  assert.equal(planNotes(call).warning, PLAYBOOK_PLAN.warning);
});

test('playbookPlanExtras: a bare plan, a command list or junk gives empty extras', () => {
  const empty = { id: '', manualSteps: [], verify: [], estimatedMinutes: null, estimatedDiskGb: null, needsTerminalExpected: false };
  assert.deepEqual(playbookPlanExtras({ plan: SERVER_PLAN }), empty);
  assert.deepEqual(playbookPlanExtras({ plan: ['sudo apt-get update'] }), empty);
  assert.deepEqual(playbookPlanExtras({}), empty);
  assert.deepEqual(playbookPlanExtras(undefined), empty);
  assert.deepEqual(playbookPlanExtras(null), empty);
  assert.deepEqual(playbookPlanExtras({ plan: { manual_steps: 'x', verify: 'y', estimates: 'z', needs_terminal_expected: 'yes' } }), empty);
  // One estimates shape: `plan_dict` sends `estimates: {minutes, disk_gb}`; the catalogue rows' flat
  // `estimated_*` fields are not read off a plan, so the two readers cannot drift apart.
  assert.equal(playbookPlanExtras({ plan: { estimated_minutes: 5 } }).estimatedMinutes, null);
  assert.equal(playbookPlanExtras({ plan: { estimates: { minutes: 5, disk_gb: 0.2 } } }).estimatedDiskGb, 0.2);
});

test('manual notes split: MANUAL: lines render as their own list and leave the plain notes', () => {
  const notes = planNotes({ plan: PLAYBOOK_PLAN }).notes;
  assert.deepEqual(manualStepsFromNotes(notes), PLAYBOOK_PLAN.manual_steps);
  assert.deepEqual(planNotesWithoutManual(notes), ['Image is ~7 GB.']);
  assert.deepEqual(manualStepsFromNotes(['manual: lower-case prefix', 'MANUAL:', '  MANUAL:   spaced  ']), ['lower-case prefix', 'spaced']);
  assert.deepEqual(planNotesWithoutManual(['MANUAL: x', 'keep', 'a MANUAL: in the middle stays']), ['keep', 'a MANUAL: in the middle stays']);
  assert.deepEqual(manualStepsFromNotes([]), []);
});

// ─── step index helpers ───────────────────────────────────────────────────────

test('playbookStepIndex / playbookStepNumber: the wire index is 0-based, the display is 1-based', () => {
  assert.equal(playbookStepIndex({ step: 0 }), 0);
  assert.equal(playbookStepIndex({ step: '2' }), 2);
  assert.equal(playbookStepIndex({ step_index: 3 }), 3);
  assert.equal(playbookStepIndex({ index: 1 }), 1);
  assert.equal(playbookStepIndex({ step: -1 }), null);
  assert.equal(playbookStepIndex({ step: 'two' }), null);
  assert.equal(playbookStepIndex({}), null);
  assert.equal(playbookStepIndex(undefined), null);
  assert.equal(playbookStepNumber(0), 1);
  assert.equal(playbookStepNumber(4), 5);
  assert.equal(playbookStepNumber(2.7), 3);
  assert.equal(playbookStepNumber(null), null);
  assert.equal(playbookStepNumber(undefined), null);
  assert.equal(playbookStepNumber(-1), null);
});

// ─── jobOutcome ───────────────────────────────────────────────────────────────

test('jobOutcome: complete → done, error → failed at the step, everything in between → null', () => {
  assert.deepEqual(jobOutcome({ event: 'complete', status: 'complete', message: 'open-webui installed' }), { kind: 'done', message: 'open-webui installed' });
  assert.deepEqual(
    jobOutcome({ event: 'error', status: 'failed', message: 'docker pull exited 1', step: 1, exit_code: 1 }),
    { kind: 'failed', step: 1, error: 'docker pull exited 1' },
  );
  // `error` beats `message` when both are present; a bare error still names a reason.
  assert.equal(jobOutcome({ event: 'error', status: 'failed', message: 'm', error: 'e' }).error, 'e');
  assert.equal(jobOutcome({ event: 'error', status: 'failed', message: '' }).error, 'failed');
  assert.equal(jobOutcome({ event: 'error', status: 'failed', message: 'no step here' }).step, null);
  for (const event of [
    { event: 'plan', status: 'running', message: '3 steps' },
    { event: 'step', status: 'running', message: 'Pull the image', step: 1 },
    { event: 'step', status: 'complete', message: 'Pull the image', step: 1, exit_code: 0 },
    { event: 'step', status: 'skipped', message: 'already in the docker group', step: 0 },
    { event: 'log', status: 'running', message: 'Downloading layer…', step: 1 },
    { event: 'progress', status: 'running', message: '' },
  ]) {
    assert.equal(jobOutcome(event), null, event.event);
  }
  assert.equal(jobOutcome(undefined), null);
  assert.equal(jobOutcome(null), null);
  assert.equal(jobOutcome('complete'), null);
});

test('jobOutcome: a step that reports failed ends the run (the runner stops at the first failure)', () => {
  assert.deepEqual(
    jobOutcome({ event: 'step', status: 'failed', message: 'exited 100', step: 2, exit_code: 100 }),
    { kind: 'failed', step: 2, error: 'exited 100' },
  );
});

test('jobOutcome: needs_terminal is a hand-off — the exact command, or the CLI line for the playbook', () => {
  const explicit = jobOutcome({ event: 'needs_terminal', status: 'complete', message: 'sudo needs a password', step: 0, command: 'nvh playbook install open-webui', hint: 'run it in your terminal' });
  assert.deepEqual(explicit, { kind: 'needs-terminal', command: 'nvh playbook install open-webui', hint: 'run it in your terminal', step: 0 });
  // No command on the event: the playbook id (event first, then the caller's) fills it.
  assert.equal(jobOutcome({ event: 'needs_terminal', status: 'running', message: 'x', playbook: 'vllm' }).command, 'nvh playbook install vllm');
  assert.equal(jobOutcome({ event: 'needs_terminal', status: 'running', message: 'x' }, 'tailscale').command, 'nvh playbook install tailscale');
  assert.equal(jobOutcome({ event: 'needs_terminal', status: 'running', message: 'x' }).command, 'nvh playbook install <id>');
  // An argv command renders joined; the hint falls back to the message.
  const argv = jobOutcome({ event: 'needs_terminal', status: 'running', message: 'password needed', command: ['nvh', 'playbook', 'install', 'ollama'] });
  assert.equal(argv.command, 'nvh playbook install ollama');
  assert.equal(argv.hint, 'password needed');
  // Never a failure, whatever the status says.
  assert.equal(jobOutcome({ event: 'needs_terminal', status: 'failed', message: 'x' }).kind, 'needs-terminal');
});

test('jobOutcome: a complete that carries halted is the docker-group stop — never Done', () => {
  const note = 'Log out and back in (and restart nvHive) so the docker group applies, then run this playbook again — finished steps are skipped.';
  assert.deepEqual(
    jobOutcome({ event: 'complete', status: 'complete', halted: true, partial: true, applied: true, message: note, step: 0 }),
    { kind: 'halted', message: note, step: 0 },
  );
  assert.equal(jobOutcome({ event: 'complete', status: 'complete', halted: true, message: note }).step, null);
  // Only the boolean counts; a plain complete is still done.
  assert.equal(jobOutcome({ event: 'complete', status: 'complete', halted: 'yes', message: 'x' }).kind, 'done');
  assert.equal(jobOutcome({ event: 'complete', status: 'complete', halted: false, message: 'x' }).kind, 'done');
});

// ─── applyPlaybookEvent ───────────────────────────────────────────────────────

function run(overrides = {}) {
  return initialPlaybookRun({ job_id: 'job-1', playbook: 'open-webui', steps_total: 3, ...overrides });
}

test('initialPlaybookRun: an empty run for the card, the fallback id filling a blank playbook', () => {
  assert.deepEqual(run(), { jobId: 'job-1', playbook: 'open-webui', stepsTotal: 3, steps: [], outcome: null });
  assert.equal(initialPlaybookRun({ job_id: 'j', playbook: '', steps_total: 0 }, 'ollama').playbook, 'ollama');
  assert.equal(initialPlaybookRun({ job_id: 'j', playbook: 'vllm', steps_total: 0 }, 'ollama').playbook, 'vllm');
});

test('applyPlaybookEvent: step events upsert rows in index order and never mutate the input', () => {
  const s0 = run();
  const s1 = applyPlaybookEvent(s0, { event: 'step', status: 'running', message: 'Check the docker group', step: 0, title: 'Check the docker group', command: ['id', '-nG'], sudo: false });
  assert.equal(s0.steps.length, 0, 'input untouched');
  assert.deepEqual(s1.steps, [{ index: 0, title: 'Check the docker group', command: 'id -nG', sudo: false, status: 'running', exitCode: null, log: [] }]);
  const s2 = applyPlaybookEvent(s1, { event: 'step', status: 'skipped', message: 'already a member', step: 0, exit_code: 0 });
  assert.equal(s2.steps[0].status, 'skipped');
  assert.equal(s2.steps[0].exitCode, 0);
  assert.equal(s2.steps[0].title, 'Check the docker group', 'a patch without a title keeps the old one');
  const s3 = applyPlaybookEvent(s2, { event: 'step', status: 'running', message: 'Pull', step: 2, title: 'Pull the image', command: 'docker pull ghcr.io/open-webui/open-webui:ollama' });
  const s4 = applyPlaybookEvent(s3, { event: 'step', status: 'running', message: 'usermod', step: 1, title: 'Join the docker group', command: 'sudo usermod -aG docker alice', sudo: true });
  assert.deepEqual(s4.steps.map(s => s.index), [0, 1, 2], 'rows stay sorted by index');
  assert.equal(s4.steps[1].sudo, true);
  assert.equal(s4.outcome, null);
  // A step complete with an exit code.
  const s5 = applyPlaybookEvent(s4, { event: 'step', status: 'complete', message: 'done', step: 1, exit_code: 0 });
  assert.equal(s5.steps[1].status, 'ok');
  assert.equal(s5.steps[1].exitCode, 0);
  // A step without an index is ignored (nothing to attach it to).
  assert.deepEqual(applyPlaybookEvent(s5, { event: 'step', status: 'running', message: 'orphan' }).steps, s5.steps);
});

test('applyPlaybookEvent: plan refreshes the totals; the playbook id is filled only when blank', () => {
  const s = applyPlaybookEvent(initialPlaybookRun({ job_id: 'j', playbook: '', steps_total: 0 }), { event: 'plan', status: 'running', message: '4 steps', steps_total: 4, playbook: 'vllm' });
  assert.equal(s.stepsTotal, 4);
  assert.equal(s.playbook, 'vllm');
  const kept = applyPlaybookEvent(run(), { event: 'plan', status: 'running', message: '', steps_total: 0, playbook: 'other' });
  assert.equal(kept.stepsTotal, 3, 'a zero total does not erase a known one');
  assert.equal(kept.playbook, 'open-webui');
});

test('applyPlaybookEvent: log lines attach to the named step, else the running step, else the last; the newest N are kept', () => {
  let s = applyPlaybookEvent(run(), { event: 'step', status: 'running', message: 'Pull', step: 1, title: 'Pull the image' });
  s = applyPlaybookEvent(s, { event: 'log', status: 'running', message: 'layer 1/9  ', step: 1 });
  s = applyPlaybookEvent(s, { event: 'log', status: 'running', message: 'layer 2/9' });
  assert.deepEqual(s.steps[0].log, ['layer 1/9', 'layer 2/9'], 'trailing whitespace trimmed; a log without a step lands on the running step');
  // Blank logs are dropped; a log for a step nobody announced still creates the row.
  s = applyPlaybookEvent(s, { event: 'log', status: 'running', message: '   ', step: 1 });
  assert.equal(s.steps[0].log.length, 2);
  s = applyPlaybookEvent(s, { event: 'log', status: 'running', message: 'early', step: 0 });
  assert.equal(s.steps[0].index, 0);
  assert.deepEqual(s.steps[0].log, ['early']);
  assert.equal(s.steps[0].status, 'pending');
  // Throttle: only the newest PLAYBOOK_LOG_LINES_PER_STEP survive.
  for (let i = 0; i < PLAYBOOK_LOG_LINES_PER_STEP + 5; i++) {
    s = applyPlaybookEvent(s, { event: 'log', status: 'running', message: `line ${i}`, step: 1 });
  }
  const pull = s.steps.find(x => x.index === 1);
  assert.equal(pull.log.length, PLAYBOOK_LOG_LINES_PER_STEP);
  assert.equal(pull.log[pull.log.length - 1], `line ${PLAYBOOK_LOG_LINES_PER_STEP + 4}`);
  assert.equal(pull.log[0], 'line 5');
  // With no step at all a log has nowhere to go and is dropped.
  const empty = applyPlaybookEvent(run(), { event: 'log', status: 'running', message: 'nowhere' });
  assert.deepEqual(empty.steps, []);
  // A log after the running step settled falls back to the last step.
  let settled = applyPlaybookEvent(run(), { event: 'step', status: 'complete', message: '', step: 0, title: 'a' });
  settled = applyPlaybookEvent(settled, { event: 'log', status: 'running', message: 'tail' });
  assert.deepEqual(settled.steps[0].log, ['tail']);
});

test('applyPlaybookEvent: complete settles the run and closes any step still marked running', () => {
  let s = applyPlaybookEvent(run(), { event: 'step', status: 'running', message: '', step: 0, title: 'a' });
  s = applyPlaybookEvent(s, { event: 'complete', status: 'complete', message: 'open-webui installed' });
  assert.deepEqual(s.outcome, { kind: 'done', message: 'open-webui installed' });
  assert.equal(s.steps[0].status, 'ok');
  assert.equal(playbookCardStatus(s.outcome), 'ok');
  assert.equal(playbookOutcomeLabel(s.outcome), 'Done');
});

test('applyPlaybookEvent: error settles the run as failed at the step it names, with the exit code', () => {
  let s = applyPlaybookEvent(run(), { event: 'step', status: 'running', message: '', step: 1, title: 'Pull the image', command: 'docker pull x' });
  s = applyPlaybookEvent(s, { event: 'error', status: 'failed', message: 'docker pull exited 1', step: 1, exit_code: 1 });
  assert.deepEqual(s.outcome, { kind: 'failed', step: 1, error: 'docker pull exited 1' });
  assert.equal(s.steps[0].status, 'failed');
  assert.equal(s.steps[0].exitCode, 1);
  assert.equal(s.steps[0].title, 'Pull the image');
  assert.equal(playbookCardStatus(s.outcome), 'error');
  assert.equal(playbookOutcomeLabel(s.outcome), 'Failed at step 2: docker pull exited 1');
  // An error without a step still fails the run, with a plain label.
  const noStep = applyPlaybookEvent(run(), { event: 'error', status: 'failed', message: 'runner crashed' });
  assert.equal(playbookOutcomeLabel(noStep.outcome), 'Failed: runner crashed');
  assert.equal(playbookOutcomeLabel({ kind: 'failed', step: null, error: '' }), 'Failed');
});

test('applyPlaybookEvent: needs_terminal settles as a hand-off and a later complete does not overwrite it', () => {
  let s = applyPlaybookEvent(run(), { event: 'step', status: 'complete', message: '', step: 0, title: 'Check the docker group', exit_code: 1 });
  s = applyPlaybookEvent(s, { event: 'needs_terminal', status: 'complete', message: 'sudo needs a password here', step: 1, title: 'Join the docker group', command: 'nvh playbook install open-webui', sudo: true });
  assert.deepEqual(s.outcome, { kind: 'needs-terminal', command: 'nvh playbook install open-webui', hint: 'sudo needs a password here', step: 1 });
  assert.equal(s.steps[1].status, 'needs-terminal');
  assert.equal(s.steps[1].title, 'Join the docker group');
  assert.equal(s.steps[1].command, '', 'the hand-off command is the CLI line, not the step command');
  const after = applyPlaybookEvent(s, { event: 'complete', status: 'complete', message: 'job ended' });
  assert.equal(after.outcome.kind, 'needs-terminal', 'first outcome wins');
  assert.equal(playbookCardStatus(after.outcome), 'needs-terminal');
  assert.equal(playbookOutcomeLabel(after.outcome), 'Stopped: needs a terminal — run: nvh playbook install open-webui');
  // The chat history line built from that status names the exact command.
  const history = historyToolOutcome('playbook_install', playbookCardStatus(after.outcome), playbookOutcomeLabel(after.outcome), { command: after.outcome.command });
  assert.deepEqual(history, { name: 'playbook_install', ok: false, summary: 'needs a terminal: nvh playbook install open-webui' });
});

test('applyPlaybookEvent: a halted complete settles the run as halted — the step that ran closes, the card is never Done', () => {
  const note = 'Log out and back in (and restart nvHive) so the docker group applies, then run this playbook again — finished steps are skipped.';
  let s = applyPlaybookEvent(run(), { event: 'step', status: 'running', message: '', step: 0, title: 'Join the docker group', command: 'sudo usermod -aG docker alice', sudo: true });
  s = applyPlaybookEvent(s, { event: 'step', status: 'complete', message: 'done', step: 0, exit_code: 0 });
  s = applyPlaybookEvent(s, { event: 'log', status: 'running', message: `MANUAL: ${note}`, step: 0 });
  s = applyPlaybookEvent(s, { event: 'complete', status: 'complete', halted: true, partial: true, applied: true, message: note });
  assert.equal(s.outcome.kind, 'halted');
  assert.equal(s.steps[0].status, 'ok', 'the usermod step itself completed; the run stopped after it');
  assert.equal(playbookCardStatus(s.outcome), 'halted');
  assert.equal(playbookOutcomeLabel(s.outcome), `Stopped for you to act: ${note}`);
  assert.notEqual(playbookOutcomeLabel(s.outcome), 'Done');
  assert.equal(playbookOutcomeLabel({ kind: 'halted', message: '', step: null }), 'Stopped for you to act — then run the playbook again');
  // Amber "your turn" chrome — settled, not pending, not the green Done, not the red failure.
  const chrome = cardChrome('privileged', 'halted');
  assert.equal(chrome.badge, 'Stopped — your turn');
  assert.notEqual(chrome.badge, '✓ Done');
  assert.notEqual(chrome.badge, '✗ Failed');
  assert.equal(chrome.clickable, false);
  assert.equal(isPendingStatus('halted'), false);
  // The next turn's history tells the model the install did not finish.
  const history = historyToolOutcome('playbook_install', 'halted', playbookOutcomeLabel(s.outcome));
  assert.equal(history.ok, false);
  assert.match(history.summary, /^stopped for the user to act — not installed yet: Stopped for you to act/);
  // A later job-complete (the poller's terminal status) does not turn it into Done.
  assert.equal(settlePlaybookRun(s, 'complete', 'Install job complete.').outcome.kind, 'halted');
  assert.equal(applyPlaybookEvent(s, { event: 'complete', status: 'complete', message: 'job ended' }).outcome.kind, 'halted');
});

// ─── settlePlaybookRun ────────────────────────────────────────────────────────

test('settlePlaybookRun: the job ended without a settling event — complete is done, anything else failed', () => {
  let s = applyPlaybookEvent(run(), { event: 'step', status: 'running', message: '', step: 2, title: 'Start' });
  const done = settlePlaybookRun(s, 'complete', 'all steps ran');
  assert.deepEqual(done.outcome, { kind: 'done', message: 'all steps ran' });
  assert.equal(done.steps[0].status, 'ok');
  const failed = settlePlaybookRun(s, 'failed', 'Install job failed');
  assert.deepEqual(failed.outcome, { kind: 'failed', step: 2, error: 'Install job failed' });
  assert.equal(failed.steps[0].status, 'failed');
  assert.equal(settlePlaybookRun(run(), 'canceled', '').outcome.error, 'job canceled');
  assert.equal(settlePlaybookRun(run(), '', '').outcome.error, 'job ended');
  assert.equal(settlePlaybookRun(run(), 'failed', 'x').outcome.step, null, 'no running step → no step number');
  // An outcome already on the run always wins: a hand-off followed by a failed job status stays a hand-off.
  s = applyPlaybookEvent(s, { event: 'needs_terminal', status: 'running', message: 'password', step: 2 });
  assert.equal(settlePlaybookRun(s, 'failed', 'Install job failed').outcome.kind, 'needs-terminal');
  assert.equal(settlePlaybookRun(s, 'complete', '').outcome.kind, 'needs-terminal');
});

// ─── playbookCardStatus / playbookOutcomeLabel / playbookProgressLabel ────────

test('playbookCardStatus: running until settled; the three outcomes map to the three card statuses', () => {
  assert.equal(playbookCardStatus(null), 'running');
  assert.equal(playbookCardStatus(undefined), 'running');
  assert.equal(playbookCardStatus({ kind: 'done', message: '' }), 'ok');
  assert.equal(playbookCardStatus({ kind: 'needs-terminal', command: 'nvh playbook install x', hint: '', step: null }), 'needs-terminal');
  assert.equal(playbookCardStatus({ kind: 'failed', step: 0, error: 'x' }), 'error');
  // The hand-off never wears the failure chrome.
  assert.notEqual(cardChrome('privileged', playbookCardStatus({ kind: 'needs-terminal', command: 'x', hint: '', step: null })).badge, '✗ Failed');
});

test('playbookOutcomeLabel: empty while running; the three fixed shapes once settled', () => {
  assert.equal(playbookOutcomeLabel(null), '');
  assert.equal(playbookOutcomeLabel(undefined), '');
  assert.equal(playbookOutcomeLabel({ kind: 'done', message: 'whatever' }), 'Done');
  assert.equal(playbookOutcomeLabel({ kind: 'needs-terminal', command: 'nvh playbook install ollama', hint: '', step: 0 }), 'Stopped: needs a terminal — run: nvh playbook install ollama');
  assert.equal(playbookOutcomeLabel({ kind: 'failed', step: 0, error: 'exit 1' }), 'Failed at step 1: exit 1');
});

test('playbookProgressLabel: step N of total while running, counts settled steps otherwise', () => {
  assert.equal(playbookProgressLabel(run()), '0 of 3 steps');
  assert.equal(playbookProgressLabel(initialPlaybookRun({ job_id: 'j', playbook: '', steps_total: 0 })), 'starting');
  let s = applyPlaybookEvent(run(), { event: 'step', status: 'running', message: '', step: 1 });
  assert.equal(playbookProgressLabel(s), 'step 2 of 3');
  s = applyPlaybookEvent(s, { event: 'step', status: 'complete', message: '', step: 1 });
  assert.equal(playbookProgressLabel(s), 'step 1 of 3', 'one settled step, none running');
  const noTotal = applyPlaybookEvent(initialPlaybookRun({ job_id: 'j', playbook: '', steps_total: 0 }), { event: 'step', status: 'running', message: '', step: 0 });
  assert.equal(playbookProgressLabel(noTotal), 'step 1');
});
