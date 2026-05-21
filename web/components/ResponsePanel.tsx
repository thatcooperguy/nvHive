'use client';

import { useEffect, useRef } from 'react';
import type { CompletionResponse } from '@/lib/types';

interface Props {
  content: string;
  streaming?: boolean;
  response?: CompletionResponse | null;
  error?: string | null;
  placeholder?: string;
  className?: string;
}

function formatUSD(val: string | null | undefined): string {
  if (!val) return '—';
  const n = parseFloat(val);
  if (isNaN(n)) return '—';
  return n < 0.001 ? `$${n.toFixed(5)}` : `$${n.toFixed(4)}`;
}

export default function ResponsePanel({
  content,
  streaming = false,
  response,
  error,
  placeholder = 'Response will appear here...',
  className = '',
}: Props) {
  const scrollRef = useRef<HTMLDivElement>(null);

  // Auto-scroll while streaming
  useEffect(() => {
    if (streaming && scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, [content, streaming]);

  return (
    <div className={`flex flex-col gap-3 ${className}`}>
      {/* Content area */}
      <div
        ref={scrollRef}
        className="relative min-h-[200px] max-h-[500px] overflow-y-auto bg-[#ffffff] border border-[#e5e5e5] rounded-xl p-4 dark:bg-[#0a0a0a] dark:border-[#262626]"
      >
        {error ? (
          <div className="flex items-start gap-3">
            <div className="w-8 h-8 rounded-full bg-[#dc2626]/10 flex items-center justify-center flex-shrink-0 mt-0.5">
              <svg className="w-4 h-4 text-[#dc2626]" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2}
                  d="M12 9v3.75m-9.303 3.376c-.866 1.5.217 3.374 1.948 3.374h14.71c1.73 0 2.813-1.874 1.948-3.374L13.949 3.378c-.866-1.5-3.032-1.5-3.898 0L2.697 16.126zM12 15.75h.007v.008H12v-.008z" />
              </svg>
            </div>
            <div>
              <div className="text-sm font-medium text-[#dc2626] mb-1">Error</div>
              <div className="text-sm text-[#737373] font-mono dark:text-[#a3a3a3]">{error}</div>
            </div>
          </div>
        ) : content ? (
          <div className={`prose-sm font-mono text-sm text-[#0a0a0a] whitespace-pre-wrap leading-relaxed ${
            streaming ? 'streaming-cursor' : ''
          }`}>
            {content}
          </div>
        ) : streaming ? (
          <div className="flex items-center gap-2 text-[#737373] dark:text-[#a3a3a3]">
            <div className="flex gap-1">
              {[0, 1, 2].map(i => (
                <div
                  key={i}
                  className="w-2 h-2 bg-[#2563eb] rounded-full animate-bounce"
                  style={{ animationDelay: `${i * 0.15}s` }}
                />
              ))}
            </div>
            <span className="text-sm">Generating...</span>
          </div>
        ) : (
          <div className="text-[#737373] text-sm italic dark:text-[#a3a3a3]">{placeholder}</div>
        )}
      </div>

      {/* Metadata bar */}
      {response && !error && (
        <div className="flex flex-wrap items-center gap-3 px-1">
          <MetaChip
            label="Provider"
            value={response.provider}
            color="#2563eb"
          />
          <MetaChip
            label="Model"
            value={response.model}
            color="#7c3aed"
          />
          <MetaChip
            label="Tokens"
            value={`${response.usage?.total_tokens ?? '—'}`}
            color="#16a34a"
          />
          <MetaChip
            label="Cost"
            value={formatUSD(response.cost_usd)}
            color="#d97706"
          />
          <MetaChip
            label="Latency"
            value={`${Math.round(response.latency_ms)}ms`}
            color="#0891b2"
          />
          {response.cache_hit && (
            <span className="tag bg-[#16a34a]/10 text-[#16a34a] border border-[#16a34a]/20">
              <svg className="w-3 h-3" fill="currentColor" viewBox="0 0 20 20">
                <path d="M4 4a2 2 0 00-2 2v1h16V6a2 2 0 00-2-2H4z" />
                <path fillRule="evenodd" d="M18 9H2v5a2 2 0 002 2h12a2 2 0 002-2V9zM4 13a1 1 0 011-1h1a1 1 0 110 2H5a1 1 0 01-1-1zm5-1a1 1 0 100 2h1a1 1 0 100-2H9z" clipRule="evenodd" />
              </svg>
              Cached
            </span>
          )}
          {response.fallback_from && (
            <span className="tag bg-[#d97706]/10 text-[#d97706] border border-[#d97706]/20">
              Fallback from {response.fallback_from}
            </span>
          )}
        </div>
      )}
    </div>
  );
}

function MetaChip({ label, value, color }: { label: string; value: string; color: string }) {
  return (
    <div className="flex items-center gap-1.5 bg-[#ffffff] border border-[#e5e5e5] rounded-lg px-2.5 py-1 dark:bg-[#0a0a0a] dark:border-[#262626]">
      <span className="text-xs text-[#737373] dark:text-[#a3a3a3]">{label}:</span>
      <span className="text-xs font-mono font-medium" style={{ color }}>{value}</span>
    </div>
  );
}
