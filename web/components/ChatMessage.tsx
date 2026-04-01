'use client';

import { useState, useCallback } from 'react';
import type { ChatMessage as ChatMessageType } from '@/lib/types';

// ─── Helpers ──────────────────────────────────────────────────────────────────

function formatUSD(val: string | null | undefined): string {
  if (!val) return '';
  const n = parseFloat(val);
  if (isNaN(n) || n === 0) return 'FREE';
  return n < 0.001 ? `$${n.toFixed(5)}` : `$${n.toFixed(4)}`;
}

function timeStr(ts: number): string {
  return new Date(ts).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}

// ─── Simple markdown renderer ─────────────────────────────────────────────────
// Converts common markdown to React elements without a dependency

interface MarkdownProps {
  content: string;
  streaming?: boolean;
}

function parseInline(text: string): React.ReactNode[] {
  const parts: React.ReactNode[] = [];
  // Handle **bold**, *italic*, `code`, and plain text
  const regex = /(\*\*(.+?)\*\*|\*(.+?)\*|`(.+?)`)/g;
  let last = 0;
  let match: RegExpExecArray | null;

  while ((match = regex.exec(text)) !== null) {
    if (match.index > last) {
      parts.push(text.slice(last, match.index));
    }
    if (match[2] !== undefined) {
      parts.push(<strong key={match.index} className="font-semibold text-white">{match[2]}</strong>);
    } else if (match[3] !== undefined) {
      parts.push(<em key={match.index} className="italic text-[#cccccc]">{match[3]}</em>);
    } else if (match[4] !== undefined) {
      parts.push(
        <code key={match.index} className="bg-[#1a1a1a] border border-[#333333] px-1 py-0.5 text-[#76B900] font-mono text-[0.85em]">
          {match[4]}
        </code>
      );
    }
    last = match.index + match[0].length;
  }
  if (last < text.length) {
    parts.push(text.slice(last));
  }
  return parts;
}

// ─── Syntax highlighting (regex-based, display-only) ─────────────────────────

function highlightLine(line: string): React.ReactNode[] {
  // Process one line into colored spans.
  // Order matters: comments first, then strings, then keywords/numbers.
  const nodes: React.ReactNode[] = [];

  // Comment detection (full-line or inline // and #)
  const commentMatch = line.match(/^(\s*)(\/\/.*|#.*)$/);
  if (commentMatch) {
    if (commentMatch[1]) nodes.push(commentMatch[1]);
    nodes.push(<span key="c" className="code-comment">{commentMatch[2]}</span>);
    return nodes;
  }

  const KEYWORDS = /\b(function|class|def|return|import|from|export|const|let|var|if|else|elif|for|while|in|not|and|or|is|None|True|False|null|undefined|true|false|new|this|self|async|await|try|except|catch|finally|raise|throw|yield|pass|break|continue|switch|case|default|typeof|instanceof|void|static|public|private|protected|abstract|interface|extends|implements|super|override|readonly|type|enum|struct|fn|mut|use|mod|pub|where)\b/g;
  const STRINGS = /(["'`])(?:(?!\1)[^\\]|\\.)*?\1/g;
  const NUMBERS = /\b(\d+\.?\d*)\b/g;

  // We'll do a multi-pass token split
  type Token = { type: 'kw' | 'str' | 'num' | 'plain'; text: string };
  const tokens: Token[] = [];

  // Collect all match ranges for strings, keywords, numbers
  type Range = { start: number; end: number; type: 'kw' | 'str' | 'num' };
  const ranges: Range[] = [];

  let m: RegExpExecArray | null;

  // Strings (highest priority — avoid coloring keywords inside strings)
  STRINGS.lastIndex = 0;
  while ((m = STRINGS.exec(line)) !== null) {
    ranges.push({ start: m.index, end: m.index + m[0].length, type: 'str' });
  }

  // Sort and find gaps for keyword/number matching
  const stringRanges = [...ranges];

  const isInsideString = (idx: number) =>
    stringRanges.some(r => idx >= r.start && idx < r.end);

  KEYWORDS.lastIndex = 0;
  while ((m = KEYWORDS.exec(line)) !== null) {
    if (!isInsideString(m.index)) {
      ranges.push({ start: m.index, end: m.index + m[0].length, type: 'kw' });
    }
  }

  NUMBERS.lastIndex = 0;
  while ((m = NUMBERS.exec(line)) !== null) {
    if (!isInsideString(m.index)) {
      ranges.push({ start: m.index, end: m.index + m[0].length, type: 'num' });
    }
  }

  // Sort by start, resolve overlaps (first match wins)
  ranges.sort((a, b) => a.start - b.start);
  const merged: Range[] = [];
  let cursor = 0;
  for (const r of ranges) {
    if (r.start >= cursor) {
      merged.push(r);
      cursor = r.end;
    }
  }

  // Build token list
  let pos = 0;
  for (const r of merged) {
    if (r.start > pos) tokens.push({ type: 'plain', text: line.slice(pos, r.start) });
    tokens.push({ type: r.type, text: line.slice(r.start, r.end) });
    pos = r.end;
  }
  if (pos < line.length) tokens.push({ type: 'plain', text: line.slice(pos) });

  return tokens.map((t, i) => {
    if (t.type === 'plain') return <span key={i}>{t.text}</span>;
    if (t.type === 'str') return <span key={i} className="code-string">{t.text}</span>;
    if (t.type === 'kw') return <span key={i} className="code-keyword">{t.text}</span>;
    if (t.type === 'num') return <span key={i} className="code-number">{t.text}</span>;
    return <span key={i}>{t.text}</span>;
  });
}

function CodeBlock({ code, lang }: { code: string; lang: string }) {
  const [copied, setCopied] = useState(false);
  const [showLineNumbers, setShowLineNumbers] = useState(false);

  const handleCopy = useCallback(() => {
    navigator.clipboard.writeText(code).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    });
  }, [code]);

  const lines = code.split('\n');
  // Remove trailing empty line that often appears from fenced blocks
  if (lines[lines.length - 1] === '') lines.pop();

  return (
    <div className="relative my-3 group">
      {/* Header bar */}
      <div className="flex items-center justify-between bg-[#111111] border border-[#333333] border-b-0 px-3 py-1.5">
        <span className="text-[10px] font-mono text-[#555555] uppercase tracking-wider">
          {lang || 'code'}
        </span>
        <div className="flex items-center gap-2">
          {/* Line numbers toggle */}
          <button
            onClick={() => setShowLineNumbers(v => !v)}
            className={`text-[10px] font-mono transition-colors flex items-center gap-1 ${
              showLineNumbers ? 'text-[#76B900]' : 'text-[#444444] hover:text-[#999999]'
            }`}
            title="Toggle line numbers"
          >
            <svg className="w-3 h-3" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
              <path strokeLinecap="round" strokeLinejoin="round" d="M4 6h16M4 10h16M4 14h16M4 18h16" />
            </svg>
            #
          </button>
          {/* Copy button */}
          <button
            onClick={handleCopy}
            className="text-[10px] font-mono text-[#444444] hover:text-[#76B900] transition-colors flex items-center gap-1"
            title="Copy code"
          >
            {copied ? (
              <>
                <svg className="w-3 h-3" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                  <path strokeLinecap="round" strokeLinejoin="round" d="M4.5 12.75l6 6 9-13.5" />
                </svg>
                COPIED!
              </>
            ) : (
              <>
                <svg className="w-3 h-3" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                  <path strokeLinecap="round" strokeLinejoin="round" d="M15.666 3.888A2.25 2.25 0 0013.5 2.25h-3c-1.03 0-1.9.693-2.166 1.638m7.332 0c.055.194.084.4.084.612v0a.75.75 0 01-.75.75H9a.75.75 0 01-.75-.75v0c0-.212.03-.418.084-.612m7.332 0c.646.049 1.288.11 1.927.184 1.1.128 1.907 1.077 1.907 2.185V19.5a2.25 2.25 0 01-2.25 2.25H6.75A2.25 2.25 0 014.5 19.5V6.257c0-1.108.806-2.057 1.907-2.185a48.208 48.208 0 011.927-.184" />
                </svg>
                COPY
              </>
            )}
          </button>
        </div>
      </div>
      {/* Code content */}
      <pre className="bg-[#0d0d0d] border border-[#333333] overflow-x-auto p-4 text-sm font-mono leading-relaxed">
        {lines.map((line, idx) => (
          <div key={idx} className="flex">
            {showLineNumbers && (
              <span className="code-line-number select-none w-8 text-right flex-shrink-0">
                {idx + 1}
              </span>
            )}
            <code className="flex-1">{highlightLine(line)}</code>
          </div>
        ))}
      </pre>
    </div>
  );
}

function MarkdownContent({ content, streaming }: MarkdownProps) {
  const lines = content.split('\n');
  const elements: React.ReactNode[] = [];
  let i = 0;
  let listItems: React.ReactNode[] = [];
  let orderedItems: React.ReactNode[] = [];

  const flushList = () => {
    if (listItems.length > 0) {
      elements.push(
        <ul key={`ul-${i}`} className="list-none space-y-1 my-2 pl-2">
          {listItems.map((item, idx) => (
            <li key={idx} className="flex items-start gap-2 text-sm text-[#cccccc]">
              <span className="text-[#76B900] mt-1 flex-shrink-0">▸</span>
              <span>{item}</span>
            </li>
          ))}
        </ul>
      );
      listItems = [];
    }
    if (orderedItems.length > 0) {
      elements.push(
        <ol key={`ol-${i}`} className="space-y-1 my-2 pl-2">
          {orderedItems.map((item, idx) => (
            <li key={idx} className="flex items-start gap-2 text-sm text-[#cccccc]">
              <span className="text-[#76B900] font-mono text-xs mt-0.5 flex-shrink-0 w-4">{idx + 1}.</span>
              <span>{item}</span>
            </li>
          ))}
        </ol>
      );
      orderedItems = [];
    }
  };

  while (i < lines.length) {
    const line = lines[i];

    // Fenced code block
    if (line.startsWith('```')) {
      flushList();
      const lang = line.slice(3).trim();
      const codeLines: string[] = [];
      i++;
      while (i < lines.length && !lines[i].startsWith('```')) {
        codeLines.push(lines[i]);
        i++;
      }
      elements.push(<CodeBlock key={`code-${i}`} code={codeLines.join('\n')} lang={lang} />);
      i++;
      continue;
    }

    // Headings
    if (line.startsWith('### ')) {
      flushList();
      elements.push(
        <h3 key={i} className="text-base font-bold text-white mt-4 mb-1">
          {parseInline(line.slice(4))}
        </h3>
      );
      i++;
      continue;
    }
    if (line.startsWith('## ')) {
      flushList();
      elements.push(
        <h2 key={i} className="text-lg font-bold text-white mt-4 mb-2">
          {parseInline(line.slice(3))}
        </h2>
      );
      i++;
      continue;
    }
    if (line.startsWith('# ')) {
      flushList();
      elements.push(
        <h1 key={i} className="text-xl font-bold text-white mt-4 mb-2">
          {parseInline(line.slice(2))}
        </h1>
      );
      i++;
      continue;
    }

    // Unordered list
    if (line.match(/^[-*] /)) {
      if (orderedItems.length > 0) flushList();
      listItems.push(parseInline(line.slice(2)));
      i++;
      continue;
    }

    // Ordered list
    const orderedMatch = line.match(/^\d+\. (.*)/);
    if (orderedMatch) {
      if (listItems.length > 0) flushList();
      orderedItems.push(parseInline(orderedMatch[1]));
      i++;
      continue;
    }

    // Horizontal rule
    if (line.match(/^[-*]{3,}$/)) {
      flushList();
      elements.push(<hr key={i} className="border-[#333333] my-3" />);
      i++;
      continue;
    }

    // Flush any open list before non-list lines
    flushList();

    // Empty line
    if (line.trim() === '') {
      elements.push(<div key={i} className="h-2" />);
      i++;
      continue;
    }

    // Normal paragraph line
    elements.push(
      <p key={i} className="text-sm text-[#cccccc] leading-relaxed">
        {parseInline(line)}
      </p>
    );
    i++;
  }

  flushList();

  return (
    <div className="space-y-0.5">
      {elements}
      {streaming && (
        <span
          className="inline-block w-[2px] h-[1.1em] bg-[#76B900] align-middle ml-0.5"
          style={{ animation: 'blink 1s step-end infinite' }}
        />
      )}
    </div>
  );
}

// ─── Conversation Export ──────────────────────────────────────────────────────

export function exportConversationMarkdown(
  messages: ChatMessageType[],
  advisorLabel?: string,
  dateStr?: string
): string {
  const date = dateStr ?? new Date().toISOString().slice(0, 10);
  const advisor = advisorLabel ?? 'unknown';

  const lines: string[] = [
    '# NVHive Conversation',
    `**Date:** ${date}`,
    `**Advisor:** ${advisor}`,
    '',
    '---',
    '',
  ];

  for (const msg of messages) {
    if (msg.role === 'user') {
      lines.push(`**You:** ${msg.content}`);
      lines.push('');
    } else if (msg.role === 'assistant') {
      lines.push(`**${advisor}:** ${msg.content}`);
      lines.push('');
      lines.push('---');
      lines.push('');
    } else if (msg.role === 'error') {
      lines.push(`**Error:** ${msg.content}`);
      lines.push('');
    }
  }

  lines.push('*Exported from NVHive — nvhive.dev*');
  return lines.join('\n');
}

// ─── Council Expert Panel (shown in right panel) ──────────────────────────────

interface ExpertPanelProps {
  data: NonNullable<ChatMessageType['council_data']>;
}

export function CouncilExpertPanel({ data }: ExpertPanelProps) {
  const COLORS = ['#3b82f6', '#a855f7', '#22c55e', '#f59e0b', '#06b6d4', '#f97316'];
  const order = data.member_order ?? Object.keys(data.member_responses);

  return (
    <div className="space-y-3 p-4">
      <div className="text-[10px] font-mono text-[#555555] uppercase tracking-wider mb-2">
        ◈ Convene Members
      </div>
      {order.map((key, i) => {
        const member = data.member_responses[key];
        if (!member) return null;
        const color = COLORS[i % COLORS.length];
        return (
          <div
            key={key}
            className="border p-3 bg-[#0d0d0d]"
            style={{ borderColor: `${color}30` }}
          >
            <div className="flex items-center justify-between mb-2">
              <span className="text-xs font-mono font-bold" style={{ color }}>
                {member.provider.toUpperCase()}
              </span>
              <span className="text-[10px] font-mono text-[#444444]">{member.model}</span>
            </div>
            <div className="text-xs text-[#999999] font-mono whitespace-pre-wrap leading-relaxed max-h-32 overflow-y-auto">
              {member.content}
            </div>
            <div className="flex gap-3 mt-2 text-[10px] font-mono text-[#444444]">
              <span>{member.tokens} tokens</span>
              {member.cost && parseFloat(member.cost) > 0 && (
                <span className="text-[#f59e0b]">${parseFloat(member.cost).toFixed(4)}</span>
              )}
              {member.latency_ms && <span>{member.latency_ms}ms</span>}
            </div>
          </div>
        );
      })}
      {data.total_cost && parseFloat(data.total_cost) > 0 && (
        <div className="text-[10px] font-mono text-[#f59e0b] pt-2 border-t border-[#222222]">
          Total: ${parseFloat(data.total_cost).toFixed(4)}
        </div>
      )}
    </div>
  );
}

// ─── Main ChatMessage component ───────────────────────────────────────────────

interface ChatMessageProps {
  message: ChatMessageType;
}

export default function ChatMessage({ message }: ChatMessageProps) {
  const isUser = message.role === 'user';
  const isError = message.role === 'error';

  if (isUser) {
    return (
      <div className="flex justify-end px-4 py-2 group">
        <div className="max-w-[75%]">
          <div className="bg-[#1a2a0a] border border-[#76B900]/30 px-4 py-3 text-sm text-[#e2e8f0] font-mono whitespace-pre-wrap leading-relaxed">
            {message.content}
          </div>
          <div className="flex justify-end mt-1">
            <span className="text-[10px] font-mono text-[#333333] group-hover:text-[#555555] transition-colors">
              {timeStr(message.timestamp)}
            </span>
          </div>
        </div>
      </div>
    );
  }

  if (isError) {
    return (
      <div className="flex px-4 py-2">
        <div className="max-w-[85%] border border-[#ef4444]/30 bg-[#ef4444]/5 px-4 py-3">
          <div className="flex items-center gap-2 mb-1">
            <svg className="w-3.5 h-3.5 text-[#ef4444]" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
              <path strokeLinecap="round" strokeLinejoin="round" d="M12 9v3.75m-9.303 3.376c-.866 1.5.217 3.374 1.948 3.374h14.71c1.73 0 2.813-1.874 1.948-3.374L13.949 3.378c-.866-1.5-3.032-1.5-3.898 0L2.697 16.126zM12 15.75h.007v.008H12v-.008z" />
            </svg>
            <span className="text-[10px] font-mono text-[#ef4444] uppercase tracking-wider">Error</span>
          </div>
          <div className="text-sm text-[#ef4444]/80 font-mono">{message.content}</div>
        </div>
      </div>
    );
  }

  // Assistant message
  const costStr = formatUSD(message.cost_usd);
  const isCouncil = message.mode === 'council' || !!message.council_data;
  const isCompare = message.mode === 'compare' || !!message.compare_data;

  return (
    <div className="flex px-4 py-2 group">
      <div className="flex-1 min-w-0">
        {/* Provider badge row */}
        <div className="flex items-center gap-2 mb-2">
          {/* Green accent diamond */}
          <span
            className="w-2 h-2 bg-[#76B900] flex-shrink-0"
            style={{ clipPath: 'polygon(50% 0%, 100% 50%, 50% 100%, 0% 50%)' }}
          />
          {isCouncil ? (
            <span className="text-[10px] font-mono text-[#76B900] uppercase tracking-wider">Convene</span>
          ) : isCompare ? (
            <span className="text-[10px] font-mono text-[#999999] uppercase tracking-wider">Compare</span>
          ) : message.provider ? (
            <span className="text-[10px] font-mono text-[#76B900] uppercase tracking-wider">
              {message.provider}
            </span>
          ) : null}
          {message.model && !isCouncil && !isCompare && (
            <span className="text-[10px] font-mono text-[#444444]">{message.model}</span>
          )}
          {isCouncil && message.council_data && (
            <span className="text-[10px] font-mono text-[#555555]">
              Synthesized from {Object.keys(message.council_data.member_responses).length} experts
            </span>
          )}
          <span className="ml-auto text-[10px] font-mono text-[#333333] group-hover:text-[#555555] transition-colors">
            {timeStr(message.timestamp)}
          </span>
        </div>

        {/* Message body */}
        <div className="bg-[#141414] border-l-2 border-[#76B900]/40 pl-4 pr-3 py-3">
          {isCompare && message.compare_data ? (
            <CompareContent data={message.compare_data} />
          ) : (
            <MarkdownContent content={message.content} streaming={message.streaming} />
          )}
        </div>

        {/* Metadata footer */}
        {!message.streaming && !isCompare && (
          <div className="flex items-center gap-3 mt-1.5 flex-wrap">
            {message.tokens && message.tokens > 0 && (
              <span className="text-[10px] font-mono text-[#444444]">{message.tokens} tokens</span>
            )}
            {costStr && (
              <span className={`text-[10px] font-mono ${costStr === 'FREE' ? 'text-[#76B900]' : 'text-[#f59e0b]'}`}>
                {costStr}
              </span>
            )}
            {message.latency_ms && message.latency_ms > 0 && (
              <span className="text-[10px] font-mono text-[#333333]">{message.latency_ms}ms</span>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

// ─── Compare Content ──────────────────────────────────────────────────────────

function CompareContent({ data }: { data: NonNullable<ChatMessageType['compare_data']> }) {
  const COLORS = ['#76B900', '#f59e0b', '#3b82f6', '#ef4444', '#a855f7', '#06b6d4'];
  const entries = Object.entries(data);

  return (
    <div className="space-y-3">
      <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
        {entries.map(([provider, resp], i) => (
          <div
            key={provider}
            className="border p-3 bg-[#0a0a0a]"
            style={{ borderColor: `${COLORS[i % COLORS.length]}30` }}
          >
            <div className="flex items-center justify-between mb-2">
              <span className="text-xs font-mono font-bold" style={{ color: COLORS[i % COLORS.length] }}>
                {provider.toUpperCase()}
              </span>
              <span className="text-[10px] font-mono text-[#444444]">{resp.model}</span>
            </div>
            <div className="text-xs text-[#cccccc] font-mono whitespace-pre-wrap leading-relaxed max-h-40 overflow-y-auto">
              {resp.content}
            </div>
            <div className="flex gap-3 mt-2 text-[10px] font-mono text-[#444444]">
              {resp.tokens && <span>{resp.tokens} tokens</span>}
              {resp.cost_usd && parseFloat(resp.cost_usd) > 0 ? (
                <span className="text-[#f59e0b]">${parseFloat(resp.cost_usd).toFixed(4)}</span>
              ) : (
                <span className="text-[#76B900]">FREE</span>
              )}
              {resp.latency_ms && <span>{Math.round(resp.latency_ms)}ms</span>}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
