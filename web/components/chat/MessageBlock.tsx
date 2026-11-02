'use client';

/**
 * One row of the Wizard thread: a system line, a user bubble (with its image
 * thumbnails) or an assistant bubble — specialist header, the answer, the
 * server tool trace, deferred-call notes, sources, the fallback / budget
 * banners, the meter footer and the tool cards. Extracted from the Wizard
 * thread (chat/WizardThread.tsx), which owns the `Message` state
 * (chat/types.ts) and the card handlers passed in as props.
 *
 * The status dot and the "offline helper" footer decide by the reason the
 * message carries (`isDeliberateRefusalMessage`), never by `mode` alone — a
 * pinned local-only specialist declining is its answer, not the offline
 * helper's; tests/test_web_wizard_refusal_status.py pins that split here.
 */

import AgentAvatar from '@/components/AgentAvatar';
import {
  isWizardDeliberateRefusal,
  type AgentProfileSchema,
  type WizardChatToolCall,
  type WizardToolSchema,
} from '@/lib/api';
import { formatSize } from '@/lib/attachments';
import { costPill, formatWizardCost } from '@/lib/cost';
import DeferredToolNotes from './DeferredToolNotes';
import ServerToolTrace from './ServerToolTrace';
import SourcesFooter from './SourcesFooter';
import ToolCard from './ToolCard';
import type { Message } from './types';

/**
 * True when a rendered deterministic message is a specialist's deliberate
 * refusal (a pinned local-only profile declining because no local provider
 * was up) rather than the offline helper covering a failed model path.
 *
 * Same predicate as the banner and the mascot (`isWizardDeliberateRefusal`),
 * fed the reason the message carries — from the live `error` event or the
 * persisted wizard-meta tail — so the footer and the avatar's status dot
 * cannot drift from what the header already credits to the specialist.
 */
function isDeliberateRefusalMessage(message: Message): boolean {
  return (
    message.mode === 'deterministic'
    && isWizardDeliberateRefusal({ fallback_reason: message.fallbackReason })
  );
}

/** The footer's cost pill: "$x" when priced, "$0.0000" when a priced model
 * cost nothing (never hidden), "cost unknown" with a hover explanation when
 * the catalog has no price for the model (`cost_known: false`) — lib/cost.ts. */
function CostPillSpan({ costUsd, costKnown }: { costUsd?: number; costKnown?: boolean }) {
  const pill = costPill(costUsd, costKnown, formatWizardCost);
  if (!pill) return null;
  return (
    <span
      title={pill.title}
      style={pill.title ? { cursor: 'help' } : undefined}
      data-cost-state={pill.state}
    >
      · {pill.text}
    </span>
  );
}

// Accent colors per built-in profile — keep in sync with the SVG spec in
// nvh/integrations/wizard/avatars.py so the bubble border matches the
// avatar background a user sees in the picker.
const PROFILE_ACCENTS: Record<string, string> = {
  wizard: '#76B900',
  coder: '#0ea5e9',
  researcher: '#a855f7',
  writer: '#f59e0b',
  ops: '#dc2626',
  'vault-rag': '#10b981',
};

function statusForMessage(message: Message): { color: string; label: string } | null {
  if (message.mode === 'deterministic') {
    // A pinned local-only specialist declining is ITS answer, not the offline
    // helper's: keep the local green the specialist would normally wear
    // (nothing left the box) and say so on hover. Only a genuine fallback —
    // the helper standing in for a failed model path — is grey "offline".
    if (isDeliberateRefusalMessage(message)) {
      return { color: '#76B900', label: 'declined' };
    }
    return { color: '#737373', label: 'offline' };
  }
  if (message.fallbackFrom) {
    return { color: '#d97706', label: 'fallback' };
  }
  if ((message.usedProvider ?? '').toLowerCase() === 'ollama') {
    return { color: '#76B900', label: 'local' };
  }
  if (message.usedProvider) {
    return { color: '#0ea5e9', label: 'cloud' };
  }
  return null;
}

export default function MessageBlock({
  message,
  tools,
  profileMap,
  privilegedEnabled,
  onConfirmTool,
  onDismissTool,
}: {
  message: Message;
  tools: Map<string, WizardToolSchema>;
  profileMap: Map<string, AgentProfileSchema>;
  /** `privileged_enabled` from the tool list; undefined = the API never said. */
  privilegedEnabled?: boolean;
  onConfirmTool: (call: WizardChatToolCall) => void;
  onDismissTool: (call: WizardChatToolCall) => void;
}) {
  if (message.role === 'system') {
    return (
      <div className="text-[10px] font-mono" style={{ color: 'var(--text-muted)' }}>
        {message.content}
      </div>
    );
  }
  const isUser = message.role === 'user';
  const profile = !isUser && message.agentProfile
    ? profileMap.get(message.agentProfile) ?? null
    : null;
  const accent = profile ? PROFILE_ACCENTS[profile.name] ?? '#76B900' : null;
  const status = !isUser ? statusForMessage(message) : null;
  return (
    <div className={`flex items-end gap-2 ${isUser ? 'justify-end' : 'justify-start'}`}>
      {!isUser && (
        <div className="relative flex-shrink-0">
          <AgentAvatar profile={profile} size="md" />
          {status && (
            <span
              className="absolute -bottom-0.5 -right-0.5 h-2 w-2 rounded-full border"
              style={{ background: status.color, borderColor: 'var(--bg-card)' }}
              title={status.label}
            />
          )}
        </div>
      )}
      <div
        className={`max-w-[80%] rounded-lg border px-3 py-2 text-sm whitespace-pre-wrap ${
          isUser ? '' : 'shadow-sm'
        }`}
        style={{
          background: isUser ? '#f7fdf0' : 'var(--bg-card)',
          borderColor: isUser
            ? 'var(--border-green)'
            : (accent ? `${accent}55` : 'var(--border)'),
          borderLeftWidth: !isUser && accent ? '3px' : undefined,
          borderLeftColor: !isUser && accent ? accent : undefined,
          color: 'var(--text-primary)',
        }}
      >
        {!isUser && profile && (
          <div
            className="mb-1 text-[10px] font-mono font-bold uppercase tracking-[0.14em]"
            style={{ color: accent ?? '#76B900' }}
            title={message.profileReason ?? undefined}
          >
            {profile.title}
          </div>
        )}
        <div>{message.content}</div>

        {isUser && message.attachments && message.attachments.length > 0 && (
          <div className="mt-2 flex flex-wrap gap-1.5" aria-label="Attached images">
            {message.attachments.map(a => (
              // eslint-disable-next-line @next/next/no-img-element
              <img
                key={a.id}
                src={a.previewUrl}
                alt={a.name}
                title={`${a.name} · ${formatSize(a.size)}`}
                className="h-16 max-w-[8rem] rounded-sm border object-cover"
                style={{ borderColor: 'var(--border-green)' }}
              />
            ))}
          </div>
        )}

        {!isUser && message.serverToolTrace && message.serverToolTrace.length > 0 && (
          <ServerToolTrace
            trace={message.serverToolTrace}
            // An auto turn streams under the general-Wizard placeholder until
            // `done` names who answered; a refusal seen before then belongs to
            // "this specialist", not to "AI Wizard".
            specialist={
              message.attributionPending
                ? 'this specialist'
                : (profile?.title ?? message.agentProfile ?? 'this specialist')
            }
          />
        )}

        {!isUser && message.deferredToolCalls && message.deferredToolCalls.length > 0 && (
          <DeferredToolNotes calls={message.deferredToolCalls} />
        )}

        {!isUser && message.serverToolTrace && message.serverToolTrace.length > 0 && (
          <SourcesFooter trace={message.serverToolTrace} />
        )}

        {!isUser && message.fallbackFrom && (
          <div
            className="mt-1 rounded-sm border px-2 py-1 text-[10px] font-mono"
            style={{
              borderColor: '#d97706',
              background: 'rgba(217,119,6,0.08)',
              color: '#92400e',
            }}
          >
            Routed via {message.usedProvider ?? 'fallback'} — original target
            {' '}({message.fallbackFrom}) was unavailable.
          </div>
        )}

        {!isUser && message.costCeilingHit && (
          <div
            className="mt-1 rounded-sm border px-2 py-1 text-[10px] font-mono"
            style={{
              borderColor: '#d97706',
              background: 'rgba(217,119,6,0.08)',
              color: '#92400e',
            }}
          >
            Stopped at the profile&apos;s budget
            {typeof message.costCeilingUsd === 'number' && (
              <> (${message.costCeilingUsd.toFixed(2)}/turn)</>
            )}
            . Follow-up tool calls were skipped to stay under cost.
          </div>
        )}

        {message.mode === 'deterministic' && !isDeliberateRefusalMessage(message) && (
          <div className="mt-1 text-[9px] font-mono uppercase tracking-[0.14em]" style={{ color: 'var(--text-faint)' }}>
            offline helper
          </div>
        )}
        {message.mode === 'llm' && message.usedProvider && (
          <div className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-0.5 text-[9px] font-mono uppercase tracking-[0.14em]" style={{ color: 'var(--text-faint)' }}>
            <span
              title={message.routingReason ?? 'Why this provider was picked is unavailable for this turn.'}
              style={{ cursor: message.routingReason ? 'help' : undefined }}
            >
              {message.usedProvider}{message.usedModel ? ` · ${message.usedModel.replace(/^.*\//, '')}` : ''}
            </span>
            {message.iterations && message.iterations > 1 && (
              <span>· {message.iterations} round-trips</span>
            )}
            {typeof message.latencyMs === 'number' && message.latencyMs > 0 && (
              <span>· {(message.latencyMs / 1000).toFixed(1)}s</span>
            )}
            <CostPillSpan costUsd={message.costUsd} costKnown={message.costKnown} />
          </div>
        )}

        {message.toolCalls?.map(call => (
          <ToolCard
            key={call.name}
            call={call}
            schema={tools.get(call.name)}
            status={message.toolStatus?.[call.name] ?? 'idle'}
            result={message.toolResults?.[call.name]}
            detail={message.toolDetails?.[call.name]}
            run={message.playbookRuns?.[call.name]}
            privilegedEnabled={privilegedEnabled}
            onConfirm={() => onConfirmTool(call)}
            onDismiss={() => onDismissTool(call)}
          />
        ))}
      </div>
    </div>
  );
}
