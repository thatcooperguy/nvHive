'use client';

import type { WizardDeferredToolCall } from '@/lib/api';
import { deferredReasonLabel } from '@/lib/privileged';

/**
 * DeferredToolNotes lists the calls the server chose NOT to run this turn —
 * an auto-class call behind Depth 1 or the profile's cost ceiling, or (0.46)
 * anything held behind an unanswered question (`question_pending`: a second
 * `ask_user`, or a confirm / privileged sibling the model made conditional
 * on the answer) — with the reason in the user's words
 * (`deferredReasonLabel`; the depth / ceiling reasons keep the server's
 * wording). Informational only — these never execute client-side; the user
 * can answer the question, raise Depth or ask again. Kept out of
 * ServerToolTrace so "used N tools" stays an honest count of what actually
 * ran.
 */
export default function DeferredToolNotes({ calls }: { calls: WizardDeferredToolCall[] }) {
  return (
    <div
      className="mt-1 space-y-0.5 text-[10px] font-mono"
      style={{ color: 'var(--text-faint)' }}
      aria-label="Tools not run this turn"
    >
      {calls.map((c, i) => {
        const reason = deferredReasonLabel(c.reason);
        return (
          <div key={`${c.name}-${i}`}>
            not run: <span style={{ color: 'var(--text-muted)' }}>{c.name}</span>
            {reason ? ` — ${reason}` : ''}
          </div>
        );
      })}
    </div>
  );
}
