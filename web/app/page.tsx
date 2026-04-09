'use client';

import { useEffect, useRef, useState, useCallback } from 'react';
import Sidebar from '@/components/Sidebar';
import ChatMessage, { CouncilExpertPanel, exportConversationMarkdown } from '@/components/ChatMessage';
import ChatInput from '@/components/ChatInput';
import { useUIShell } from '@/components/UIShellProvider';
import {
  queryStream,
  runCouncil,
  compare,
  getModels,
  getGPUInfo,
  getBudgetStatus,
  getConversations,
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

// ─── GPU status pill ──────────────────────────────────────────────────────────

function GPUPill() {
  const [info, setInfo] = useState<{ name: string; pct: number } | null>(null);

  useEffect(() => {
    getGPUInfo()
      .then(g => {
        if (g.gpus.length > 0) {
          const gpu = g.gpus[0];
          setInfo({ name: gpu.name, pct: gpu.utilization_pct });
        }
      })
      .catch(() => {});
  }, []);

  if (!info) return null;

  const shortName = info.name.replace('NVIDIA GeForce ', '').replace('NVIDIA ', '');
  const color = info.pct > 90 ? '#ef4444' : info.pct > 70 ? '#f59e0b' : '#76B900';

  return (
    <div className="flex items-center gap-1.5 px-2 py-0.5 bg-[#111111] border border-[#222222] text-[10px] font-mono">
      <svg className="w-3 h-3" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
        <path strokeLinecap="round" strokeLinejoin="round"
          d="M9 3H5a2 2 0 00-2 2v4m6-6h10a2 2 0 012 2v4M9 3v18m0 0h10a2 2 0 002-2V9M9 21H5a2 2 0 01-2-2V9m0 0h18" />
      </svg>
      <span className="text-[#555555]">{shortName}</span>
      <span style={{ color }}>{info.pct}%</span>
    </div>
  );
}

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
    <div className="flex items-center gap-1 px-2 py-0.5 bg-[#111111] border border-[#222222] text-[10px] font-mono text-[#f59e0b]">
      <span>💰</span>
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
  const COLORS = ['#3b82f6', '#a855f7', '#22c55e', '#f59e0b', '#06b6d4', '#f97316'];

  return (
    <div className="p-4 space-y-3">
      <div className="flex items-center gap-2 mb-3">
        <span className="text-[#76B900] text-xs">◈</span>
        <span className="text-[10px] font-mono text-[#76B900] uppercase tracking-wider">Hive</span>
        {(phase === 'streaming' || phase === 'connecting') && (
          <span className="text-[9px] font-mono text-[#76B900] animate-pulse ml-1">LIVE</span>
        )}
        {phase === 'synthesis' && (
          <span className="text-[9px] font-mono text-[#3b82f6] animate-pulse ml-1">SYNTHESIZING</span>
        )}
        {phase === 'done' && (
          <span className="text-[9px] font-mono text-[#22c55e] ml-1">COMPLETE</span>
        )}
      </div>

      {memberOrder.length === 0 && (
        <div className="text-center py-8 text-[#333333] text-xs font-mono">
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
            className="border p-3 bg-[#0d0d0d] transition-all"
            style={{
              borderColor: state.status === 'streaming' ? color : `${color}30`,
            }}
          >
            <div className="flex items-center gap-2 mb-1.5">
              <span
                className={`w-1.5 h-1.5 rounded-full flex-shrink-0 ${
                  state.status === 'streaming' ? 'bg-[#76B900] animate-pulse' :
                  state.status === 'complete' ? 'bg-[#22c55e]' :
                  state.status === 'failed' ? 'bg-[#ef4444]' :
                  'bg-[#333333]'
                }`}
              />
              <span className="text-xs font-mono font-bold" style={{ color }}>
                {state.provider.toUpperCase()}
              </span>
              {state.persona && (
                <span className="text-[9px] font-mono text-[#555555]">{state.persona}</span>
              )}
              {state.status === 'complete' && (
                <span className="ml-auto text-[9px] font-mono text-[#444444]">
                  {state.tokens > 0 ? `${state.tokens}t` : ''}
                </span>
              )}
            </div>
            <div className="text-[11px] text-[#888888] font-mono whitespace-pre-wrap leading-relaxed max-h-28 overflow-y-auto">
              {state.status === 'waiting' && (
                <span className="text-[#333333] italic">Waiting...</span>
              )}
              {state.status === 'failed' && (
                <span className="text-[#ef4444]">{state.error || 'Failed'}</span>
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
        <div className="border border-[#3b82f6]/30 p-3 bg-[#0a0d1a]">
          <div className="text-[9px] font-mono text-[#3b82f6] uppercase tracking-wider mb-2 flex items-center gap-1.5">
            <span>◈</span>
            <span>Synthesis</span>
            {synthesisStatus === 'streaming' && (
              <span className="animate-pulse">Generating...</span>
            )}
          </div>
          <div className="text-[11px] text-[#cccccc] font-mono whitespace-pre-wrap leading-relaxed max-h-40 overflow-y-auto">
            {synthesisContent}
            {synthesisStatus === 'streaming' && (
              <span
                className="inline-block w-[2px] h-[1em] bg-[#3b82f6] align-middle ml-0.5"
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

function EmptyState({ onPrompt }: { onPrompt: (p: string) => void }) {
  const suggestions = [
    'Debug my segfault in C — here\'s the backtrace...',
    'Explain how transformers work in simple terms',
    'Write a Python script to batch rename files',
    'Compare weighted voting vs majority vote for ensemble models',
  ];

  return (
    <div className="flex flex-col items-center justify-center flex-1 px-6 py-12 text-center">
      {/* Logo hex */}
      <div className="relative mb-6">
        <div
          className="w-16 h-16 bg-[#76B900]/5 border border-[#76B900]/20 flex items-center justify-center"
          style={{ clipPath: 'polygon(50% 0%, 100% 25%, 100% 75%, 50% 100%, 0% 75%, 0% 25%)' }}
        >
          <svg className="w-8 h-8 text-[#76B900]/60" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1}>
            <path strokeLinecap="round" strokeLinejoin="round"
              d="M7.5 8.25h9m-9 3H12m-9.75 1.51c0 1.6 1.123 2.994 2.707 3.227 1.129.166 2.27.293 3.423.379.35.026.67.21.865.501L12 21l2.755-4.133a1.14 1.14 0 01.865-.501 48.172 48.172 0 003.423-.379c1.584-.233 2.707-1.626 2.707-3.228V6.741c0-1.602-1.123-2.995-2.707-3.228A48.394 48.394 0 0012 3c-2.392 0-4.744.175-7.043.513C3.373 3.746 2.25 5.14 2.25 6.741v6.018z" />
          </svg>
        </div>
      </div>

      <h2 className="text-lg font-bold text-white mb-1 tracking-tight">Hive AI</h2>
      <p className="text-sm text-[#555555] font-mono mb-8 max-w-sm">
        Your NVIDIA-powered multi-model AI. Ask anything — single model, hive of advisors, or side-by-side comparison.
      </p>

      <div className="grid grid-cols-1 sm:grid-cols-2 gap-2 w-full max-w-lg">
        {suggestions.map((s, i) => (
          <button
            key={i}
            onClick={() => onPrompt(s)}
            className="text-left px-4 py-3 border border-[#222222] bg-[#111111] hover:border-[#76B900]/30 hover:bg-[#141414] transition-all group"
          >
            <div className="text-xs text-[#666666] group-hover:text-[#999999] leading-snug font-mono">
              {s}
            </div>
          </button>
        ))}
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
  const [models, setModels] = useState<Array<{ model_id: string; provider: string; display_name: string; is_local?: boolean; cost_tier?: 'free' | 'low' | 'high' }>>([]);
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
    getModels()
      .then(data => {
        const mapped = data.models.map(m => ({
          model_id: m.model_id,
          provider: m.provider,
          display_name: m.display_name,
          is_local: m.provider === 'ollama' || m.provider === 'local',
          cost_tier: m.input_cost_per_1m_tokens
            ? (parseFloat(m.input_cost_per_1m_tokens) > 1 ? 'high' as const : 'low' as const)
            : 'free' as const,
        }));
        setModels(mapped);
      })
      .catch(() => {});
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
    setMemberStates({});
    setSynthesisContent('');
    setSynthesisStatus('hidden');
  }, [updateStoredState]);

  const handleSelectConversation = useCallback((id: string) => {
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
    setMemberStates({});
    setSynthesisContent('');
    setSynthesisStatus('hidden');
    setRightPanelOpen(false);
  }, [storedState.messages]);

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

  const resetCouncilState = useCallback(() => {
    setCouncilPhase('idle');
    setMemberOrder([]);
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

  const handleSubmit = useCallback(async () => {
    const prompt = inputValue.trim();
    if (!prompt || streaming) return;

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
            data.members.forEach(m => {
              const label = m.persona ? `${m.provider}:${m.persona}` : m.provider;
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
            setMemberStates(states);
            setCouncilPhase('streaming');
          },
          onMemberStart: (member) => {
            setMemberStates(prev => ({
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
            setMemberStates(prev => ({
              ...prev,
              [member]: { ...(prev[member] ?? { label: member, provider: member.split(':')[0], persona: '', tokens: 0, cost: '0', latency_ms: 0, elapsedMs: 0 }), accumulated, status: 'streaming' },
            }));
          },
          onMemberComplete: (member, content, tokens, cost, latency) => {
            setMemberStates(prev => ({
              ...prev,
              [member]: { ...(prev[member] ?? { label: member, provider: member.split(':')[0], persona: '', accumulated: '', elapsedMs: 0 }), status: 'complete', accumulated: content, tokens, cost, latency_ms: latency },
            }));
            completedCostRef.current += parseFloat(cost) || 0;
          },
          onMemberFailed: (member, err) => {
            setMemberStates(prev => ({
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
            const memberStatesSnapshot = {} as Record<string, { content: string; provider: string; model: string; tokens: number; cost: string; latency_ms?: number }>;
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
    <div className="flex h-screen overflow-hidden bg-[#0a0a0a]">
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
          conversations={allConversations}
          activeConversationId={activeConvId}
          onNewChat={() => { handleNewChat(); setMobileSidebarOpen(false); }}
          onSelectConversation={(id) => { handleSelectConversation(id); setMobileSidebarOpen(false); }}
          onRenameConversation={handleRenameConversation}
          onDeleteConversation={handleDeleteConversation}
          onPinConversation={handlePinConversation}
        />
      </div>

      {/* Main area */}
      <div className="flex flex-col flex-1 min-w-0 overflow-hidden">
        {/* Top bar */}
        <div className="flex items-center px-3 h-10 border-b border-[#1a1a1a] bg-[#0d0d0d] flex-shrink-0 gap-2">
          {/* Hamburger — mobile only */}
          <button
            className="md:hidden flex-shrink-0 w-8 h-8 flex items-center justify-center text-[#555555] hover:text-[#76B900] transition-colors"
            onClick={() => setMobileSidebarOpen(true)}
            title="Open sidebar"
          >
            <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
              <path strokeLinecap="round" strokeLinejoin="round" d="M4 6h16M4 12h16M4 18h16" />
            </svg>
          </button>

          <div className="flex items-center gap-2 flex-1 min-w-0">
            {activeConvId && (
              <span className="text-xs font-mono text-[#555555] truncate">
                {allConversations.find(c => c.id === activeConvId)?.title ?? 'Chat'}
              </span>
            )}
          </div>
          <div className="flex items-center gap-2">
            <GPUPill />
            <BudgetPill />
            {/* Share button */}
            {messages.length > 0 && (
              <button
                onClick={handleShare}
                className="text-[10px] font-mono px-2 py-0.5 border border-[#333333] text-[#555555] hover:border-[#76B900]/30 hover:text-[#76B900] transition-colors flex items-center gap-1"
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
                    : 'border-[#333333] text-[#555555] hover:border-[#76B900]/30 hover:text-[#76B900]'
                }`}
                title="Toggle hive panel"
              >
                ◈ Panel
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
                    <div className="mx-4 mt-2 border border-[#76B900]/20 bg-[#0d0d0d]">
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
            <div className="w-80 flex-shrink-0 border-l border-[#1a1a1a] bg-[#0d0d0d] overflow-y-auto animate-slide-in-right">
              {hasCouncilActivity ? (
                <LiveCouncilPanel
                  memberOrder={memberOrder}
                  memberStates={memberStates}
                  synthesisContent={synthesisContent}
                  synthesisStatus={synthesisStatus}
                  phase={councilPhase}
                />
              ) : (
                <div className="p-6 flex flex-col items-center justify-center h-full text-center">
                  <div className="text-3xl text-[#333333] mb-3">◈</div>
                  <div className="text-xs font-mono text-[#444444] uppercase tracking-wider mb-1">
                    Convene Mode
                  </div>
                  <div className="text-[10px] font-mono text-[#333333] leading-relaxed">
                    Advisor responses will stream here as the hive deliberates
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
