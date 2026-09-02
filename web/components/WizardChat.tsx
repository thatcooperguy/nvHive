'use client';

/**
 * WizardChat — the conversational AI Wizard surface.
 *
 * Wires together the three wizard backends shipped earlier:
 *  - /v1/wizard/chat       — live-state-grounded LLM answer
 *  - /v1/wizard/tools/execute — confirm-aware tool execution
 *  - tool_calls in the chat response → inline action cards
 *
 * Flow per user message:
 *   1. POST /v1/wizard/chat with the question + recent history
 *   2. Render the assistant answer; if tool_calls came back, render each
 *      as an inline action card under the message.
 *   3. Surface a "Run" / "Skip" card for every call in `confirm_required`
 *      (and any that only `done.tool_calls` carried). Nothing auto-runs
 *      client-side: the server already ran every auto-class call it was
 *      willing to, so whatever reaches the UI needs a click (the server
 *      re-checks `confirmed` on execute). Auto-class calls the server chose
 *      not to run (depth 1, cost ceiling) arrive as `deferred_tool_calls` and
 *      are listed as "not run: <reason>"; whitelist refusals arrive as
 *      `tool_result` with `not_allowed` and are listed as "not allowed for
 *      <specialist>: <tool>" — neither is executed or counted as used.
 *   4. After execution, append a small system message showing what ran.
 *
 * Stream `error` events always carry the deterministic text that saved the
 * turn (`fallback`) plus a `fallback_reason`. The bubble shows the text every
 * time; the red banner shows unless the reason is a specialist's deliberate
 * refusal (`isWizardDeliberateRefusal`), because then the text IS the answer
 * — see `wizardErrorBanner`. The mascot applies the same split: the whole
 * event (reason included) goes to `applyMascotEvent`, and
 * `deriveMascotState` lands a refusal like `done` instead of in the error
 * strip. So does the bubble itself: the reason is kept on the message
 * (`fallbackReason`, from the event or the persisted wizard-meta tail) and
 * `isDeliberateRefusalMessage` decides the footer and the avatar's status
 * dot — a refusal is the specialist's answer (no "offline helper" line, a
 * 'declined' dot), while a genuine deterministic fallback keeps the offline
 * footer and the grey dot. Nothing in the bubble keys on `mode` alone.
 *
 * Profiles: the composer defaults to `auto` (the concierge picks a hidden
 * specialist per turn); the bubble credits whoever answered and that
 * attribution rides along in the next turn's history (`used_profile`) and in
 * the persisted wizard-meta tail so it survives a reload. While an auto turn
 * streams, `agentProfile` is only the general-Wizard placeholder
 * (`attributionPending`), so nothing in the bubble may pin a refusal on it.
 */

import { useSearchParams } from 'next/navigation';
import { useCallback, useEffect, useRef, useState } from 'react';
import AgentAvatar from '@/components/AgentAvatar';
import AgentProfilePicker from '@/components/AgentProfilePicker';
import CreateAgentModal from '@/components/CreateAgentModal';
import {
  AUTO_PROFILE,
  GENERAL_PROFILE,
  announceConversationsChanged,
  isAutoProfile,
  isWizardDeliberateRefusal,
  createConversation,
  executeWizardTool,
  getConversation,
  isRefusedToolResult,
  listAgentProfiles,
  listWizardTools,
  pinConversation,
  saveVaultMemory,
  uploadAndIngest,
  wizardChatStream,
  wizardDiagnostics,
  type AgentProfileSchema,
  type WizardChatToolCall,
  type WizardChatToolResult,
  type WizardChatTurn,
  type WizardDeferredToolCall,
  type WizardDiagnosticsPayload,
  type WizardStreamEvent,
  type WizardToolSchema,
} from '@/lib/api';
import {
  applyMascotEvent,
  markMascotTipProbed,
  mascotTipProbeDue,
  noteMascotTyping,
  sayMascotTip,
  setMascotState,
} from '@/lib/mascot';
import { metaNumber, parseWizardMeta } from '@/lib/wizardMeta';

interface Message {
  id: string;
  role: 'user' | 'assistant' | 'system';
  content: string;
  toolCalls?: WizardChatToolCall[];
  toolStatus?: Record<string, ToolCardStatus>;
  toolResults?: Record<string, string>;
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
  // fills them at result time. Footer hides values that are zero/missing.
  costUsd?: number;
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

type ToolCardStatus = 'idle' | 'running' | 'ok' | 'error' | 'awaiting-confirm' | 'dismissed';

function makeId(): string {
  return `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
}

/** Confirm cards on a message the user has neither run nor skipped. */
function pendingConfirmCalls(message: Message | undefined, except?: string): WizardChatToolCall[] {
  if (!message?.toolCalls) return [];
  return message.toolCalls.filter(call => {
    if (call.name === except) return false;
    const status = message.toolStatus?.[call.name] ?? 'idle';
    return status === 'idle' || status === 'awaiting-confirm';
  });
}

/**
 * Every call that reaches the UI needs a click. The server has already run
 * every auto-class call it was willing to run (and deferred / refused the
 * rest), so nothing here is a candidate for client-side auto-execution —
 * regardless of what the local tool catalog says its safety class is.
 */
function awaitingConfirm(
  calls: WizardChatToolCall[],
  prev: Record<string, ToolCardStatus> | undefined,
): Record<string, ToolCardStatus> {
  const next: Record<string, ToolCardStatus> = { ...(prev ?? {}) };
  for (const call of calls) {
    if (!next[call.name] || next[call.name] === 'idle') next[call.name] = 'awaiting-confirm';
  }
  return next;
}

/**
 * Attribution from a persisted wizard-meta tail. `used_profile` present and
 * null means the general Wizard answered; the key being absent means a row
 * persisted before attribution existed (unknown → no avatar / title).
 */
function hydrateAttribution(
  meta: Record<string, unknown> | null,
): Pick<Message, 'agentProfile' | 'profileReason' | 'usedProfile'> {
  if (!meta || !('used_profile' in meta)) return {};
  const used = typeof meta.used_profile === 'string' && meta.used_profile ? meta.used_profile : null;
  const reason =
    typeof meta.profile_reason === 'string' && meta.profile_reason ? meta.profile_reason : undefined;
  return { usedProfile: used, agentProfile: used ?? GENERAL_PROFILE, profileReason: reason };
}

/**
 * Banner text for a stream `error` event, or null for no banner.
 *
 * The server sets `fallback_reason` on EVERY error event that ended in a
 * deterministic answer, so gating the banner on its presence hides every
 * genuine failure. The value is what matters: a local-only specialist
 * declining an explicit pin is an answer (the bubble carries it, attributed —
 * no banner); "engine not initialized" or an LLM exception means the offline
 * helper stood in for a failed model path, and the banner says so with the
 * server's reason. An error with no fallback text at all is just an error.
 */
function wizardErrorBanner(event: Extract<WizardStreamEvent, { type: 'error' }>): string | null {
  if (isWizardDeliberateRefusal(event)) return null;
  if (event.fallback) {
    return `The offline helper answered because the model path failed: ${event.error}`;
  }
  return event.error;
}

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

/** `fallback_reason` from a persisted wizard-meta tail (deterministic rows only). */
function metaFallbackReason(meta: Record<string, unknown> | null): string | null | undefined {
  if (!meta || !('fallback_reason' in meta)) return undefined;
  return typeof meta.fallback_reason === 'string' && meta.fallback_reason ? meta.fallback_reason : null;
}

/** Deferred (not-run) auto-class calls from a persisted wizard-meta tail. */
function metaDeferredToolCalls(meta: Record<string, unknown> | null): WizardDeferredToolCall[] | undefined {
  const raw = meta?.deferred_tool_calls;
  if (!Array.isArray(raw)) return undefined;
  const out: WizardDeferredToolCall[] = [];
  for (const item of raw) {
    const r = (item ?? {}) as Record<string, unknown>;
    if (typeof r.name !== 'string' || !r.name) continue;
    out.push({
      name: r.name,
      arguments: (r.arguments && typeof r.arguments === 'object' ? r.arguments : {}) as Record<string, unknown>,
      reason: typeof r.reason === 'string' ? r.reason : undefined,
    });
  }
  return out.length > 0 ? out : undefined;
}

export default function WizardChat() {
  const [messages, setMessages] = useState<Message[]>([]);
  // Latest committed messages, for handlers that inspect state after an
  // await (e.g. "are confirm cards still pending?") without a stale closure.
  const messagesRef = useRef<Message[]>([]);
  useEffect(() => {
    messagesRef.current = messages;
  }, [messages]);
  const [draft, setDraft] = useState('');
  const [sending, setSending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [tools, setTools] = useState<Map<string, WizardToolSchema>>(new Map());
  // Profile catalog cached so MessageBlock can render the matching avatar
  // without each bubble making its own /v1/wizard/profiles call.
  const [profileMap, setProfileMap] = useState<Map<string, AgentProfileSchema>>(new Map());
  // One-line "thinking…" / "calling X…" status under the spinner. Drives the
  // perceived-latency win — even before the first token arrives, the user sees
  // the Wizard moving.
  const [iterationStatus, setIterationStatus] = useState<string | null>(null);
  // Drag-drop ingest state. dragActive controls the overlay; uploading is the
  // "we're embedding your files right now" indicator under the chat.
  const [dragActive, setDragActive] = useState(false);
  const [uploading, setUploading] = useState(false);
  // Conversation id is created on first user message — until then chats are
  // not persisted. /save, /pin, and reconnect-resume all key off this id.
  const [conversationId, setConversationId] = useState<string | null>(null);
  // Active agent profile name. Defaults to `auto`: the concierge picks a
  // hidden specialist per turn (the API treats null / "" / "auto" as auto).
  // Anything else — including "wizard", the general persona — is an explicit
  // pin, set from the Advanced picker, the /agents cards or ?profile=.
  const [profile, setProfile] = useState<string>(AUTO_PROFILE);
  const [creatingAgent, setCreatingAgent] = useState(false);
  // The profile picker lives behind this disclosure so the primary composer
  // row stays "type and Send" (proposal §3.1: the picker leaves the composer).
  const [advancedOpen, setAdvancedOpen] = useState(false);
  // Tool-budget slider: 1 = "just answer", 3 = "let it chain". Sent on every
  // turn so the user can dial the Wizard's chattiness per question.
  const [maxIterations, setMaxIterations] = useState<number>(3);
  const scrollRef = useRef<HTMLDivElement>(null);
  // Abort any in-flight stream when the user re-sends or unmounts.
  const abortRef = useRef<AbortController | null>(null);
  // Setup-page bridge: when the user clicks a System Check item we navigate
  // here with ?issue=<finding_id>. We fetch the matching finding from
  // /v1/wizard/diagnostics, seed the draft with "Help me with: <title>", and
  // auto-send so the conversation lands directly on the issue.
  const searchParams = useSearchParams();
  const issueHandledRef = useRef(false);
  // Ref so the URL-param effect can invoke send() without a temporal-dead-zone
  // reference to a function declared further down the component body.
  const sendRef = useRef<(() => Promise<void>) | null>(null);
  // /v1/wizard/diagnostics is a full workspace probe (~360 ms). Two things on
  // this page may want it — the ?issue= deep link and the mascot tip — so it
  // is fetched at most once per mount and only when something asks for it.
  const diagnosticsRef = useRef<Promise<WizardDiagnosticsPayload | null> | null>(null);
  const loadDiagnostics = useCallback((): Promise<WizardDiagnosticsPayload | null> => {
    if (!diagnosticsRef.current) {
      diagnosticsRef.current = wizardDiagnostics().catch(() => null);
    }
    return diagnosticsRef.current;
  }, []);

  /** Slash-command handler. Returns true if the input was consumed as a
   * command (so caller should NOT forward it to the Wizard chat). */
  const handleSlashCommand = async (raw: string): Promise<boolean> => {
    const text = raw.trim();
    if (!text.startsWith('/')) return false;
    const [cmd, ...rest] = text.slice(1).split(/\s+/);
    const arg = rest.join(' ').trim();

    const announce = (content: string) => {
      setMessages(prev => [...prev, { id: makeId(), role: 'system', content }]);
    };

    switch (cmd.toLowerCase()) {
      case 'help':
      case '?':
        announce(
          'Available commands: /save [title] (save chat to vault), ' +
            '/pin (pin this chat for reconnect resume), ' +
            '/clear (reset this chat), ' +
            '/tools (list available Wizard tools), ' +
            '/help (this message).',
        );
        return true;
      case 'clear':
        abortRef.current?.abort();
        setMessages([]);
        setConversationId(null);
        // Drop the resume param so a reload starts fresh instead of
        // resurrecting the thread the user just cleared — and re-arm the
        // resume guard so re-selecting the same chat from the sidebar
        // hydrates again.
        resumeHandledRef.current = null;
        if (typeof window !== 'undefined') {
          const url = new URL(window.location.href);
          url.searchParams.delete('conversation');
          window.history.replaceState(null, '', url.toString());
        }
        return true;
      case 'tools': {
        const names = Array.from(tools.values())
          .map(t => `• ${t.name} [${t.safety_class}] — ${t.description}`)
          .join('\n');
        announce(names || 'No tools registered.');
        return true;
      }
      case 'pin': {
        if (!conversationId) {
          announce('No active conversation to pin yet — say something first.');
          return true;
        }
        const ok = await pinConversation(conversationId, true);
        if (ok) announceConversationsChanged();
        announce(ok ? '✓ Pinned this chat. It will surface on next reconnect.' : 'Could not pin (server unavailable).');
        return true;
      }
      case 'save': {
        // Roll the conversation into a Markdown vault note. Doesn't require
        // a conversation_id since we have the in-memory thread already.
        const title = arg || `Wizard chat ${new Date().toISOString().slice(0, 16)}`;
        const body = messages
          .filter(m => m.role !== 'system')
          .map(m => `## ${m.role === 'user' ? 'You' : 'Wizard'}\n\n${m.content}`)
          .join('\n\n');
        if (!body.trim()) {
          announce('Nothing to save yet.');
          return true;
        }
        try {
          await saveVaultMemory({ title, body, category: 'wizard-chats', tags: ['wizard'] });
          announce(`✓ Saved to your vault: ${title}`);
        } catch (err) {
          announce(`✗ Save failed: ${err instanceof Error ? err.message : 'unknown error'}`);
        }
        return true;
      }
      default:
        announce(`Unknown command: /${cmd}. Type /help for the list.`);
        return true;
    }
  };

  const handleDrop = async (event: React.DragEvent<HTMLDivElement>) => {
    event.preventDefault();
    setDragActive(false);
    const files = Array.from(event.dataTransfer?.files ?? []);
    if (files.length === 0) return;
    setUploading(true);
    setError(null);
    try {
      const result = await uploadAndIngest(files);
      const dropId = makeId();
      const summary = result.ok
        ? `Indexed ${result.files_ingested ?? 0} file${result.files_ingested === 1 ? '' : 's'} into the RAG store (${result.chunks ?? 0} chunks). You can now ask questions about them.`
        : `Upload-ingest failed: ${result.error ?? 'unknown error'}`;
      setMessages(prev => [
        ...prev,
        { id: dropId, role: 'system', content: summary },
      ]);
      if (result.hint) {
        setMessages(prev => [
          ...prev,
          { id: makeId(), role: 'system', content: result.hint as string },
        ]);
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Upload failed');
    } finally {
      setUploading(false);
    }
  };

  // Load the tool catalog + profile catalog once so message bubbles can
  // render avatars and tool-safety badges without per-bubble API hits.
  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const [tList, pList] = await Promise.all([
          listWizardTools(),
          listAgentProfiles().catch(() => ({ profiles: [] as AgentProfileSchema[] })),
        ]);
        if (cancelled) return;
        const tmap = new Map<string, WizardToolSchema>();
        for (const t of tList.tools) tmap.set(t.name, t);
        setTools(tmap);
        const pmap = new Map<string, AgentProfileSchema>();
        for (const p of pList.profiles) pmap.set(p.name, p);
        setProfileMap(pmap);
      } catch {
        // Endpoints missing on older builds — fall back gracefully.
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  // Mascot nudge: the most severe open finding, as a speech bubble. The probe
  // behind it runs only while there is a mascot to show it (not hidden) and
  // at most once per browser session; it shares one fetch with the ?issue=
  // deep link. The finding id is burned inside sayMascotTip only when the
  // bubble actually renders, and the session flag is set only after that.
  useEffect(() => {
    if (!mascotTipProbeDue()) return;
    let cancelled = false;
    void loadDiagnostics().then(diag => {
      if (cancelled || !diag) return; // offline: leave the flag unset, retry next mount
      const topFinding =
        diag.findings.find(f => f.severity === 'error') ?? diag.findings.find(f => f.severity === 'warn');
      if (topFinding) {
        sayMascotTip(`Heads up: ${topFinding.title}`, { id: `finding:${topFinding.id}`, ttlMs: 15_000 });
      }
      markMascotTipProbed();
    });
    return () => {
      cancelled = true;
    };
  }, [loadDiagnostics]);

  // Auto-scroll to bottom on new messages.
  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: 'smooth' });
  }, [messages]);

  // Deep-link from the /agents discovery page: `?profile=<name>` pins the
  // matching agent profile so the next user message routes through that
  // persona + LLM mapping. Applied ONCE per distinct URL value — after that
  // the picker owns the state, so a user arriving from /agents can switch
  // back to Auto without the still-present param re-pinning them. (Depending
  // on `profile` here would do exactly that on every change.)
  const appliedProfileParamRef = useRef<string | null>(null);
  useEffect(() => {
    const requested = searchParams?.get('profile') ?? null;
    if (appliedProfileParamRef.current === requested) return;
    appliedProfileParamRef.current = requested;
    if (requested) setProfile(requested);
  }, [searchParams]);

  // Deep-link from the setup page: either ?issue=<finding_id> (we look up the
  // matching finding from /v1/wizard/diagnostics so the starter cites the real
  // title) or ?starter=<text> (free-form starter, no lookup). Whichever lands,
  // we auto-send so the conversation opens on the issue. Best-effort: any
  // failure leaves the chat empty.
  useEffect(() => {
    const issueId = searchParams?.get('issue');
    const rawStarter = searchParams?.get('starter');
    if (!issueId && !rawStarter) return;
    if (issueHandledRef.current) return;
    issueHandledRef.current = true;
    let cancelled = false;
    void (async () => {
      let starter = rawStarter ?? '';
      if (issueId) {
        // One probe per mount, shared with the mascot tip effect above.
        const d = await loadDiagnostics();
        if (cancelled) return;
        const match = d?.findings.find(f => f.id === issueId);
        starter = match
          ? `Help me with: ${match.title}`
          : `Help me with issue: ${issueId}`;
      }
      if (!starter || cancelled) return;
      setDraft(starter);
      // One tick so React commits the draft state before send() reads it.
      setTimeout(() => {
        if (!cancelled && sendRef.current) void sendRef.current();
      }, 0);
    })();
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [searchParams]);

  // Resume a persisted conversation: /wizard?conversation=<id> (the shared
  // sidebar routes wizard-mode chats here). Hydrates the visible thread from
  // the server and re-arms conversationId so new turns append to the same
  // conversation.
  //
  // The handled-guard is set only AFTER hydration commits: under React
  // StrictMode's double-invoked effects the first run's fetch is discarded
  // by its cleanup, so marking the id handled up front would leave the
  // second run skipping and the chat permanently empty in dev.
  const resumeHandledRef = useRef<string | null>(null);
  useEffect(() => {
    const resumeId = searchParams?.get('conversation');
    if (!resumeId || resumeHandledRef.current === resumeId) return;
    let cancelled = false;
    void (async () => {
      const detail = await getConversation(resumeId);
      if (cancelled) return;
      // 404/offline: leave the guard unset so a later re-select retries.
      if (!detail) return;
      resumeHandledRef.current = resumeId;
      abortRef.current?.abort();
      // A zero-message conversation (created lazily, tab closed before the
      // first turn persisted) still becomes the ACTIVE thread — replying
      // must append to the conversation the user clicked, not fork a new one.
      const hydrated: Message[] = (detail.messages ?? [])
        .filter(m => m.role !== 'system')
        .map(m => {
          if (m.role === 'user') {
            return { id: m.id, role: 'user' as const, content: m.content };
          }
          const { text, meta } = parseWizardMeta(m.content);
          return {
            id: m.id,
            role: 'assistant' as const,
            content: text,
            // Restore the meter footer: persisted rows carry provider/model
            // on the message and cost/latency in the meta tail.
            mode: (meta?.mode === 'deterministic' ? 'deterministic' : 'llm') as Message['mode'],
            // A deterministic row's reason survives too, so a reloaded
            // specialist refusal still renders as a refusal (no offline
            // footer / grey dot) — `_TurnSetup.meta_for` persists it.
            fallbackReason: metaFallbackReason(meta),
            usedProvider: m.provider || undefined,
            usedModel: m.model || undefined,
            iterations: metaNumber(meta?.iterations),
            costUsd: metaNumber(meta?.cost_usd),
            latencyMs: metaNumber(meta?.latency_ms),
            // Attribution survives reload: the tail carries used_profile
            // (null = general Wizard) and profile_reason once the API persists them.
            ...hydrateAttribution(meta),
            deferredToolCalls: metaDeferredToolCalls(meta),
          };
        });
      setMessages(hydrated);
      setConversationId(resumeId);
    })();
    return () => {
      cancelled = true;
    };
  }, [searchParams]);

  // Mascot follow-up once a confirm card settles: back to `asking` while
  // sibling cards still wait, otherwise the outcome (happy / error / idle).
  const settleMascot = (messageId: string, callName: string, outcome: 'happy' | 'error' | 'idle') => {
    const msg = messagesRef.current.find(m => m.id === messageId);
    setMascotState(pendingConfirmCalls(msg, callName).length > 0 ? 'asking' : outcome);
  };

  // "Skip": leave a confirm-class call un-run. The card stays as a record of
  // what the Wizard proposed; the mascot stops asking once nothing is pending.
  const dismissTool = (messageId: string, call: WizardChatToolCall) => {
    setMessages(prev => prev.map(m =>
      m.id === messageId
        ? {
          ...m,
          toolStatus: { ...(m.toolStatus ?? {}), [call.name]: 'dismissed' },
        }
        : m,
    ));
    settleMascot(messageId, call.name, 'idle');
  };

  const runTool = async (
    messageId: string,
    call: WizardChatToolCall,
    confirmed: boolean,
  ): Promise<void> => {
    setMessages(prev => prev.map(m =>
      m.id === messageId
        ? {
          ...m,
          toolStatus: { ...(m.toolStatus ?? {}), [call.name]: 'running' },
        }
        : m,
    ));

    try {
      const result = await executeWizardTool(call.name, {
        arguments: call.arguments,
        confirmed,
      });

      if (result.needs_confirmation) {
        setMessages(prev => prev.map(m =>
          m.id === messageId
            ? {
              ...m,
              toolStatus: { ...(m.toolStatus ?? {}), [call.name]: 'awaiting-confirm' },
              toolResults: {
                ...(m.toolResults ?? {}),
                [call.name]: result.summary ?? 'Needs your confirmation.',
              },
            }
            : m,
        ));
        setMascotState('asking');
        return;
      }

      const summary = result.ok
        ? formatToolResultSummary(call.name, result.result)
        : `Tool failed: ${result.error ?? 'unknown error'}`;

      const nextStatus: 'ok' | 'error' = result.ok ? 'ok' : 'error';
      setMessages(prev => [
        ...prev.map(m =>
          m.id === messageId
            ? {
              ...m,
              toolStatus: { ...(m.toolStatus ?? {}), [call.name]: nextStatus },
              toolResults: { ...(m.toolResults ?? {}), [call.name]: summary },
            }
            : m,
        ),
        {
          id: makeId(),
          role: 'system' as const,
          content: result.ok
            ? `✓ ${call.name} — ${summary}`
            : `✗ ${call.name} — ${summary}`,
        },
      ]);
      settleMascot(messageId, call.name, result.ok ? 'happy' : 'error');
    } catch (err) {
      const message = err instanceof Error ? err.message : 'Tool call failed';
      setMessages(prev => prev.map(m =>
        m.id === messageId
          ? {
            ...m,
            toolStatus: { ...(m.toolStatus ?? {}), [call.name]: 'error' },
            toolResults: { ...(m.toolResults ?? {}), [call.name]: message },
          }
          : m,
      ));
      settleMascot(messageId, call.name, 'error');
    }
  };

  // `confirm_required` (and `done.tool_calls`): show a Run / Skip card for
  // every call. No client-side auto-run branch exists any more — the server
  // owns auto-class execution and only hands the UI what needs a human.
  const surfaceConfirmCards = (messageId: string, calls: WizardChatToolCall[]) => {
    if (calls.length === 0) return;
    setMessages(prev => prev.map(m =>
      m.id === messageId
        ? { ...m, toolCalls: calls, toolStatus: awaitingConfirm(calls, m.toolStatus) }
        : m,
    ));
  };

  const send = async () => {
    const text = draft.trim();
    if (!text || sending) return;

    // Slash commands are handled locally and never sent to the LLM.
    if (text.startsWith('/')) {
      setDraft('');
      const consumed = await handleSlashCommand(text);
      if (consumed) return;
    }

    setError(null);
    setSending(true);
    setIterationStatus(null);

    // Abort any prior stream — the user just sent a new turn.
    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;

    // Lazily create a conversation on the first real message so /save and
    // /pin have something to attach to. Failures are silent — chat still
    // works without persistence.
    let convId = conversationId;
    if (!convId) {
      // Empty title → the backend auto-titles from this first message.
      const conv = await createConversation('', 'wizard');
      if (conv?.id) {
        convId = conv.id;
        setConversationId(conv.id);
        // Stamp the URL so a reload resumes this thread. replaceState (not
        // router.replace) so the resume effect doesn't re-fire and hydrate
        // over the live in-memory messages; belt-and-braces, also mark the
        // id as handled.
        resumeHandledRef.current = conv.id;
        if (typeof window !== 'undefined') {
          const url = new URL(window.location.href);
          url.searchParams.set('conversation', conv.id);
          window.history.replaceState(null, '', url.toString());
        }
        // Refresh mounted sidebars so the new thread appears without a
        // navigation (the LayoutShell listener refetches on this event).
        announceConversationsChanged();
      }
    }

    const userMsg: Message = { id: makeId(), role: 'user', content: text };
    const assistantId = makeId();
    const assistantSeed: Message = {
      id: assistantId,
      role: 'assistant',
      content: '',
      toolCalls: [],
      toolStatus: {},
      serverToolTrace: [],
      // Placeholder while streaming: an auto turn shows the general Wizard
      // until `done` names the specialist that actually answered.
      agentProfile: isAutoProfile(profile) ? GENERAL_PROFILE : profile,
      attributionPending: isAutoProfile(profile),
    };
    setMessages(prev => [...prev, userMsg, assistantSeed]);
    setDraft('');

    const history: WizardChatTurn[] = messages
      .filter(m => m.role !== 'system')
      .slice(-12)
      .map(m => ({
        role: m.role as 'user' | 'assistant',
        content: m.content,
        // Continuity: assistant turns name the specialist that produced them
        // so the concierge can stay with it on a weak follow-up ("and then?").
        ...(m.role === 'assistant' && m.usedProfile !== undefined ? { used_profile: m.usedProfile } : {}),
      }));

    const updateAssistant = (mut: (m: Message) => Message) => {
      setMessages(prev => prev.map(m => (m.id === assistantId ? mut(m) : m)));
    };

    // Confirm-class calls this turn has surfaced so far. The server's `error`
    // event does not repeat them (only `confirm_required` / `done` do), so the
    // mascot mapping gets them attached: an error after confirm_required must
    // resume `asking`, not idle under cards that still need a click.
    //
    // The event is spread, not rebuilt, so `fallback_reason` reaches the
    // mapping as well: `deriveMascotState` tells a specialist's deliberate
    // refusal (an answer — no banner, no error strip) from a genuine failure
    // by that value, exactly as `wizardErrorBanner` does in the `error` case
    // below. The bare `{ type: 'error' }` the catch block publishes carries
    // no reason and is always a genuine failure.
    let pendingConfirm: WizardChatToolCall[] = [];
    const publishMascot = (event: WizardStreamEvent | { type: 'error' }) => {
      applyMascotEvent(event.type === 'error' ? { ...event, tool_calls: pendingConfirm } : event);
    };

    try {
      for await (const event of wizardChatStream(text, {
        history,
        conversationId: convId ?? undefined,
        profile,
        maxIterations,
        signal: controller.signal,
      })) {
        publishMascot(event);
        switch (event.type) {
          case 'iteration':
            setIterationStatus(event.n === 1 ? 'thinking…' : `reacting (round ${event.n})…`);
            break;
          case 'token':
            updateAssistant(m => ({ ...m, content: m.content + event.text }));
            break;
          case 'tool_call':
            setIterationStatus(`calling ${event.name}…`);
            break;
          case 'tool_result':
            // Ran server-side OR refused by the whitelist (result.not_allowed).
            // Both land in the trace; the renderers split them so refusals are
            // never counted as used tools or mined for sources.
            updateAssistant(m => ({
              ...m,
              serverToolTrace: [
                ...(m.serverToolTrace ?? []),
                {
                  name: event.name,
                  // Stream events don't echo the args back; the trace card
                  // tolerates an empty object and only renders args when present.
                  arguments: {} as Record<string, unknown>,
                  result: event.result as WizardChatToolResult['result'],
                },
              ],
            }));
            break;
          case 'confirm_required':
            pendingConfirm = event.tool_calls ?? [];
            surfaceConfirmCards(assistantId, event.tool_calls);
            break;
          case 'done':
            updateAssistant(m => ({
              ...m,
              // Server's final answer is authoritative — replace whatever
              // tokens accumulated to handle the rare case where token+done
              // diverge (e.g. a tool result rewrites the text).
              content: event.answer || m.content || '(empty response)',
              mode: 'llm',
              usedProvider: event.used_provider,
              usedModel: event.used_model,
              iterations: event.iterations,
              costUsd: event.cost_usd,
              latencyMs: event.latency_ms,
              fallbackFrom: event.fallback_from,
              costCeilingHit: event.cost_ceiling_hit,
              costCeilingUsd: event.cost_ceiling_usd,
              routingReason: event.routing_reason,
              // Attribution: the concierge may have routed this turn to a
              // hidden specialist. null = the general Wizard answered;
              // undefined = an older API without the field (keep the seed).
              usedProfile: event.used_profile,
              agentProfile: event.used_profile ?? m.agentProfile,
              profileReason: event.profile_reason ?? undefined,
              // The turn is over: whatever `agentProfile` now says is final.
              attributionPending: false,
              // Confirm-class cards normally arrive via confirm_required; if
              // only `done` carried them, show the cards (never run them).
              toolCalls: m.toolCalls?.length ? m.toolCalls : (event.tool_calls ?? []),
              toolStatus: m.toolCalls?.length
                ? m.toolStatus
                : awaitingConfirm(event.tool_calls ?? [], m.toolStatus),
              // Auto-class calls the server skipped (depth 1 / cost ceiling):
              // display only, never executed client-side.
              deferredToolCalls: event.deferred_tool_calls ?? [],
            }));
            setIterationStatus(null);
            break;
          case 'error': {
            // Two shapes share this event and BOTH carry `fallback_reason`.
            // (a) The LLM path failed ("engine not initialized" or the
            // exception text) and the offline helper saved the turn:
            // `fallback` is the answer and the banner explains why it, not
            // the model, answered. (b) A specialist itself declined
            // (local-only profile, no local provider): `error` === `fallback`
            // and `used_profile` says who — the bubble shows that text once,
            // attributed, with no red banner. `wizardErrorBanner` tells them
            // apart by the reason's value, never by its presence — and so does
            // the mascot (`publishMascot` above ran first): a refusal never
            // plays the error strip or announces "something went wrong".
            // The reason rides onto the message as well, so the bubble's
            // footer and status dot (`isDeliberateRefusalMessage`) make the
            // same call instead of keying on `mode` alone.
            const attributed = event.used_profile !== undefined;
            updateAssistant(m => ({
              ...m,
              ...(event.fallback
                ? {
                  content: event.fallback,
                  mode: 'deterministic' as const,
                  fallbackReason: event.fallback_reason ?? null,
                }
                : {}),
              ...(attributed
                ? {
                  usedProfile: event.used_profile ?? null,
                  agentProfile: event.used_profile ?? GENERAL_PROFILE,
                  profileReason: event.profile_reason ?? undefined,
                  attributionPending: false,
                }
                : {}),
            }));
            const banner = wizardErrorBanner(event);
            if (banner) setError(banner);
            setIterationStatus(null);
            break;
          }
        }
      }
    } catch (err) {
      if ((err as { name?: string })?.name === 'AbortError') {
        // User reissued the chat — silent abort.
      } else {
        setError(err instanceof Error ? err.message : 'Wizard chat failed');
        publishMascot({ type: 'error' });
      }
    } finally {
      setSending(false);
      setIterationStatus(null);
      if (abortRef.current === controller) {
        abortRef.current = null;
      }
    }
  };

  // Keep sendRef pointed at the latest ``send`` closure so the URL-param
  // auto-send effect (declared earlier in the body) can invoke it without
  // a forward-reference / temporal-dead-zone error.
  useEffect(() => {
    sendRef.current = send;
  });

  const handleKey = (event: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault();
      void send();
    }
  };

  return (
    <div
      className="relative flex h-full flex-col"
      onDragOver={(e) => {
        // Only react if files are being dragged in; text drags shouldn't
        // trigger the upload overlay.
        if (e.dataTransfer?.types?.includes('Files')) {
          e.preventDefault();
          setDragActive(true);
        }
      }}
      onDragLeave={(e) => {
        // dragleave fires when crossing internal boundaries too — bail out
        // only when the pointer actually exits the chat surface.
        if (e.currentTarget.contains(e.relatedTarget as Node | null)) return;
        setDragActive(false);
      }}
      onDrop={handleDrop}
    >
      {dragActive && (
        <div
          className="pointer-events-none absolute inset-0 z-30 flex items-center justify-center rounded-md border-2 border-dashed text-sm font-mono"
          style={{
            borderColor: '#76B900',
            background: 'rgba(118, 185, 0, 0.10)',
            color: '#5a9100',
          }}
        >
          Drop to ingest files into your RAG index
        </div>
      )}
      <div
        ref={scrollRef}
        className="flex-1 overflow-y-auto px-4 py-4 space-y-3"
        style={{ background: 'var(--bg-primary)' }}
      >
        {messages.length === 0 && (
          <EmptyState
            onPickPrompt={(p) => setDraft(p)}
            profileMap={profileMap}
            activeProfile={profile}
            onPickAgent={(name) => setProfile(name)}
          />
        )}
        {messages.map(message => (
          <MessageBlock
            key={message.id}
            message={message}
            tools={tools}
            profileMap={profileMap}
            onConfirmTool={(call) => {
              setMascotState('working');
              void runTool(message.id, call, true);
            }}
            onDismissTool={(call) => dismissTool(message.id, call)}
          />
        ))}
        {iterationStatus && (
          <div
            className="flex items-center gap-2 text-[10px] font-mono italic"
            style={{ color: 'var(--text-muted)' }}
          >
            <span className="inline-block h-1.5 w-1.5 animate-pulse rounded-full" style={{ background: '#76B900' }} />
            {iterationStatus}
          </div>
        )}
        {uploading && (
          <div
            className="flex items-center gap-2 text-[10px] font-mono italic"
            style={{ color: 'var(--text-muted)' }}
          >
            <span className="inline-block h-1.5 w-1.5 animate-pulse rounded-full" style={{ background: '#76B900' }} />
            ingesting dropped files…
          </div>
        )}
        {error && (
          <div className="rounded-md border border-[#dc2626]/30 bg-[#fef2f2] p-3 text-xs text-[#dc2626]">
            {error}
          </div>
        )}
      </div>

      <div
        className="border-t p-3"
        style={{ borderColor: 'var(--border)', background: 'var(--bg-card)' }}
      >
        <div className="flex items-end gap-2">
          <textarea
            value={draft}
            onChange={e => {
              setDraft(e.target.value);
              // Settles happy / error / sleeping back to idle; never yanks
              // the mascot out of thinking / working / asking.
              noteMascotTyping();
            }}
            onKeyDown={handleKey}
            placeholder="Ask the Wizard — about your setup, what to install, what just broke..."
            rows={2}
            className="flex-1 resize-none rounded-md border p-2 text-sm"
            style={{
              background: 'var(--bg-card)',
              borderColor: 'var(--border-bright)',
              color: 'var(--text-primary)',
            }}
            disabled={sending}
          />
          <button
            type="button"
            onClick={() => void send()}
            disabled={sending || !draft.trim()}
            className="btn-primary px-4 py-2 text-xs font-mono"
          >
            {sending ? 'Asking...' : 'Send'}
          </button>
        </div>
        <div className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-1 text-[10px] font-mono" style={{ color: 'var(--text-faint)' }}>
          <CreateAgentModal
            open={creatingAgent}
            onClose={() => setCreatingAgent(false)}
            onCreated={(name) => setProfile(name)}
            toolNames={Array.from(tools.keys())}
          />
          <div
            className="flex items-center gap-1"
            title={
              maxIterations === 1
                ? 'Just answer me — skip tool chains.'
                : maxIterations === 2
                  ? 'Allow one tool round + one reaction.'
                  : 'Let the Wizard chain up to 3 rounds of tool calls.'
            }
          >
            <label htmlFor="iter-budget" style={{ color: 'var(--text-muted)' }}>
              Depth:
            </label>
            <input
              id="iter-budget"
              type="range"
              min={1}
              max={3}
              step={1}
              value={maxIterations}
              onChange={(e) => setMaxIterations(Number.parseInt(e.target.value, 10))}
              className="h-1 w-12 cursor-pointer accent-[#76B900]"
            />
            <span className="font-mono" style={{ color: 'var(--text-primary)' }}>
              {maxIterations}
            </span>
          </div>
          <button
            type="button"
            onClick={() => setAdvancedOpen(open => !open)}
            aria-expanded={advancedOpen}
            aria-controls="wizard-advanced"
            className="flex items-center gap-1 transition-colors hover:text-[#76B900]"
            style={{ color: 'var(--text-muted)' }}
            title="Pin a specialist instead of letting the Wizard choose one per turn"
          >
            <span aria-hidden>{advancedOpen ? '▾' : '▸'}</span>
            <span>Advanced</span>
            {!isAutoProfile(profile) && (
              <span style={{ color: '#76B900' }}>
                · pinned: {profileMap.get(profile)?.title ?? profile}
              </span>
            )}
          </button>
          <a
            href={
              draft.trim()
                ? `/?mode=council&prompt=${encodeURIComponent(draft.trim())}`
                : '/?mode=council'
            }
            title={
              draft.trim()
                ? 'Open the chat in council mode with this question prefilled — multiple expert advisors answer in parallel, then synthesize.'
                : 'Open the chat in council mode — multiple expert advisors answer in parallel, then synthesize.'
            }
            className="flex items-center gap-1 rounded-md border px-2 py-0.5 hover:bg-[var(--bg-hover)]"
            style={{ borderColor: 'var(--border)', color: 'var(--text-muted)' }}
          >
            <span aria-hidden>⚖</span>
            <span>Convene council</span>
          </a>
          <span>
            Press Enter to send, Shift+Enter for a newline. Type{' '}
            <span className="text-[#76B900]">/help</span> for commands. Drop
            files into the chat to ingest them.
          </span>
        </div>
        {advancedOpen && (
          <div
            id="wizard-advanced"
            className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-1 text-[10px] font-mono"
            style={{ color: 'var(--text-faint)' }}
          >
            <AgentProfilePicker
              value={profile}
              onChange={setProfile}
              onCreateNew={() => setCreatingAgent(true)}
            />
            <span>
              Auto lets the Wizard route each question to a hidden specialist;
              a pin keeps one persona and its tool set for every turn.
            </span>
          </div>
        )}
      </div>
    </div>
  );
}

// Per-agent starter prompts shown on the empty Wizard state. Picking a card
// both swaps the active profile AND seeds the composer with an on-persona
// question, so first-time users get a "click → ready to send" experience.
const AGENT_STARTERS: Record<string, string> = {
  wizard: 'What can my GPU run right now, and what should I fix first?',
  coder: 'Review the diff in my current branch — call out the riskiest change.',
  researcher: 'Summarize the latest on Llama 4 release vs Llama 3, with sources.',
  writer: 'Help me draft a launch announcement for an internal tool.',
  ops: 'Run a safe repair pass and tell me what changed.',
  'vault-rag': 'What did I write about my mount-autopilot setup last week?',
};

function EmptyState({
  onPickPrompt,
  profileMap,
  activeProfile,
  onPickAgent,
}: {
  onPickPrompt: (prompt: string) => void;
  profileMap: Map<string, AgentProfileSchema>;
  activeProfile: string;
  onPickAgent: (name: string) => void;
}) {
  // Adaptive starters — seeded from the reconnect payload so the Wizard's
  // suggestions match the user's actual workspace state. Falls back to the
  // generic set when reconnect is unavailable (cold first boot, offline, etc.).
  const [starters, setStarters] = useState<string[]>([
    'What can my GPU run right now?',
    "Why isn't Ollama responding?",
    'Help me pick a mission for image gen.',
    'Refresh the local model list.',
  ]);
  // Proactive repair nudge — when reconnect surfaces unhealthy receipts or
  // attention items we render a single-line banner above the agent grid so
  // the user sees the problem before they pick an agent. One-click = fire
  // off the repair-workspace tool via a "Fix this" prompt.
  const [proactiveNudge, setProactiveNudge] = useState<{ text: string; prompt: string } | null>(null);
  const agentList = Array.from(profileMap.values()).slice(0, 6);

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const { wizardReconnect } = await import('@/lib/api');
        const r = await wizardReconnect();
        if (cancelled) return;
        const adaptive: string[] = [];
        // First-run user → walk them through setup directly.
        if (r.first_run) {
          adaptive.push('Walk me through setting up this workspace from scratch.');
        }
        // Concrete attention items → surface the top one as a starter.
        for (const item of r.needs_attention.slice(0, 2)) {
          const title = (item as { title?: string }).title;
          if (typeof title === 'string' && title.length > 0) {
            adaptive.push(`Help me with: ${title}`);
          }
        }
        // Auto-repair already ran? Offer to summarize what just happened.
        if (r.auto_repaired.length > 0) {
          adaptive.push('What did you just auto-repair, and what should I check?');
        }
        // Vault present → invite recall-style questions.
        const vaultNotes = (r as unknown as { vault?: { memory_files?: number } })?.vault?.memory_files ?? 0;
        if (vaultNotes > 0) {
          adaptive.push('What did I write about in my vault recently?');
        }
        if (adaptive.length > 0) {
          // Always keep one generic fallback so the grid never looks empty.
          adaptive.push('What can my GPU run right now?');
          setStarters(adaptive.slice(0, 4));
        }

        // Proactive nudge: build a one-line "I noticed X looks broken — want
        // me to fix it?" message from the most important attention item. This
        // is the "self-healing" promise made visible.
        if (r.needs_attention.length > 0) {
          const top = r.needs_attention[0] as { title?: string; summary?: string };
          const summary = top.summary || top.title;
          if (summary) {
            setProactiveNudge({
              text: `Heads up — ${summary}.`,
              prompt: `Run a safe repair on the issue: ${top.title ?? summary}`,
            });
          }
        } else if ((r.auto_repaired ?? []).length > 0) {
          setProactiveNudge({
            text: `I auto-repaired ${r.auto_repaired.length} item(s) on reconnect.`,
            prompt: 'What did you just auto-repair, and is anything still broken?',
          });
        }
      } catch {
        // Reconnect unavailable — keep the static starters.
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);
  return (
    <div className="mx-auto max-w-2xl pt-6">
      <div className="text-center">
        <div className="text-[10px] font-mono uppercase tracking-[0.18em]" style={{ color: '#76B900' }}>
          AI Wizard
        </div>
        <div className="mt-2 text-lg font-semibold" style={{ color: 'var(--text-primary)' }}>
          Ask anything — the Wizard picks the specialist.
        </div>
        <div className="mt-1 text-xs leading-relaxed" style={{ color: 'var(--text-secondary)' }}>
          The Wizard reads your live GPU, storage, providers, and vault before
          it answers, and routes each question to a hidden specialist that can
          run repairs, refresh models, and validate keys. Pick a card below to
          pin one persona instead.
        </div>
      </div>

      {proactiveNudge && (
        <button
          type="button"
          onClick={() => onPickPrompt(proactiveNudge.prompt)}
          className="mt-4 flex w-full items-center justify-between gap-3 rounded-md border px-3 py-2 text-left text-xs transition-colors hover:border-[#d97706]"
          style={{
            borderColor: '#d97706',
            background: 'rgba(217,119,6,0.08)',
            color: '#92400e',
          }}
          title="Click to seed a repair request"
        >
          <span>{proactiveNudge.text}</span>
          <span className="font-mono text-[10px] uppercase tracking-[0.18em]" style={{ color: '#92400e' }}>
            Fix it →
          </span>
        </button>
      )}

      {agentList.length > 0 && (
        <div className="mt-4 grid grid-cols-2 gap-2 sm:grid-cols-3">
          {agentList.map(p => {
            const isActive = p.name === activeProfile;
            return (
              <button
                key={p.name}
                type="button"
                onClick={() => {
                  onPickAgent(p.name);
                  const starter = AGENT_STARTERS[p.name];
                  if (starter) onPickPrompt(starter);
                }}
                className="flex items-start gap-2 rounded-md border p-2 text-left transition-colors hover:border-[#76B900]/40"
                style={{
                  borderColor: isActive ? '#76B900' : 'var(--border)',
                  background: isActive ? 'rgba(118,185,0,0.05)' : 'var(--bg-card)',
                }}
                title={p.description}
              >
                <AgentAvatar profile={p} size="md" />
                <div className="min-w-0 flex-1">
                  <div
                    className="truncate text-xs font-mono font-bold"
                    style={{ color: 'var(--text-primary)' }}
                  >
                    {p.title}
                  </div>
                  <div
                    className="mt-0.5 line-clamp-2 text-[10px]"
                    style={{ color: 'var(--text-secondary)' }}
                  >
                    {p.description}
                  </div>
                </div>
              </button>
            );
          })}
        </div>
      )}

      <div className="mt-6 text-center">
        <div className="text-[10px] font-mono uppercase tracking-[0.18em]" style={{ color: 'var(--text-muted)' }}>
          Or jump straight in
        </div>
      </div>
      <div className="mt-4 grid grid-cols-1 gap-2 text-left sm:grid-cols-2">
        {starters.map(s => (
          <button
            key={s}
            type="button"
            onClick={() => onPickPrompt(s)}
            className="rounded-md border px-3 py-2 text-xs transition-colors hover:border-[#76B900]/40"
            style={{
              borderColor: 'var(--border)',
              background: 'var(--bg-card)',
              color: 'var(--text-secondary)',
            }}
          >
            {s}
          </button>
        ))}
      </div>
    </div>
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

function MessageBlock({
  message,
  tools,
  profileMap,
  onConfirmTool,
  onDismissTool,
}: {
  message: Message;
  tools: Map<string, WizardToolSchema>;
  profileMap: Map<string, AgentProfileSchema>;
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
            {typeof message.costUsd === 'number' && message.costUsd > 0 && (
              <span>· ${message.costUsd.toFixed(message.costUsd >= 0.01 ? 3 : 5)}</span>
            )}
          </div>
        )}

        {message.toolCalls?.map(call => (
          <ToolCard
            key={call.name}
            call={call}
            schema={tools.get(call.name)}
            status={message.toolStatus?.[call.name] ?? 'idle'}
            result={message.toolResults?.[call.name]}
            onConfirm={() => onConfirmTool(call)}
            onDismiss={() => onDismissTool(call)}
          />
        ))}
      </div>
    </div>
  );
}

function ToolCard({
  call,
  schema,
  status,
  result,
  onConfirm,
  onDismiss,
}: {
  call: WizardChatToolCall;
  schema: WizardToolSchema | undefined;
  status: ToolCardStatus;
  result?: string;
  onConfirm: () => void;
  onDismiss: () => void;
}) {
  // Every call that reaches a card needs a click, whatever the catalog says
  // its safety class is — the server already ran what it was willing to.
  const description = schema?.description ?? call.name;

  let badge = '';
  let badgeColor = '#737373';
  switch (status) {
    case 'running':
      badge = 'Running...';
      badgeColor = '#d97706';
      break;
    case 'ok':
      badge = '✓ Done';
      badgeColor = '#16a34a';
      break;
    case 'error':
      badge = '✗ Failed';
      badgeColor = '#dc2626';
      break;
    case 'awaiting-confirm':
      badge = 'Needs confirmation';
      badgeColor = '#d97706';
      break;
    case 'dismissed':
      badge = 'Skipped';
      badgeColor = '#737373';
      break;
    default:
      badge = 'Click to run';
      badgeColor = '#d97706';
  }

  return (
    <div
      className="mt-2 rounded-md border p-2 text-xs"
      style={{ background: 'var(--bg-subtle)', borderColor: 'var(--border)' }}
    >
      <div className="flex items-center justify-between gap-2">
        <div className="flex items-baseline gap-2">
          <span className="font-mono font-semibold" style={{ color: 'var(--text-primary)' }}>
            {call.name}
          </span>
          <span className="text-[9px] font-mono uppercase tracking-[0.14em]" style={{ color: badgeColor }}>
            {badge}
          </span>
        </div>
        {(status === 'idle' || status === 'awaiting-confirm') && (
          <div className="flex items-center gap-1">
            <button
              type="button"
              onClick={onDismiss}
              className="rounded-sm border px-2 py-1 text-[10px] font-mono transition-colors hover:bg-[var(--bg-hover)]"
              style={{ borderColor: 'var(--border)', color: 'var(--text-muted)' }}
              title="Leave this tool un-run"
            >
              Skip
            </button>
            <button
              type="button"
              onClick={onConfirm}
              className="btn-primary px-2 py-1 text-[10px] font-mono"
            >
              Run
            </button>
          </div>
        )}
      </div>
      <div className="mt-1" style={{ color: 'var(--text-secondary)' }}>
        {description}
      </div>
      {Object.keys(call.arguments).length > 0 && (
        <div className="mt-1 font-mono text-[10px]" style={{ color: 'var(--text-muted)' }}>
          args: {JSON.stringify(call.arguments)}
        </div>
      )}
      {result && (
        <div className="mt-1 font-mono text-[10px]" style={{ color: 'var(--text-secondary)' }}>
          {result}
        </div>
      )}
    </div>
  );
}

function formatToolResultSummary(name: string, result: Record<string, unknown> | undefined): string {
  if (!result) return 'completed.';
  if (typeof result.summary === 'string') return result.summary;
  // Specific shapes we recognize for prettier surface text:
  if (name === 'validate_provider_key' && typeof result.valid === 'boolean') {
    if (result.valid) {
      const count = typeof result.model_count === 'number' ? `, ${result.model_count} model(s)` : '';
      const latency = typeof result.latency_ms === 'number' ? ` in ${result.latency_ms} ms` : '';
      return `valid${latency}${count}`;
    }
    return `invalid: ${result.error ?? 'unknown'}`;
  }
  if (name === 'save_provider_key' && result.ok === true) {
    return `saved under ${result.config_file ?? 'workspace config'}`;
  }
  // Fall through: stringify a short version.
  try {
    return JSON.stringify(result).slice(0, 120);
  } catch {
    return 'completed.';
  }
}

/**
 * DeferredToolNotes lists auto-class calls the server chose NOT to run this
 * turn (Depth 1 or the profile's cost ceiling) with the reason it gave.
 * Informational only — these never execute client-side; the user can raise
 * Depth or ask again. Kept out of ServerToolTrace so "used N tools" stays an
 * honest count of what actually ran.
 */
function DeferredToolNotes({ calls }: { calls: WizardDeferredToolCall[] }) {
  return (
    <div
      className="mt-1 space-y-0.5 text-[10px] font-mono"
      style={{ color: 'var(--text-faint)' }}
      aria-label="Tools not run this turn"
    >
      {calls.map((c, i) => (
        <div key={`${c.name}-${i}`}>
          not run: <span style={{ color: 'var(--text-muted)' }}>{c.name}</span>
          {c.reason ? ` — ${c.reason}` : ''}
        </div>
      ))}
    </div>
  );
}

/** Split a trace into what actually ran and what the whitelist refused. */
function splitToolTrace(trace: WizardChatToolResult[]): {
  ran: WizardChatToolResult[];
  refused: WizardChatToolResult[];
} {
  const ran: WizardChatToolResult[] = [];
  const refused: WizardChatToolResult[] = [];
  for (const entry of trace) (isRefusedToolResult(entry) ? refused : ran).push(entry);
  return { ran, refused };
}

/**
 * ServerToolTrace renders auto-class tool calls that ran server-side
 * inside the Wizard's follow-up loop, so the user can see "Wizard
 * called rag_ask → got 4 chunks from notes.md, ideas.md" rather than
 * just an answer that appears out of nowhere.
 *
 * Whitelist refusals travel in the same list (`result.not_allowed`) but
 * never ran, so they are split out here: the "used N tools" header counts
 * only what executed, and each refusal is a muted one-liner instead.
 *
 * Collapsed by default to keep the chat feeling like chat. Click to
 * expand the details (args + raw result excerpt) for the curious.
 */
function ServerToolTrace({ trace, specialist }: { trace: WizardChatToolResult[]; specialist: string }) {
  const [open, setOpen] = useState(false);
  const { ran, refused } = splitToolTrace(trace);
  if (ran.length === 0 && refused.length === 0) return null;
  return (
    <>
      {ran.length > 0 && (
        <div className="mt-2 rounded-md border" style={{ background: 'var(--bg-subtle)', borderColor: 'var(--border)' }}>
          <button
            type="button"
            onClick={() => setOpen(o => !o)}
            className="flex w-full items-center justify-between px-2 py-1 text-[10px] font-mono"
            style={{ color: 'var(--text-muted)' }}
          >
            <span>
              {open ? '▾' : '▸'} Wizard used {ran.length} tool{ran.length === 1 ? '' : 's'} to answer
              <span style={{ color: 'var(--text-faint)' }}>
                {' '}
                ({ran.map(t => t.name).join(' → ')})
              </span>
            </span>
          </button>
          {open && (
            <div className="border-t px-2 py-2 space-y-2" style={{ borderColor: 'var(--border)' }}>
              {ran.map((entry, i) => (
                <ServerToolTraceItem key={`${entry.name}-${i}`} entry={entry} />
              ))}
            </div>
          )}
        </div>
      )}
      {refused.length > 0 && (
        <div
          className="mt-1 space-y-0.5 text-[10px] font-mono"
          style={{ color: 'var(--text-faint)' }}
          aria-label="Tools this specialist may not use"
        >
          {refused.map((entry, i) => (
            <div key={`${entry.name}-refused-${i}`}>
              not allowed for {specialist}:{' '}
              <span style={{ color: 'var(--text-muted)' }}>{entry.name}</span>
            </div>
          ))}
        </div>
      )}
    </>
  );
}

function ServerToolTraceItem({ entry }: { entry: WizardChatToolResult }) {
  const ok = entry.result?.ok !== false;
  const innerResult = (entry.result?.result as Record<string, unknown> | undefined) ?? undefined;
  const summary = formatServerTraceSummary(entry.name, innerResult, entry.result?.error);
  return (
    <div className="text-[10px]" style={{ color: 'var(--text-secondary)' }}>
      <div className="flex items-baseline gap-2">
        <span
          className="font-mono font-semibold"
          style={{ color: ok ? 'var(--text-primary)' : '#dc2626' }}
        >
          {entry.name}
        </span>
        <span className="text-[9px] font-mono uppercase tracking-[0.14em]" style={{ color: ok ? '#16a34a' : '#dc2626' }}>
          {ok ? '✓' : '✗'}
        </span>
      </div>
      <div className="mt-0.5" style={{ color: 'var(--text-secondary)' }}>{summary}</div>
      {Object.keys(entry.arguments).length > 0 && (
        <div className="mt-0.5 font-mono text-[9px]" style={{ color: 'var(--text-muted)' }}>
          args: {JSON.stringify(entry.arguments).slice(0, 200)}
        </div>
      )}
    </div>
  );
}

function formatServerTraceSummary(
  name: string,
  inner: Record<string, unknown> | undefined,
  error: string | undefined,
): string {
  if (error) return error;
  if (!inner) return 'completed.';
  // Shape-aware renderings — only the tools we ship today. Unknown shapes
  // fall through to a stringified excerpt.
  if (name === 'rag_ask' || name === 'rag_ask_vault') {
    const chunks = Array.isArray(inner.chunks) ? inner.chunks : [];
    const sources = new Set<string>();
    for (const c of chunks) {
      const src = (c as Record<string, unknown>)?.source;
      if (typeof src === 'string') {
        const tail = src.split('/').pop() ?? src;
        sources.add(tail);
      }
    }
    const srcList = Array.from(sources).slice(0, 4).join(', ');
    const auto = inner.auto_indexed ? ' (auto-indexed vault)' : '';
    return `${chunks.length} chunk(s) from ${srcList || 'index'}${auto}`;
  }
  if (name === 'web_search') {
    const results = Array.isArray(inner.results) ? inner.results : [];
    const backend = typeof inner.backend === 'string' ? inner.backend : 'web';
    return `${results.length} result(s) via ${backend}`;
  }
  if (name === 'rag_ingest') {
    const ingested = typeof inner.files_ingested === 'number' ? inner.files_ingested : 0;
    const chunks = typeof inner.chunks === 'number' ? inner.chunks : 0;
    return `indexed ${ingested} file(s), ${chunks} chunk(s)`;
  }
  if (name === 'refresh_models' && typeof inner.summary === 'string') {
    return inner.summary;
  }
  try {
    return JSON.stringify(inner).slice(0, 140);
  } catch {
    return 'completed.';
  }
}

/**
 * SourcesFooter renders inline-numbered citations beneath an answer when the
 * Wizard pulled chunks from rag_ask, rag_ask_vault, or web_search. Each hit
 * gets a [1], [2], … number with the source filename / URL and a click-to-
 * expand snippet, so users can see exactly where the answer is grounded.
 *
 * Beats a separate sources panel (the 2023 pattern). Stitching [N] back into
 * the answer text is a follow-up — for now numbering matches order of arrival.
 */
function SourcesFooter({ trace }: { trace: WizardChatToolResult[] }) {
  const sources = collectSources(trace);
  const [openIdx, setOpenIdx] = useState<number | null>(null);
  if (sources.length === 0) return null;
  return (
    <div className="mt-2 border-t pt-2 text-[10px] font-mono" style={{ borderColor: 'var(--border)' }}>
      <div className="mb-1 uppercase tracking-[0.14em]" style={{ color: 'var(--text-muted)' }}>
        Sources
      </div>
      <div className="space-y-1">
        {sources.map((src, i) => {
          const isOpen = openIdx === i;
          return (
            <div key={`${src.label}-${i}`}>
              <button
                type="button"
                onClick={() => setOpenIdx(isOpen ? null : i)}
                className="text-left transition-colors hover:text-[#76B900]"
                style={{ color: 'var(--text-secondary)' }}
              >
                <span className="mr-1" style={{ color: '#76B900' }}>[{i + 1}]</span>
                {src.label}
                {src.kind === 'web' && src.url && (
                  <span style={{ color: 'var(--text-faint)' }}> · {new URL(src.url).hostname}</span>
                )}
              </button>
              {isOpen && src.snippet && (
                <div
                  className="mt-1 ml-5 rounded-sm border-l-2 px-2 py-1 text-[10px]"
                  style={{
                    borderColor: '#76B900',
                    background: 'var(--bg-subtle)',
                    color: 'var(--text-secondary)',
                  }}
                >
                  {src.snippet}
                  {src.url && (
                    <div className="mt-1">
                      <a
                        href={src.url}
                        target="_blank"
                        rel="noopener noreferrer"
                        className="text-[#76B900] underline"
                      >
                        Open source ↗
                      </a>
                    </div>
                  )}
                </div>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}

interface SourceEntry {
  label: string;
  kind: 'rag' | 'web';
  snippet?: string;
  url?: string;
}

function collectSources(trace: WizardChatToolResult[]): SourceEntry[] {
  const out: SourceEntry[] = [];
  const seen = new Set<string>();
  for (const entry of trace) {
    // A refused call never ran, so it has nothing to cite — skip it even if
    // a future payload happened to carry a `result` body.
    if (isRefusedToolResult(entry)) continue;
    const inner = (entry.result?.result as Record<string, unknown> | undefined) ?? undefined;
    if (!inner) continue;
    if (entry.name === 'rag_ask' || entry.name === 'rag_ask_vault') {
      const chunks = Array.isArray(inner.chunks) ? inner.chunks : [];
      for (const chunk of chunks) {
        const c = chunk as Record<string, unknown>;
        const src = typeof c.source === 'string' ? c.source : '';
        if (!src) continue;
        const dedupe = `rag:${src}:${c.chunk_index ?? ''}`;
        if (seen.has(dedupe)) continue;
        seen.add(dedupe);
        const label = src.split('/').pop() ?? src;
        const text = typeof c.text === 'string' ? c.text : '';
        out.push({ label, kind: 'rag', snippet: text.slice(0, 240) });
      }
    } else if (entry.name === 'web_search') {
      const results = Array.isArray(inner.results) ? inner.results : [];
      for (const r of results) {
        const w = r as Record<string, unknown>;
        const url = typeof w.url === 'string' ? w.url : '';
        if (!url) continue;
        const dedupe = `web:${url}`;
        if (seen.has(dedupe)) continue;
        seen.add(dedupe);
        out.push({
          label: typeof w.title === 'string' ? w.title : url,
          kind: 'web',
          snippet: typeof w.snippet === 'string' ? w.snippet : undefined,
          url,
        });
      }
    }
  }
  return out;
}
