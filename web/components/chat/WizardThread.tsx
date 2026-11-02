'use client';

/**
 * WizardThread — the conversational AI Wizard surface: the thread that
 * owns the messages, the stream, the cards' handlers and the mascot feed.
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
 *      A question card (`ask_user`, 0.46) is answered here, never run:
 *      Answer sends the text as the next user message with a `tool_results`
 *      entry closing the card ({name, ok: true, summary: <answer ≤300>});
 *      Skip records "skipped by the user; not answered". Calls the server
 *      held behind the question arrive deferred as `question_pending`.
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
 *
 * The view components live beside this file in components/chat/:
 * MessageBlock (one bubble: header, trace, sources, footer, status dot and
 * the tool cards), ToolCard (one proposed tool — Run / Skip / hand-off),
 * ServerToolTrace, SourcesFooter, DeferredToolNotes and EmptyState. The
 * `Message` model is in chat/types.ts so both sides import it without a
 * cycle. `/wizard` mounts this with `conversationParam="conversation"`;
 * the merged chat surface passes `c`.
 */

import { useSearchParams } from 'next/navigation';
import { useCallback, useEffect, useRef, useState } from 'react';
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
  listAgentProfiles,
  listWizardTools,
  pinConversation,
  saveVaultMemory,
  uploadAndIngest,
  followPlaybookJob,
  wizardChatStream,
  wizardDiagnostics,
  type AgentProfileSchema,
  type WizardChatToolCall,
  type WizardChatToolResult,
  type WizardChatTurn,
  type WizardDeferredToolCall,
  type WizardChatToolOutcome,
  type WizardDiagnosticsPayload,
  type WizardStreamEvent,
  type WizardToolExecuteResult,
  type WizardToolSchema,
} from '@/lib/api';
import {
  PRIVILEGED_DISABLED_NOTE,
  applyHistoryPatch,
  historyToolOutcome,
  isPendingStatus,
  isPrivilegedCall,
  isPrivilegedDisabled,
  isQuestionCall,
  isToolOutcomeFailure,
  needsTerminal,
  playbookCardStatus,
  playbookOutcomeLabel,
  playbookRunStart,
  questionAnswerDetail,
  toolKey,
  unwrapToolResult,
  type HistoryPatch,
  type PlaybookRunState,
  type ToolCardStatus,
} from '@/lib/privileged';
import {
  NO_VISION_TOOL_NOTE,
  WIZARD_ATTACHMENT_MAX_BYTES,
  WIZARD_ATTACHMENT_MAX_COUNT,
  WIZARD_IMAGE_ACCEPT,
  attachmentPayloads,
  attachmentPreview,
  formatSize,
  hasVisionTool,
  pastedImageFiles,
  questionWithAttachments,
  splitDroppedFiles,
  toAttachment,
  validateAttachment,
  withAttachedImagePaths,
  type PendingAttachment,
} from '@/lib/attachments';
import type { PlaybookRunStart } from '@/lib/types';
import {
  MASCOT_ASKING_QUESTION_LABEL,
  applyMascotEvent,
  markMascotTipProbed,
  mascotAskingLabel,
  mascotTipProbeDue,
  noteMascotTyping,
  sayMascotTip,
  setMascotState,
} from '@/lib/mascot';
import { costKnownFlag, costNumber } from '@/lib/cost';
import { metaNumber, parseWizardMeta } from '@/lib/wizardMeta';
import EmptyState from './EmptyState';
import MessageBlock from './MessageBlock';
import { QuestionAnswerContext, formatToolResultSummary } from './ToolCard';
import type { Message } from './types';

/**
 * A card outcome to fold into the next request's history. `send` builds the
 * history from the `messages` closure, so a status a caller sets in the same
 * tick (a question card's answer, say) would not be in it yet: the patch is
 * applied to that message first — the same keys `runTool` patches — and
 * committed to state as well, so the card agrees with what was sent. The
 * fold itself is `applyHistoryPatch` in lib/privileged.ts (pure, Node-tested).
 */
export type SendHistoryPatch = HistoryPatch;

/**
 * What a caller may hand `send` instead of the composer's contents. With no
 * options `send` reads the draft and the pending images and clears both;
 * with `text` it sends that text and leaves the composer alone — the
 * `?starter=` deep link uses it, and so does a question card's answer.
 */
export interface SendOptions {
  text?: string;
  historyPatch?: SendHistoryPatch;
}

function makeId(): string {
  return `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
}

/** Confirm cards on a message the user has neither run nor skipped. */
function pendingConfirmCalls(message: Message | undefined, except?: string): WizardChatToolCall[] {
  if (!message?.toolCalls) return [];
  return message.toolCalls.filter(call => {
    if (call.name === except) return false;
    return isPendingStatus(message.toolStatus?.[call.name] ?? 'idle');
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
 * Outcomes of a message's tool cards for the next turn's history. The model
 * never saw a confirm / privileged card run — the cards execute here, after
 * the turn ended — so this is how it learns what happened (ran, refused,
 * handed to a terminal, skipped). Pending cards are left out; empty when
 * nothing has settled.
 */
function toolOutcomesFor(message: Message): Pick<WizardChatTurn, 'tool_results'> {
  const outcomes: WizardChatToolOutcome[] = [];
  for (const call of message.toolCalls ?? []) {
    const outcome = historyToolOutcome(
      call.name,
      message.toolStatus?.[call.name] ?? 'idle',
      message.toolResults?.[call.name],
      message.toolDetails?.[call.name],
    );
    if (outcome) outcomes.push(outcome);
  }
  return outcomes.length > 0 ? { tool_results: outcomes } : {};
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

export default function WizardThread({
  conversationParam = 'conversation',
}: {
  /**
   * The query parameter a freshly created thread is stamped into the URL
   * under (and that `/clear` drops). `/wizard` uses `conversation`; the
   * merged chat surface passes `c`. Resume reads both spellings regardless,
   * so a link in either form opens the thread.
   */
  conversationParam?: 'conversation' | 'c';
}) {
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
  // `privileged_enabled` from the tool list: false means NVH_ALLOW_PRIVILEGED
  // is off, so privileged cards render disabled with no Run button. undefined
  // = the API never said (older build) — cards stay enabled and the refusal,
  // if any, comes from execute.
  const [privilegedEnabled, setPrivilegedEnabled] = useState<boolean | undefined>(undefined);
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
  // Images waiting to ride on the next turn (the chips above the composer):
  // dropped, pasted or picked with the attach button. Documents never land
  // here — they go to RAG ingest. `attachNote` is the one line naming a file
  // that was turned away (a document, too big, a seventh image).
  const [attachments, setAttachments] = useState<PendingAttachment[]>([]);
  const [attachNote, setAttachNote] = useState<string | null>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
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
  // Stop functions of the `playbook-run` job pollers this chat started, by
  // job id, so leaving the page stops them. The jobs themselves keep running
  // server-side; only the polling stops.
  const playbookWatchersRef = useRef<Map<string, () => void>>(new Map());
  useEffect(() => () => {
    for (const stop of playbookWatchersRef.current.values()) stop();
    playbookWatchersRef.current.clear();
  }, []);
  // Setup-page bridge: when the user clicks a System Check item we navigate
  // here with ?issue=<finding_id>. We fetch the matching finding from
  // /v1/wizard/diagnostics and send "Help me with: <title>" as the first
  // turn (`send({ text })`), so the conversation lands directly on the issue.
  const searchParams = useSearchParams();
  const issueHandledRef = useRef(false);
  // Ref so the URL-param effect can invoke send() without a temporal-dead-zone
  // reference to a function declared further down the component body.
  const sendRef = useRef<((opts?: SendOptions) => Promise<void>) | null>(null);
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
        setAttachments([]);
        setAttachNote(null);
        // Drop the resume param so a reload starts fresh instead of
        // resurrecting the thread the user just cleared — and re-arm the
        // resume guard so re-selecting the same chat from the sidebar
        // hydrates again.
        resumeHandledRef.current = null;
        if (typeof window !== 'undefined') {
          const url = new URL(window.location.href);
          // Both spellings: resume reads `?conversation=` and `?c=` alike,
          // so a thread opened as `/wizard?c=<id>` must not come back.
          url.searchParams.delete('conversation');
          url.searchParams.delete('c');
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
        // a conversation_id since we have the in-memory thread already. It
        // lands in the vault's `Wizard Memory` folder as a web-sourced note
        // (the same record shape the Wizard's `remember` tool writes).
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
          await saveVaultMemory({
            title,
            body,
            category: 'Wizard Memory',
            tags: ['wizard', 'chat'],
            source: 'web',
          });
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

  /**
   * Validate and encode image files into pending attachments. Files that
   * are turned away (a document, an empty or oversized file, a seventh
   * image) are named in one line under the chips; the rest are added. The
   * count cap is checked against what is pending plus what this batch has
   * already accepted, so a drop of ten images adds six and says why.
   */
  const addImageFiles = async (files: File[]) => {
    if (files.length === 0) return;
    const accepted: PendingAttachment[] = [];
    const problems: string[] = [];
    let count = attachments.length;
    for (const file of files) {
      const verdict = validateAttachment(file, count);
      if (!verdict.ok) {
        problems.push(verdict.reason);
        continue;
      }
      try {
        accepted.push(await toAttachment(file, makeId()));
        count += 1;
      } catch {
        problems.push(`${file.name || 'image'}: could not be read`);
      }
    }
    if (accepted.length > 0) {
      setAttachments(prev => [...prev, ...accepted].slice(0, WIZARD_ATTACHMENT_MAX_COUNT));
    }
    setAttachNote(problems.length > 0 ? problems.join(' · ') : null);
  };

  const removeAttachment = (id: string) => {
    setAttachments(prev => prev.filter(a => a.id !== id));
    setAttachNote(null);
  };

  // Pasting a screenshot attaches it; a text paste is left to the textarea.
  const handlePaste = (event: React.ClipboardEvent<HTMLTextAreaElement>) => {
    const images = pastedImageFiles(event.clipboardData?.items);
    if (images.length === 0) return;
    event.preventDefault();
    void addImageFiles(images);
  };

  const handleDrop = async (event: React.DragEvent<HTMLDivElement>) => {
    event.preventDefault();
    setDragActive(false);
    const dropped = Array.from(event.dataTransfer?.files ?? []);
    if (dropped.length === 0) return;
    // Two roads: images attach to the next message (the Wizard reads them
    // with analyze_image / read_text_from_image); documents keep going to
    // the RAG store exactly as before.
    const { images, documents: files } = splitDroppedFiles(dropped);
    if (images.length > 0) void addImageFiles(images);
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
        setPrivilegedEnabled(tList.privileged_enabled);
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
  // we send it as the first turn (`send({ text })` — the composer is left
  // alone) so the conversation opens on the issue. Best-effort: any failure
  // leaves the chat empty.
  useEffect(() => {
    const issueId = searchParams?.get('issue');
    const rawStarter = searchParams?.get('starter');
    if (!issueId && !rawStarter) return;
    if (issueHandledRef.current) return;
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
      // Yield once so the first commit's effects have all run: this effect is
      // declared before the one that points `sendRef` at `send`, and a bare
      // ?starter= reaches here with no await above. The handled-guard is set
      // only now, so a run that StrictMode cancels before this point leaves
      // it for the re-run — which is the one that sends.
      await Promise.resolve();
      if (cancelled || issueHandledRef.current) return;
      issueHandledRef.current = true;
      void sendRef.current?.({ text: starter });
    })();
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [searchParams]);

  // Resume a persisted conversation: /wizard?conversation=<id> (the shared
  // sidebar routes wizard-mode chats here) or ?c=<id> on the merged chat
  // surface — both spellings are read. Hydrates the visible thread from
  // the server and re-arms conversationId so new turns append to the same
  // conversation.
  //
  // The handled-guard is set only AFTER hydration commits: under React
  // StrictMode's double-invoked effects the first run's fetch is discarded
  // by its cleanup, so marking the id handled up front would leave the
  // second run skipping and the chat permanently empty in dev.
  const resumeHandledRef = useRef<string | null>(null);
  useEffect(() => {
    const resumeId = searchParams?.get('conversation') ?? searchParams?.get('c');
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
            // costNumber keeps a persisted $0 (metaNumber would drop it) so a
            // reload renders the same pill the live turn did.
            costUsd: costNumber(meta?.cost_usd),
            costKnown: costKnownFlag(meta?.cost_known),
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
  // A skipped question lands in the next turn's history as "skipped by the
  // user; not answered" (`historyToolOutcome` picks the words by name).
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

  // "Answer" on a question card (`ask_user`, 0.46). The answer becomes the
  // next user message, and — through `send`'s `historyPatch` — the card
  // settles `answered` with the answer as its detail BEFORE the history is
  // built, so the request carries `tool_results: [{name: 'ask_user', ok:
  // true, summary: <answer ≤300>}]` beside the message: the server renders
  // `TOOL_RESULT ask_user` and the model knows which question was answered.
  // Nothing is POSTed to /v1/wizard/tools/execute. While a turn is streaming
  // the card's Answer is disabled (`busy` on the provider below), as the
  // composer's Send is; the `sending` check here is the backstop.
  const answerQuestion = (messageId: string, call: WizardChatToolCall, answer: string) => {
    const text = answer.trim();
    if (!text || sending) return;
    const key = toolKey(call);
    void send({
      text,
      historyPatch: { messageId, key, status: 'answered', summary: text, detail: questionAnswerDetail(text) },
    });
  };

  const runTool = async (
    messageId: string,
    call: WizardChatToolCall,
    confirmed: boolean,
  ): Promise<void> => {
    // A question is answered in the chat (`answerQuestion`), never executed:
    // the server would only answer `answered_in_chat`. Nothing to run.
    if (isQuestionCall(call)) return;
    // The one updater every branch below uses: the card's status, its
    // one-line summary and (when there is one) the raw execute response the
    // card reads `plan` / `command` / `hint` / `disabled` from.
    const patchMessage = (
      status: ToolCardStatus,
      summary?: string,
      detail?: WizardToolExecuteResult,
    ) => {
      setMessages(prev => prev.map(m =>
        m.id === messageId
          ? {
            ...m,
            toolStatus: { ...(m.toolStatus ?? {}), [call.name]: status },
            ...(summary !== undefined
              ? { toolResults: { ...(m.toolResults ?? {}), [call.name]: summary } }
              : {}),
            ...(detail
              ? { toolDetails: { ...(m.toolDetails ?? {}), [call.name]: detail } }
              : {}),
          }
          : m,
      ));
    };
    patchMessage('running');

    try {
      const envelope = await executeWizardTool(call.name, {
        // Exactly the object the card arrived with — never trimmed, renamed
        // or re-derived. A confirm-class planner card (`generate_image`)
        // carries the server-pinned `profile` and `profile_home` in here;
        // the server refuses a run whose pin is missing or altered.
        arguments: call.arguments,
        confirmed,
        // The token the server issued with this card; a confirmed privileged
        // call is refused without it. The model never sees it.
        approvalToken: call.approval_token,
      });
      // The registry nests a handler's answer under `result`; the hand-off,
      // a deny-list refusal and a real receipt all live there, so verdicts
      // are read from the unwrapped outcome, never from the envelope's
      // "the tool answered" `ok: true`.
      const outcome = unwrapToolResult(envelope);
      const detail: WizardToolExecuteResult = { ...envelope, ...outcome, result: envelope.result };

      if (envelope.needs_confirmation) {
        patchMessage('awaiting-confirm', envelope.summary ?? 'Needs your confirmation.', detail);
        setMascotState('asking');
        return;
      }

      // Kill switch: privileged tools are off for this workspace. A refusal,
      // not a failure — the card goes muted and loses its Run button rather
      // than showing a red "✗ Failed" the user cannot act on.
      if (envelope.disabled || outcome.disabled) {
        patchMessage('disabled', envelope.error ?? outcome.error ?? PRIVILEGED_DISABLED_NOTE, detail);
        settleMascot(messageId, call.name, 'idle');
        return;
      }

      // sudo wants a password, so nvHive did not run it and hands over the
      // exact command instead. This must be checked BEFORE `ok`: the hand-off
      // arrives as a nested ok=false and it is never a red error.
      if (needsTerminal(outcome)) {
        patchMessage(
          'needs-terminal',
          typeof outcome.summary === 'string' ? outcome.summary : 'Run this yourself — sudo needs your password.',
          detail,
        );
        settleMascot(messageId, call.name, 'idle');
        return;
      }

      // `refused: true` — from the handler (run_code without Docker, a vision
      // path outside the allowed roots, a shell card whose isolation changed)
      // or from the HTTP layer (a confirmed privileged call from the
      // network): nothing ran, so it is not a failure. The card's `refused`
      // status (lib/privileged.ts owns its amber chrome) shows the server's
      // reason (which names what to do: install Docker, attach the image, ask
      // again), the model hears "nothing ran" in the next turn's history, and
      // the mascot does not play the error strip. Checked before `ok` so the
      // generic "Tool failed" text never wins.
      if (detail.refused === true) {
        const reason = outcome.error ?? envelope.error ?? 'the tool declined to run';
        const summary = `Refused — nothing ran: ${reason}`;
        patchMessage('refused', summary, detail);
        setMessages(prev => [
          ...prev,
          { id: makeId(), role: 'system' as const, content: `⊘ ${call.name} — ${summary}` },
        ]);
        settleMascot(messageId, call.name, 'idle');
        return;
      }

      // A nested ok=false is an in-band refusal (deny list, validation, a
      // failed apply) and renders as a failure with its error, never as Done.
      // `remember` has no `ok` key at all and says `saved: false` instead;
      // `isToolOutcomeFailure` reads both.
      const ok = envelope.ok && !isToolOutcomeFailure(outcome);

      // A confirmed `playbook_install` answers {job_id, playbook, steps_total}:
      // the run continues as a job. The card stays `running` and streams it;
      // its verdict (Done / hand-off / failed at step N) comes from the job's
      // events, never from this envelope's "the tool answered" ok.
      const start = ok ? playbookRunStart(outcome) : null;
      if (start) {
        followPlaybookRun(messageId, call, start, detail);
        return;
      }

      const summary = ok
        ? formatToolResultSummary(call.name, outcome)
        : `Tool failed: ${outcome.error ?? envelope.error ?? 'unknown error'}`;
      patchMessage(ok ? 'ok' : 'error', summary, detail);
      setMessages(prev => [
        ...prev,
        {
          id: makeId(),
          role: 'system' as const,
          content: ok
            ? `✓ ${call.name} — ${summary}`
            : `✗ ${call.name} — ${summary}`,
        },
      ]);
      settleMascot(messageId, call.name, ok ? 'happy' : 'error');
    } catch (err) {
      const message = err instanceof Error ? err.message : 'Tool call failed';
      patchMessage('error', message, { ok: false, error: message });
      settleMascot(messageId, call.name, 'error');
    }
  };

  /**
   * Stream a `playbook-run` job into its card. Every event is folded with the
   * pure reducers in lib/privileged.ts (`followPlaybookJob`); the card reads
   * the folded state. The run settles on the first `complete` /
   * `needs_terminal` / `error` event (or on the job's terminal status if none
   * came), and the card's status, summary and detail are patched the same way
   * a one-shot tool's are — a hand-off lands as `needs-terminal` with the
   * exact CLI line copyable, and the docker-group stop as `halted`, neither
   * ever as a failure nor as Done; the next turn's history carries them as
   * `needs a terminal: nvh playbook install <id>` / `stopped for the user to
   * act — not installed yet: …`, so the model does not claim the install
   * happened.
   */
  const followPlaybookRun = (
    messageId: string,
    call: WizardChatToolCall,
    start: PlaybookRunStart,
    detail: WizardToolExecuteResult,
  ) => {
    const argId = typeof call.arguments.id === 'string' ? call.arguments.id : '';
    const playbook = start.playbook || argId;
    const patchRun = (run: PlaybookRunState) => {
      setMessages(prev => prev.map(m => (
        m.id === messageId
          ? { ...m, playbookRuns: { ...(m.playbookRuns ?? {}), [call.name]: run } }
          : m
      )));
    };
    const patchStatus = (status: ToolCardStatus, summary: string, extra?: Partial<WizardToolExecuteResult>) => {
      setMessages(prev => prev.map(m => (
        m.id === messageId
          ? {
            ...m,
            toolStatus: { ...(m.toolStatus ?? {}), [call.name]: status },
            toolResults: { ...(m.toolResults ?? {}), [call.name]: summary },
            toolDetails: { ...(m.toolDetails ?? {}), [call.name]: { ...detail, ...(extra ?? {}) } },
          }
          : m
      )));
    };
    patchStatus(
      'running',
      `Running ${playbook || 'the playbook'}${start.steps_total ? ` — ${start.steps_total} step${start.steps_total === 1 ? '' : 's'}` : ''}`,
    );
    setMascotState('working');

    const stop = followPlaybookJob(start, argId, {
      onRun: patchRun,
      onFinish: run => {
        playbookWatchersRef.current.delete(start.job_id);
        const outcome = run.outcome;
        if (!outcome) return;
        const status = playbookCardStatus(outcome);
        const label = playbookOutcomeLabel(outcome);
        const touched = run.steps.some(s => s.status === 'ok' || s.status === 'failed');
        // `applied: true` marks a finished install only; a hand-off or a halt
        // left steps unrun and says so (`partial`), so nothing downstream
        // reads it as installed.
        const extra: Partial<WizardToolExecuteResult> = outcome.kind === 'needs-terminal'
          ? { needs_terminal: true, command: outcome.command, ...(outcome.hint ? { hint: outcome.hint } : {}), partial: touched }
          : outcome.kind === 'halted'
            ? { partial: true, applied: touched }
            : outcome.kind === 'failed'
              ? { ok: false, error: outcome.error, partial: touched }
              : { applied: true };
        patchStatus(status, label, extra);
        const mark = status === 'ok' ? '✓' : status === 'error' ? '✗' : '→';
        setMessages(prev => [
          ...prev,
          { id: makeId(), role: 'system' as const, content: `${mark} ${call.name} — ${run.playbook || 'playbook'}: ${label}` },
        ]);
        settleMascot(messageId, call.name, status === 'ok' ? 'happy' : status === 'error' ? 'error' : 'idle');
      },
    });
    playbookWatchersRef.current.set(start.job_id, stop);
  };

  // Initial card status for freshly surfaced calls: awaiting a click, except
  // a privileged card the kill switch has already turned off (the server said
  // so on the call, or the tool list did). That one starts settled as
  // `disabled` — no Run button, and it does not keep the mascot asking.
  const seedToolStatus = (
    calls: WizardChatToolCall[],
    prev: Record<string, ToolCardStatus> | undefined,
  ): Record<string, ToolCardStatus> => {
    const next = awaitingConfirm(calls, prev);
    for (const call of calls) {
      if (!isPendingStatus(next[call.name])) continue;
      if (call.disabled === true || isPrivilegedDisabled(tools.get(call.name), call, privilegedEnabled)) {
        next[call.name] = 'disabled';
      }
    }
    return next;
  };

  // `confirm_required` (and `done.tool_calls`): show a Run / Skip card for
  // every call. No client-side auto-run branch exists any more — the server
  // owns auto-class execution and only hands the UI what needs a human.
  const surfaceConfirmCards = (messageId: string, calls: WizardChatToolCall[]) => {
    if (calls.length === 0) return;
    setMessages(prev => prev.map(m =>
      m.id === messageId
        ? { ...m, toolCalls: calls, toolStatus: seedToolStatus(calls, m.toolStatus) }
        : m,
    ));
    // A red card is a bigger ask than a confirm card, so the mascot says so in
    // words rather than leaving the user to read the chrome. The `asking`
    // state itself comes from the stream event; this only sharpens the
    // wording, once per tool per browser session (`sayMascotTip`), and is a
    // no-op while the mascot is hidden. A question card (every pending card
    // an `ask_user`) gets its own words — "the Wizard has a question for
    // you" — once per question rather than once per session, because each
    // one is new; the privileged wording still wins when both are present.
    const classes = calls.map(c => tools.get(c.name)?.safety_class);
    const label = mascotAskingLabel(classes, calls.map(c => c.name));
    if (classes.some(cls => cls === 'privileged')) {
      const named = calls
        .filter(c => isPrivilegedCall(tools.get(c.name)))
        .map(c => c.name)
        .sort()
        .join(',');
      sayMascotTip(label, { id: `privileged-approval:${named}` });
    } else if (label === MASCOT_ASKING_QUESTION_LABEL) {
      sayMascotTip(label, { id: `question:${messageId}` });
    }
  };

  const send = async (opts: SendOptions = {}) => {
    // No `text`: the composer is the source — the draft is the message, the
    // pending images ride along, and both are cleared once the turn is
    // seeded. With `text` the composer is left exactly as it is.
    const fromComposer = opts.text === undefined;
    const typed = (opts.text ?? draft).trim();
    // The images waiting in the composer ride on this turn; a slash command
    // leaves them pending for the next real message.
    const pending = fromComposer ? attachments : [];
    if ((!typed && pending.length === 0) || sending) return;

    // Slash commands are handled locally and never sent to the LLM. Only
    // the composer issues them: a question card's answer or a `?starter=`
    // deep link that begins with '/' (a path such as `/mnt/nvme0`) is text
    // and goes to the model like any other.
    if (fromComposer && typed.startsWith('/')) {
      setDraft('');
      const consumed = await handleSlashCommand(typed);
      if (consumed) return;
    }

    // Images with no words: the server needs a non-empty question, so ask
    // the obvious one rather than refusing the send.
    const text = questionWithAttachments(typed, pending.length);

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
        // id as handled. (Ref mutation inside an event handler, not during
        // render — the compiler rule cannot see that `send` only runs on
        // submit, so it is silenced here on purpose.)
        // eslint-disable-next-line react-hooks/immutability
        resumeHandledRef.current = conv.id;
        if (typeof window !== 'undefined') {
          const url = new URL(window.location.href);
          url.searchParams.set(conversationParam, conv.id);
          window.history.replaceState(null, '', url.toString());
        }
        // Refresh mounted sidebars so the new thread appears without a
        // navigation (the LayoutShell listener refetches on this event).
        announceConversationsChanged();
      }
    }

    const userMsg: Message = {
      id: makeId(),
      role: 'user',
      content: text,
      // Thumbnails for the bubble only; the payload goes out on the request
      // below and is not kept on the message.
      ...(pending.length > 0 ? { attachments: pending.map(attachmentPreview) } : {}),
    };
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
    // A card outcome settled in this same tick is not in the `messages`
    // closure yet: fold it into the list the history is built from, and
    // commit the same patch so the card agrees with what was sent.
    const patch = opts.historyPatch;
    const source = patch ? applyHistoryPatch(messages, patch) : messages;
    if (patch) setMessages(prev => applyHistoryPatch(prev, patch));
    setMessages(prev => [...prev, userMsg, assistantSeed]);
    if (fromComposer) {
      setDraft('');
      setAttachments([]);
      setAttachNote(null);
    }

    const history: WizardChatTurn[] = source
      .filter(m => m.role !== 'system')
      .slice(-12)
      .map(m => ({
        role: m.role as 'user' | 'assistant',
        // A user turn that attached images sends what the server ran for it
        // (with the attached-images line), never just the typed text.
        content: m.historyContent ?? m.content,
        // Continuity: assistant turns name the specialist that produced them
        // so the concierge can stay with it on a weak follow-up ("and then?").
        ...(m.role === 'assistant' && m.usedProfile !== undefined ? { used_profile: m.usedProfile } : {}),
        // What the user did with that turn's cards: the model never saw a
        // confirm / privileged tool run, so the next turn tells it.
        ...(m.role === 'assistant' ? toolOutcomesFor(m) : {}),
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
        // The wire shape only ({name, content, mime_type, is_image}); the
        // server lands the bytes and tells the model the paths.
        attachments: attachmentPayloads(pending),
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
            // `publishMascot` above already handed the event — `safety_class`
            // included — to `deriveMascotState`, which moves the sprite only
            // for an auto call. The status line follows the same split: a
            // confirm / privileged call is about to become a card, not run.
            setIterationStatus(
              event.safety_class && event.safety_class !== 'auto'
                ? `proposing ${event.name}…`
                : `calling ${event.name}…`,
            );
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
              costKnown: event.cost_known,
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
                : seedToolStatus(event.tool_calls ?? [], m.toolStatus),
              // Auto-class calls the server skipped (depth 1 / cost ceiling):
              // display only, never executed client-side.
              deferredToolCalls: event.deferred_tool_calls ?? [],
            }));
            if (event.attachment_paths && event.attachment_paths.length > 0) {
              // The server landed this turn's images and told the model the
              // paths; keep that line on the user turn for the follow-ups.
              const historyContent = withAttachedImagePaths(text, event.attachment_paths);
              setMessages(prev => prev.map(m => (m.id === userMsg.id ? { ...m, historyContent } : m)));
            }
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
          Drop images to attach them to your message · documents are indexed into your RAG store
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
          // The question card's Answer reaches `answerQuestion` through a
          // per-message provider (chat/ToolCard.tsx `QuestionAnswerContext`):
          // the message id is bound here, so the bubble passes nothing new
          // and the card needs only the call and the text. `busy` disables
          // the card's Answer while a turn streams, as the composer's Send is.
          <QuestionAnswerContext.Provider
            key={message.id}
            value={{ answer: (call, answer) => answerQuestion(message.id, call, answer), busy: sending }}
          >
            <MessageBlock
              message={message}
              tools={tools}
              profileMap={profileMap}
              privilegedEnabled={privilegedEnabled}
              onConfirmTool={(call) => {
                // A question card never reaches here (its Answer is the
                // provider above); `runTool` refuses one anyway.
                setMascotState('working');
                void runTool(message.id, call, true);
              }}
              onDismissTool={(call) => dismissTool(message.id, call)}
            />
          </QuestionAnswerContext.Provider>
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
        {(attachments.length > 0 || attachNote) && (
          <div className="mb-2 flex flex-wrap items-center gap-2" aria-label="Images attached to your next message">
            {attachments.map(a => (
              <div
                key={a.id}
                className="flex items-center gap-1.5 rounded-md border py-1 pl-1 pr-1.5 text-[10px] font-mono"
                style={{ borderColor: 'var(--border-green)', background: 'var(--bg-subtle)', color: 'var(--text-secondary)' }}
                title={`${a.name} · ${formatSize(a.size)} · ${a.mime_type}`}
              >
                {/* A data: URL built from the bytes already in memory — next/image has nothing to optimise here. */}
                {/* eslint-disable-next-line @next/next/no-img-element */}
                <img src={a.previewUrl} alt="" className="h-7 w-7 rounded-sm object-cover" />
                <span className="max-w-[10rem] truncate" style={{ color: 'var(--text-primary)' }}>{a.name}</span>
                <span style={{ color: 'var(--text-muted)' }}>{formatSize(a.size)}</span>
                <button
                  type="button"
                  onClick={() => removeAttachment(a.id)}
                  disabled={sending}
                  aria-label={`Remove ${a.name}`}
                  title="Remove this image"
                  className="rounded-sm px-1 transition-colors hover:bg-[var(--bg-hover)]"
                  style={{ color: 'var(--text-muted)' }}
                >
                  ×
                </button>
              </div>
            ))}
            {attachments.length > 0 && tools.size > 0 && !hasVisionTool(tools.keys()) && (
              <div className="text-[10px] font-mono" style={{ color: '#d97706' }} role="note">
                {NO_VISION_TOOL_NOTE}
              </div>
            )}
            {attachNote && (
              <div className="text-[10px] font-mono" style={{ color: '#d97706' }} role="status">
                {attachNote}
              </div>
            )}
          </div>
        )}
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
            onPaste={handlePaste}
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
          <input
            ref={fileInputRef}
            type="file"
            accept={WIZARD_IMAGE_ACCEPT}
            multiple
            className="hidden"
            onChange={e => {
              const picked = Array.from(e.target.files ?? []);
              // Reset so picking the same file again fires change again.
              e.target.value = '';
              void addImageFiles(picked);
            }}
          />
          <button
            type="button"
            onClick={() => fileInputRef.current?.click()}
            disabled={sending || attachments.length >= WIZARD_ATTACHMENT_MAX_COUNT}
            aria-label="Attach an image"
            title={`Attach an image — png, jpeg, webp or gif, up to ${formatSize(WIZARD_ATTACHMENT_MAX_BYTES)}, ${WIZARD_ATTACHMENT_MAX_COUNT} per message. Or drop / paste one.`}
            className="rounded-md border px-2 py-2 text-xs font-mono transition-colors hover:bg-[var(--bg-hover)] disabled:opacity-50"
            style={{ borderColor: 'var(--border)', color: 'var(--text-muted)' }}
          >
            <svg aria-hidden className="h-3.5 w-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2}>
              <path
                strokeLinecap="round"
                strokeLinejoin="round"
                d="M21.44 11.05l-9.19 9.19a6 6 0 01-8.49-8.49l9.19-9.19a4 4 0 015.66 5.66l-9.2 9.19a2 2 0 01-2.83-2.83l8.49-8.48"
              />
            </svg>
          </button>
          <button
            type="button"
            onClick={() => void send()}
            disabled={sending || (!draft.trim() && attachments.length === 0)}
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
