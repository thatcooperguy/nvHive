'use client';

import { useCallback, useEffect, useRef, useState } from 'react';

/**
 * SystemConsole — collapsible top-of-WebUI CLI surface.
 *
 * Solves the problem: when something is broken on a fresh install, users
 * used to be told "run `nvh serve` in a terminal" — which defeats the
 * out-of-the-box promise. The console sits below the top status bar,
 * tails $NVH_HOME/logs/*.log via /api/logs (a Next.js route that reads
 * disk directly, so it works even when the FastAPI server is down), and
 * offers one-click [Restart API] + [Run Doctor] via /api/services/* —
 * bridges that spawn the rootless `nvh` binary from inside the Next.js
 * process.
 *
 * Behaviour:
 *   - Collapsed (single 24px row) by default. Shows API + Ollama status
 *     chips + a "Logs" button to expand.
 *   - Expanded: 280px panel with source tabs (API / WebUI / Ollama /
 *     Install), live-tailing log content, action buttons.
 *   - Persists collapsed/expanded state in localStorage.
 *   - Polls /api/logs every 4s while expanded, every 30s while collapsed.
 *
 * Why this is in Next.js and not FastAPI: see the comment at the top of
 * /api/logs/route.ts — the most common failure is "FastAPI is down,"
 * which would make a FastAPI-hosted log viewer useless.
 */

type Source = 'api' | 'webui' | 'ollama' | 'install';

const SOURCES: { key: Source; label: string; filename: string }[] = [
  { key: 'api', label: 'API', filename: 'api-server.log' },
  { key: 'webui', label: 'WebUI', filename: 'webui-bootstrap.log' },
  { key: 'ollama', label: 'Ollama', filename: 'ollama.log' },
  { key: 'install', label: 'Install', filename: 'install.log' },
];

interface LogsResponse {
  source: string;
  path: string;
  exists: boolean;
  mtime: number;
  sizeBytes: number;
  lines: string[];
  hint?: string;
  error?: string;
}

const API_BASE: string =
  typeof window !== 'undefined' && (window as unknown as { __HIVE_API_URL__?: string }).__HIVE_API_URL__
    ? (window as unknown as { __HIVE_API_URL__: string }).__HIVE_API_URL__
    : 'http://localhost:8000';

const COLLAPSED_KEY = 'nvh-system-console-collapsed';

function readCollapsed(): boolean {
  if (typeof window === 'undefined') return true;
  try {
    return window.localStorage.getItem(COLLAPSED_KEY) !== '0';
  } catch {
    return true;
  }
}

function writeCollapsed(value: boolean): void {
  if (typeof window === 'undefined') return;
  try {
    window.localStorage.setItem(COLLAPSED_KEY, value ? '1' : '0');
  } catch {
    /* localStorage unavailable */
  }
}

export default function SystemConsole() {
  const [collapsed, setCollapsed] = useState<boolean>(true);
  const [source, setSource] = useState<Source>('api');
  const [logs, setLogs] = useState<LogsResponse | null>(null);
  const [apiHealthy, setApiHealthy] = useState<boolean | null>(null);
  const [ollamaHealthy, setOllamaHealthy] = useState<boolean | null>(null);
  const [actionMessage, setActionMessage] = useState<string | null>(null);
  const [actionBusy, setActionBusy] = useState<boolean>(false);
  const logRef = useRef<HTMLPreElement | null>(null);

  // Restore last collapsed state.
  useEffect(() => {
    setCollapsed(readCollapsed());
  }, []);

  // Probe API + Ollama every 8s, both states feed the chips at the top.
  useEffect(() => {
    let cancelled = false;
    const probe = async () => {
      try {
        const ctl = new AbortController();
        const t = setTimeout(() => ctl.abort(), 2500);
        const r = await fetch(`${API_BASE}/v1/health`, { signal: ctl.signal, cache: 'no-store' });
        clearTimeout(t);
        if (!cancelled) setApiHealthy(r.ok);
      } catch {
        if (!cancelled) setApiHealthy(false);
      }
      try {
        const ctl = new AbortController();
        const t = setTimeout(() => ctl.abort(), 2500);
        const r = await fetch('http://localhost:11434/api/tags', { signal: ctl.signal, cache: 'no-store' });
        clearTimeout(t);
        if (!cancelled) setOllamaHealthy(r.ok);
      } catch {
        if (!cancelled) setOllamaHealthy(false);
      }
    };
    void probe();
    const interval = setInterval(() => void probe(), 8_000);
    return () => {
      cancelled = true;
      clearInterval(interval);
    };
  }, []);

  // Poll the active log source.
  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | null = null;

    const tick = async () => {
      try {
        const r = await fetch(`/api/logs?source=${encodeURIComponent(source)}&lines=200`, {
          cache: 'no-store',
        });
        if (cancelled) return;
        if (r.ok) {
          const data: LogsResponse = await r.json();
          setLogs(data);
        }
      } catch {
        /* keep last logs */
      }
      const interval = collapsed ? 30_000 : 4_000;
      timer = setTimeout(tick, interval);
    };
    void tick();

    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
  }, [source, collapsed]);

  // Auto-scroll to the end of new log content.
  useEffect(() => {
    if (logRef.current) {
      logRef.current.scrollTop = logRef.current.scrollHeight;
    }
  }, [logs]);

  const toggleCollapsed = useCallback(() => {
    setCollapsed(prev => {
      const next = !prev;
      writeCollapsed(next);
      return next;
    });
  }, []);

  const restartApi = useCallback(async () => {
    setActionBusy(true);
    setActionMessage('Starting API…');
    try {
      const r = await fetch('/api/services/start-api', {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ port: 8000 }),
      });
      const data = await r.json();
      if (data.started) {
        setActionMessage(`API spawning (pid ${data.pid}). Tailing api-server.log — clears when /v1/health is 200.`);
        setSource('api');
      } else {
        setActionMessage(`Could not start API: ${data.reason || 'unknown error'}`);
      }
    } catch (err) {
      setActionMessage(`Bridge route failed: ${err instanceof Error ? err.message : String(err)}`);
    } finally {
      setActionBusy(false);
    }
  }, []);

  const runDoctor = useCallback(async () => {
    setActionBusy(true);
    setActionMessage('Running nvh doctor…');
    try {
      const r = await fetch('/api/services/doctor', { cache: 'no-store' });
      const data = await r.json();
      if (data.ok) {
        const summary = data.format === 'json'
          ? (typeof data.report === 'object' && data.report
            ? `Doctor OK — ${Object.keys(data.report as object).length} sections in report. See WebUI log for details.`
            : 'Doctor OK.')
          : 'Doctor finished (text mode).';
        setActionMessage(summary);
      } else {
        setActionMessage(`Doctor reported issues: ${data.reason || 'see exit code'}`);
      }
    } catch (err) {
      setActionMessage(`Bridge route failed: ${err instanceof Error ? err.message : String(err)}`);
    } finally {
      setActionBusy(false);
    }
  }, []);

  const apiChip = apiHealthy === null
    ? { label: 'API ?', color: '#71717a' }
    : apiHealthy
      ? { label: 'API up', color: '#22c55e' }
      : { label: 'API down', color: '#ef4444' };
  const ollamaChip = ollamaHealthy === null
    ? { label: 'Ollama ?', color: '#71717a' }
    : ollamaHealthy
      ? { label: 'Ollama up', color: '#22c55e' }
      : { label: 'Ollama down', color: '#f59e0b' };

  return (
    <div
      className="fixed left-0 right-0 z-[55] border-b"
      style={{
        top: '32px', // anchored below the 32px top status bar
        background: 'var(--bg-secondary, #0a0a0a)',
        borderColor: 'var(--border, #27272a)',
        color: 'var(--text-primary, #fafafa)',
        maxHeight: collapsed ? '24px' : '280px',
        transition: 'max-height 160ms ease-out',
        overflow: 'hidden',
      }}
      role="region"
      aria-label="nvHive system console"
    >
      {/* Collapsed bar — always visible. */}
      <div className="flex items-center gap-3 px-3 h-6 text-[10px] font-mono">
        <span
          className="uppercase tracking-wider px-1.5 py-0.5 rounded-sm"
          style={{ background: apiChip.color, color: '#0a0a0a' }}
          title={`FastAPI on :8000 ${apiHealthy ? 'is healthy' : 'is not responding'}`}
        >
          {apiChip.label}
        </span>
        <span
          className="uppercase tracking-wider px-1.5 py-0.5 rounded-sm"
          style={{ background: ollamaChip.color, color: '#0a0a0a' }}
          title={`Ollama on :11434 ${ollamaHealthy ? 'is healthy' : 'is not responding'}`}
        >
          {ollamaChip.label}
        </span>
        {actionMessage && !collapsed === false ? (
          <span className="opacity-80 truncate">{actionMessage}</span>
        ) : null}
        <div className="ml-auto flex items-center gap-2">
          <button
            type="button"
            onClick={() => void restartApi()}
            disabled={actionBusy}
            className="uppercase tracking-wider px-2 py-0.5 border rounded-sm disabled:opacity-40 hover:opacity-90"
            style={{ borderColor: 'var(--border-bright, #3f3f46)' }}
            title="Spawn `nvh serve` rootlessly via the Next.js bridge"
          >
            Restart API
          </button>
          <button
            type="button"
            onClick={() => void runDoctor()}
            disabled={actionBusy}
            className="uppercase tracking-wider px-2 py-0.5 border rounded-sm disabled:opacity-40 hover:opacity-90"
            style={{ borderColor: 'var(--border-bright, #3f3f46)' }}
            title="Run `nvh doctor --json` via the Next.js bridge"
          >
            Doctor
          </button>
          <button
            type="button"
            onClick={toggleCollapsed}
            className="uppercase tracking-wider px-2 py-0.5 border rounded-sm hover:opacity-90"
            style={{ borderColor: 'var(--border-bright, #3f3f46)' }}
            aria-expanded={!collapsed}
            title={collapsed ? 'Expand logs' : 'Collapse logs'}
          >
            {collapsed ? 'Logs ▾' : 'Hide ▴'}
          </button>
        </div>
      </div>

      {/* Expanded panel — tabs + log content. */}
      {!collapsed && (
        <div className="px-3 py-2 flex flex-col" style={{ height: '256px' }}>
          <div className="flex items-center gap-1 mb-1">
            {SOURCES.map(s => (
              <button
                key={s.key}
                type="button"
                onClick={() => setSource(s.key)}
                className="text-[10px] font-mono uppercase tracking-wider px-2 py-0.5 border rounded-sm"
                style={{
                  background: source === s.key ? 'var(--bg-card, #18181b)' : 'transparent',
                  borderColor: source === s.key ? 'var(--accent, #76B900)' : 'var(--border, #27272a)',
                  color: source === s.key ? 'var(--accent, #76B900)' : 'var(--text-muted, #a1a1aa)',
                }}
              >
                {s.label}
              </button>
            ))}
            <span className="text-[10px] font-mono ml-2 opacity-60">
              {logs?.path || '…'}
            </span>
            {logs && !logs.exists && (
              <span className="text-[10px] font-mono ml-2" style={{ color: '#f59e0b' }}>
                (no log file yet — service hasn&apos;t logged anything)
              </span>
            )}
          </div>
          <pre
            ref={logRef}
            className="flex-1 overflow-y-auto text-[10px] font-mono leading-tight whitespace-pre-wrap break-all"
            style={{
              background: 'var(--bg-primary, #050505)',
              color: 'var(--text-primary, #d4d4d8)',
              border: '1px solid var(--border, #27272a)',
              padding: '6px 8px',
            }}
          >
            {logs && logs.lines.length > 0
              ? logs.lines.join('\n')
              : logs?.hint || 'waiting for log content…'}
          </pre>
          {actionMessage && (
            <div className="text-[10px] font-mono mt-1 opacity-90">{actionMessage}</div>
          )}
        </div>
      )}
    </div>
  );
}
