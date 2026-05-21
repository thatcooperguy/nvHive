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
 *   3. Auto-execute `auto`-class tools immediately; surface a "Run" button
 *      for `confirm`-class tools so the user can review before acting.
 *   4. After execution, append a small system message showing what ran.
 */

import { useSearchParams } from 'next/navigation';
import { useEffect, useRef, useState } from 'react';
import AgentAvatar from '@/components/AgentAvatar';
import AgentProfilePicker from '@/components/AgentProfilePicker';
import CreateAgentModal from '@/components/CreateAgentModal';
import {
  createConversation,
  executeWizardTool,
  listAgentProfiles,
  listWizardTools,
  pinConversation,
  saveVaultMemory,
  uploadAndIngest,
  wizardChatStream,
  type AgentProfileSchema,
  type WizardChatToolCall,
  type WizardChatToolResult,
  type WizardStreamEvent,
  type WizardToolSchema,
} from '@/lib/api';

interface Message {
  id: string;
  role: 'user' | 'assistant' | 'system';
  content: string;
  toolCalls?: WizardChatToolCall[];
  toolStatus?: Record<string, 'idle' | 'running' | 'ok' | 'error' | 'awaiting-confirm'>;
  toolResults?: Record<string, string>;
  // Auto-class tool results that ran server-side inside the follow-up loop.
  // Surfaced as a compact "Wizard's reasoning" trace below the answer so the
  // user can see exactly what fired and what came back. Builds trust in the
  // model's grounding (RAG, web search, etc.) without dumping raw JSON.
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
  // Name of the agent profile that produced this reply ("wizard", "coder",
  // …). Snapshot at send time so the bubble keeps showing the right avatar
  // even after the user swaps to a different profile mid-conversation.
  agentProfile?: string;
}

function makeId(): string {
  return `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
}

export default function WizardChat() {
  const [messages, setMessages] = useState<Message[]>([]);
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
  // Active agent profile name. "wizard" = default persona. Picker swaps this
  // and the next turn gets the new persona + LLM preferences via the chat
  // stream's `profile` field.
  const [profile, setProfile] = useState<string>('wizard');
  const [creatingAgent, setCreatingAgent] = useState(false);
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

  // Auto-scroll to bottom on new messages.
  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: 'smooth' });
  }, [messages]);

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
        try {
          const { wizardDiagnostics } = await import('@/lib/api');
          const d = await wizardDiagnostics();
          if (cancelled) return;
          const match = d.findings.find(f => f.id === issueId);
          starter = match
            ? `Help me with: ${match.title}`
            : `Help me with issue: ${issueId}`;
        } catch {
          starter = `Help me with issue: ${issueId}`;
        }
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
    }
  };

  const handleAssistantToolCalls = (messageId: string, calls: WizardChatToolCall[]) => {
    for (const call of calls) {
      const schema = tools.get(call.name);
      // Auto-class: run immediately. Confirm-class: surface an inline card
      // with a Run button. Unknown tool: treat as confirm-required so we
      // never silently execute something we don't recognize.
      const isAuto = schema?.safety_class === 'auto';
      if (isAuto) {
        void runTool(messageId, call, true);
      } else {
        setMessages(prev => prev.map(m =>
          m.id === messageId
            ? {
              ...m,
              toolStatus: { ...(m.toolStatus ?? {}), [call.name]: 'awaiting-confirm' },
            }
            : m,
        ));
      }
    }
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
      const conv = await createConversation('Wizard chat');
      if (conv?.id) {
        convId = conv.id;
        setConversationId(conv.id);
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
      agentProfile: profile,
    };
    setMessages(prev => [...prev, userMsg, assistantSeed]);
    setDraft('');

    const history = messages
      .filter(m => m.role !== 'system')
      .slice(-12)
      .map(m => ({
        role: m.role as 'user' | 'assistant',
        content: m.content,
      }));

    const updateAssistant = (mut: (m: Message) => Message) => {
      setMessages(prev => prev.map(m => (m.id === assistantId ? mut(m) : m)));
    };

    try {
      for await (const event of wizardChatStream(text, {
        history,
        conversationId: convId ?? undefined,
        profile,
        signal: controller.signal,
      })) {
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
            updateAssistant(m => ({ ...m, toolCalls: event.tool_calls }));
            handleAssistantToolCalls(assistantId, event.tool_calls);
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
            }));
            setIterationStatus(null);
            break;
          case 'error':
            if (event.fallback) {
              updateAssistant(m => ({ ...m, content: event.fallback as string, mode: 'deterministic' }));
            }
            setError(event.error);
            setIterationStatus(null);
            break;
        }
      }
    } catch (err) {
      if ((err as { name?: string })?.name === 'AbortError') {
        // User reissued the chat — silent abort.
      } else {
        setError(err instanceof Error ? err.message : 'Wizard chat failed');
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
            onConfirmTool={(call) => runTool(message.id, call, true)}
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
            onChange={e => setDraft(e.target.value)}
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
          <AgentProfilePicker
            value={profile}
            onChange={setProfile}
            onCreateNew={() => setCreatingAgent(true)}
          />
          <CreateAgentModal
            open={creatingAgent}
            onClose={() => setCreatingAgent(false)}
            onCreated={(name) => setProfile(name)}
          />
          <span>
            Press Enter to send, Shift+Enter for a newline. Type{' '}
            <span className="text-[#76B900]">/help</span> for commands. Drop
            files into the chat to ingest them.
          </span>
        </div>
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
          Pick an agent to start.
        </div>
        <div className="mt-1 text-xs leading-relaxed" style={{ color: 'var(--text-secondary)' }}>
          Each profile maps to a persona + a preferred LLM. The default Wizard
          reads your live GPU, storage, providers, and vault before it answers
          and can run repairs, refresh models, and validate keys.
        </div>
      </div>

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
}: {
  message: Message;
  tools: Map<string, WizardToolSchema>;
  profileMap: Map<string, AgentProfileSchema>;
  onConfirmTool: (call: WizardChatToolCall) => void;
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
          >
            {profile.title}
          </div>
        )}
        <div>{message.content}</div>

        {!isUser && message.serverToolTrace && message.serverToolTrace.length > 0 && (
          <ServerToolTrace trace={message.serverToolTrace} />
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

        {message.mode === 'deterministic' && (
          <div className="mt-1 text-[9px] font-mono uppercase tracking-[0.14em]" style={{ color: 'var(--text-faint)' }}>
            offline helper
          </div>
        )}
        {message.mode === 'llm' && message.usedProvider && (
          <div className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-0.5 text-[9px] font-mono uppercase tracking-[0.14em]" style={{ color: 'var(--text-faint)' }}>
            <span>{message.usedProvider}{message.usedModel ? ` · ${message.usedModel.replace(/^.*\//, '')}` : ''}</span>
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
}: {
  call: WizardChatToolCall;
  schema: WizardToolSchema | undefined;
  status: 'idle' | 'running' | 'ok' | 'error' | 'awaiting-confirm';
  result?: string;
  onConfirm: () => void;
}) {
  const isConfirm = schema?.safety_class === 'confirm' || !schema; // unknown tools default to confirm
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
    default:
      badge = isConfirm ? 'Click to run' : 'Auto';
      badgeColor = isConfirm ? '#d97706' : '#76B900';
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
        {isConfirm && (status === 'idle' || status === 'awaiting-confirm') && (
          <button
            type="button"
            onClick={onConfirm}
            className="btn-primary px-2 py-1 text-[10px] font-mono"
          >
            Run
          </button>
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
 * ServerToolTrace renders auto-class tool calls that ran server-side
 * inside the Wizard's follow-up loop, so the user can see "Wizard
 * called rag_ask → got 4 chunks from notes.md, ideas.md" rather than
 * just an answer that appears out of nowhere.
 *
 * Collapsed by default to keep the chat feeling like chat. Click to
 * expand the details (args + raw result excerpt) for the curious.
 */
function ServerToolTrace({ trace }: { trace: WizardChatToolResult[] }) {
  const [open, setOpen] = useState(false);
  if (trace.length === 0) return null;
  return (
    <div className="mt-2 rounded-md border" style={{ background: 'var(--bg-subtle)', borderColor: 'var(--border)' }}>
      <button
        type="button"
        onClick={() => setOpen(o => !o)}
        className="flex w-full items-center justify-between px-2 py-1 text-[10px] font-mono"
        style={{ color: 'var(--text-muted)' }}
      >
        <span>
          {open ? '▾' : '▸'} Wizard used {trace.length} tool{trace.length === 1 ? '' : 's'} to answer
          <span style={{ color: 'var(--text-faint)' }}>
            {' '}
            ({trace.map(t => t.name).join(' → ')})
          </span>
        </span>
      </button>
      {open && (
        <div className="border-t px-2 py-2 space-y-2" style={{ borderColor: 'var(--border)' }}>
          {trace.map((entry, i) => (
            <ServerToolTraceItem key={`${entry.name}-${i}`} entry={entry} />
          ))}
        </div>
      )}
    </div>
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
