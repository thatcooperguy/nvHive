'use client';

import { useEffect, useRef, useState } from 'react';
import type {
  CouncilResult,
  CompletionResponse,
  MemberStreamState,
  MemberStreamStatus,
} from '@/lib/types';

// ─── Colour palette ───────────────────────────────────────────────────────────

const MEMBER_COLORS = [
  { border: '#3b82f6', bg: '#3b82f615', text: '#3b82f6' },
  { border: '#a855f7', bg: '#a855f715', text: '#a855f7' },
  { border: '#22c55e', bg: '#22c55e15', text: '#22c55e' },
  { border: '#f59e0b', bg: '#f59e0b15', text: '#f59e0b' },
  { border: '#06b6d4', bg: '#06b6d415', text: '#06b6d4' },
  { border: '#f97316', bg: '#f9731615', text: '#f97316' },
];

// ─── Helpers ──────────────────────────────────────────────────────────────────

function formatUSD(val: string | number | null | undefined): string {
  if (val === null || val === undefined || val === '') return '—';
  const n = typeof val === 'number' ? val : parseFloat(val);
  if (isNaN(n)) return '—';
  return n < 0.001 ? `$${n.toFixed(5)}` : `$${n.toFixed(4)}`;
}

function formatMs(ms: number): string {
  if (ms < 1000) return `${ms}ms`;
  return `${(ms / 1000).toFixed(1)}s`;
}

// ─── Status badge ─────────────────────────────────────────────────────────────

function StatusDot({ status }: { status: MemberStreamStatus }) {
  if (status === 'waiting') {
    return (
      <span className="inline-block w-2 h-2 rounded-full bg-[#444444] border border-[#555555]" title="Waiting" />
    );
  }
  if (status === 'streaming') {
    return (
      <span
        className="inline-block w-2 h-2 rounded-full bg-[#76B900] animate-pulse"
        title="Streaming"
      />
    );
  }
  if (status === 'complete') {
    return (
      <span className="inline-block w-2 h-2 rounded-full bg-[#22c55e]" title="Complete" />
    );
  }
  // failed
  return (
    <span className="inline-block w-2 h-2 rounded-full bg-[#ef4444]" title="Failed" />
  );
}

// ─── Blinking cursor ──────────────────────────────────────────────────────────

function StreamCursor() {
  return (
    <span
      className="inline-block w-[2px] h-[1em] bg-current align-middle ml-0.5"
      style={{ animation: 'blink 1s step-end infinite' }}
    />
  );
}

// ─── Live elapsed timer ───────────────────────────────────────────────────────

function ElapsedTimer({ startedAt }: { startedAt: number }) {
  const [elapsed, setElapsed] = useState(0);

  useEffect(() => {
    const id = setInterval(() => {
      setElapsed(Date.now() - startedAt);
    }, 100);
    return () => clearInterval(id);
  }, [startedAt]);

  return <span>{formatMs(elapsed)}</span>;
}

// ─── Streaming member panel ───────────────────────────────────────────────────

interface StreamingMemberPanelProps {
  state: MemberStreamState;
  colorIndex: number;
  weight: number;
  startedAt?: number;
}

function StreamingMemberPanel({
  state,
  colorIndex,
  weight,
  startedAt,
}: StreamingMemberPanelProps) {
  const color = MEMBER_COLORS[colorIndex % MEMBER_COLORS.length];
  const contentRef = useRef<HTMLDivElement>(null);

  // Auto-scroll content area while streaming
  useEffect(() => {
    if (state.status === 'streaming' && contentRef.current) {
      contentRef.current.scrollTop = contentRef.current.scrollHeight;
    }
  }, [state.accumulated, state.status]);

  const borderColor =
    state.status === 'failed'
      ? '#ef444440'
      : state.status === 'complete'
      ? `${color.border}80`
      : state.status === 'streaming'
      ? color.border
      : `${color.border}30`;

  return (
    <div
      className="rounded-xl border p-4 flex flex-col gap-3 transition-all duration-300"
      style={{ borderColor, backgroundColor: '#0d0d1a' }}
    >
      {/* Header */}
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2 flex-wrap">
            <StatusDot status={state.status} />
            <span
              className="text-sm font-semibold truncate"
              style={{ color: state.status === 'failed' ? '#ef4444' : color.text }}
            >
              {state.provider}
            </span>
            {state.persona && (
              <span
                className="text-xs px-2 py-0.5 rounded-full border font-medium shrink-0"
                style={{
                  color: color.text,
                  borderColor: `${color.border}40`,
                  backgroundColor: color.bg,
                }}
              >
                {state.persona}
              </span>
            )}
          </div>
          <div className="text-[10px] text-[#475569] mt-0.5 font-mono uppercase tracking-wider">
            {state.status === 'waiting' && 'Waiting…'}
            {state.status === 'streaming' && 'Generating…'}
            {state.status === 'complete' && 'Complete'}
            {state.status === 'failed' && 'Failed'}
          </div>
        </div>

        {/* Weight + timer */}
        <div className="flex items-center gap-3 shrink-0">
          {state.status === 'streaming' && startedAt && (
            <div className="text-[10px] font-mono text-[#76B900] tabular-nums">
              <ElapsedTimer startedAt={startedAt} />
            </div>
          )}
          {state.status === 'complete' && (
            <div className="text-[10px] font-mono text-[#555555] tabular-nums">
              {formatMs(state.latency_ms)}
            </div>
          )}
          <div className="text-right">
            <div className="text-[10px] text-[#475569]">weight</div>
            <div className="text-sm font-mono font-bold" style={{ color: color.text }}>
              {weight.toFixed(2)}
            </div>
          </div>
        </div>
      </div>

      {/* Weight progress bar */}
      <div className="h-px bg-[#1a1a1a] overflow-hidden">
        <div
          className="h-full transition-all duration-500"
          style={{
            width: `${Math.min(100, weight * 100)}%`,
            backgroundColor: state.status === 'failed' ? '#ef4444' : color.border,
          }}
        />
      </div>

      {/* Content area */}
      <div
        ref={contentRef}
        className="bg-[#0a0a0a] rounded-lg p-3 min-h-[80px] max-h-[220px] overflow-y-auto"
      >
        {state.status === 'waiting' && (
          <div className="flex items-center gap-2 text-xs text-[#475569] font-mono italic">
            <span className="inline-block w-1.5 h-1.5 rounded-full bg-[#333333] animate-pulse" />
            Waiting for response…
          </div>
        )}
        {state.status === 'failed' && (
          <div className="text-xs text-[#ef4444] font-mono">
            {state.error || 'Response failed'}
          </div>
        )}
        {(state.status === 'streaming' || state.status === 'complete') &&
          state.accumulated && (
            <div className="text-sm text-[#e2e8f0] font-mono whitespace-pre-wrap leading-relaxed">
              {state.accumulated}
              {state.status === 'streaming' && <StreamCursor />}
            </div>
          )}
        {(state.status === 'streaming' || state.status === 'complete') &&
          !state.accumulated && (
            <div className="flex items-center gap-1.5 text-xs text-[#76B900] font-mono">
              <span className="inline-block w-1.5 h-1.5 rounded-full bg-[#76B900] animate-pulse" />
              Receiving tokens…
            </div>
          )}
      </div>

      {/* Metadata — shown when complete */}
      {state.status === 'complete' && (
        <div className="flex flex-wrap gap-2">
          {[
            { label: 'Tokens', value: state.tokens > 0 ? String(state.tokens) : '—' },
            { label: 'Cost', value: formatUSD(state.cost) },
            { label: 'Latency', value: formatMs(state.latency_ms) },
          ].map(({ label, value }) => (
            <div key={label} className="bg-[#0a0a0a] border border-[#2a2a3e] rounded-md px-2 py-1">
              <span className="text-xs text-[#475569]">{label}: </span>
              <span className="text-xs font-mono text-[#94a3b8]">{value}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

// ─── Static member panel (used when rendering a finished CouncilResult) ───────

function MemberPanel({
  label,
  response,
  colorIndex,
  weight,
  persona,
  failed,
}: {
  label: string;
  response: CompletionResponse | null;
  colorIndex: number;
  weight: number;
  persona: string | null;
  failed: boolean;
}) {
  const color = MEMBER_COLORS[colorIndex % MEMBER_COLORS.length];

  return (
    <div
      className="rounded-xl border p-4 flex flex-col gap-3"
      style={{ borderColor: failed ? '#ef444440' : `${color.border}40`, backgroundColor: '#0d0d1a' }}
    >
      <div className="flex items-start justify-between">
        <div>
          <div className="flex items-center gap-2">
            <span className="text-sm font-semibold" style={{ color: failed ? '#ef4444' : color.text }}>
              {label}
            </span>
            {persona && (
              <span
                className="text-xs px-2 py-0.5 rounded-full border font-medium"
                style={{ color: color.text, borderColor: `${color.border}30`, backgroundColor: color.bg }}
              >
                {persona}
              </span>
            )}
          </div>
          {response && (
            <div className="text-xs text-[#475569] mt-0.5 font-mono">{response.model}</div>
          )}
        </div>
        <div className="flex items-center gap-2">
          <div className="text-right">
            <div className="text-xs text-[#475569]">weight</div>
            <div className="text-sm font-mono font-bold" style={{ color: color.text }}>
              {weight.toFixed(2)}
            </div>
          </div>
          <div
            className="w-8 h-8 rounded-full flex items-center justify-center text-xs font-bold border-2"
            style={{
              borderColor: failed ? '#ef4444' : color.border,
              color: failed ? '#ef4444' : color.text,
              backgroundColor: failed ? '#ef444410' : color.bg,
            }}
          >
            {failed ? '✗' : '✓'}
          </div>
        </div>
      </div>

      <div className="progress-bar">
        <div
          className="progress-fill"
          style={{ width: `${Math.min(100, weight * 100)}%`, backgroundColor: failed ? '#ef4444' : color.border }}
        />
      </div>

      <div className="bg-[#0a0a0a] rounded-lg p-3 min-h-[80px] max-h-[200px] overflow-y-auto">
        {failed ? (
          <div className="text-xs text-[#ef4444] font-mono italic">Response failed</div>
        ) : response ? (
          <div className="text-sm text-[#e2e8f0] font-mono whitespace-pre-wrap leading-relaxed">
            {response.content}
          </div>
        ) : (
          <div className="text-xs text-[#475569] italic">No response</div>
        )}
      </div>

      {response && !failed && (
        <div className="flex flex-wrap gap-2">
          {[
            { label: 'Tokens', value: `${response.usage?.total_tokens ?? '—'}` },
            { label: 'Cost', value: formatUSD(response.cost_usd) },
            { label: 'Latency', value: `${Math.round(response.latency_ms)}ms` },
          ].map(({ label: l, value }) => (
            <div key={l} className="bg-[#0a0a0a] border border-[#2a2a3e] rounded-md px-2 py-1">
              <span className="text-xs text-[#475569]">{l}: </span>
              <span className="text-xs font-mono text-[#94a3b8]">{value}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

// ─── Synthesis streaming panel ────────────────────────────────────────────────

interface SynthesisPanelProps {
  status: 'hidden' | 'streaming' | 'complete';
  content: string;
  tokens?: number;
  cost?: string;
  result?: CompletionResponse | null;  // populated when rendering a static CouncilResult
}

function SynthesisPanel({ status, content, tokens, cost, result }: SynthesisPanelProps) {
  const contentRef = useRef<HTMLDivElement>(null);
  const wrapperRef = useRef<HTMLDivElement>(null);
  const hasScrolledIntoView = useRef(false);

  useEffect(() => {
    if (status === 'streaming' && contentRef.current) {
      contentRef.current.scrollTop = contentRef.current.scrollHeight;
    }
  }, [content, status]);

  // When synthesis first starts, scroll the panel into view. Otherwise
  // users with many member panels never see the synthesis appear — it
  // renders below the fold and the "feedback in the center UI" vanishes
  // as soon as the members push it off-screen.
  useEffect(() => {
    if (status !== 'hidden' && !hasScrolledIntoView.current && wrapperRef.current) {
      hasScrolledIntoView.current = true;
      wrapperRef.current.scrollIntoView({ behavior: 'smooth', block: 'start' });
    }
    if (status === 'hidden') {
      hasScrolledIntoView.current = false;
    }
  }, [status]);

  if (status === 'hidden') return null;

  const displayContent = result?.content ?? content;
  const displayModel = result?.model;
  const displayTokens = result ? (result.usage?.total_tokens ?? 0) : (tokens ?? 0);
  const displayCost = result ? result.cost_usd : cost;
  const displayLatency = result ? result.latency_ms : undefined;

  return (
    <div ref={wrapperRef} className="scroll-mt-4">
      <div className="section-label mb-3 flex items-center gap-2">
        <span className="text-[#3b82f6]">◈</span>
        <span>Synthesis</span>
        {status === 'streaming' && (
          <span className="text-[10px] font-mono text-[#76B900] animate-pulse uppercase tracking-wider">
            Generating…
          </span>
        )}
      </div>
      <div className="card p-5 border-2 border-[#3b82f6]/40 bg-[#3b82f6]/[0.02] shadow-[0_0_24px_rgba(59,130,246,0.08)] transition-all duration-300">
        <div className="flex items-center gap-2 mb-3">
          <div className="w-7 h-7 rounded-lg bg-[#3b82f6]/10 flex items-center justify-center">
            <svg className="w-4 h-4 text-[#3b82f6]" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
              <path strokeLinecap="round" strokeLinejoin="round"
                d="M9.813 15.904L9 18.75l-.813-2.846a4.5 4.5 0 00-3.09-3.09L2.25 12l2.846-.813a4.5 4.5 0 003.09-3.09L9 5.25l.813 2.846a4.5 4.5 0 003.09 3.09L15.75 12l-2.846.813a4.5 4.5 0 00-3.09 3.09z" />
            </svg>
          </div>
          <div>
            <div className="text-sm font-semibold text-[#e2e8f0]">Synthesized Response</div>
            {displayModel && (
              <div className="text-xs text-[#475569] font-mono">{displayModel}</div>
            )}
          </div>
        </div>

        <div
          ref={contentRef}
          className="bg-[#0a0a0a] rounded-lg p-4 font-mono text-sm text-[#e2e8f0] whitespace-pre-wrap leading-relaxed max-h-[400px] overflow-y-auto"
        >
          {displayContent || (
            <span className="text-[#475569] italic flex items-center gap-1.5">
              <span className="inline-block w-1.5 h-1.5 rounded-full bg-[#3b82f6] animate-pulse" />
              Synthesizing responses…
            </span>
          )}
          {status === 'streaming' && displayContent && <StreamCursor />}
        </div>

        {(status === 'complete' || result) && (
          <div className="flex flex-wrap gap-2 mt-3">
            {[
              { label: 'Tokens', value: displayTokens > 0 ? String(displayTokens) : '—' },
              { label: 'Cost', value: formatUSD(displayCost) },
              ...(displayLatency !== undefined
                ? [{ label: 'Latency', value: formatMs(displayLatency) }]
                : []),
            ].map(({ label, value }) => (
              <div key={label} className="bg-[#0a0a0a] border border-[#2a2a3e] rounded-md px-2 py-1">
                <span className="text-xs text-[#475569]">{label}: </span>
                <span className="text-xs font-mono text-[#94a3b8]">{value}</span>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

// ─── Props ────────────────────────────────────────────────────────────────────

interface StreamingProps {
  /** Streaming mode: member states keyed by label */
  memberStates: Record<string, MemberStreamState>;
  /** Timer start timestamps keyed by member label */
  memberStartTimes: Record<string, number>;
  /** Ordered list of member labels as they appear */
  memberOrder: string[];
  /** Weight map from council_start */
  memberWeights: Record<string, number>;
  /** Synthesis streaming state */
  synthesisStatus: 'hidden' | 'streaming' | 'complete';
  synthesisContent: string;
  synthesisTokens: number;
  synthesisCost: string;
  /** Live total cost (updates as members complete) */
  liveTotalCost: string;
  /** Quorum result (set on council_complete) */
  quorumMet?: boolean;
  /** Total latency (set on council_complete) */
  totalLatencyMs?: number;
  /** Strategy */
  strategy?: string;
  failedMembers: Record<string, string>;
}

interface StaticProps {
  result: CouncilResult;
}

export type CouncilPanelProps =
  | ({ mode: 'streaming' } & StreamingProps)
  | ({ mode: 'static' } & StaticProps);

// ─── Main component ───────────────────────────────────────────────────────────

export default function CouncilPanel(props: CouncilPanelProps) {
  if (props.mode === 'static') {
    return <StaticCouncilPanel result={props.result} />;
  }
  return <LiveCouncilPanel {...props} />;
}

// ─── Live (streaming) council panel ──────────────────────────────────────────

function LiveCouncilPanel({
  memberStates,
  memberStartTimes,
  memberOrder,
  memberWeights,
  synthesisStatus,
  synthesisContent,
  synthesisTokens,
  synthesisCost,
  liveTotalCost,
  quorumMet,
  totalLatencyMs,
  strategy,
  failedMembers,
}: StreamingProps) {
  const failedKeys = Object.keys(failedMembers).filter(k => k !== '_synthesis');

  return (
    <div className="space-y-6">
      {/* Stats bar */}
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
        {[
          { label: 'Strategy', value: strategy || 'weighted_consensus', color: '#3b82f6' },
          {
            label: 'Total Cost',
            value: formatUSD(liveTotalCost),
            color: '#f59e0b',
          },
          {
            label: 'Total Latency',
            value: totalLatencyMs !== undefined ? formatMs(totalLatencyMs) : '—',
            color: '#06b6d4',
          },
          {
            label: 'Quorum',
            value: quorumMet === undefined ? '—' : quorumMet ? 'Met' : 'Not Met',
            color: quorumMet === undefined ? '#555555' : quorumMet ? '#22c55e' : '#ef4444',
          },
        ].map(({ label, value, color }) => (
          <div key={label} className="card p-3 text-center">
            <div className="text-xs text-[#475569] mb-1">{label}</div>
            <div className="text-sm font-mono font-bold" style={{ color }}>{value}</div>
          </div>
        ))}
      </div>

      {/* Synthesis — the answer. Rendered first so it reads like a
          question/answer flow, with the per-member deliberations below
          as supporting detail the user can scan if curious. */}
      <SynthesisPanel
        status={synthesisStatus}
        content={synthesisContent}
        tokens={synthesisTokens}
        cost={synthesisCost}
      />

      {/* Member streaming panels */}
      <div>
        <div className="section-label mb-3">Council Members</div>
        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          {memberOrder.map((label, i) => {
            const state = memberStates[label];
            if (!state) return null;
            return (
              <StreamingMemberPanel
                key={label}
                state={state}
                colorIndex={i}
                weight={memberWeights[label] ?? 0}
                startedAt={memberStartTimes[label]}
              />
            );
          })}
        </div>
      </div>

      {/* Failed members */}
      {failedKeys.length > 0 && (
        <div className="bg-[#ef4444]/5 border border-[#ef4444]/20 rounded-xl p-4">
          <div className="flex items-center gap-2 mb-2">
            <svg className="w-4 h-4 text-[#ef4444]" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2}
                d="M12 9v3.75m-9.303 3.376c-.866 1.5.217 3.374 1.948 3.374h14.71c1.73 0 2.813-1.874 1.948-3.374L13.949 3.378c-.866-1.5-3.032-1.5-3.898 0L2.697 16.126zM12 15.75h.007v.008H12v-.008z" />
            </svg>
            <span className="text-sm font-medium text-[#ef4444]">Failed Members</span>
          </div>
          <div className="flex flex-wrap gap-2">
            {failedKeys.map(m => (
              <span key={m} className="tag bg-[#ef4444]/10 text-[#ef4444] border border-[#ef4444]/20">
                {m}
              </span>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

// ─── Static (completed HTTP result) panel ────────────────────────────────────

function StaticCouncilPanel({ result }: { result: CouncilResult }) {
  return (
    <div className="space-y-6">
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
        {[
          { label: 'Strategy', value: result.strategy, color: '#3b82f6' },
          { label: 'Total Cost', value: formatUSD(result.total_cost_usd), color: '#f59e0b' },
          { label: 'Total Latency', value: `${Math.round(result.total_latency_ms)}ms`, color: '#06b6d4' },
          {
            label: 'Quorum',
            value: result.quorum_met ? 'Met' : 'Not Met',
            color: result.quorum_met ? '#22c55e' : '#ef4444',
          },
        ].map(({ label, value, color }) => (
          <div key={label} className="card p-3 text-center">
            <div className="text-xs text-[#475569] mb-1">{label}</div>
            <div className="text-sm font-mono font-bold" style={{ color }}>{value}</div>
          </div>
        ))}
      </div>

      <div>
        <div className="section-label mb-3">Council Members</div>
        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          {result.members.map((member, i) => {
            const label = member.persona
              ? `${member.persona} (${member.provider})`
              : member.provider;
            const response = result.member_responses[member.provider] ?? null;
            const failed = result.failed_members.includes(member.provider);
            return (
              <MemberPanel
                key={`${member.provider}-${i}`}
                label={label}
                response={response}
                colorIndex={i}
                weight={member.weight}
                persona={member.persona ?? null}
                failed={failed}
              />
            );
          })}
        </div>
      </div>

      {result.synthesis && (
        <SynthesisPanel
          status="complete"
          content={result.synthesis.content}
          result={result.synthesis}
        />
      )}

      {result.failed_members.length > 0 && (
        <div className="bg-[#ef4444]/5 border border-[#ef4444]/20 rounded-xl p-4">
          <div className="flex items-center gap-2 mb-2">
            <svg className="w-4 h-4 text-[#ef4444]" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2}
                d="M12 9v3.75m-9.303 3.376c-.866 1.5.217 3.374 1.948 3.374h14.71c1.73 0 2.813-1.874 1.948-3.374L13.949 3.378c-.866-1.5-3.032-1.5-3.898 0L2.697 16.126zM12 15.75h.007v.008H12v-.008z" />
            </svg>
            <span className="text-sm font-medium text-[#ef4444]">Failed Members</span>
          </div>
          <div className="flex flex-wrap gap-2">
            {result.failed_members.map(m => (
              <span key={m} className="tag bg-[#ef4444]/10 text-[#ef4444] border border-[#ef4444]/20">
                {m}
              </span>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
