'use client';

/**
 * The "Wizard used N tools" trace under an answer: auto-class calls that ran
 * server-side inside the follow-up loop, with whitelist refusals split out as
 * muted one-liners. Extracted from the Wizard thread (chat/WizardThread.tsx);
 * the Sources footer (chat/SourcesFooter.tsx) mines the same trace.
 */

import { useState } from 'react';
import { isRefusedToolResult, type WizardChatToolResult } from '@/lib/api';

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
export default function ServerToolTrace({ trace, specialist }: { trace: WizardChatToolResult[]; specialist: string }) {
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
