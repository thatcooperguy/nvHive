"use client";

import { useCallback, useEffect, useState } from "react";

/* ---------- Types ---------- */

interface PlatformInfo {
  name: string;
  display_name: string;
  detected: boolean;
  already_configured: boolean;
  detection_method: string;
  config_path: string;
  integration_type: string; // "mcp" | "inference"
  notes: string[];
}

interface ConnectResult {
  platform: string;
  display_name: string;
  action: string;
  message?: string;
  reason?: string;
  success: boolean;
}

/* ---------- Constants ---------- */

const PLATFORM_META: Record<
  string,
  { subtitle: string; icon: string; badge?: string }
> = {
  nemoclaw: {
    subtitle: "AI agents on NVIDIA GPUs",
    icon: "🟢",
    badge: "NVIDIA",
  },
  openclaw: {
    subtitle: "Open-source AI coding assistant",
    icon: "🦞",
  },
  claude_code: {
    subtitle: "Anthropic\u2019s CLI coding agent",
    icon: "⌨️",
  },
  cursor: {
    subtitle: "AI-powered code editor",
    icon: "▸",
  },
  claude_desktop: {
    subtitle: "Anthropic\u2019s desktop app",
    icon: "🖥",
  },
};

/* ---------- API helpers ---------- */

function apiUrl(path: string): string {
  const base =
    typeof window !== "undefined" && (window as any).__HIVE_API_URL__
      ? (window as any).__HIVE_API_URL__
      : process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";
  return `${base}${path}`;
}

async function apiGet<T>(path: string): Promise<T> {
  const res = await fetch(apiUrl(path));
  const json = await res.json();
  return json.data ?? json;
}

async function apiPost<T>(path: string, body: object): Promise<T> {
  const res = await fetch(apiUrl(path), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const json = await res.json();
  return json.data ?? json;
}

/* ---------- Components ---------- */

function StatusDot({ status }: { status: "connected" | "available" | "missing" | "error" | "connecting" }) {
  const colors = {
    connected: "bg-[#76B900] shadow-[0_0_6px_rgba(118,185,0,0.4)]",
    available: "bg-[#666]",
    missing: "bg-[#333]",
    error: "bg-[#ef4444] shadow-[0_0_6px_rgba(239,68,68,0.4)]",
    connecting: "bg-[#f59e0b] animate-pulse",
  };
  const labels = {
    connected: "Connected",
    available: "Not connected",
    missing: "Not installed",
    error: "Error",
    connecting: "Connecting",
  };
  return (
    <span className="inline-flex items-center gap-2">
      <span
        className={`w-2 h-2 rotate-45 ${colors[status]}`}
        aria-hidden="true"
      />
      <span className="text-xs font-mono uppercase tracking-wider text-[--text-secondary]">
        {labels[status]}
      </span>
    </span>
  );
}

function PlatformRow({
  platform,
  onConnect,
  onDisconnect,
  connecting,
  error,
}: {
  platform: PlatformInfo;
  onConnect: (name: string) => void;
  onDisconnect: (name: string) => void;
  connecting: boolean;
  error: string | null;
}) {
  const meta = PLATFORM_META[platform.name] || {
    subtitle: platform.integration_type === "mcp" ? "Tool connection" : "Inference provider",
    icon: "🔌",
  };

  const status: "connected" | "available" | "missing" | "connecting" | "error" = connecting
    ? "connecting"
    : error
      ? "error"
      : platform.already_configured
        ? "connected"
        : platform.detected
          ? "available"
          : "missing";

  return (
    <div
      className={`
        group relative flex items-center gap-4 px-6 py-5
        border border-[--border] transition-all duration-150
        ${status === "connected" ? "border-l-2 border-l-[#76B900]" : ""}
        hover:border-[#76B900]/30 hover:shadow-[0_0_15px_rgba(118,185,0,0.08)]
      `}
      role="listitem"
      aria-label={`${platform.display_name}, status: ${status}`}
    >
      {/* Icon */}
      <div className="flex-shrink-0 w-10 h-10 flex items-center justify-center bg-[#1a1a1a] text-lg">
        {meta.icon}
      </div>

      {/* Info */}
      <div className="flex-1 min-w-0">
        <div className="flex items-center gap-2">
          <span className="font-medium text-[--text-primary]">
            {platform.display_name}
          </span>
          {meta.badge && (
            <span className="px-1.5 py-0.5 text-[10px] font-mono uppercase tracking-wider bg-[#76B900]/10 text-[#76B900] border border-[#76B900]/20">
              {meta.badge}
            </span>
          )}
          <span className="text-[10px] font-mono uppercase tracking-wider text-[--text-muted]">
            {platform.integration_type === "mcp" ? "tools" : "inference"}
          </span>
        </div>
        <p className="text-sm text-[--text-secondary] mt-0.5">
          {meta.subtitle}
        </p>
        {error && (
          <p className="text-xs text-[#ef4444] mt-1 font-mono">{error}</p>
        )}
        {status === "connected" && platform.detection_method && (
          <p className="text-xs text-[--text-muted] mt-1 font-mono opacity-0 group-hover:opacity-100 transition-opacity">
            {platform.detection_method}
          </p>
        )}
      </div>

      {/* Status */}
      <div className="flex-shrink-0">
        <StatusDot status={status} />
      </div>

      {/* Action */}
      <div className="flex-shrink-0 w-28 text-right">
        {status === "connected" && (
          <button
            onClick={() => onDisconnect(platform.name)}
            className="text-xs font-mono text-[--text-muted] hover:text-[#ef4444] transition-colors px-3 py-1.5 border border-transparent hover:border-[#ef4444]/20"
          >
            Disconnect
          </button>
        )}
        {status === "available" && (
          <button
            onClick={() => onConnect(platform.name)}
            className="text-xs font-mono text-[#76B900] hover:bg-[#76B900]/10 transition-colors px-3 py-1.5 border border-[#76B900]/30 hover:border-[#76B900]/60"
          >
            Connect
          </button>
        )}
        {status === "connecting" && (
          <span className="text-xs font-mono text-[#f59e0b]">
            Connecting...
          </span>
        )}
        {status === "error" && (
          <button
            onClick={() => onConnect(platform.name)}
            className="text-xs font-mono text-[#f59e0b] hover:bg-[#f59e0b]/10 transition-colors px-3 py-1.5 border border-[#f59e0b]/30"
          >
            Retry
          </button>
        )}
        {status === "missing" && (
          <span className="text-xs text-[--text-muted]">
            Learn more &rarr;
          </span>
        )}
      </div>
    </div>
  );
}

/* ---------- Main Page ---------- */

export default function IntegrationsPage() {
  const [platforms, setPlatforms] = useState<PlatformInfo[]>([]);
  const [loading, setLoading] = useState(true);
  const [scanError, setScanError] = useState<string | null>(null);
  const [connecting, setConnecting] = useState<Record<string, boolean>>({});
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [connectAllRunning, setConnectAllRunning] = useState(false);
  const [connectAllResults, setConnectAllResults] = useState<ConnectResult[] | null>(null);

  // --- Scan ---
  const scan = useCallback(async () => {
    setLoading(true);
    setScanError(null);
    try {
      const data = await apiGet<{ platforms: PlatformInfo[] }>("/v1/integrations/scan");
      setPlatforms(data.platforms);
    } catch (e: any) {
      setScanError(e.message || "Failed to scan for platforms");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    scan();
  }, [scan]);

  // --- Connect one ---
  const connectPlatform = async (name: string) => {
    setConnecting((prev) => ({ ...prev, [name]: true }));
    setErrors((prev) => {
      const next = { ...prev };
      delete next[name];
      return next;
    });
    try {
      const result = await apiPost<{ success: boolean; message: string }>(
        "/v1/integrations/connect",
        { platform: name }
      );
      if (result.success) {
        await scan(); // refresh
      } else {
        setErrors((prev) => ({ ...prev, [name]: result.message }));
      }
    } catch (e: any) {
      setErrors((prev) => ({ ...prev, [name]: e.message || "Connection failed" }));
    } finally {
      setConnecting((prev) => ({ ...prev, [name]: false }));
    }
  };

  // --- Connect all ---
  const connectAll = async () => {
    setConnectAllRunning(true);
    setConnectAllResults(null);
    try {
      const data = await apiPost<{ results: ConnectResult[]; connected: number }>(
        "/v1/integrations/connect-all",
        {}
      );
      setConnectAllResults(data.results);
      await scan(); // refresh
    } catch (e: any) {
      setScanError(e.message || "Connect all failed");
    } finally {
      setConnectAllRunning(false);
    }
  };

  // --- Disconnect (placeholder) ---
  const disconnectPlatform = (name: string) => {
    // TODO: implement disconnect API
    alert(`Disconnect ${name} — coming soon`);
  };

  // --- Derived state ---
  const sorted = [...platforms].sort((a, b) => {
    // Connected first, then available, then missing
    const rank = (p: PlatformInfo) =>
      p.already_configured ? 0 : p.detected ? 1 : 2;
    return rank(a) - rank(b);
  });

  const connectedCount = platforms.filter((p) => p.already_configured).length;
  const detectedCount = platforms.filter((p) => p.detected).length;
  const hasUnconfigured = platforms.some((p) => p.detected && !p.already_configured);

  return (
    <div className="max-w-3xl mx-auto px-6 py-10">
      {/* --- Page Header --- */}
      <div className="mb-10">
        <div className="h-[2px] bg-gradient-to-r from-[#76B900] to-transparent mb-6" />
        <p className="section-label text-[#76B900] font-mono text-xs uppercase tracking-[0.2em] mb-2">
          Integrations
        </p>
        <h1 className="text-2xl font-semibold text-[--text-primary] mb-2">
          Connect your tools to nvHive
        </h1>
        <p className="text-sm text-[--text-secondary]">
          Route any AI tool through nvHive for smart model selection, council
          consensus, and cost optimization.
        </p>
      </div>

      {/* --- Scan & Connect All --- */}
      {!loading && (
        <div className="mb-8 p-6 border border-[--border] bg-[#111]">
          {connectAllResults ? (
            /* Results view */
            <div>
              <div className="flex items-center justify-between mb-4">
                <h2 className="font-medium text-[--text-primary]">
                  Setup Complete
                </h2>
                <button
                  onClick={() => setConnectAllResults(null)}
                  className="text-xs font-mono text-[--text-muted] hover:text-[--text-primary]"
                >
                  Dismiss
                </button>
              </div>
              <div className="space-y-2">
                {connectAllResults.map((r) => (
                  <div
                    key={r.platform}
                    className="flex items-center gap-3 text-sm"
                  >
                    <span className="w-4 text-center">
                      {r.success ? (
                        <span className="text-[#76B900]">&#10003;</span>
                      ) : r.action === "skipped" ? (
                        <span className="text-[--text-muted]">&middot;</span>
                      ) : (
                        <span className="text-[#ef4444]">&#10005;</span>
                      )}
                    </span>
                    <span className="text-[--text-primary]">
                      {r.display_name}
                    </span>
                    <span className="text-[--text-muted] text-xs font-mono">
                      {r.action === "skipped"
                        ? r.reason
                        : r.action === "connected"
                          ? "Connected"
                          : r.message || "Failed"}
                    </span>
                  </div>
                ))}
              </div>
              <p className="text-xs text-[--text-muted] mt-4 font-mono">
                {connectAllResults.filter((r) => r.success).length} of{" "}
                {connectAllResults.length} tools connected
              </p>
            </div>
          ) : (
            /* CTA view */
            <div className="flex items-center justify-between">
              <div>
                <h2 className="font-medium text-[--text-primary] mb-1">
                  {connectedCount === 0
                    ? "Get started"
                    : hasUnconfigured
                      ? "Connect remaining tools"
                      : "All tools connected"}
                </h2>
                <p className="text-sm text-[--text-secondary]">
                  {connectedCount === 0
                    ? "Detect installed tools and connect them automatically."
                    : `${connectedCount} connected, ${detectedCount - connectedCount} available`}
                </p>
              </div>
              {hasUnconfigured && (
                <button
                  onClick={connectAll}
                  disabled={connectAllRunning}
                  className={`
                    px-5 py-2.5 text-sm font-medium transition-all duration-150
                    ${connectAllRunning
                      ? "bg-[#76B900]/20 text-[#76B900]/60 cursor-wait"
                      : "bg-[#76B900] text-black hover:shadow-[0_0_15px_rgba(118,185,0,0.3)]"
                    }
                  `}
                >
                  {connectAllRunning
                    ? "Scanning..."
                    : "Scan & Connect All"}
                </button>
              )}
              {!hasUnconfigured && connectedCount > 0 && (
                <span className="text-sm text-[#76B900] font-mono">
                  &#10003; All set
                </span>
              )}
            </div>
          )}
        </div>
      )}

      {/* --- Error banner --- */}
      {scanError && (
        <div className="mb-6 p-4 border border-[#ef4444]/20 bg-[#ef4444]/5 text-sm text-[#ef4444]">
          {scanError}
          <button
            onClick={scan}
            className="ml-3 underline hover:no-underline"
          >
            Retry
          </button>
        </div>
      )}

      {/* --- Platform List --- */}
      {loading ? (
        <div className="space-y-1">
          {[1, 2, 3, 4, 5].map((i) => (
            <div
              key={i}
              className="h-[76px] bg-[#1a1a1a] animate-pulse border border-[--border]"
            />
          ))}
        </div>
      ) : (
        <div role="list" className="space-y-1">
          {sorted.map((p) => (
            <PlatformRow
              key={p.name}
              platform={p}
              onConnect={connectPlatform}
              onDisconnect={disconnectPlatform}
              connecting={!!connecting[p.name]}
              error={errors[p.name] || null}
            />
          ))}
        </div>
      )}

      {/* --- Refresh --- */}
      {!loading && (
        <div className="mt-4 flex justify-between items-center">
          <button
            onClick={scan}
            className="text-xs font-mono text-[--text-muted] hover:text-[#76B900] transition-colors"
          >
            &#8635; Refresh
          </button>
          <span className="text-xs text-[--text-muted] font-mono">
            {platforms.length} platforms &middot; {connectedCount} connected
          </span>
        </div>
      )}

      {/* --- Troubleshooting --- */}
      <details className="mt-10 group">
        <summary className="text-xs font-mono text-[--text-muted] cursor-pointer hover:text-[--text-secondary] select-none">
          Having trouble? View manual setup &darr;
        </summary>
        <div className="mt-4 space-y-4 text-sm">
          <div className="p-4 bg-[#111] border border-[--border]">
            <h3 className="font-mono text-xs text-[#76B900] uppercase tracking-wider mb-3">
              NemoClaw (Inference Provider)
            </h3>
            <pre className="text-xs font-mono text-[--text-secondary] whitespace-pre-wrap leading-relaxed">
{`# Start nvHive proxy
nvh nemoclaw --start

# Register with NemoClaw
openshell provider create \\
  --name nvhive --type openai \\
  --credential OPENAI_API_KEY=nvhive \\
  --config OPENAI_BASE_URL=http://host.openshell.internal:8000/v1/proxy

# Set as default
openshell inference set --provider nvhive --model auto`}
            </pre>
          </div>

          <div className="p-4 bg-[#111] border border-[--border]">
            <h3 className="font-mono text-xs text-[#76B900] uppercase tracking-wider mb-3">
              Claude Code (MCP Tools)
            </h3>
            <pre className="text-xs font-mono text-[--text-secondary] whitespace-pre-wrap leading-relaxed">
{`# Register nvHive as MCP server
claude mcp add nvhive nvhive-mcp`}
            </pre>
          </div>

          <div className="p-4 bg-[#111] border border-[--border]">
            <h3 className="font-mono text-xs text-[#76B900] uppercase tracking-wider mb-3">
              OpenClaw / Cursor (MCP Config)
            </h3>
            <pre className="text-xs font-mono text-[--text-secondary] whitespace-pre-wrap leading-relaxed">
{`# Add to openclaw.json or ~/.cursor/mcp.json:
{
  "mcpServers": {
    "nvhive": {
      "command": "nvhive-mcp"
    }
  }
}`}
            </pre>
          </div>

          <p className="text-xs text-[--text-muted]">
            CLI equivalent:{" "}
            <code className="font-mono">nvh integrate --auto</code>
          </p>
        </div>
      </details>
    </div>
  );
}
