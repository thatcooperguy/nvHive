'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import CouncilPanel from '@/components/CouncilPanel';
import AgentBadge from '@/components/AgentBadge';
import {
  streamCouncil,
  getAgentPresets,
  analyzeAgents,
  getProviders,
} from '@/lib/api';
import type {
  AgentPreset,
  AgentPersona,
  MemberStreamState,
  WsCouncilStart,
} from '@/lib/types';

// ─── Types ────────────────────────────────────────────────────────────────────

type SessionPhase =
  | 'idle'        // nothing running
  | 'connecting'  // WS opened, council_start not yet received
  | 'streaming'   // members streaming
  | 'synthesis'   // all members done, synthesis in progress
  | 'done'        // council_complete received
  | 'error';      // fatal error

// ─── Page ─────────────────────────────────────────────────────────────────────

export default function CouncilPage() {
  // ── Config state ────────────────────────────────────────────────────────────
  const [prompt, setPrompt] = useState('');
  const [autoAgents, setAutoAgents] = useState(false);
  const [preset, setPreset] = useState('');
  const [numAgents, setNumAgents] = useState(3);
  const [strategy, setStrategy] = useState('');
  const [synthesize, setSynthesize] = useState(true);
  const [temperature, setTemperature] = useState(0.7);
  const [maxTokens, setMaxTokens] = useState(1024);
  const [systemPrompt, setSystemPrompt] = useState('');

  const [presets, setPresets] = useState<AgentPreset[]>([]);
  const [analyzedAgents, setAnalyzedAgents] = useState<AgentPersona[]>([]);
  const [analyzing, setAnalyzing] = useState(false);

  const [providers, setProviders] = useState<string[]>([]);
  const [customWeights, setCustomWeights] = useState<Record<string, number>>({});

  // ── Session state ────────────────────────────────────────────────────────────
  const [phase, setPhase] = useState<SessionPhase>('idle');
  const [error, setError] = useState<string | null>(null);

  // Member streaming
  const [memberOrder, setMemberOrder] = useState<string[]>([]);
  const [memberWeights, setMemberWeights] = useState<Record<string, number>>({});
  const [memberStates, setMemberStates] = useState<Record<string, MemberStreamState>>({});
  const [memberStartTimes, setMemberStartTimes] = useState<Record<string, number>>({});

  // Synthesis streaming
  const [synthesisStatus, setSynthesisStatus] = useState<'hidden' | 'streaming' | 'complete'>('hidden');
  const [synthesisContent, setSynthesisContent] = useState('');
  const [synthesisTokens, setSynthesisTokens] = useState(0);
  const [synthesisCost, setSynthesisCost] = useState('');

  // Final stats
  const [liveTotalCost, setLiveTotalCost] = useState('0');
  const [quorumMet, setQuorumMet] = useState<boolean | undefined>(undefined);
  const [totalLatencyMs, setTotalLatencyMs] = useState<number | undefined>(undefined);

  // Active WS ref so we can cancel
  const wsRef = useRef<WebSocket | null>(null);

  // ── Bootstrap ────────────────────────────────────────────────────────────────
  useEffect(() => {
    let mounted = true;
    Promise.all([getAgentPresets(), getProviders()]).then(([aData, pData]) => {
      if (!mounted) return;
      setPresets(aData.presets);
      setProviders(pData.providers.map(p => p.name));
    }).catch(() => {});
    return () => { mounted = false; };
  }, []);

  // Cleanup WS on unmount
  useEffect(() => {
    return () => {
      if (wsRef.current) {
        wsRef.current.close();
        wsRef.current = null;
      }
    };
  }, []);

  // ── Helpers ──────────────────────────────────────────────────────────────────

  const resetSession = useCallback(() => {
    setPhase('idle');
    setError(null);
    setMemberOrder([]);
    setMemberWeights({});
    setMemberStates({});
    setMemberStartTimes({});
    setSynthesisStatus('hidden');
    setSynthesisContent('');
    setSynthesisTokens(0);
    setSynthesisCost('');
    setLiveTotalCost('0');
    setQuorumMet(undefined);
    setTotalLatencyMs(undefined);
  }, []);

  // Running sum of completed member costs
  const completedCostRef = useRef(0);

  // ── Analyze ──────────────────────────────────────────────────────────────────
  const handleAnalyze = async () => {
    if (!prompt.trim()) return;
    setAnalyzing(true);
    try {
      const r = await analyzeAgents(prompt, numAgents, preset || undefined);
      setAnalyzedAgents(r.agents);
    } catch {
      // ignore
    } finally {
      setAnalyzing(false);
    }
  };

  // ── Run ──────────────────────────────────────────────────────────────────────
  const handleRun = (e: React.FormEvent) => {
    e.preventDefault();
    if (!prompt.trim() || phase === 'streaming' || phase === 'synthesis' || phase === 'connecting') return;

    // Cancel any ongoing session
    if (wsRef.current) {
      wsRef.current.close();
      wsRef.current = null;
    }

    resetSession();
    completedCostRef.current = 0;
    setPhase('connecting');

    const ws = streamCouncil(
      {
        prompt,
        auto_agents: autoAgents,
        preset: preset || undefined,
        strategy: strategy || undefined,
        synthesize,
        temperature,
        max_tokens: maxTokens,
        system_prompt: systemPrompt || undefined,
        weights: Object.keys(customWeights).length > 0 ? customWeights : undefined,
      },
      {
        onStart: (data: WsCouncilStart) => {
          const order = data.members.map(m =>
            m.persona ? `${m.provider}:${m.persona}` : m.provider
          );
          const weights: Record<string, number> = {};
          const states: Record<string, MemberStreamState> = {};

          data.members.forEach(m => {
            const label = m.persona ? `${m.provider}:${m.persona}` : m.provider;
            weights[label] = m.weight;
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
          setMemberWeights(weights);
          setMemberStates(states);
          setPhase('streaming');
        },

        onMemberStart: (member) => {
          setMemberStartTimes(prev => ({ ...prev, [member]: Date.now() }));
          setMemberStates(prev => ({
            ...prev,
            [member]: prev[member]
              ? { ...prev[member], status: 'streaming' }
              : {
                  label: member,
                  provider: member.split(':')[0],
                  persona: member.includes(':') ? member.split(':').slice(1).join(':') : '',
                  status: 'streaming',
                  accumulated: '',
                  tokens: 0,
                  cost: '0',
                  latency_ms: 0,
                  elapsedMs: 0,
                },
          }));
        },

        onMemberChunk: (member, _delta, accumulated) => {
          setMemberStates(prev => ({
            ...prev,
            [member]: prev[member]
              ? { ...prev[member], accumulated, status: 'streaming' }
              : {
                  label: member,
                  provider: member.split(':')[0],
                  persona: member.includes(':') ? member.split(':').slice(1).join(':') : '',
                  status: 'streaming',
                  accumulated,
                  tokens: 0,
                  cost: '0',
                  latency_ms: 0,
                  elapsedMs: 0,
                },
          }));
        },

        onMemberComplete: (member, content, tokens, cost, latency) => {
          setMemberStates(prev => ({
            ...prev,
            [member]: prev[member]
              ? { ...prev[member], status: 'complete', accumulated: content, tokens, cost, latency_ms: latency }
              : {
                  label: member,
                  provider: member.split(':')[0],
                  persona: member.includes(':') ? member.split(':').slice(1).join(':') : '',
                  status: 'complete',
                  accumulated: content,
                  tokens,
                  cost,
                  latency_ms: latency,
                  elapsedMs: latency,
                },
          }));
          // Update live total cost
          const c = parseFloat(cost) || 0;
          completedCostRef.current += c;
          setLiveTotalCost(completedCostRef.current.toFixed(6));
        },

        onMemberFailed: (member, err) => {
          setMemberStates(prev => ({
            ...prev,
            [member]: prev[member]
              ? { ...prev[member], status: 'failed', error: err }
              : {
                  label: member,
                  provider: member.split(':')[0],
                  persona: member.includes(':') ? member.split(':').slice(1).join(':') : '',
                  status: 'failed',
                  accumulated: '',
                  tokens: 0,
                  cost: '0',
                  latency_ms: 0,
                  elapsedMs: 0,
                  error: err,
                },
          }));
        },

        onSynthesisStart: () => {
          setPhase('synthesis');
          setSynthesisStatus('streaming');
        },

        onSynthesisChunk: (_delta, accumulated) => {
          setSynthesisContent(accumulated);
        },

        onSynthesisComplete: (content, tokens, cost) => {
          setSynthesisContent(content);
          setSynthesisTokens(tokens);
          setSynthesisCost(cost);
          setSynthesisStatus('complete');
          // Add synthesis cost to total
          const c = parseFloat(cost) || 0;
          completedCostRef.current += c;
          setLiveTotalCost(completedCostRef.current.toFixed(6));
        },

        onComplete: (totalCost, totalLatency, qMet) => {
          setLiveTotalCost(totalCost);
          setTotalLatencyMs(totalLatency);
          setQuorumMet(qMet);
          setPhase('done');
          wsRef.current = null;
        },

        onError: (err) => {
          setError(err);
          setPhase('error');
          wsRef.current = null;
        },
      }
    );

    wsRef.current = ws;
  };

  const handleStop = () => {
    if (wsRef.current) {
      wsRef.current.close();
      wsRef.current = null;
    }
    setPhase('idle');
  };

  // ── Derived ──────────────────────────────────────────────────────────────────
  const isRunning = phase === 'connecting' || phase === 'streaming' || phase === 'synthesis';

  const failedMembers: Record<string, string> = {};
  Object.values(memberStates).forEach(s => {
    if (s.status === 'failed') failedMembers[s.label] = s.error ?? 'Failed';
  });

  // ── Render ───────────────────────────────────────────────────────────────────
  return (
    <div className="p-6 space-y-6 max-w-7xl mx-auto">
      {/* Header */}
      <div className="nvidia-corner relative border border-[#333333] bg-[#111111] p-5 overflow-hidden">
        <div className="absolute top-0 left-0 right-0 h-px bg-gradient-to-r from-transparent via-[#76B900] to-transparent" />
        <div className="relative">
          <div className="text-[10px] font-mono text-[#76B900] tracking-[0.2em] uppercase mb-0.5">Multi-LLM Orchestration</div>
          <h1 className="text-2xl font-bold text-white">Convene Mode</h1>
          <p className="text-xs font-mono text-[#555555] mt-1">
            Orchestrate multiple AI advisors with real-time streaming and weighted synthesis
          </p>
        </div>
      </div>

      <div className="grid grid-cols-1 xl:grid-cols-4 gap-6">
        {/* Config sidebar */}
        <div className="xl:col-span-1 space-y-4">
          <form onSubmit={handleRun} className="space-y-4">

            {/* Prompt */}
            <div className="card p-4 space-y-3 nvidia-corner">
              <div className="section-label">Prompt</div>
              <textarea
                value={prompt}
                onChange={e => setPrompt(e.target.value)}
                placeholder="Enter your prompt for the hive..."
                rows={5}
                className="input-base w-full px-3 py-2.5 text-sm resize-none font-mono"
                disabled={isRunning}
              />

              <div className="flex gap-2">
                <button
                  type="submit"
                  disabled={!prompt.trim() || isRunning}
                  className="btn-primary flex-1 py-2.5 text-sm font-mono tracking-widest flex items-center justify-center gap-2"
                >
                  {isRunning ? (
                    <>
                      <svg className="animate-spin w-4 h-4" viewBox="0 0 24 24" fill="none">
                        <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
                        <path className="opacity-75" fill="currentColor"
                          d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z" />
                      </svg>
                      {phase === 'connecting' ? 'CONNECTING…' : phase === 'synthesis' ? 'SYNTHESIZING…' : 'DELIBERATING…'}
                    </>
                  ) : (
                    <>
                      <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                        <path strokeLinecap="round" strokeLinejoin="round"
                          d="M18 18.72a9.094 9.094 0 003.741-.479 3 3 0 00-4.682-2.72m.94 3.198l.001.031c0 .225-.012.447-.037.666A11.944 11.944 0 0112 21c-2.17 0-4.207-.576-5.963-1.584A6.062 6.062 0 016 18.719m12 0a5.971 5.971 0 00-.941-3.197m0 0A5.995 5.995 0 0012 12.75a5.995 5.995 0 00-5.058 2.772m0 0a3 3 0 00-4.681 2.72 8.986 8.986 0 003.74.477m.94-3.197a5.971 5.971 0 00-.94 3.197M15 6.75a3 3 0 11-6 0 3 3 0 016 0zm6 3a2.25 2.25 0 11-4.5 0 2.25 2.25 0 014.5 0zm-13.5 0a2.25 2.25 0 11-4.5 0 2.25 2.25 0 014.5 0z" />
                      </svg>
                      CONVENE COUNCIL
                    </>
                  )}
                </button>

                {isRunning && (
                  <button
                    type="button"
                    onClick={handleStop}
                    className="btn-secondary px-3 py-2.5 text-sm font-mono"
                    title="Stop"
                  >
                    <svg className="w-4 h-4" fill="currentColor" viewBox="0 0 24 24">
                      <rect x="6" y="6" width="12" height="12" />
                    </svg>
                  </button>
                )}
              </div>
            </div>

            {/* Agent settings */}
            <div className="card p-4 space-y-4">
              <div className="section-label">Agent Settings</div>

              <div className="flex items-center justify-between">
                <div>
                  <div className="text-xs font-mono text-[#999999]">Auto Agents</div>
                  <div className="text-[10px] font-mono text-[#555555]">AI-generated personas</div>
                </div>
                <button
                  type="button"
                  onClick={() => setAutoAgents(!autoAgents)}
                  className={`relative w-10 h-5 transition-colors ${autoAgents ? 'bg-[#76B900]' : 'bg-[#222222] border border-[#333333]'}`}
                >
                  <span className={`absolute top-0.5 w-4 h-4 bg-white shadow transition-transform ${autoAgents ? 'translate-x-5' : 'translate-x-0.5'}`} />
                </button>
              </div>

              {presets.length > 0 && (
                <div>
                  <label className="block text-[10px] font-mono text-[#666666] mb-1.5 uppercase tracking-wider">Cabinet</label>
                  <select
                    value={preset}
                    onChange={e => setPreset(e.target.value)}
                    className="input-base w-full px-3 py-2 text-sm"
                  >
                    <option value="">None</option>
                    {presets.map(p => (
                      <option key={p.name} value={p.name}>{p.name}</option>
                    ))}
                  </select>
                  {preset && presets.find(p => p.name === preset) && (
                    <div className="mt-1.5 text-[10px] font-mono text-[#555555]">
                      {presets.find(p => p.name === preset)?.description}
                    </div>
                  )}
                </div>
              )}

              <div>
                <div className="flex items-center justify-between mb-1.5">
                  <label className="text-[10px] font-mono text-[#666666] uppercase tracking-wider">Num Agents</label>
                  <span className="text-xs font-mono text-[#76B900]">{numAgents}</span>
                </div>
                <input type="range" min="2" max="8" step="1" value={numAgents}
                  onChange={e => setNumAgents(parseInt(e.target.value))}
                  className="w-full"
                />
              </div>

              <button
                type="button"
                onClick={handleAnalyze}
                disabled={analyzing || !prompt.trim()}
                className="btn-secondary w-full py-2 text-[10px] font-mono uppercase tracking-wider flex items-center justify-center gap-2"
              >
                {analyzing ? (
                  <><svg className="animate-spin w-3 h-3" viewBox="0 0 24 24" fill="none"><circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" /><path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" /></svg>Analyzing...</>
                ) : 'Preview Agents'}
              </button>

              {analyzedAgents.length > 0 && (
                <div className="space-y-2">
                  {analyzedAgents.map((a, i) => (
                    <AgentBadge key={i} agent={a} index={i} />
                  ))}
                </div>
              )}
            </div>

            {/* Convene options */}
            <div className="card p-4 space-y-4">
              <div className="section-label">Convene Options</div>

              <div>
                <label className="block text-[10px] font-mono text-[#666666] mb-1.5 uppercase tracking-wider">Strategy</label>
                <select
                  value={strategy}
                  onChange={e => setStrategy(e.target.value)}
                  className="input-base w-full px-3 py-2 text-sm"
                >
                  <option value="">Default</option>
                  <option value="weighted_consensus">Weighted Consensus</option>
                  <option value="majority_vote">Majority Vote</option>
                  <option value="best_of">Best Of</option>
                </select>
              </div>

              <div className="flex items-center justify-between">
                <div>
                  <div className="text-xs font-mono text-[#999999]">Synthesize</div>
                  <div className="text-[10px] font-mono text-[#555555]">Combine responses</div>
                </div>
                <button
                  type="button"
                  onClick={() => setSynthesize(!synthesize)}
                  className={`relative w-10 h-5 transition-colors ${synthesize ? 'bg-[#76B900]' : 'bg-[#222222] border border-[#333333]'}`}
                >
                  <span className={`absolute top-0.5 w-4 h-4 bg-white shadow transition-transform ${synthesize ? 'translate-x-5' : 'translate-x-0.5'}`} />
                </button>
              </div>

              <div>
                <div className="flex items-center justify-between mb-1.5">
                  <label className="text-[10px] font-mono text-[#666666] uppercase tracking-wider">Temperature</label>
                  <span className="text-xs font-mono text-[#76B900]">{temperature.toFixed(2)}</span>
                </div>
                <input
                  type="range" min="0" max="2" step="0.05" value={temperature}
                  onChange={e => setTemperature(parseFloat(e.target.value))}
                  className="w-full"
                />
              </div>

              <div>
                <label className="block text-[10px] font-mono text-[#666666] mb-1.5 uppercase tracking-wider">Max Tokens</label>
                <input
                  type="number" min="1" max="32000" value={maxTokens}
                  onChange={e => setMaxTokens(parseInt(e.target.value) || 1024)}
                  className="input-base w-full px-3 py-2 text-sm font-mono"
                />
              </div>
            </div>

            {/* Provider weights */}
            {providers.length > 0 && (
              <div className="card p-4 space-y-3">
                <div className="section-label">Advisor Weights</div>
                <div className="text-[10px] font-mono text-[#444444] mb-2">Leave at 0 to use defaults</div>
                {providers.slice(0, 6).map(p => (
                  <div key={p}>
                    <div className="flex items-center justify-between mb-1">
                      <span className="text-[10px] font-mono text-[#666666] uppercase">{p}</span>
                      <span className="text-xs font-mono text-[#76B900]">
                        {(customWeights[p] ?? 0).toFixed(2)}
                      </span>
                    </div>
                    <input
                      type="range" min="0" max="2" step="0.05"
                      value={customWeights[p] ?? 0}
                      onChange={e => {
                        const v = parseFloat(e.target.value);
                        setCustomWeights(prev => v === 0
                          ? Object.fromEntries(Object.entries(prev).filter(([k]) => k !== p))
                          : { ...prev, [p]: v }
                        );
                      }}
                      className="w-full"
                    />
                  </div>
                ))}
              </div>
            )}

            {/* System prompt */}
            <div className="card p-4">
              <label className="block section-label mb-2">System Prompt</label>
              <textarea
                value={systemPrompt}
                onChange={e => setSystemPrompt(e.target.value)}
                placeholder="Optional system context..."
                rows={3}
                className="input-base w-full px-3 py-2 text-sm resize-none font-mono"
              />
            </div>
          </form>
        </div>

        {/* Results panel */}
        <div className="xl:col-span-3">
          {phase === 'error' ? (
            <div className="card p-6 border-[#ef4444]/30">
              <div className="flex items-center gap-3 mb-4">
                <div className="w-8 h-8 bg-[#ef4444]/10 border border-[#ef4444]/30 flex items-center justify-center">
                  <svg className="w-4 h-4 text-[#ef4444]" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                    <path strokeLinecap="round" strokeLinejoin="round"
                      d="M12 9v3.75m-9.303 3.376c-.866 1.5.217 3.374 1.948 3.374h14.71c1.73 0 2.813-1.874 1.948-3.374L13.949 3.378c-.866-1.5-3.032-1.5-3.898 0L2.697 16.126zM12 15.75h.007v.008H12v-.008z" />
                  </svg>
                </div>
                <div>
                  <div className="font-mono font-bold text-[#ef4444] text-sm">HIVE ERROR</div>
                  <div className="text-xs text-[#999999] font-mono mt-1">{error}</div>
                </div>
              </div>
            </div>

          ) : phase === 'connecting' ? (
            <div className="card p-8 flex flex-col items-center justify-center gap-4 min-h-[300px] border-[#76B900]/20">
              <div className="relative w-16 h-16">
                <div className="absolute inset-0 border border-[#76B900]/30 animate-spin"
                  style={{ animationDuration: '3s', clipPath: 'polygon(50% 0%, 100% 50%, 50% 100%, 0% 50%)' }} />
                <div className="absolute inset-3 border border-[#76B900]/60 animate-spin"
                  style={{ animationDirection: 'reverse', animationDuration: '2s', clipPath: 'polygon(50% 0%, 100% 50%, 50% 100%, 0% 50%)' }} />
                <div className="absolute inset-6 bg-[#76B900] animate-pulse"
                  style={{ clipPath: 'polygon(50% 0%, 100% 50%, 50% 100%, 0% 50%)' }} />
              </div>
              <div className="text-[#76B900] text-sm font-mono uppercase tracking-widest">Connecting…</div>
              <div className="text-xs font-mono text-[#444444]">Opening WebSocket connection to hive…</div>
            </div>

          ) : (phase === 'streaming' || phase === 'synthesis' || phase === 'done') && memberOrder.length > 0 ? (
            <div className="card p-5 border-[#76B900]/20">
              <div className="section-label mb-4 flex items-center gap-2 text-[#76B900]">
                <span>◈</span>
                <span>Convene</span>
                {phase === 'streaming' && (
                  <span className="text-[10px] font-mono text-[#76B900] animate-pulse uppercase tracking-wider ml-1">
                    Live
                  </span>
                )}
                {phase === 'synthesis' && (
                  <span className="text-[10px] font-mono text-[#3b82f6] animate-pulse uppercase tracking-wider ml-1">
                    Synthesizing
                  </span>
                )}
                {phase === 'done' && (
                  <span className="text-[10px] font-mono text-[#22c55e] uppercase tracking-wider ml-1">
                    Complete
                  </span>
                )}
              </div>
              <CouncilPanel
                mode="streaming"
                memberStates={memberStates}
                memberStartTimes={memberStartTimes}
                memberOrder={memberOrder}
                memberWeights={memberWeights}
                synthesisStatus={synthesisStatus}
                synthesisContent={synthesisContent}
                synthesisTokens={synthesisTokens}
                synthesisCost={synthesisCost}
                liveTotalCost={liveTotalCost}
                quorumMet={quorumMet}
                totalLatencyMs={totalLatencyMs}
                strategy={strategy || 'weighted_consensus'}
                failedMembers={failedMembers}
              />
            </div>

          ) : (
            <div className="card p-10 flex flex-col items-center justify-center gap-4 min-h-[300px] text-center nvidia-corner">
              <div className="absolute top-0 left-0 right-0 h-px bg-gradient-to-r from-transparent via-[#76B900]/30 to-transparent" />
              <div
                className="w-20 h-20 border border-[#76B900]/30 bg-[#76B900]/5 flex items-center justify-center"
                style={{ clipPath: 'polygon(50% 0%, 100% 25%, 100% 75%, 50% 100%, 0% 75%, 0% 25%)' }}
              >
                <svg className="w-10 h-10 text-[#76B900]" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1}>
                  <path strokeLinecap="round" strokeLinejoin="round"
                    d="M18 18.72a9.094 9.094 0 003.741-.479 3 3 0 00-4.682-2.72m.94 3.198l.001.031c0 .225-.012.447-.037.666A11.944 11.944 0 0112 21c-2.17 0-4.207-.576-5.963-1.584A6.062 6.062 0 016 18.719m12 0a5.971 5.971 0 00-.941-3.197m0 0A5.995 5.995 0 0012 12.75a5.995 5.995 0 00-5.058 2.772m0 0a3 3 0 00-4.681 2.72 8.986 8.986 0 003.74.477m.94-3.197a5.971 5.971 0 00-.94 3.197M15 6.75a3 3 0 11-6 0 3 3 0 016 0zm6 3a2.25 2.25 0 11-4.5 0 2.25 2.25 0 014.5 0zm-13.5 0a2.25 2.25 0 11-4.5 0 2.25 2.25 0 014.5 0z" />
                </svg>
              </div>
              <div>
                <div className="text-lg font-bold text-white mb-2 font-mono">THE HIVE AWAITS</div>
                <div className="text-xs font-mono text-[#555555] max-w-sm">
                  Configure settings and enter a prompt to convene the hive of AI advisors.
                  Each member streams in real-time — a synthesis is generated live.
                </div>
              </div>
              <div className="flex flex-wrap justify-center gap-2 mt-2">
                {['Live Streaming', 'Agent Personas', 'Real-time Synthesis', 'Multi-Advisor'].map(tag => (
                  <span key={tag} className="text-[10px] font-mono px-2 py-1 bg-[#76B900]/10 text-[#76B900] border border-[#76B900]/20">
                    {tag.toUpperCase()}
                  </span>
                ))}
              </div>
            </div>
          )}
        </div>
      </div>

      {/* Blink animation injected once */}
      <style jsx global>{`
        @keyframes blink {
          0%, 100% { opacity: 1; }
          50% { opacity: 0; }
        }
      `}</style>
    </div>
  );
}
