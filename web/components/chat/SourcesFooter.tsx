'use client';

import { useState } from 'react';
import { isRefusedToolResult, type WizardChatToolResult } from '@/lib/api';

/**
 * SourcesFooter renders inline-numbered citations beneath an answer when the
 * Wizard pulled chunks from rag_ask, rag_ask_vault, or web_search. Each hit
 * gets a [1], [2], … number with the source filename / URL and a click-to-
 * expand snippet, so users can see exactly where the answer is grounded.
 *
 * Beats a separate sources panel (the 2023 pattern). Stitching [N] back into
 * the answer text is a follow-up — for now numbering matches order of arrival.
 */
export default function SourcesFooter({ trace }: { trace: WizardChatToolResult[] }) {
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
