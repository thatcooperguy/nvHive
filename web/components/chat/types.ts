/**
 * The Wizard thread's message model — shared by the thread that owns the
 * state (chat/WizardThread.tsx) and the bubble that renders it
 * (chat/MessageBlock.tsx). Types only: no React, no DOM, so either side can
 * import it without a cycle.
 */

import type {
  WizardChatToolCall,
  WizardChatToolResult,
  WizardDeferredToolCall,
  WizardToolExecuteResult,
} from '@/lib/api';
import type { AttachmentPreview } from '@/lib/attachments';
import type { PlaybookRunState, ToolCardStatus } from '@/lib/privileged';

export interface Message {
  id: string;
  role: 'user' | 'assistant' | 'system';
  content: string;
  // User turns only: the images that rode on this turn's request, kept as
  // thumbnails for the bubble. Display only — the base64 payload went out
  // once and is not stored on the message or re-sent in history.
  attachments?: AttachmentPreview[];
  // User turns only: what the server actually ran for this turn when images
  // were attached — the typed text plus the `Attached images (…): <paths>`
  // line, rebuilt from `done.attachment_paths`. The bubble shows `content`;
  // the next request's history sends this, so the model can name the paths
  // again on a follow-up ("now read the second one").
  historyContent?: string;
  toolCalls?: WizardChatToolCall[];
  toolStatus?: Record<string, ToolCardStatus>;
  toolResults?: Record<string, string>;
  // The raw execute response per tool, kept alongside the summary string so
  // the card can render what only the structured result carries: the sudo
  // `plan`, the `needs_terminal` hand-off command and its hint, and the
  // `disabled` refusal. Display only — nothing here is re-executed.
  toolDetails?: Record<string, WizardToolExecuteResult>;
  // A confirmed `playbook_install` does not finish inside the execute call: the
  // handler starts a `playbook-run` job and the card streams it. Per tool,
  // the folded job state (steps, log lines, outcome) the live step log draws.
  playbookRuns?: Record<string, PlaybookRunState>;
  // Auto-class calls the server skipped this turn (depth 1 / cost ceiling),
  // with its reason. Rendered as muted "not run" lines; never executed here.
  deferredToolCalls?: WizardDeferredToolCall[];
  // Auto-class tool results that ran server-side inside the follow-up loop.
  // Surfaced as a compact "Wizard's reasoning" trace below the answer so the
  // user can see exactly what fired and what came back. Builds trust in the
  // model's grounding (RAG, web search, etc.) without dumping raw JSON.
  // Whitelist refusals (`result.not_allowed`) ride along in the same list
  // (that is how the stream delivers them) but ServerToolTrace / Sources
  // split them out: they never ran, so they are neither counted nor cited.
  serverToolTrace?: WizardChatToolResult[];
  iterations?: number;
  mode?: 'llm' | 'deterministic';
  usedProvider?: string | null;
  usedModel?: string | null;
  // Cost + latency + fallback signal for the meter footer. All optional —
  // the streaming path only fills these on `done`, the non-streaming path
  // fills them at result time. Footer hides values that are zero/missing,
  // except the cost: a priced $0 shows as "$0.0000" and `costKnown === false`
  // (no catalog price for the model) shows as "cost unknown" — lib/cost.ts.
  costUsd?: number;
  costKnown?: boolean;
  latencyMs?: number;
  fallbackFrom?: string | null;
  // Why no LLM answered a deterministic turn — the stream `error` event's
  // `fallback_reason`, or `fallback_reason` from the persisted wizard-meta
  // tail on reload. Its VALUE, not its presence, tells a specialist's
  // deliberate refusal from the offline helper standing in for a failed
  // model path (`isDeliberateRefusalMessage`); the footer and the status
  // dot must never decide that from `mode` alone.
  fallbackReason?: string | null;
  // Profile cost-ceiling diagnostics. costCeilingHit=true triggers an inline
  // banner so the user understands why the follow-up loop stopped early.
  costCeilingHit?: boolean;
  costCeilingUsd?: number | null;
  // Router's one-line explanation for why this provider/model was chosen.
  // Surfaces as a tooltip on the provider chip in the message footer.
  routingReason?: string | null;
  // Name of the agent profile that produced this reply ("wizard", "coder",
  // …). Seeded at send time (an auto turn shows the general Wizard while
  // streaming) and replaced by `used_profile` on `done`, so the bubble keeps
  // showing the right avatar even after the user swaps profiles later.
  agentProfile?: string;
  // Why the concierge routed this turn to that specialist (tooltip text).
  profileReason?: string | null;
  // Raw `used_profile` from the server: null = the general Wizard answered,
  // undefined = unknown (older API / row persisted before attribution). Echoed back in the next
  // turn's history so the concierge can stay with the same specialist.
  usedProfile?: string | null;
  // True while an auto turn streams: `agentProfile` is the general-Wizard
  // placeholder, not who is answering, so per-specialist text (the whitelist
  // refusal line) must stay generic. Cleared when `done` or an attributed
  // `error` names the specialist. Never set on pinned turns or hydrated rows.
  attributionPending?: boolean;
}
