'use client';

/**
 * The live step log of a `playbook-run` job — one component for both places
 * that stream a run: the Wizard chat's tool card (components/WizardChat.tsx)
 * and the Setup page's "Spark playbooks" cards (app/setup/page.tsx). One row
 * per step (mark, number, title, `sudo` tag, exit code), the rendered command
 * under it, the newest log lines (`PLAYBOOK_LOG_LINES_PER_STEP`) and, once
 * the run has settled, the verdict — `Done`, `Stopped for you to act: …` (the
 * docker-group re-login), `Stopped: needs a terminal — run: nvh playbook
 * install <id>` (the copy button is the caller's hand-off block, which reads
 * the same command) or `Failed at step N` with the error.
 *
 * Display only: every value comes off the job's events, folded by the pure
 * reducers in lib/privileged.ts. Themed with the app's CSS variables so light
 * and dark mode read the same in both hosts.
 */

import {
  PRIVILEGED_COLOR,
  playbookOutcomeLabel,
  playbookProgressLabel,
  playbookStepNumber,
  type PlaybookRunOutcome,
  type PlaybookRunState,
  type PlaybookStepStatus,
} from '@/lib/privileged';

/** Amber: the user's turn (a hand-off or a deliberate stop), never a failure. */
const YOUR_TURN_COLOR = '#d97706';
const DONE_COLOR = '#16a34a';

/** The glyph, colour and accessible label for each step state. */
export const PLAYBOOK_STEP_MARK: Record<PlaybookStepStatus, { mark: string; color: string; label: string }> = {
  pending: { mark: '·', color: 'var(--text-faint)', label: 'pending' },
  running: { mark: '…', color: YOUR_TURN_COLOR, label: 'running' },
  ok: { mark: '✓', color: DONE_COLOR, label: 'done' },
  skipped: { mark: '↷', color: 'var(--text-muted)', label: 'skipped (already in place)' },
  failed: { mark: '✗', color: PRIVILEGED_COLOR, label: 'failed' },
  'needs-terminal': { mark: '→', color: YOUR_TURN_COLOR, label: 'needs a terminal' },
};

/** The verdict line's colour: green when done, amber for the user's turn, red for a failure, muted while running. */
export function playbookOutcomeColor(outcome: PlaybookRunOutcome | null | undefined): string {
  if (!outcome) return 'var(--text-muted)';
  switch (outcome.kind) {
    case 'done':
      return DONE_COLOR;
    case 'halted':
    case 'needs-terminal':
      return YOUR_TURN_COLOR;
    case 'failed':
      return PRIVILEGED_COLOR;
  }
}

export default function PlaybookRunLog({
  run,
  borderColor = null,
}: {
  run: PlaybookRunState;
  /** The host card's border (the red of a privileged card), or `null` for the themed default. */
  borderColor?: string | null;
}) {
  const label = playbookOutcomeLabel(run.outcome);
  return (
    <div
      className="mt-1 rounded-sm border p-1.5 font-mono text-[10px] leading-relaxed"
      style={{ background: 'var(--bg-card)', borderColor: borderColor ?? 'var(--border)' }}
      role="log"
      aria-live="polite"
      aria-label={`Playbook run ${run.playbook || run.jobId}`}
    >
      <div className="flex flex-wrap items-baseline justify-between gap-2" style={{ color: 'var(--text-muted)' }}>
        <span>{run.playbook || 'playbook'} · job {run.jobId}</span>
        {!run.outcome && <span>{playbookProgressLabel(run)}</span>}
      </div>
      {run.steps.map(step => {
        const mark = PLAYBOOK_STEP_MARK[step.status];
        return (
          <div key={step.index} className="mt-1">
            <div className="flex flex-wrap items-baseline gap-2">
              <span style={{ color: mark.color }} title={mark.label} aria-label={mark.label}>{mark.mark}</span>
              <span style={{ color: 'var(--text-primary)' }}>
                {playbookStepNumber(step.index)}. {step.title || step.command || '(step)'}
              </span>
              {step.sudo && (
                <span className="text-[9px] uppercase tracking-[0.14em]" style={{ color: PRIVILEGED_COLOR }}>sudo</span>
              )}
              {step.exitCode !== null && (
                <span style={{ color: step.exitCode === 0 ? 'var(--text-faint)' : PRIVILEGED_COLOR }}>exit {step.exitCode}</span>
              )}
            </div>
            {step.command && step.title && (
              <div className="ml-4 overflow-x-auto whitespace-pre" style={{ color: 'var(--text-secondary)' }}>{step.command}</div>
            )}
            {step.log.length > 0 && (
              <pre className="ml-4 max-h-24 overflow-auto whitespace-pre-wrap" style={{ color: 'var(--text-muted)' }}>
                {step.log.join('\n')}
              </pre>
            )}
          </div>
        );
      })}
      {label && (
        <div className="mt-1 font-semibold" style={{ color: playbookOutcomeColor(run.outcome) }} role="status">
          {label}
        </div>
      )}
    </div>
  );
}
