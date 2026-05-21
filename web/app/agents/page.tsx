'use client';

/**
 * /agents — agent discovery surface.
 *
 * Lists every agent profile the WebUI knows about (built-ins + user-defined
 * YAML under NVH_HOME/agent-profiles/) as a browsable grid of cards. The
 * dropdown inside the Wizard composer is fine for swapping personas mid-chat,
 * but a fresh user lands on the chat page and never sees that the catalog
 * exists — so this page is the discoverable index of "who can I talk to?"
 *
 * Each card shows the persona avatar, title, description, capability tags,
 * the model + temperature it prefers, and (when set) the per-turn cost
 * ceiling. "Use this agent" deep-links into /wizard?profile=<name> so the
 * existing chat surface opens with the selected persona active.
 */

import Link from 'next/link';
import { useEffect, useState } from 'react';
import AgentAvatar from '@/components/AgentAvatar';
import CreateAgentModal from '@/components/CreateAgentModal';
import PageHeader from '@/components/PageHeader';
import { listAgentProfiles, type AgentProfileSchema } from '@/lib/api';

// Sort: built-ins (by name, ascending) first, then user-defined (by name,
// ascending). Built-ins are the curated default catalog so they should
// always be the first thing a user sees.
function sortProfiles(profiles: AgentProfileSchema[]): AgentProfileSchema[] {
  const sorted = [...profiles];
  sorted.sort((a, b) => {
    if (a.built_in !== b.built_in) return a.built_in ? -1 : 1;
    return a.name.localeCompare(b.name);
  });
  return sorted;
}

function formatTemperature(value: number | null | undefined): string | null {
  if (value === null || value === undefined) return null;
  if (!Number.isFinite(value)) return null;
  return value.toFixed(value < 1 ? 2 : 1);
}

function formatCost(value: number | null | undefined): string | null {
  if (value === null || value === undefined) return null;
  if (!Number.isFinite(value) || value <= 0) return null;
  // Keep the precision tight; cost ceilings are usually a few cents.
  return value < 1 ? `$${value.toFixed(2)}/turn` : `$${value.toFixed(2)}/turn`;
}

interface AgentCardProps {
  profile: AgentProfileSchema;
}

function AgentCard({ profile }: AgentCardProps) {
  const temperature = formatTemperature(profile.temperature);
  const cost = formatCost(profile.max_cost_usd_per_turn);
  const modelLabel = profile.model || (profile.provider ? `${profile.provider} (auto-pick)` : 'Router picks');

  return (
    <div className="flex h-full flex-col gap-3 rounded-lg border border-[#e5e5e5] bg-white p-4 transition-colors hover:border-[#76B900]/40 dark:border-[#262626] dark:bg-[#0a0a0a]">
      <div className="flex items-start gap-3">
        <AgentAvatar profile={profile} size="lg" />
        <div className="min-w-0 flex-1">
          <div className="flex items-start justify-between gap-2">
            <div className="min-w-0">
              <div className="truncate text-sm font-bold text-[#0a0a0a] dark:text-[#fafafa]">{profile.title}</div>
              <div className="truncate text-[10px] font-mono text-[#a3a3a3] dark:text-[#737373]">{profile.name}</div>
            </div>
            {profile.built_in ? (
              <span className="flex-shrink-0 rounded border border-[#76B900]/30 bg-[#76B900]/10 px-1.5 py-0.5 text-[9px] font-mono font-bold uppercase tracking-wider text-[#76B900]">
                Built-in
              </span>
            ) : (
              <span className="flex-shrink-0 rounded border border-[#d4d4d4] bg-[#f5f5f5] px-1.5 py-0.5 text-[9px] font-mono font-bold uppercase tracking-wider text-[#737373] dark:border-[#404040] dark:bg-[#141414] dark:text-[#a3a3a3]">
                Custom
              </span>
            )}
          </div>
        </div>
      </div>

      {profile.description && (
        <p className="text-xs leading-relaxed text-[#525252] dark:text-[#a3a3a3]">{profile.description}</p>
      )}

      {profile.tags.length > 0 && (
        <div className="flex flex-wrap gap-1">
          {profile.tags.map(tag => (
            <span
              key={tag}
              className="rounded border border-[#e5e5e5] bg-[#fafafa] px-1.5 py-0.5 text-[9px] font-mono uppercase tracking-wider text-[#737373] dark:border-[#262626] dark:bg-[#141414] dark:text-[#a3a3a3]"
            >
              {tag}
            </span>
          ))}
        </div>
      )}

      <dl className="grid grid-cols-1 gap-1 text-[10px] font-mono text-[#737373] dark:text-[#a3a3a3]">
        <div className="flex items-baseline justify-between gap-2">
          <dt className="text-[#a3a3a3] dark:text-[#737373]">Model</dt>
          <dd className="truncate text-right text-[#0a0a0a] dark:text-[#fafafa]" title={modelLabel}>{modelLabel}</dd>
        </div>
        {temperature && (
          <div className="flex items-baseline justify-between gap-2">
            <dt className="text-[#a3a3a3] dark:text-[#737373]">Temperature</dt>
            <dd className="text-[#0a0a0a] dark:text-[#fafafa]">{temperature}</dd>
          </div>
        )}
        {cost && (
          <div className="flex items-baseline justify-between gap-2">
            <dt className="text-[#a3a3a3] dark:text-[#737373]">Cost ceiling</dt>
            <dd className="text-[#d97706]">{cost}</dd>
          </div>
        )}
      </dl>

      <div className="mt-auto flex items-center gap-2 pt-1">
        <Link
          href={`/wizard?profile=${encodeURIComponent(profile.name)}`}
          className="flex-1 rounded bg-[#76B900] px-3 py-1.5 text-center text-[10px] font-mono font-bold uppercase tracking-wider text-black transition-colors hover:bg-[#5a9100]"
        >
          Use this agent
        </Link>
      </div>
    </div>
  );
}

export default function AgentsPage() {
  const [profiles, setProfiles] = useState<AgentProfileSchema[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [createOpen, setCreateOpen] = useState(false);
  const [reloadKey, setReloadKey] = useState(0);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    void (async () => {
      try {
        const result = await listAgentProfiles();
        if (cancelled) return;
        setProfiles(sortProfiles(result.profiles));
      } catch (err) {
        if (cancelled) return;
        setError(err instanceof Error ? err.message : 'Could not load agents');
        setProfiles([]);
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [reloadKey]);

  const builtInCount = profiles.filter(p => p.built_in).length;
  const customCount = profiles.length - builtInCount;

  const subtitle = loading
    ? 'Loading agent catalog…'
    : profiles.length === 0
      ? 'No agents available yet — create your first one.'
      : `${profiles.length} agent${profiles.length === 1 ? '' : 's'} (${builtInCount} built-in${customCount ? `, ${customCount} custom` : ''})`;

  return (
    <div>
      <PageHeader
        eyebrow="Agents"
        title="Agent catalog"
        subtitle={subtitle}
        trailing={
          <button
            type="button"
            onClick={() => setCreateOpen(true)}
            className="rounded bg-[#76B900] px-3 py-2 text-xs font-mono font-bold uppercase tracking-wider text-black transition-colors hover:bg-[#5a9100]"
          >
            + New agent
          </button>
        }
      />
      <div className="mx-auto max-w-6xl space-y-6 p-6">
        <p className="text-xs leading-relaxed text-[#525252] dark:text-[#a3a3a3]">
          Each agent is a saved persona + preferred LLM mapping. Built-ins ship with nvHive and route through whichever provider is available on your workspace.
          Custom agents live as YAML under <code className="rounded bg-[#f5f5f5] px-1 py-0.5 font-mono text-[10px] text-[#737373] dark:bg-[#141414] dark:text-[#a3a3a3]">NVH_HOME/agent-profiles/</code> so they survive reconnects.
        </p>

        {error && (
          <div
            className="rounded border px-3 py-2 text-xs font-mono"
            style={{ borderColor: '#dc2626', background: 'rgba(220,38,38,0.08)', color: '#dc2626' }}
          >
            <div className="font-bold uppercase tracking-wider">Could not load agents</div>
            <div className="mt-1 text-[10px] text-[#525252] dark:text-[#a3a3a3]">{error}</div>
            <button
              type="button"
              onClick={() => setReloadKey(k => k + 1)}
              className="mt-2 rounded bg-[#76B900] px-2 py-1 text-[10px] font-bold uppercase tracking-wider text-black"
            >
              Retry
            </button>
          </div>
        )}

        {loading ? (
          <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
            {[0, 1, 2, 3, 4, 5].map(i => (
              <div key={i} className="h-56 animate-pulse rounded-lg border border-[#e5e5e5] bg-[#fafafa] dark:border-[#262626] dark:bg-[#141414]" />
            ))}
          </div>
        ) : profiles.length === 0 && !error ? (
          <div className="rounded-lg border border-[#e5e5e5] bg-white p-10 text-center dark:border-[#262626] dark:bg-[#0a0a0a]">
            <div className="mb-2 text-2xl text-[#a3a3a3] dark:text-[#737373]">◇</div>
            <div className="text-sm font-mono font-bold uppercase tracking-wider text-[#525252] dark:text-[#a3a3a3]">
              No agents available
            </div>
            <div className="mx-auto mt-2 max-w-md text-[11px] font-mono text-[#a3a3a3] dark:text-[#737373]">
              The catalog came back empty. The Hive API normally ships with at least the default Wizard persona — check that the API is online.
            </div>
            <button
              type="button"
              onClick={() => setCreateOpen(true)}
              className="mt-4 rounded bg-[#76B900] px-3 py-2 text-xs font-mono font-bold uppercase tracking-wider text-black transition-colors hover:bg-[#5a9100]"
            >
              + Create your first agent
            </button>
          </div>
        ) : (
          <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
            {profiles.map(profile => (
              <AgentCard key={profile.name} profile={profile} />
            ))}
          </div>
        )}
      </div>

      <CreateAgentModal
        open={createOpen}
        onClose={() => setCreateOpen(false)}
        onCreated={() => {
          // Refresh the catalog so the new card shows up without a full reload.
          setReloadKey(k => k + 1);
        }}
      />
    </div>
  );
}
