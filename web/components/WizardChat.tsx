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

import { useEffect, useRef, useState } from 'react';
import {
  executeWizardTool,
  listWizardTools,
  wizardChatStream,
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
  // One-line "thinking…" / "calling X…" status under the spinner. Drives the
  // perceived-latency win — even before the first token arrives, the user sees
  // the Wizard moving.
  const [iterationStatus, setIterationStatus] = useState<string | null>(null);
  const scrollRef = useRef<HTMLDivElement>(null);
  // Abort any in-flight stream when the user re-sends or unmounts.
  const abortRef = useRef<AbortController | null>(null);

  // Load the tool catalog once so we can look up safety classes for any
  // tool the LLM mentions, even before the server echoes one back.
  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const list = await listWizardTools();
        if (cancelled) return;
        const map = new Map<string, WizardToolSchema>();
        for (const t of list.tools) map.set(t.name, t);
        setTools(map);
      } catch {
        // Wizard tools endpoint missing on older builds — fall back to
        // treating all tool calls as confirm-class so we never auto-run.
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
    setError(null);
    setSending(true);
    setIterationStatus(null);

    // Abort any prior stream — the user just sent a new turn.
    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;

    const userMsg: Message = { id: makeId(), role: 'user', content: text };
    const assistantId = makeId();
    const assistantSeed: Message = {
      id: assistantId,
      role: 'assistant',
      content: '',
      toolCalls: [],
      toolStatus: {},
      serverToolTrace: [],
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
      for await (const event of wizardChatStream(text, { history, signal: controller.signal })) {
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

  const handleKey = (event: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault();
      void send();
    }
  };

  return (
    <div className="flex h-full flex-col">
      <div
        ref={scrollRef}
        className="flex-1 overflow-y-auto px-4 py-4 space-y-3"
        style={{ background: 'var(--bg-primary)' }}
      >
        {messages.length === 0 && (
          <EmptyState onPickPrompt={(p) => setDraft(p)} />
        )}
        {messages.map(message => (
          <MessageBlock
            key={message.id}
            message={message}
            tools={tools}
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
        <div className="mt-1 text-[10px] font-mono" style={{ color: 'var(--text-faint)' }}>
          Wizard answers from live workspace state. Press Enter to send,
          Shift+Enter for a newline.
        </div>
      </div>
    </div>
  );
}

function EmptyState({ onPickPrompt }: { onPickPrompt: (prompt: string) => void }) {
  // Adaptive starters — seeded from the reconnect payload so the Wizard's
  // suggestions match the user's actual workspace state. Falls back to the
  // generic set when reconnect is unavailable (cold first boot, offline, etc.).
  const [starters, setStarters] = useState<string[]>([
    'What can my GPU run right now?',
    "Why isn't Ollama responding?",
    'Help me pick a mission for image gen.',
    'Refresh the local model list.',
  ]);

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
    <div className="mx-auto max-w-lg pt-6 text-center">
      <div className="text-[10px] font-mono uppercase tracking-[0.18em]" style={{ color: '#76B900' }}>
        AI Wizard
      </div>
      <div className="mt-2 text-lg font-semibold" style={{ color: 'var(--text-primary)' }}>
        Ask me about your nvHive setup.
      </div>
      <div className="mt-1 text-xs leading-relaxed" style={{ color: 'var(--text-secondary)' }}>
        I read your live GPU, persistent storage, providers, install jobs, and
        receipts before I answer. I can also <em>do</em> things — refresh the
        model list, run safe repairs, validate keys. Confirm-class actions
        always show you a button first.
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

function MessageBlock({
  message,
  tools,
  onConfirmTool,
}: {
  message: Message;
  tools: Map<string, WizardToolSchema>;
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
  return (
    <div className={`flex ${isUser ? 'justify-end' : 'justify-start'}`}>
      <div
        className={`max-w-[80%] rounded-lg border px-3 py-2 text-sm whitespace-pre-wrap ${
          isUser ? '' : 'shadow-sm'
        }`}
        style={{
          background: isUser ? '#f7fdf0' : 'var(--bg-card)',
          borderColor: isUser ? 'var(--border-green)' : 'var(--border)',
          color: 'var(--text-primary)',
        }}
      >
        <div>{message.content}</div>

        {!isUser && message.serverToolTrace && message.serverToolTrace.length > 0 && (
          <ServerToolTrace trace={message.serverToolTrace} />
        )}

        {message.mode === 'deterministic' && (
          <div className="mt-1 text-[9px] font-mono uppercase tracking-[0.14em]" style={{ color: 'var(--text-faint)' }}>
            offline helper
          </div>
        )}
        {message.mode === 'llm' && message.usedProvider && (
          <div className="mt-1 text-[9px] font-mono uppercase tracking-[0.14em]" style={{ color: 'var(--text-faint)' }}>
            {message.usedProvider}
            {message.usedModel ? ` · ${message.usedModel.replace(/^.*\//, '')}` : ''}
            {message.iterations && message.iterations > 1 && (
              <> · {message.iterations} round-trips</>
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
