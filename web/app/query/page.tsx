'use client';

import { useEffect, useState, useRef, Suspense } from 'react';
import Link from 'next/link';
import { useSearchParams } from 'next/navigation';
import QueryInput from '@/components/QueryInput';
import ResponsePanel from '@/components/ResponsePanel';
import CouncilPanel from '@/components/CouncilPanel';
import {
  query,
  queryStream,
  runCouncil,
  compare,
  getModels,
  getAgentPresets,
  askSetupAssistant,
} from '@/lib/api';
import { useProviderHealth } from '@/lib/useProviderHealth';
import type {
  CompletionResponse,
  CouncilResult,
  CompareResult,
  QueryMode,
  RecentQuery,
  AgentPreset,
} from '@/lib/types';

const STORAGE_KEY = 'council_recent_queries';

function saveRecent(q: RecentQuery) {
  if (typeof window === 'undefined') return;
  try {
    const existing: RecentQuery[] = JSON.parse(localStorage.getItem(STORAGE_KEY) ?? '[]');
    localStorage.setItem(STORAGE_KEY, JSON.stringify([q, ...existing].slice(0, 20)));
  } catch {
    // ignore
  }
}

function friendlyQueryError(message: string): string {
  const raw = message || 'Request failed';
  const lower = raw.toLowerCase();
  if (lower.includes('stalled') || lower.includes('no tokens')) {
    return [
      'Local AI is still warming up.',
      'The model is installed, but Ollama did not emit a token before the safety timer.',
      'Wait a moment and retry, or open Setup to run Fix Setup and verify the selected model.',
      `Last error: ${raw}`,
    ].join(' ');
  }
  if (
    lower.includes('internal server error') ||
    lower.includes('ollama') ||
    lower.includes('model') ||
    lower.includes('provider') ||
    lower.includes('connection refused') ||
    lower.includes('timed out')
  ) {
    return [
      'Local AI is not ready yet.',
      'Open Setup, run Fix Setup, then use Download Models or Install Runtime if nvWizard recommends it.',
      `Last error: ${raw}`,
    ].join(' ');
  }
  return raw;
}

function QueryPageInner() {
  const searchParams = useSearchParams();
  const initialPrompt = searchParams.get('prompt') ?? '';
  const initialMode = (searchParams.get('mode') as QueryMode) ?? 'simple';

  // Live-polled provider health (30s interval) so connected/offline
  // stays accurate throughout the session.
  const { providers } = useProviderHealth();
  const healthyProviderCount = providers.filter(p => p.healthy).length;
  const [models, setModels] = useState<Array<{ model_id: string; provider: string; display_name: string }>>([]);
  const [presets, setPresets] = useState<AgentPreset[]>([]);

  const [loading, setLoading] = useState(false);
  const [streamContent, setStreamContent] = useState('');
  const [response, setResponse] = useState<CompletionResponse | null>(null);
  const [councilResult, setCouncilResult] = useState<CouncilResult | null>(null);
  const [compareResult, setCompareResult] = useState<CompareResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [currentMode, setCurrentMode] = useState<QueryMode>(initialMode);

  const [councilPreset, setCouncilPreset] = useState('');
  const [autoAgents, setAutoAgents] = useState(false);
  const [numAgents, setNumAgents] = useState(3);
  const [councilStrategy, setCouncilStrategy] = useState('');
  const [synthesize, setSynthesize] = useState(true);

  const stopStreamRef = useRef<(() => void) | null>(null);

  useEffect(() => {
    let mounted = true;
    Promise.all([getModels(), getAgentPresets()]).then(([mData, aData]) => {
      if (!mounted) return;
      setModels(mData.models.map(m => ({ model_id: m.model_id, provider: m.provider, display_name: m.display_name })));
      setPresets(aData.presets);
    }).catch(() => {});
    return () => { mounted = false; };
  }, []);

  const handleSubmit = async (params: {
    prompt: string;
    mode: QueryMode;
    provider?: string;
    model?: string;
    systemPrompt?: string;
    temperature: number;
    maxTokens: number;
    stream: boolean;
  }) => {
    stopStreamRef.current?.();
    stopStreamRef.current = null;

    setCurrentMode(params.mode);
    setLoading(true);
    setError(null);
    setResponse(null);
    setCouncilResult(null);
    setCompareResult(null);
    setStreamContent('');

    const baseOpts = {
      system_prompt: params.systemPrompt,
      temperature: params.temperature,
      max_tokens: params.maxTokens,
    };

    let streamingStarted = false;

    const showWizardFallback = async (rawError: string) => {
      const wizard = await askSetupAssistant(
        [
          `The user asked the AI chat: "${params.prompt}".`,
          `The selected AI source failed with: ${rawError}.`,
          'Explain what is happening in nvHive, what button to press next, and what local AI needs before general questions can be answered.',
        ].join(' '),
      );
      setResponse({
        content: wizard.answer,
        model: wizard.mode ?? 'offline-helper',
        provider: 'nvwizard',
        usage: { prompt_tokens: 0, completion_tokens: 0, total_tokens: 0 },
        cost_usd: '0',
        latency_ms: 0,
        finish_reason: 'fallback',
        cache_hit: false,
        fallback_from: 'local-ai',
        metadata: {
          focus: wizard.focus,
          available_immediately: wizard.available_immediately ?? true,
          actions: wizard.actions?.slice(0, 3).map(action => action.id) ?? [],
        },
      });
      setError(null);
    };

    try {
      if (params.mode === 'simple') {
        if (params.stream) {
          const stop = queryStream(
            { prompt: params.prompt, provider: params.provider, model: params.model, ...baseOpts, stream: true },
            (chunk) => setStreamContent(chunk.accumulated),
            (done) => {
              setLoading(false);
              setStreamContent('');
              setResponse({
                content: done.content,
                model: done.model,
                provider: done.provider,
                usage: done.usage ?? { prompt_tokens: 0, completion_tokens: 0, total_tokens: 0 },
                cost_usd: done.cost_usd ?? null,
                latency_ms: 0,
                finish_reason: done.finish_reason ?? 'stop',
                cache_hit: false,
                fallback_from: null,
                metadata: {},
              });
              saveRecent({
                id: `${Date.now()}`,
                prompt: params.prompt,
                mode: 'simple',
                provider: done.provider,
                timestamp: Date.now(),
                cost: done.cost_usd,
                tokens: done.usage?.total_tokens,
              });
            },
            (err) => {
              setLoading(false);
              void showWizardFallback(err).catch(() => setError(friendlyQueryError(err)));
            }
          );
          stopStreamRef.current = stop;
          streamingStarted = true;
          return;
        } else {
          const resp = await query(params.prompt, {
            provider: params.provider,
            model: params.model,
            ...baseOpts,
          });
          setResponse(resp);
          saveRecent({ id: `${Date.now()}`, prompt: params.prompt, mode: 'simple', provider: resp.provider, timestamp: Date.now(), cost: resp.cost_usd ?? undefined, tokens: resp.usage?.total_tokens });
        }
      } else if (params.mode === 'council') {
        const result = await runCouncil(params.prompt, {
          auto_agents: autoAgents,
          preset: councilPreset || undefined,
          num_agents: numAgents,
          strategy: councilStrategy || undefined,
          synthesize,
          ...baseOpts,
        });
        setCouncilResult(result);
        saveRecent({ id: `${Date.now()}`, prompt: params.prompt, mode: 'council', timestamp: Date.now(), cost: result.total_cost_usd ?? undefined });
      } else if (params.mode === 'compare') {
        const result = await compare(params.prompt, undefined, baseOpts);
        setCompareResult(result);
        saveRecent({ id: `${Date.now()}`, prompt: params.prompt, mode: 'compare', timestamp: Date.now() });
      }
    } catch (err) {
      const message = err instanceof Error ? err.message : 'Request failed';
      if (params.mode === 'simple') {
        try {
          await showWizardFallback(message);
        } catch {
          setError(friendlyQueryError(message));
        }
      } else {
        setError(friendlyQueryError(message));
      }
    } finally {
      if (!streamingStarted) {
        setLoading(false);
      }
    }
  };

  const handleStop = () => {
    stopStreamRef.current?.();
    stopStreamRef.current = null;
    setLoading(false);
  };

  return (
    <div className="p-6 space-y-6 max-w-6xl mx-auto">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <div className="text-[10px] font-mono text-[#76B900] tracking-[0.2em] uppercase mb-0.5">Ask AI</div>
          <h1 className="text-2xl font-bold text-[#0a0a0a]">Ask AI</h1>
          <p className="text-xs font-mono text-[#737373] mt-1">Ask your local model, or compare answers when you need a second opinion.</p>
        </div>
        <div className="flex flex-wrap gap-2 justify-end">
          <Link href="/setup" className="btn-ghost px-3 py-2 text-[10px] font-mono uppercase tracking-wider">
            Setup Local AI
          </Link>
          <Link href="/providers" className="btn-primary px-3 py-2 text-[10px] font-mono uppercase tracking-wider">
            AI Connections
          </Link>
        </div>
      </div>

      {healthyProviderCount === 0 && (
        <div className="border border-[#d97706]/30 bg-[#fff7ed] px-4 py-3 text-xs text-[#7c2d12]">
          No AI source is answering yet. Connect an API key or install the local runtime from Setup, then retry this page.
        </div>
      )}

      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
        {/* Input panel */}
        <div className="lg:col-span-1 space-y-4">
          <div className="card p-5">
            <QueryInput
              onSubmit={handleSubmit}
              loading={loading}
              providers={providers}
              models={models}
              defaultMode={initialMode}
              showModeToggle
            />
          </div>

          {/* Convene extras */}
          {currentMode === 'council' && (
            <div className="card p-4 space-y-4 animate-fade-in">
              <div className="section-label">Study Group Settings</div>

              <div className="flex items-center justify-between">
                <div>
                  <div className="text-xs font-mono text-[#525252]">Pick helpers automatically</div>
                  <div className="text-[10px] font-mono text-[#a3a3a3]">Choose useful perspectives from the prompt</div>
                </div>
                <button
                  onClick={() => setAutoAgents(!autoAgents)}
                  className={`relative w-10 h-5 transition-colors ${autoAgents ? 'bg-[#76B900]' : 'bg-[#e5e5e5] border border-[#d4d4d4]'}`}
                >
                  <span className={`absolute top-0.5 w-4 h-4 bg-white shadow transition-transform ${autoAgents ? 'translate-x-5' : 'translate-x-0.5'}`} />
                </button>
              </div>

              {presets.length > 0 && (
                <div>
                  <label className="block text-[10px] font-mono text-[#737373] mb-1.5 uppercase tracking-wider">Cabinet</label>
                  <select
                    value={councilPreset}
                    onChange={e => setCouncilPreset(e.target.value)}
                    className="input-base w-full px-3 py-2 text-sm"
                  >
                    <option value="">None</option>
                    {presets.map(p => (
                      <option key={p.name} value={p.name}>{p.name}</option>
                    ))}
                  </select>
                </div>
              )}

              {autoAgents && (
                <div>
                  <div className="flex items-center justify-between mb-1.5">
                    <label className="text-[10px] font-mono text-[#737373] uppercase tracking-wider">Helpers</label>
                    <span className="text-xs font-mono text-[#76B900]">{numAgents}</span>
                  </div>
                  <input
                    type="range" min="2" max="8" step="1" value={numAgents}
                    onChange={e => setNumAgents(parseInt(e.target.value))}
                    className="w-full"
                  />
                </div>
              )}

              <div>
                <label className="block text-[10px] font-mono text-[#737373] mb-1.5 uppercase tracking-wider">Strategy</label>
                <select
                  value={councilStrategy}
                  onChange={e => setCouncilStrategy(e.target.value)}
                  className="input-base w-full px-3 py-2 text-sm"
                >
                  <option value="">Default</option>
                  <option value="weighted">Weighted</option>
                  <option value="unanimous">Unanimous</option>
                  <option value="majority">Majority</option>
                </select>
              </div>

              <div className="flex items-center justify-between">
                <div>
                  <div className="text-xs font-mono text-[#525252]">Combine into one answer</div>
                  <div className="text-[10px] font-mono text-[#a3a3a3]">Summarize the group into one response</div>
                </div>
                <button
                  onClick={() => setSynthesize(!synthesize)}
                  className={`relative w-10 h-5 transition-colors ${synthesize ? 'bg-[#76B900]' : 'bg-[#e5e5e5] border border-[#d4d4d4]'}`}
                >
                  <span className={`absolute top-0.5 w-4 h-4 bg-white shadow transition-transform ${synthesize ? 'translate-x-5' : 'translate-x-0.5'}`} />
                </button>
              </div>

              <div className="border border-[#e5e5e5] bg-[#fafafa] p-3 text-[10px] font-mono text-[#737373] leading-relaxed">
                Agent perspectives are selected when you run the query, using the current question and settings.
              </div>
            </div>
          )}
        </div>

        {/* Response panel */}
        <div className="lg:col-span-2 space-y-4">
          {loading && (
            <div className="flex items-center justify-between card px-4 py-3 border-[#76B900]/30">
              <div className="flex items-center gap-2 text-xs font-mono text-[#76B900]">
                <div className="w-1.5 h-1.5 bg-[#76B900] animate-pulse" style={{ clipPath: 'polygon(50% 0%, 100% 50%, 50% 100%, 0% 50%)' }} />
                {currentMode === 'council' ? 'COUNCIL DELIBERATING...' :
                 currentMode === 'compare' ? 'COMPARING PROVIDERS...' :
                 streamContent ? 'STREAMING...' : 'PROCESSING...'}
              </div>
              <button onClick={handleStop} className="text-[10px] font-mono text-[#dc2626] hover:text-[#fca5a5] transition-colors">
                STOP
              </button>
            </div>
          )}

          {currentMode === 'simple' && (
            <div className="card p-5">
              <div className="section-label mb-3">Response</div>
              <ResponsePanel
                content={streamContent || response?.content || ''}
                streaming={loading && !!streamContent}
                response={streamContent ? null : response}
                error={error}
                placeholder="Response will appear here after you send a query"
              />
              {/* Response metadata */}
              {response && (
                <div className="mt-3 pt-3 border-t border-[#e5e5e5] flex items-center gap-4 flex-wrap">
                  <span className="text-[10px] font-mono text-[#76B900]">
                    {response.usage?.total_tokens} tokens
                  </span>
                  <span className="text-[10px] font-mono text-[#a3a3a3]">
                    {response.provider} / {response.model}
                  </span>
                  {response.cost_usd && parseFloat(String(response.cost_usd)) > 0 && (
                    <span className="text-[10px] font-mono text-[#d97706]">
                      ${parseFloat(String(response.cost_usd)).toFixed(4)}
                    </span>
                  )}
                  {response.latency_ms > 0 && (
                    <span className="text-[10px] font-mono text-[#a3a3a3]">
                      {response.latency_ms}ms
                    </span>
                  )}
                  {response.cache_hit && (
                    <span className="text-[10px] font-mono text-[#76B900] bg-[#76B900]/10 px-1.5 py-0.5">
                      CACHED
                    </span>
                  )}
                </div>
              )}
            </div>
          )}

          {currentMode === 'council' && (
            <div className="card p-5">
              <div className="section-label mb-3 flex items-center gap-2">
                <span className="text-[#76B900]">◈</span>
                Convene Response
              </div>
              {error ? (
                <ResponsePanel content="" error={error} />
              ) : councilResult ? (
                <CouncilPanel mode="static" result={councilResult} />
              ) : !loading ? (
                <div className="text-center py-10 text-[#a3a3a3]">
                  <div className="text-4xl mb-3">◈</div>
                  <div className="text-xs font-mono">RUN A CONVENE QUERY TO SEE MULTI-LLM RESPONSES</div>
                </div>
              ) : null}
            </div>
          )}

          {currentMode === 'compare' && (
            <div className="card p-5">
              <div className="section-label mb-3 flex items-center gap-2">
                <span className="text-[#525252]">▣</span>
                Advisor Comparison
              </div>
              {error ? (
                <ResponsePanel content="" error={error} />
              ) : compareResult ? (
                <CompareView result={compareResult} />
              ) : !loading ? (
                <div className="text-center py-10 text-[#a3a3a3]">
                  <div className="text-4xl mb-3">▣</div>
                  <div className="text-xs font-mono">RUN A COMPARE QUERY TO SEE SIDE-BY-SIDE RESPONSES</div>
                </div>
              ) : null}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

function CompareView({ result }: { result: CompareResult }) {
  const entries = Object.entries(result);
  const COLORS = ['#76B900', '#d97706', '#2563eb', '#dc2626', '#7c3aed', '#0891b2'];

  return (
    <div className="space-y-4">
      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        {entries.map(([provider, resp], i) => (
          <div
            key={provider}
            className="border p-4 bg-[#ffffff]"
            style={{ borderColor: `${COLORS[i % COLORS.length]}30` }}
          >
            <div className="flex items-center justify-between mb-3">
              <div>
                <div className="font-mono font-bold text-sm capitalize" style={{ color: COLORS[i % COLORS.length] }}>
                  {provider.toUpperCase()}
                </div>
                <div className="text-[10px] font-mono text-[#a3a3a3]">{resp.model}</div>
              </div>
              <div className="text-right">
                <div className="text-[10px] font-mono text-[#525252]">{resp.usage?.total_tokens ?? '—'} tokens</div>
                <div className="text-[10px] font-mono text-[#a3a3a3]">{Math.round(resp.latency_ms)}ms</div>
              </div>
            </div>
            <div className="bg-[#ffffff] border border-[#e5e5e5] p-3 max-h-48 overflow-y-auto">
              <div className="text-xs text-[#262626] font-mono whitespace-pre-wrap leading-relaxed">
                {resp.content}
              </div>
            </div>
          </div>
        ))}
      </div>

      {/* Summary table */}
      <div className="overflow-x-auto border border-[#e5e5e5]">
        <table className="w-full text-xs">
          <thead>
            <tr className="border-b border-[#e5e5e5] bg-[#ffffff]">
              <th className="text-left py-2 px-3 font-mono text-[#a3a3a3] uppercase tracking-wider">Provider</th>
              <th className="text-right py-2 px-3 font-mono text-[#a3a3a3] uppercase tracking-wider">Tokens</th>
              <th className="text-right py-2 px-3 font-mono text-[#a3a3a3] uppercase tracking-wider">Cost</th>
              <th className="text-right py-2 px-3 font-mono text-[#a3a3a3] uppercase tracking-wider">Latency</th>
              <th className="text-right py-2 px-3 font-mono text-[#a3a3a3] uppercase tracking-wider">Cached</th>
            </tr>
          </thead>
          <tbody>
            {entries.map(([provider, resp]) => (
              <tr key={provider} className="border-b border-[#e5e5e5] hover:bg-[#f5f5f5]">
                <td className="py-2 px-3 font-mono font-bold text-[#0a0a0a] capitalize">{provider}</td>
                <td className="py-2 px-3 text-right font-mono text-[#525252]">{resp.usage?.total_tokens ?? '—'}</td>
                <td className="py-2 px-3 text-right font-mono text-[#d97706]">
                  {resp.cost_usd ? `$${parseFloat(resp.cost_usd).toFixed(4)}` : (
                    <span className="text-[#76B900]">FREE</span>
                  )}
                </td>
                <td className="py-2 px-3 text-right font-mono text-[#a3a3a3]">{Math.round(resp.latency_ms)}ms</td>
                <td className="py-2 px-3 text-right font-mono">
                  {resp.cache_hit ? (
                    <span className="text-[#76B900]">YES</span>
                  ) : (
                    <span className="text-[#333333]">NO</span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

export default function QueryPage() {
  return (
    <Suspense fallback={
      <div className="p-6 flex items-center gap-2 text-xs font-mono text-[#a3a3a3]">
        <div className="w-1.5 h-1.5 bg-[#76B900] animate-pulse" style={{ clipPath: 'polygon(50% 0%, 100% 50%, 50% 100%, 0% 50%)' }} />
        LOADING...
      </div>
    }>
      <QueryPageInner />
    </Suspense>
  );
}
