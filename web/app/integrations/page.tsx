"use client";

import { useCallback, useEffect, useState } from "react";
import PageHeader from "@/components/PageHeader";

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
  { subtitle: string; badge?: string }
> = {
  nemoclaw: {
    subtitle: "AI agents on NVIDIA GPUs",
    badge: "NVIDIA",
  },
  openclaw: {
    subtitle: "Open-source AI coding assistant",
  },
  claude_code: {
    subtitle: "Anthropic\u2019s CLI coding agent",
  },
  cursor: {
    subtitle: "AI-powered code editor",
  },
  claude_desktop: {
    subtitle: "Anthropic\u2019s desktop app",
  },
};

function platformInitials(platform: PlatformInfo): string {
  const known: Record<string, string> = {
    nemoclaw: "NC",
    openclaw: "OC",
    claude_code: "CC",
    cursor: "CU",
    claude_desktop: "CD",
  };
  return known[platform.name] ?? platform.display_name.slice(0, 2).toUpperCase();
}

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
    error: "bg-[#dc2626] shadow-[0_0_6px_rgba(239,68,68,0.4)]",
    connecting: "bg-[#d97706] animate-pulse",
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
  connecting,
  error,
}: {
  platform: PlatformInfo;
  onConnect: (name: string) => void;
  connecting: boolean;
  error: string | null;
}) {
  const meta = PLATFORM_META[platform.name] || {
    subtitle: platform.integration_type === "mcp" ? "Tool connection" : "Inference provider",
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
      <div className="flex-shrink-0 w-10 h-10 flex items-center justify-center bg-[#f5f5f5] text-[10px] font-mono font-bold text-[#525252] dark:bg-[#141414] dark:text-[#a3a3a3]">
        {platformInitials(platform)}
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
          <p className="text-xs text-[#dc2626] mt-1 font-mono">{error}</p>
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
          <span
            className="inline-flex px-3 py-1.5 text-xs font-mono text-[#76B900] border border-[#76B900]/20 bg-[#76B900]/5"
            title="nvHive found this tool's configuration. Disconnect is managed inside the tool itself."
          >
            Connected
          </span>
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
          <span className="text-xs font-mono text-[#d97706]">
            Connecting...
          </span>
        )}
        {status === "error" && (
          <button
            onClick={() => onConnect(platform.name)}
            className="text-xs font-mono text-[#d97706] hover:bg-[#d97706]/10 transition-colors px-3 py-1.5 border border-[#d97706]/30"
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
    <div>
      <PageHeader
        eyebrow="Developer Tools"
        title="Connect developer tools to nvHive"
        subtitle="Use nvHive inside coding tools after the core local AI setup is working."
      />
      {/* Narrow-form content: platform rows + manual-setup snippets read best
          single-column, so this page intentionally keeps max-w-3xl. */}
      <div className="max-w-3xl mx-auto p-6">

      {/* --- Scan & Connect All --- */}
      {!loading && (
        <div className="mb-8 p-6 border border-[--border] bg-white dark:bg-[#0a0a0a]">
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
                        <span className="text-[#dc2626]">&#10005;</span>
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
                    : "Find Installed Tools"}
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
        <div className="mb-6 p-4 border border-[#dc2626]/20 bg-[#dc2626]/5 text-sm text-[#dc2626]">
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
              className="h-[76px] bg-[#f5f5f5] animate-pulse border border-[--border] dark:bg-[#141414]"
            />
          ))}
        </div>
      ) : (
        <div role="list" className="space-y-1">
          {sorted.length === 0 ? (
            <div className="border border-[--border] bg-white p-6 text-sm text-[--text-secondary] dark:bg-[#0a0a0a]">
              No developer tools were detected yet. Install Cursor, Claude Code, OpenClaw, or NemoClaw, then check again.
            </div>
          ) : (
            sorted.map((p) => (
              <PlatformRow
                key={p.name}
                platform={p}
                onConnect={connectPlatform}
                connecting={!!connecting[p.name]}
                error={errors[p.name] || null}
              />
            ))
          )}
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
            <pre className="text-xs font-mono text-[#d4d4d4] whitespace-pre-wrap leading-relaxed">
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
            <pre className="text-xs font-mono text-[#d4d4d4] whitespace-pre-wrap leading-relaxed">
{`# Register nvHive as MCP server
claude mcp add nvhive nvhive-mcp`}
            </pre>
          </div>

          <div className="p-4 bg-[#111] border border-[--border]">
            <h3 className="font-mono text-xs text-[#76B900] uppercase tracking-wider mb-3">
              OpenClaw / Cursor (MCP Config)
            </h3>
            <pre className="text-xs font-mono text-[#d4d4d4] whitespace-pre-wrap leading-relaxed">
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

      {/* --- External MCP tool servers (roadmap critical #1, 2026-08-05) ---
          The reverse direction of the section above: instead of exposing
          nvHive AS an MCP server to coding tools, attach EXTERNAL MCP
          servers so their tools appear in the Wizard's toolset. */}
      <McpServersSection />
      </div>
    </div>
  );
}

interface McpServerStatus {
  name: string;
  command: string;
  auto_approve: string[];
  cached: boolean;
  ok: boolean;
  error: string | null;
  refreshed_at: string | null;
  tool_count: number;
  tools: string[];
}

function McpServersSection() {
  const [servers, setServers] = useState<McpServerStatus[] | null>(null);
  const [configured, setConfigured] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const data = await apiGet<{ configured: boolean; servers: McpServerStatus[] }>(
        "/v1/mcp/servers",
      );
      setConfigured(data.configured);
      setServers(data.servers);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      setServers([]);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const refresh = async () => {
    setRefreshing(true);
    try {
      await apiPost<{ servers: McpServerStatus[] }>("/v1/mcp/refresh", {});
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setRefreshing(false);
    }
  };

  return (
    <div className="mt-8 p-6 border border-[--border] bg-white dark:bg-[#0a0a0a]">
      <div className="flex items-center justify-between mb-2">
        <h2 className="font-medium text-[--text-primary]">External MCP tool servers</h2>
        {configured && (
          <button
            onClick={() => void refresh()}
            disabled={refreshing}
            className="text-xs font-mono px-2 py-1 border border-[--border] text-[--text-muted] hover:text-[--text-primary] disabled:opacity-40"
          >
            {refreshing ? "Refreshing…" : "Refresh tools"}
          </button>
        )}
      </div>
      <p className="text-xs text-[--text-muted] mb-4">
        Attach external Model Context Protocol servers and their tools appear in the
        AI Wizard&apos;s toolset (named <code className="font-mono">mcp_&lt;server&gt;_&lt;tool&gt;</code>,
        confirm-before-run by default). Config lives at{" "}
        <code className="font-mono">$NVH_HOME/config/mcp-servers.json</code> — same
        <code className="font-mono"> mcpServers</code> format as Claude Desktop.
      </p>
      {error && (
        <p className="text-xs font-mono text-[#dc2626]">{error}</p>
      )}
      {servers !== null && !configured && !error && (
        <p className="text-xs font-mono text-[--text-muted]">
          No servers configured yet. Create the config file, then run{" "}
          <code>nvh mcp refresh</code> or click Refresh tools.
        </p>
      )}
      {servers?.map(s => (
        <div key={s.name} className="mb-2 flex items-baseline gap-3 text-xs font-mono">
          <StatusDot status={s.ok ? "connected" : s.cached ? "error" : "available"} />
          <span className="font-bold text-[--text-primary]">{s.name}</span>
          <span className="text-[--text-muted]">{s.command}</span>
          {s.ok ? (
            <span className="text-[--text-muted]">
              {s.tool_count} tools{s.auto_approve.length ? ` (${s.auto_approve.length} auto-approved)` : ""}
            </span>
          ) : s.cached ? (
            <span className="text-[#dc2626] truncate" title={s.error ?? ""}>{s.error}</span>
          ) : (
            <span className="text-[--text-muted]">not refreshed yet</span>
          )}
        </div>
      ))}
    </div>
  );
}
