'use client';

/**
 * The body of a privileged approval card — the server's dry run, rendered the
 * same way wherever a plan is approved: the Wizard chat's tool card
 * (components/WizardChat.tsx) and the Setup page's "Spark playbooks" cards
 * (app/setup/page.tsx).
 *
 * In DOM order: the exact commands, a deny-list refusal, what changes, the
 * warning, the "sudo will want a password here" note, the manual steps
 * (shown, never run), the undo preview, the verify commands and the remaining
 * notes. Everything is read off `source.plan` through the pure helpers in
 * lib/privileged.ts — never a tool call's `arguments`, never fetched — so
 * what the user approves is what the server said it would run. Rendered
 * before the buttons so a screen reader reaches the plan before the control
 * that approves it.
 */

import {
  PLAYBOOK_MANUAL_NOTE,
  PLAYBOOK_NEEDS_TERMINAL_EXPECTED_NOTE,
  manualStepsFromNotes,
  planLines,
  planNotes,
  planNotesWithoutManual,
  playbookPlanExtras,
} from '@/lib/privileged';

/** Amber for cautions the user must read before approving. */
const WARNING_COLOR = '#d97706';

export default function PlanDetails({
  source,
  borderColor = null,
  commandsLabel = 'Commands this tool will run',
}: {
  /** The surfaced call or the execute answer carrying the server's `plan`. */
  source: { plan?: unknown } | undefined | null;
  /** The host card's border (the red of a privileged card), or `null` for the themed default. */
  borderColor?: string | null;
  /** Accessible name of the commands block. */
  commandsLabel?: string;
}) {
  const plan = planLines(source);
  const notes = planNotes(source);
  // Playbook plans add manual steps (browser logins, TUIs, cabling — shown,
  // never run), verify commands and whether sudo is expected to want a
  // password. Empty for every other tool's plan.
  const extras = playbookPlanExtras(source);
  const manualSteps = extras.manualSteps.length > 0 ? extras.manualSteps : manualStepsFromNotes(notes.notes);
  const plainNotes = planNotesWithoutManual(notes.notes);
  const refusalColor = borderColor ?? '#dc2626';

  return (
    <>
      {plan.length > 0 && (
        <pre
          className="mt-1 overflow-x-auto rounded-sm border p-1.5 font-mono text-[10px] leading-relaxed"
          style={{
            background: 'var(--bg-card)',
            borderColor: borderColor ?? 'var(--border)',
            color: 'var(--text-primary)',
          }}
          aria-label={commandsLabel}
        >
          {plan.join('\n')}
        </pre>
      )}
      {notes.error && (
        <div className="mt-1 text-[10px]" style={{ color: refusalColor }} role="note">
          Refused: {notes.error}
        </div>
      )}
      {notes.changes && (
        <div className="mt-1 text-[10px]" style={{ color: 'var(--text-secondary)' }}>
          {notes.changes}
        </div>
      )}
      {notes.warning && (
        <div className="mt-1 text-[10px] font-semibold" style={{ color: WARNING_COLOR }} role="note">
          Warning: {notes.warning}
        </div>
      )}
      {extras.needsTerminalExpected && (
        <div className="mt-1 text-[10px]" style={{ color: WARNING_COLOR }} role="note">
          {PLAYBOOK_NEEDS_TERMINAL_EXPECTED_NOTE}
        </div>
      )}
      {manualSteps.length > 0 && (
        <div className="mt-1 text-[10px]" style={{ color: 'var(--text-secondary)' }} aria-label="Manual steps">
          <div className="font-semibold">Manual steps — {PLAYBOOK_MANUAL_NOTE}:</div>
          {manualSteps.map((step, i) => (
            <div key={`manual-${i}`}>{i + 1}. {step}</div>
          ))}
        </div>
      )}
      {notes.undo.length > 0 && (
        <div className="mt-1 font-mono text-[10px]" style={{ color: 'var(--text-muted)' }}>
          undo (preview only): {notes.undo.join(' ; ')}
        </div>
      )}
      {extras.verify.length > 0 && (
        <div className="mt-1 font-mono text-[10px]" style={{ color: 'var(--text-muted)' }}>
          verify: {extras.verify.join(' ; ')}
        </div>
      )}
      {plainNotes.length > 0 && (
        <div className="mt-1 text-[10px]" style={{ color: 'var(--text-muted)' }}>
          {plainNotes.map((note, i) => (
            <div key={`note-${i}`}>{note}</div>
          ))}
        </div>
      )}
    </>
  );
}
