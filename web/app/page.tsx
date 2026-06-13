'use client';

import { useEffect, useRef, useState, useCallback } from 'react';
import Link from 'next/link';
import Sidebar from '@/components/Sidebar';
import ChatMessage, { exportConversationMarkdown } from '@/components/ChatMessage';
import ChatInput, { type ChatInputAttachment } from '@/components/ChatInput';
import { HardwareWidgetCompact, HardwareWidgetHero } from '@/components/HardwareWidget';
import WelcomeBackPanel from '@/components/WelcomeBackPanel';
import { useUIShell } from '@/components/UIShellProvider';
import {
  queryStream,
  query,
  runCouncil,
  compare,
  getModels,
  getStudioModels,
  getBudgetStatus,
  getConversations,
  getConversation,
  streamCouncil,
} from '@/lib/api';
import { useProviderHealth } from '@/lib/useProviderHealth';
import type {
  ChatMessage as ChatMessageType,
  ChatMode,
  ConversationSummary,
  MemberStreamState,
  WsCouncilStart,
} from '@/lib/types';

// ─── Local storage helpers ────────────────────────────────────────────────────

const STORAGE_KEY = 'council_chats_v2';

interface StoredState {
  conversations: ConversationSummary[];
  messages: Record<string, ChatMessageType[]>;
}

interface ModelOption {
  model_id: string;
  provider: string;
  display_name: string;
  is_local?: boolean;
  cost_tier?: 'free' | 'low' | 'high';
  supports_vision?: boolean;
}

function loadState(): StoredState {
  if (typeof window === 'undefined') return { conversations: [], messages: {} };
  try {
    return JSON.parse(localStorage.getItem(STORAGE_KEY) ?? '{"conversations":[],"messages":{}}');
  } catch {
    return { conversations: [], messages: {} };
  }
}

function saveState(state: StoredState) {
  if (typeof window === 'undefined') return;
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(state));
  } catch {
    // ignore quota errors
  }
}

function makeId(): string {
  return `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
}

function titleFromMessage(msg: string): string {
  const clean = msg.trim().replace(/\s+/g, ' ');
  return clean.length > 50 ? clean.slice(0, 47) + '...' : clean;
}

// ─── Budget pill ──────────────────────────────────────────────────────────────
// GPU pill is now <HardwareWidgetCompact /> from components/HardwareWidget.tsx,
// which also renders persistent-mount status — useful on cloud sessions.

function BudgetPill() {
  const [spend, setSpend] = useState<string | null>(null);

  useEffect(() => {
    getBudgetStatus()
      .then(b => setSpend(b.daily_spend))
      .catch(() => {});
  }, []);

  if (!spend) return null;
  const n = parseFloat(spend);
  if (isNaN(n)) return null;

  return (
    <div className="flex items-center gap-1 px-2 py-0.5 bg-[#ffffff] border border-[#e5e5e5] text-[10px] font-mono text-[#d97706] dark:bg-[#0a0a0a] dark:border-[#262626]">
      <span>USD</span>
      <span>${n.toFixed(2)} today</span>
    </div>
  );
}

// ─── Streaming council right panel ────────────────────────────────────────────

interface LiveCouncilPanelProps {
  memberOrder: string[];
  memberStates: Record<string, MemberStreamState>;
  synthesisContent: string;
  synthesisStatus: 'hidden' | 'streaming' | 'complete';
  phase: string;
}

function LiveCouncilPanel({ memberOrder, memberStates, synthesisContent, synthesisStatus, phase }: LiveCouncilPanelProps) {
  const COLORS = ['#2563eb', '#7c3aed', '#16a34a', '#d97706', '#0891b2', '#ea580c'];

  return (
    <div className="p-4 space-y-3">
      <div className="flex items-center gap-2 mb-3">
        <span className="h-2 w-2 bg-[#76B900]" />
        <span className="text-[10px] font-mono text-[#76B900] uppercase tracking-wider">Hive</span>
        {(phase === 'streaming' || phase === 'connecting') && (
          <span className="text-[9px] font-mono text-[#76B900] animate-pulse ml-1">LIVE</span>
        )}
        {phase === 'synthesis' && (
          <span className="text-[9px] font-mono text-[#2563eb] animate-pulse ml-1">SYNTHESIZING</span>
        )}
        {phase === 'done' && (
          <span className="text-[9px] font-mono text-[#16a34a] ml-1">COMPLETE</span>
        )}
      </div>

      {memberOrder.length === 0 && (
        <div className="text-center py-8 text-[#333333] text-xs font-mono dark:text-[#a3a3a3]">
          Connecting to council...
        </div>
      )}

      {memberOrder.map((label, i) => {
        const state = memberStates[label];
        if (!state) return null;
        const color = COLORS[i % COLORS.length];
        return (
          <div
            key={label}
            className="border p-3 bg-white transition-all dark:bg-[#0a0a0a]"
            style={{
              borderColor: state.status === 'streaming' ? color : `${color}30`,
            }}
          >
            <div className="flex items-center gap-2 mb-1.5">
              <span
                className={`w-1.5 h-1.5 rounded-full flex-shrink-0 ${
                  state.status === 'streaming' ? 'bg-[#76B900] animate-pulse' :
                  state.status === 'complete' ? 'bg-[#16a34a]' :
                  state.status === 'failed' ? 'bg-[#dc2626]' :
                  'bg-[#333333] dark:bg-[#525252]'
                }`}
              />
              <span className="text-xs font-mono font-bold" style={{ color }}>
                {state.provider.toUpperCase()}
              </span>
              {state.persona && (
                <span className="text-[9px] font-mono text-[#a3a3a3] dark:text-[#737373]">{state.persona}</span>
              )}
              {state.status === 'complete' && (
                <span className="ml-auto text-[9px] font-mono text-[#a3a3a3] dark:text-[#737373]">
                  {state.tokens > 0 ? `${state.tokens}t` : ''}
                </span>
              )}
            </div>
            <div className="text-[11px] text-[#525252] font-mono whitespace-pre-wrap leading-relaxed max-h-28 overflow-y-auto dark:text-[#a3a3a3]">
              {state.status === 'waiting' && (
                <span className="text-[#333333] italic dark:text-[#737373]">Waiting...</span>
              )}
              {state.status === 'failed' && (
                <span className="text-[#dc2626]">{state.error || 'Failed'}</span>
              )}
              {(state.status === 'streaming' || state.status === 'complete') && state.accumulated}
              {state.status === 'streaming' && (
                <span
                  className="inline-block w-[2px] h-[1em] bg-[#76B900] align-middle ml-0.5"
                  style={{ animation: 'blink 1s step-end infinite' }}
                />
              )}
            </div>
          </div>
        );
      })}

      {synthesisStatus !== 'hidden' && (
        <div className="border border-[#2563eb]/30 p-3 bg-[#eff6ff] dark:bg-[#2563eb]/10">
          <div className="text-[9px] font-mono text-[#2563eb] uppercase tracking-wider mb-2 flex items-center gap-1.5">
            <span className="h-2 w-2 bg-[#2563eb]" />
            <span>Synthesis</span>
            {synthesisStatus === 'streaming' && (
              <span className="animate-pulse">Generating...</span>
            )}
          </div>
          <div className="text-[11px] text-[#262626] font-mono whitespace-pre-wrap leading-relaxed max-h-40 overflow-y-auto dark:text-[#d4d4d4]">
            {synthesisContent}
            {synthesisStatus === 'streaming' && (
              <span
                className="inline-block w-[2px] h-[1em] bg-[#2563eb] align-middle ml-0.5"
                style={{ animation: 'blink 1s step-end infinite' }}
              />
            )}
          </div>
        </div>
      )}
    </div>
  );
}

// ─── Empty state ──────────────────────────────────────────────────────────────

function EmptyState({
  onPrompt,
  models,
  providerHealth,
  selectedModel,
}: {
  onPrompt: (p: string) => void;
  models: ModelOption[];
  providerHealth: Array<{ name: string; healthy: boolean; latency_ms: number | null; models_available: number; error: string | null }>;
  selectedModel: string;
}) {
  const suggestions = [
    'Help me debug this Python error',
    'Explain this calculus problem step by step',
    'Make a study guide from my notes',
    'Summarize this paper in plain English',
  ];
  const healthyProviders = providerHealth.filter(p => p.healthy);
  const localModelCount = models.filter(m => m.is_local || m.provider === 'ollama').length;
  const selected = models.find(m => m.model_id === selectedModel);
  const defaultModel = selected?.display_name || selectedModel || 'Select model';
  const readiness = [
    { label: 'AI Connections', value: providerHealth.length > 0 ? `${healthyProviders.length}/${providerHealth.length}` : 'checking' },
    { label: 'Installed Local Models', value: `${localModelCount}` },
    { label: 'Current Model', value: defaultModel },
  ];

  return (
    <div className="flex flex-1 items-center justify-center px-5 py-8">
      <div className="w-full max-w-5xl space-y-4">
        <WelcomeBackPanel />
        <div className="rounded-lg border border-[#262626] bg-white p-5 sm:p-6 shadow-[0_24px_80px_rgba(0,0,0,0.35)] dark:bg-[#0a0a0a]">
          <div className="flex flex-col gap-5 lg:flex-row lg:items-end lg:justify-between">
            <div>
              <div className="mb-2 flex items-center gap-2">
                <span className="h-2 w-2 bg-[#76B900]" />
                <span className="text-[10px] font-mono font-bold uppercase tracking-[0.24em] text-[#76B900]">
                  NVIDIA AI Workspace
                </span>
              </div>
              <h2 className="text-3xl font-semibold tracking-tight text-[#0a0a0a] sm:text-4xl dark:text-[#fafafa]">
                NVHive
              </h2>
              <p className="mt-3 max-w-2xl text-sm leading-6 text-[#525252] dark:text-[#a3a3a3]">
                Run a private AI assistant on your NVIDIA GPU, then add cloud models or a study group when you need a second opinion.
              </p>
              {localModelCount === 0 && (
                <Link
                  href="/setup"
                  className="mt-4 inline-flex items-center gap-2 rounded-md bg-[#76B900] px-4 py-2 text-xs font-mono font-bold uppercase tracking-wider text-black hover:bg-[#5a9100]"
                >
                  Install recommended local model
                </Link>
              )}
            </div>
            <div className="flex flex-col gap-3 lg:min-w-[420px]">
              <HardwareWidgetHero />
              <div className="grid grid-cols-1 gap-2 sm:grid-cols-3">
                {readiness.map(item => (
                  <div key={item.label} className="rounded-md border border-[#e5e5e5] bg-white px-3 py-2 text-left dark:bg-[#0a0a0a] dark:border-[#262626]">
                    <div className="text-[9px] font-mono uppercase tracking-[0.16em] text-[#737373] dark:text-[#a3a3a3]">{item.label}</div>
                    <div className="mt-1 truncate text-sm font-semibold text-[#0a0a0a] dark:text-[#fafafa]">{item.value}</div>
                  </div>
                ))}
              </div>
            </div>
          </div>
        </div>

        <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
          {suggestions.map((s, i) => (
            <button
              key={i}
              onClick={() => onPrompt(s)}
              className="group min-h-[72px] border border-[#e5e5e5] bg-white px-4 py-3 text-left transition-all hover:border-[#76B900]/50 hover:bg-[#f3f9e5] rounded-md dark:bg-[#0a0a0a] dark:border-[#262626]"
            >
              <div className="mb-2 flex items-center justify-between">
                <span className="text-[10px] font-mono uppercase tracking-[0.16em] text-[#737373] dark:text-[#a3a3a3]">
                  Try this
                </span>
                <span className="h-px w-8 bg-[#333333] transition-colors group-hover:bg-[#76B900] dark:bg-[#525252]" />
              </div>
              <div className="text-sm leading-snug text-[#404040] group-hover:text-[#0a0a0a] dark:text-[#d4d4d4]">
                {s}
              </div>
            </button>
          ))}
        </div>

        <div className="mt-3 flex items-center gap-2 rounded-md border border-[#e5e5e5] bg-[#fafafa] px-3 py-2 text-xs text-[#525252] dark:bg-[#141414] dark:border-[#262626] dark:text-[#a3a3a3]">
          <svg className="h-3.5 w-3.5 flex-shrink-0 text-[#76B900]" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.8}>
            <path strokeLinecap="round" strokeLinejoin="round" d="M18.375 12.739l-7.693 7.693a4.5 4.5 0 01-6.364-6.364l10.94-10.94A3 3 0 1119.5 7.372L8.552 18.32m.009-.01l-.01.01m5.699-9.941l-7.81 7.81a1.5 1.5 0 002.112 2.13" />
          </svg>
          <span>
            Drag an image into the chat below to ask about it — vision-capable models will analyze it.
          </span>
        </div>
      </div>
    </div>
  );
}

// ─── Main chat page ───────────────────────────────────────────────────────────

export default function ChatPage() {
  // Persisted state
  const [storedState, setStoredState] = useState<StoredState>({ conversations: [], messages: {} });
  const [hydrated, setHydrated] = useState(false);

  // Conversation state
  const [activeConvId, setActiveConvId] = useState<string | null>(null);
  const [messages, setMessages] = useState<ChatMessageType[]>([]);

  // Input state
  const [inputValue, setInputValue] = useState('');
  const [mode, setMode] = useState<ChatMode>('single');
  const [selectedModel, setSelectedModel] = useState('');
  const [models, setModels] = useState<ModelOption[]>([]);
  // Live-polled provider health — updates every 30s so the "connected/
  // offline" split in the model picker stays accurate throughout the
  // session without a manual refresh.
  const { providers: providerHealth } = useProviderHealth();

  // Streaming state
  const [streaming, setStreaming] = useState(false);
  const stopStreamRef = useRef<(() => void) | null>(null);

  // Share / export toast
  const [shareToast, setShareToast] = useState(false);

  // Mobile sidebar drawer
  const [mobileSidebarOpen, setMobileSidebarOpen] = useState(false);

  // Right panel (council mode)
  const [rightPanelOpen, setRightPanelOpen] = useState(false);
  const [councilPhase, setCouncilPhase] = useState<string>('idle');
  const [memberOrder, setMemberOrder] = useState<string[]>([]);
  const [memberStates, setMemberStates] = useState<Record<string, MemberStreamState>>({});
  const [synthesisContent, setSynthesisContent] = useState('');
  const [synthesisStatus, setSynthesisStatus] = useState<'hidden' | 'streaming' | 'complete'>('hidden');
  const wsRef = useRef<WebSocket | null>(null);
  const completedCostRef = useRef(0);
  const memberStatesRef = useRef<Record<string, MemberStreamState>>({});
  const memberModelByLabelRef = useRef<Record<string, string>>({});
  // Ref mirror of the live synthesis content. The WebSocket callbacks
  // are created once at submit time, so reading `synthesisContent`
  // state inside `onComplete` gives the stale closure value (empty
  // string on first council run) and wipes the message content the
  // user just watched stream in. Reading via ref always sees the
  // current value.
  const synthesisContentRef = useRef('');

  // Remote conversations (from API)
  const [remoteConvs, setRemoteConvs] = useState<ConversationSummary[]>([]);

  const messagesEndRef = useRef<HTMLDivElement>(null);

  // Hydrate from local storage
  useEffect(() => {
    const stored = loadState();
    setStoredState(stored);
    setHydrated(true);
  }, []);

  // Load models once on mount. Provider health is handled separately
  // by useProviderHealth (polled every 30s).
  useEffect(() => {
    let cancelled = false;
    Promise.allSettled([getModels(), getStudioModels()])
      .then(([catalogResult, studioResult]) => {
        if (cancelled) return;
        const mapped = catalogResult.status === 'fulfilled'
          ? catalogResult.value.models.map(m => ({
              model_id: m.model_id,
              provider: m.provider,
              display_name: m.display_name,
              is_local: m.provider === 'ollama' || m.provider === 'local',
              cost_tier: m.input_cost_per_1m_tokens
                ? (parseFloat(m.input_cost_per_1m_tokens) > 1 ? 'high' as const : 'low' as const)
                : 'free' as const,
              supports_vision: Boolean(m.supports_vision),
            }))
          : [];
        const installedLocal = studioResult.status === 'fulfilled'
          ? studioResult.value.models
              .filter(m => m.installed)
              .map(m => ({
                model_id: m.install_target,
                provider: 'ollama',
                display_name: m.title || m.install_target,
                is_local: true,
                cost_tier: 'free' as const,
                supports_vision: /vision|llava|minicpm|moondream|bakllava/i.test(m.install_target),
              }))
          : [];
        const seen = new Set<string>();
        const merged = [...installedLocal, ...mapped].filter(model => {
          const key = `${model.provider}:${model.model_id}`;
          if (seen.has(key)) return false;
          seen.add(key);
          return true;
        });
        setModels(merged);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  // Once both models and provider health are loaded, pick a sensible
  // default: first model whose provider is currently reachable. This
  // only runs while selectedModel is unset so we don't yank the user's
  // choice when health polling tells us a different provider came
  // back online.
  useEffect(() => {
    if (selectedModel) return;
    if (models.length === 0) return;
    if (providerHealth.length === 0) {
      // Health not yet loaded — temporarily use the first model so the
      // UI isn't blank. The next run of this effect (once health
      // arrives) will upgrade the pick to a healthy one.
      setSelectedModel(models[0].model_id);
      return;
    }
    const healthyNames = new Set(providerHealth.filter(p => p.healthy).map(p => p.name));
    const firstHealthy = models.find(m => healthyNames.has(m.provider) || m.is_local);
    setSelectedModel((firstHealthy ?? models[0]).model_id);
  }, [models, providerHealth, selectedModel]);

  // Try loading remote conversations
  useEffect(() => {
    getConversations().then(setRemoteConvs).catch(() => {});
  }, []);

  // Scroll to bottom when messages change
  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages]);

  // Merge local + remote conversations for sidebar
  const allConversations: ConversationSummary[] = (() => {
    // If we have remote data, prefer it; otherwise use local storage
    if (remoteConvs.length > 0) return remoteConvs;
    return storedState.conversations;
  })();

  // Persist state changes
  const updateStoredState = useCallback((updater: (prev: StoredState) => StoredState) => {
    setStoredState(prev => {
      const next = updater(prev);
      saveState(next);
      return next;
    });
  }, []);

  const updateMemberStates = useCallback((
    updater: Record<string, MemberStreamState> | ((prev: Record<string, MemberStreamState>) => Record<string, MemberStreamState>)
  ) => {
    setMemberStates(prev => {
      const next = typeof updater === 'function' ? updater(prev) : updater;
      memberStatesRef.current = next;
      return next;
    });
  }, []);

  const addMessage = useCallback((msg: ChatMessageType) => {
    setMessages(prev => [...prev, msg]);
    if (activeConvId) {
      updateStoredState(prev => ({
        ...prev,
        messages: {
          ...prev.messages,
          [activeConvId]: [...(prev.messages[activeConvId] ?? []), msg],
        },
      }));
    }
  }, [activeConvId, updateStoredState]);

  const updateLastMessage = useCallback((updater: (msg: ChatMessageType) => ChatMessageType) => {
    setMessages(prev => {
      if (prev.length === 0) return prev;
      const updated = [...prev];
      updated[updated.length - 1] = updater(updated[updated.length - 1]);
      return updated;
    });
  }, []);

  // eslint-disable-next-line react-hooks/exhaustive-deps
  const handleNewChat = useCallback(() => {
    stopStreamRef.current?.();
    stopStreamRef.current = null;
    wsRef.current?.close();
    wsRef.current = null;
    setStreaming(false);

    const id = makeId();
    const newConv: ConversationSummary = {
      id,
      title: 'New Chat',
      mode: 'single',
      message_count: 0,
      created_at: Date.now(),
      updated_at: Date.now(),
    };

    updateStoredState(prev => ({
      conversations: [newConv, ...prev.conversations],
      messages: { ...prev.messages, [id]: [] },
    }));

    setActiveConvId(id);
    setMessages([]);
    setInputValue('');
    setRightPanelOpen(false);
    setCouncilPhase('idle');
    setMemberOrder([]);
    memberStatesRef.current = {};
    memberModelByLabelRef.current = {};
    setMemberStates({});
    setSynthesisContent('');
    setSynthesisStatus('hidden');
  }, [updateStoredState]);

  const handleSelectConversation = useCallback(async (id: string) => {
    if (!id) {
      // Clear active, go to home
      setActiveConvId(null);
      setMessages([]);
      return;
    }
    stopStreamRef.current?.();
    setStreaming(false);
    setActiveConvId(id);
    const msgs = storedState.messages[id] ?? [];
    setMessages(msgs);
    setCouncilPhase('idle');
    setMemberOrder([]);
    memberStatesRef.current = {};
    memberModelByLabelRef.current = {};
    setMemberStates({});
    setSynthesisContent('');
    setSynthesisStatus('hidden');
    setRightPanelOpen(false);

    if (msgs.length === 0) {
      const remote = await getConversation(id);
      if (remote?.messages?.length) {
        const mapped: ChatMessageType[] = remote.messages
          .filter(m => m.role !== 'system')
          .map(m => ({
            id: m.id,
            role: m.role === 'user' ? 'user' : 'assistant',
            content: m.content,
            provider: m.provider,
            model: m.model,
            mode: m.mode,
            cost_usd: m.cost_usd,
            tokens: m.tokens,
            latency_ms: m.latency_ms,
            council_data: m.council_data,
            timestamp: m.timestamp,
          }));
        setMessages(mapped);
        updateStoredState(prev => ({
          ...prev,
          messages: {
            ...prev.messages,
            [id]: mapped,
          },
        }));
      }
    }
  }, [storedState.messages, updateStoredState]);

  const handleRenameConversation = useCallback((id: string, title: string) => {
    updateStoredState(prev => ({
      ...prev,
      conversations: prev.conversations.map(c => c.id === id ? { ...c, title } : c),
    }));
  }, [updateStoredState]);

  const handleDeleteConversation = useCallback((id: string) => {
    updateStoredState(prev => ({
      conversations: prev.conversations.filter(c => c.id !== id),
      messages: Object.fromEntries(Object.entries(prev.messages).filter(([k]) => k !== id)),
    }));
    if (activeConvId === id) {
      setActiveConvId(null);
      setMessages([]);
    }
  }, [activeConvId, updateStoredState]);

  const handlePinConversation = useCallback((id: string) => {
    updateStoredState(prev => ({
      ...prev,
      conversations: prev.conversations.map(c => c.id === id ? { ...c, pinned: !c.pinned } : c),
    }));
  }, [updateStoredState]);

  // Export a conversation from the sidebar context menu as a Markdown
  // download. Uses locally cached messages (remote-only conversations are
  // pulled into the cache the first time they're opened).
  const handleExportConversation = useCallback((id: string) => {
    const msgs = id === activeConvId && messages.length > 0
      ? messages
      : storedState.messages[id] ?? [];
    if (msgs.length === 0) return;
    const lastAssistant = msgs.slice().reverse().find(m => m.role === 'assistant');
    const advisorLabel = lastAssistant?.model
      ? `${lastAssistant.model}${lastAssistant.provider ? ` (${lastAssistant.provider})` : ''}`
      : lastAssistant?.provider ?? 'assistant';
    const md = exportConversationMarkdown(msgs, advisorLabel, new Date().toISOString().slice(0, 10));
    const sourceConvs = remoteConvs.length > 0 ? remoteConvs : storedState.conversations;
    const title = sourceConvs.find(c => c.id === id)?.title ?? 'conversation';
    const slug = title.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 48) || 'conversation';
    const blob = new Blob([md], { type: 'text/markdown' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `${slug}.md`;
    a.click();
    URL.revokeObjectURL(url);
  }, [activeConvId, messages, storedState, remoteConvs]);

  const resetCouncilState = useCallback(() => {
    setCouncilPhase('idle');
    setMemberOrder([]);
    memberStatesRef.current = {};
    memberModelByLabelRef.current = {};
    setMemberStates({});
    setSynthesisContent('');
    setSynthesisStatus('hidden');
  }, []);

  const handleStop = useCallback(() => {
    stopStreamRef.current?.();
    stopStreamRef.current = null;
    wsRef.current?.close();
    wsRef.current = null;
    setStreaming(false);

    // Mark last message as no longer streaming
    updateLastMessage(msg => ({ ...msg, streaming: false }));
  }, [updateLastMessage]);

  // Safety: auto-reset streaming if stuck for more than 60 seconds
  useEffect(() => {
    if (!streaming) return;
    const timer = setTimeout(() => {
      console.warn('nvHive: streaming timeout — auto-resetting');
      handleStop();
    }, 60000);
    return () => clearTimeout(timer);
  }, [streaming, handleStop]);

  const handleSubmit = useCallback(async (
    promptOverride?: string,
    attachments: ChatInputAttachment[] = []
  ) => {
    const prompt = (promptOverride ?? inputValue).trim();
    if (!prompt || streaming) return;
    const imageAttachments = attachments
      .filter(att => att.isImage)
      .map(att => ({
        name: att.name,
        content: att.content,
        mime_type: att.mimeType,
        is_image: true,
      }));
    const hasImageAttachments = imageAttachments.length > 0;

    // Pre-flight health gate: in 'single' mode, if the user has a
    // specific model selected and its backing provider is currently
    // offline, warn them inline instead of firing a query that will
    // just error out. Local models (ollama) are always considered
    // connected since they don't route through the advisor health
    // system. This catches the "I selected gpt-4o but OpenAI is
    // rate-limited" failure mode before it wastes a round trip.
    if (mode === 'single' && selectedModel && providerHealth.length > 0) {
      const modelInfo = models.find(m => m.model_id === selectedModel);
      if (modelInfo && !modelInfo.is_local) {
        const providerStatus = providerHealth.find(p => p.name === modelInfo.provider);
        if (providerStatus && !providerStatus.healthy) {
          const firstHealthy = models.find(m => {
            if (m.is_local) return true;
            const h = providerHealth.find(p => p.name === m.provider);
            return h?.healthy;
          });
          const message = firstHealthy
            ? `${modelInfo.provider} is offline. Switch to ${firstHealthy.display_name}?`
            : `${modelInfo.provider} is offline and no healthy advisors are available. Check /v1/advisors.`;
          // Non-blocking: browser confirm() is synchronous but keeps
          // the fix contained to this one line. A nicer inline toast
          // could replace this later without changing the logic.
          if (typeof window !== 'undefined' && !window.confirm(
            `${message}\n\n` +
            `OK = switch and submit, Cancel = abort.`
          )) {
            return;
          }
          if (firstHealthy) setSelectedModel(firstHealthy.model_id);
        }
      }
    }

    // Ensure we have an active conversation
    let convId = activeConvId;
    if (!convId) {
      convId = makeId();
      const newConv: ConversationSummary = {
        id: convId,
        title: titleFromMessage(prompt),
        mode,
        message_count: 0,
        created_at: Date.now(),
        updated_at: Date.now(),
      };
      updateStoredState(prev => ({
        conversations: [newConv, ...prev.conversations],
        messages: { ...prev.messages, [convId!]: [] },
      }));
      setActiveConvId(convId);
    }

    const userMsg: ChatMessageType = {
      id: makeId(),
      role: 'user',
      content: prompt,
      timestamp: Date.now(),
    };

    setMessages(prev => [...prev, userMsg]);
    updateStoredState(prev => ({
      ...prev,
      conversations: prev.conversations.map(c =>
        c.id === convId
          ? { ...c, title: c.title === 'New Chat' ? titleFromMessage(prompt) : c.title, message_count: c.message_count + 1, updated_at: Date.now(), mode }
          : c
      ),
      messages: {
        ...prev.messages,
        [convId!]: [...(prev.messages[convId!] ?? []), userMsg],
      },
    }));

    setInputValue('');
    setStreaming(true);

    if (mode === 'single') {
      // Use SSE streaming
      const assistantMsgId = makeId();
      const assistantMsg: ChatMessageType = {
        id: assistantMsgId,
        role: 'assistant',
        content: '',
        streaming: true,
        mode: 'single',
        timestamp: Date.now(),
      };

      setMessages(prev => [...prev, assistantMsg]);

      if (hasImageAttachments) {
        try {
          const resp = await query(prompt, {
            model: selectedModel || undefined,
            attachments: imageAttachments,
          });
          setStreaming(false);
          stopStreamRef.current = null;
          const finalMsg: ChatMessageType = {
            id: assistantMsgId,
            role: 'assistant',
            content: resp.content,
            streaming: false,
            mode: 'single',
            provider: resp.provider,
            model: resp.model,
            tokens: resp.usage?.total_tokens,
            cost_usd: resp.cost_usd ?? null,
            latency_ms: resp.latency_ms,
            timestamp: Date.now(),
          };
          setMessages(prev =>
            prev.map(m => m.id === assistantMsgId ? finalMsg : m)
          );
          updateStoredState(prev => ({
            ...prev,
            messages: {
              ...prev.messages,
              [convId!]: [
                ...(prev.messages[convId!] ?? []).filter(m => m.id !== assistantMsgId),
                finalMsg,
              ],
            },
          }));
        } catch (error) {
          setStreaming(false);
          stopStreamRef.current = null;
          const errMsg: ChatMessageType = {
            id: assistantMsgId,
            role: 'error',
            content: error instanceof Error ? error.message : String(error),
            timestamp: Date.now(),
          };
          setMessages(prev =>
            prev.map(m => m.id === assistantMsgId ? errMsg : m)
          );
        }
        return;
      }

      const stop = queryStream(
        { prompt, stream: true, model: selectedModel || undefined },
        (chunk) => {
          setMessages(prev =>
            prev.map(m => m.id === assistantMsgId ? { ...m, content: chunk.accumulated } : m)
          );
        },
        (done) => {
          setStreaming(false);
          stopStreamRef.current = null;
          const finalMsg: ChatMessageType = {
            id: assistantMsgId,
            role: 'assistant',
            content: done.content,
            streaming: false,
            mode: 'single',
            provider: done.provider,
            model: done.model,
            tokens: done.usage?.total_tokens,
            cost_usd: done.cost_usd ?? null,
            timestamp: Date.now(),
          };
          setMessages(prev =>
            prev.map(m => m.id === assistantMsgId ? finalMsg : m)
          );
          updateStoredState(prev => ({
            ...prev,
            messages: {
              ...prev.messages,
              [convId!]: [
                ...(prev.messages[convId!] ?? []).filter(m => m.id !== assistantMsgId),
                finalMsg,
              ],
            },
          }));
        },
        (err) => {
          setStreaming(false);
          stopStreamRef.current = null;
          const errMsg: ChatMessageType = {
            id: assistantMsgId,
            role: 'error',
            content: err,
            timestamp: Date.now(),
          };
          setMessages(prev =>
            prev.map(m => m.id === assistantMsgId ? errMsg : m)
          );
        }
      );
      stopStreamRef.current = stop;

    } else if (mode === 'council') {
      // Use WebSocket streaming for council
      setRightPanelOpen(true);
      // Reset council state inline
      setMemberOrder([]);
      memberStatesRef.current = {};
      memberModelByLabelRef.current = {};
      setMemberStates({});
      setSynthesisContent('');
      setSynthesisStatus('hidden');
      setCouncilPhase('connecting');

      const assistantMsgId = makeId();
      const placeholderMsg: ChatMessageType = {
        id: assistantMsgId,
        role: 'assistant',
        content: '',
        streaming: true,
        mode: 'council',
        timestamp: Date.now(),
      };
      setMessages(prev => [...prev, placeholderMsg]);
      completedCostRef.current = 0;

      const ws = streamCouncil(
        { prompt, synthesize: true },
        {
          onStart: (data: WsCouncilStart) => {
            const order = data.members.map(m =>
              m.persona ? `${m.provider}:${m.persona}` : m.provider
            );
            const states: Record<string, MemberStreamState> = {};
            const modelMap: Record<string, string> = {};
            data.members.forEach(m => {
              const label = m.persona ? `${m.provider}:${m.persona}` : m.provider;
              modelMap[label] = m.model;
              states[label] = {
                label,
                provider: m.provider,
                persona: m.persona ?? '',
                status: 'waiting',
                accumulated: '',
                tokens: 0,
                cost: '0',
                latency_ms: 0,
                elapsedMs: 0,
              };
            });
            setMemberOrder(order);
            memberModelByLabelRef.current = modelMap;
            updateMemberStates(states);
            setCouncilPhase('streaming');
          },
          onMemberStart: (member) => {
            updateMemberStates(prev => ({
              ...prev,
              [member]: {
                ...(prev[member] ?? {
                  label: member,
                  provider: member.split(':')[0],
                  persona: member.includes(':') ? member.split(':').slice(1).join(':') : '',
                  tokens: 0, cost: '0', latency_ms: 0, elapsedMs: 0, accumulated: '',
                }),
                status: 'streaming',
              },
            }));
          },
          onMemberChunk: (member, _delta, accumulated) => {
            updateMemberStates(prev => ({
              ...prev,
              [member]: { ...(prev[member] ?? { label: member, provider: member.split(':')[0], persona: '', tokens: 0, cost: '0', latency_ms: 0, elapsedMs: 0 }), accumulated, status: 'streaming' },
            }));
          },
          onMemberComplete: (member, content, tokens, cost, latency) => {
            updateMemberStates(prev => ({
              ...prev,
              [member]: { ...(prev[member] ?? { label: member, provider: member.split(':')[0], persona: '', accumulated: '', elapsedMs: 0 }), status: 'complete', accumulated: content, tokens, cost, latency_ms: latency },
            }));
            completedCostRef.current += parseFloat(cost) || 0;
          },
          onMemberFailed: (member, err) => {
            updateMemberStates(prev => ({
              ...prev,
              [member]: { ...(prev[member] ?? { label: member, provider: member.split(':')[0], persona: '', accumulated: '', tokens: 0, cost: '0', latency_ms: 0, elapsedMs: 0 }), status: 'failed', error: err },
            }));
          },
          onSynthesisStart: () => {
            setCouncilPhase('synthesis');
            setSynthesisStatus('streaming');
            synthesisContentRef.current = '';
          },
          onSynthesisChunk: (_delta, accumulated) => {
            synthesisContentRef.current = accumulated;
            setSynthesisContent(accumulated);
            // Update the main message with synthesis content as it streams
            setMessages(prev =>
              prev.map(m => m.id === assistantMsgId ? { ...m, content: accumulated } : m)
            );
          },
          onSynthesisComplete: (content, _tokens, cost) => {
            synthesisContentRef.current = content;
            setSynthesisContent(content);
            setSynthesisStatus('complete');
            completedCostRef.current += parseFloat(cost) || 0;
            // Lock the final content into the assistant message now —
            // before onComplete races in with its stale closure.
            setMessages(prev =>
              prev.map(m => m.id === assistantMsgId ? { ...m, content } : m)
            );
          },
          onComplete: (totalCost, _totalLatency, _quorumMet) => {
            setCouncilPhase('done');
            setStreaming(false);
            wsRef.current = null;

            // Read from the ref so we get the live synthesis, not the
            // closure value captured when handleSubmit was built.
            const finalContent = synthesisContentRef.current;
            const memberStatesSnapshot = Object.fromEntries(
              Object.entries(memberStatesRef.current).map(([label, state]) => [
                label,
                {
                  content: state.accumulated,
                  provider: state.provider,
                  model: memberModelByLabelRef.current[label] ?? state.provider,
                  tokens: state.tokens,
                  cost: state.cost,
                  latency_ms: state.latency_ms,
                },
              ])
            ) as Record<string, { content: string; provider: string; model: string; tokens: number; cost: string; latency_ms?: number }>;
            const finalMsg: ChatMessageType = {
              id: assistantMsgId,
              role: 'assistant',
              content: finalContent,
              streaming: false,
              mode: 'council',
              cost_usd: totalCost,
              timestamp: Date.now(),
              council_data: {
                member_responses: memberStatesSnapshot,
                synthesis: finalContent,
                total_cost: totalCost,
              },
            };
            setMessages(prev =>
              prev.map(m => m.id === assistantMsgId ? finalMsg : m)
            );
            updateStoredState(prev => ({
              ...prev,
              messages: {
                ...prev.messages,
                [convId!]: [
                  ...(prev.messages[convId!] ?? []).filter(m => m.id !== assistantMsgId),
                  finalMsg,
                ],
              },
            }));
          },
          onError: (err) => {
            setCouncilPhase('error');
            setStreaming(false);
            wsRef.current = null;
            const errMsg: ChatMessageType = {
              id: assistantMsgId,
              role: 'error',
              content: err,
              timestamp: Date.now(),
            };
            setMessages(prev =>
              prev.map(m => m.id === assistantMsgId ? errMsg : m)
            );
          },
        }
      );
      wsRef.current = ws;

    } else if (mode === 'compare') {
      // Non-streaming compare
      const assistantMsgId = makeId();
      const placeholderMsg: ChatMessageType = {
        id: assistantMsgId,
        role: 'assistant',
        content: 'Polling advisors...',
        streaming: true,
        mode: 'compare',
        timestamp: Date.now(),
      };
      setMessages(prev => [...prev, placeholderMsg]);

      try {
        const result = await compare(prompt);
        const compareData: NonNullable<ChatMessageType['compare_data']> = {};
        for (const [provider, resp] of Object.entries(result)) {
          compareData[provider] = {
            content: resp.content,
            model: resp.model,
            tokens: resp.usage?.total_tokens,
            cost_usd: resp.cost_usd,
            latency_ms: resp.latency_ms,
            cache_hit: resp.cache_hit,
          };
        }
        const finalMsg: ChatMessageType = {
          id: assistantMsgId,
          role: 'assistant',
          content: '',
          streaming: false,
          mode: 'compare',
          compare_data: compareData,
          timestamp: Date.now(),
        };
        setMessages(prev =>
          prev.map(m => m.id === assistantMsgId ? finalMsg : m)
        );
        updateStoredState(prev => ({
          ...prev,
          messages: {
            ...prev.messages,
            [convId!]: [
              ...(prev.messages[convId!] ?? []).filter(m => m.id !== assistantMsgId),
              finalMsg,
            ],
          },
        }));
      } catch (err) {
        const errMsg: ChatMessageType = {
          id: assistantMsgId,
          role: 'error',
          content: err instanceof Error ? err.message : 'Compare failed',
          timestamp: Date.now(),
        };
        setMessages(prev =>
          prev.map(m => m.id === assistantMsgId ? errMsg : m)
        );
      } finally {
        setStreaming(false);
      }
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [inputValue, streaming, activeConvId, mode, selectedModel, updateStoredState, synthesisContent]);

  // Mode change — open right panel for council
  const handleModeChange = useCallback((newMode: ChatMode) => {
    setMode(newMode);
    if (newMode === 'council') {
      setRightPanelOpen(true);
    } else {
      setRightPanelOpen(false);
    }
  }, []);

  // ─── Share / export conversation ─────────────────────────────────────────────

  const handleShare = useCallback(() => {
    if (messages.length === 0) return;

    // Determine advisor label from last assistant message
    const lastAssistant = messages.slice().reverse().find(m => m.role === 'assistant');
    const advisorLabel = lastAssistant?.model
      ? `${lastAssistant.model}${lastAssistant.provider ? ` (${lastAssistant.provider})` : ''}`
      : lastAssistant?.provider ?? 'assistant';

    const md = exportConversationMarkdown(
      messages,
      advisorLabel,
      new Date().toISOString().slice(0, 10)
    );

    navigator.clipboard.writeText(md).then(() => {
      setShareToast(true);
      setTimeout(() => setShareToast(false), 2500);
    }).catch(() => {});
  }, [messages]);

  // ─── Global keyboard shortcut subscriptions ──────────────────────────────────
  const { onShortcut } = useUIShell();

  useEffect(() => {
    const unsubs = [
      onShortcut('new-chat',       () => handleNewChat()),
      onShortcut('mode-ask',       () => handleModeChange('single')),
      onShortcut('mode-convene',   () => handleModeChange('council')),
      onShortcut('mode-poll',      () => handleModeChange('compare')),
      onShortcut('stop-generation', () => handleStop()),
      onShortcut('copy-last-response', () => {
        const last = messages.slice().reverse().find(m => m.role === 'assistant');
        if (last?.content) {
          navigator.clipboard.writeText(last.content).catch(() => {});
        }
      }),
    ];
    return () => unsubs.forEach(fn => fn());
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [onShortcut, handleNewChat, handleModeChange, handleStop, messages]);

  // Listen for model switch events dispatched by GlobalModals
  useEffect(() => {
    const handler = (e: Event) => {
      const { modelId } = (e as CustomEvent<{ modelId: string }>).detail;
      if (modelId) setSelectedModel(modelId);
    };
    window.addEventListener('hive:switch-model', handler);
    return () => window.removeEventListener('hive:switch-model', handler);
  }, []);

  if (!hydrated) return null;

  const showRightPanel = rightPanelOpen && mode === 'council';
  const hasCouncilActivity = memberOrder.length > 0 || councilPhase !== 'idle';

  return (
    <div className="flex h-screen overflow-hidden bg-[#ffffff] dark:bg-[#0a0a0a]">
      {/* Mobile sidebar overlay */}
      {mobileSidebarOpen && (
        <div
          className="fixed inset-0 z-40 bg-black/60 md:hidden"
          onClick={() => setMobileSidebarOpen(false)}
        />
      )}

      {/* Sidebar — receives conversations from local state */}
      <div className={`
        md:relative md:translate-x-0 md:flex
        fixed inset-y-0 left-0 z-50 transition-transform duration-200
        ${mobileSidebarOpen ? 'translate-x-0' : '-translate-x-full md:translate-x-0'}
      `}>
        <Sidebar
          topOffset={false}
          conversations={allConversations}
          activeConversationId={activeConvId}
          onNewChat={() => { handleNewChat(); setMobileSidebarOpen(false); }}
          onSelectConversation={(id) => { handleSelectConversation(id); setMobileSidebarOpen(false); }}
          onRenameConversation={handleRenameConversation}
          onDeleteConversation={handleDeleteConversation}
          onPinConversation={handlePinConversation}
          onExportConversation={handleExportConversation}
        />
      </div>

      {/* Main area */}
      <div className="flex flex-col flex-1 min-w-0 overflow-hidden">
        {/* Top bar */}
        <div className="flex items-center px-3 h-11 border-b border-[#e5e5e5] bg-white flex-shrink-0 gap-2 dark:bg-[#0a0a0a] dark:border-[#262626]">
          {/* Hamburger — mobile only */}
          <button
            className="md:hidden flex-shrink-0 w-8 h-8 flex items-center justify-center text-[#a3a3a3] hover:text-[#76B900] transition-colors dark:text-[#737373]"
            onClick={() => setMobileSidebarOpen(true)}
            title="Open sidebar"
          >
            <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
              <path strokeLinecap="round" strokeLinejoin="round" d="M4 6h16M4 12h16M4 18h16" />
            </svg>
          </button>

          <div className="flex items-center gap-2 flex-1 min-w-0">
            <span className="hidden sm:inline text-[10px] font-mono font-bold uppercase tracking-[0.22em] text-[#76B900]">
              NVHIVE
            </span>
            <span className="hidden sm:inline text-[#333333] dark:text-[#525252]">/</span>
            {activeConvId && (
              <span className="text-xs font-mono text-[#a3a3a3] truncate dark:text-[#737373]">
                {allConversations.find(c => c.id === activeConvId)?.title ?? 'Chat'}
              </span>
            )}
            {!activeConvId && (
              <span className="text-xs font-mono text-[#a3a3a3] truncate dark:text-[#737373]">
                NVIDIA AI Workspace
              </span>
            )}
          </div>
          <div className="flex items-center gap-2">
            <HardwareWidgetCompact />
            <BudgetPill />
            {/* Share button */}
            {messages.length > 0 && (
              <button
                onClick={handleShare}
                className="text-[10px] font-mono px-2 py-0.5 border border-[#d4d4d4] text-[#a3a3a3] hover:border-[#76B900]/30 hover:text-[#76B900] transition-colors flex items-center gap-1 dark:border-[#404040] dark:text-[#737373]"
                title="Export conversation as Markdown (copied to clipboard)"
              >
                <svg className="w-3 h-3" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                  <path strokeLinecap="round" strokeLinejoin="round" d="M7.217 10.907a2.25 2.25 0 100 2.186m0-2.186c.18.324.283.696.283 1.093s-.103.77-.283 1.093m0-2.186l9.566-5.314m-9.566 7.5l9.566 5.314m0 0a2.25 2.25 0 103.935 2.186 2.25 2.25 0 00-3.935-2.186zm0-12.814a2.25 2.25 0 103.933-2.185 2.25 2.25 0 00-3.933 2.185z" />
                </svg>
                <span className="hidden sm:inline">Share</span>
              </button>
            )}
            {mode === 'council' && (
              <button
                onClick={() => setRightPanelOpen(prev => !prev)}
                className={`text-[10px] font-mono px-2 py-0.5 border transition-colors ${
                  rightPanelOpen
                    ? 'border-[#76B900]/40 bg-[#76B900]/10 text-[#76B900]'
                    : 'border-[#d4d4d4] text-[#a3a3a3] hover:border-[#76B900]/30 hover:text-[#76B900]'
                }`}
                title="Toggle hive panel"
              >
                Panel
              </button>
            )}
          </div>
        </div>

        {/* Share toast */}
        {shareToast && (
          <div className="fixed bottom-24 left-1/2 -translate-x-1/2 z-50 px-4 py-2 bg-[#76B900] text-black text-xs font-mono font-bold uppercase tracking-wider shadow-lg animate-fade-in pointer-events-none">
            Copied to clipboard!
          </div>
        )}

        {/* Chat + right panel */}
        <div className="flex flex-1 overflow-hidden">
          {/* Chat area */}
          <div className="flex flex-col flex-1 min-w-0 overflow-hidden">
            {/* Messages */}
            <div className="flex-1 overflow-y-auto py-4">
              {messages.length === 0 ? (
                <EmptyState
                  models={models}
                  providerHealth={providerHealth}
                  selectedModel={selectedModel}
                  onPrompt={p => {
                    setInputValue(p);
                    // Small delay to let state update, then focus
                    setTimeout(() => {
                      const ta = document.querySelector('textarea');
                      ta?.focus();
                    }, 50);
                  }}
                />
              ) : (
                <>
                  {messages.map(msg => (
                    <ChatMessage key={msg.id} message={msg} />
                  ))}
                  {/* Convene: also show expert panel inline when right panel closed */}
                  {mode === 'council' && !showRightPanel && hasCouncilActivity && (
                    <div className="mx-4 mt-2 border border-[#76B900]/20 bg-white dark:bg-[#0a0a0a]">
                      <LiveCouncilPanel
                        memberOrder={memberOrder}
                        memberStates={memberStates}
                        synthesisContent={synthesisContent}
                        synthesisStatus={synthesisStatus}
                        phase={councilPhase}
                      />
                    </div>
                  )}
                  <div ref={messagesEndRef} />
                </>
              )}
            </div>

            {/* Input bar */}
            <ChatInput
              value={inputValue}
              onChange={setInputValue}
              onSubmit={handleSubmit}
              onStop={handleStop}
              mode={mode}
              onModeChange={handleModeChange}
              selectedModel={selectedModel}
              onModelChange={setSelectedModel}
              models={models}
              providerHealth={providerHealth}
              streaming={streaming}
            />
          </div>

          {/* Right panel — council experts */}
          {showRightPanel && (
            <div className="w-80 flex-shrink-0 border-l border-[#e5e5e5] bg-white overflow-y-auto animate-slide-in-right dark:bg-[#0a0a0a] dark:border-[#262626]">
              {hasCouncilActivity ? (
                <LiveCouncilPanel
                  memberOrder={memberOrder}
                  memberStates={memberStates}
                  synthesisContent={synthesisContent}
                  synthesisStatus={synthesisStatus}
                  phase={councilPhase}
                />
              ) : (
                <div className="p-6 flex flex-col items-center justify-center h-full text-center max-w-xs mx-auto">
                  <div className="mb-3 h-8 w-8 border border-[#d4d4d4] bg-[#ffffff] rounded dark:bg-[#0a0a0a] dark:border-[#404040]" />
                  <div className="text-xs font-mono text-[#a3a3a3] uppercase tracking-wider mb-1 dark:text-[#737373]">
                    Convene Mode
                  </div>
                  <div className="text-sm font-semibold text-[#0a0a0a] mb-2 leading-snug dark:text-[#fafafa]">
                    Ask 3+ models in parallel, get a synthesized answer
                  </div>
                  <div className="text-[11px] text-[#525252] leading-relaxed mb-3 dark:text-[#a3a3a3]">
                    Best for decisions that need a second opinion: code review,
                    architecture choices, debugging hard bugs, or anything where
                    "what would another expert say?" matters.
                  </div>
                  <div className="text-[10px] font-mono text-[#737373] leading-relaxed dark:text-[#a3a3a3]">
                    Type your question below and your selected providers will
                    deliberate in this panel.
                  </div>
                </div>
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
