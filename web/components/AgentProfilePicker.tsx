'use client';

/**
 * AgentProfilePicker — compact dropdown in the Wizard composer that lets the
 * user swap personas mid-conversation. Pulls the catalog from
 * /v1/wizard/profiles so it includes both built-ins (Code Reviewer, Research
 * Assistant, Long-form Writer, Workspace Operator, Notes RAG Agent, …) and
 * any user-defined profiles dropped into NVH_HOME/agent-profiles/.
 *
 * Selection is held by the parent and round-trips through wizard_chat as the
 * `profile` field, so the persona + LLM mapping apply per turn.
 *
 * Renders the active profile's avatar inline so the picker doubles as a
 * persona reminder while the user is typing.
 */

import { useEffect, useState } from 'react';
import AgentAvatar from '@/components/AgentAvatar';
import { listAgentProfiles, type AgentProfileSchema } from '@/lib/api';

interface Props {
  value: string;
  onChange: (name: string) => void;
  onCreateNew?: () => void;
}

export default function AgentProfilePicker({ value, onChange, onCreateNew }: Props) {
  const [profiles, setProfiles] = useState<AgentProfileSchema[]>([]);

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const result = await listAgentProfiles();
        if (cancelled) return;
        setProfiles(result.profiles);
      } catch {
        // Endpoint missing on older builds — render an empty picker.
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  if (profiles.length === 0) return null;

  const active = profiles.find(p => p.name === value);
  return (
    <div className="flex items-center gap-2 text-[10px] font-mono">
      <AgentAvatar profile={active} size="sm" />
      <label htmlFor="agent-profile" style={{ color: 'var(--text-muted)' }}>
        Agent:
      </label>
      <select
        id="agent-profile"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        title={active?.description ?? ''}
        className="rounded-sm border px-1 py-0.5 text-[10px] font-mono"
        style={{
          background: 'var(--bg-card)',
          borderColor: 'var(--border)',
          color: 'var(--text-primary)',
        }}
      >
        {/* Agent Library (2026-08-05): 100+ profiles need grouping —
            Core built-ins first, then one optgroup per library
            category, then Custom user profiles. */}
        {(() => {
          const core = profiles.filter(p => p.built_in && !p.category);
          const custom = profiles.filter(p => !p.built_in);
          const cats = new Map<string, typeof profiles>();
          for (const p of profiles) {
            if (!p.built_in || !p.category) continue;
            const list = cats.get(p.category) ?? [];
            list.push(p);
            cats.set(p.category, list);
          }
          return (
            <>
              {core.map(p => (
                <option key={p.name} value={p.name}>{p.title}</option>
              ))}
              {[...cats.keys()].sort((a, b) => a.localeCompare(b)).map(cat => (
                <optgroup key={cat} label={cat}>
                  {cats.get(cat)!.map(p => (
                    <option key={p.name} value={p.name}>{p.title}</option>
                  ))}
                </optgroup>
              ))}
              {custom.length > 0 && (
                <optgroup label="Custom">
                  {custom.map(p => (
                    <option key={p.name} value={p.name}>{p.title}</option>
                  ))}
                </optgroup>
              )}
            </>
          );
        })()}
      </select>
      {onCreateNew && (
        <button
          type="button"
          onClick={onCreateNew}
          className="ml-1 text-[10px] font-mono transition-colors hover:text-[#76B900]"
          style={{ color: 'var(--text-muted)' }}
          title="Create a new agent profile"
        >
          + New
        </button>
      )}
    </div>
  );
}
