'use client';

/**
 * AgentProfilePicker — the "pin a specialist" dropdown. With the concierge it lives
 * in the Wizard composer's *Advanced* disclosure, not the primary row: the
 * default is **Auto**, where the concierge picks a hidden specialist per turn
 * and the bubble credits it (docs/proposals/SPARK_CONCIERGE_2026-09.md §3.1).
 *
 * Options, in order:
 *   - `auto` — Auto — the Wizard picks a specialist (recommended)
 *   - core built-ins, with "AI Wizard (general)" as an EXPLICIT pin of the
 *     general persona (the API treats only null / "" / "auto" as auto)
 *   - one optgroup per Agent Library category, then Custom user profiles
 *
 * Selection is held by the parent and round-trips through the chat stream as
 * the `profile` field, so a pin applies per turn. The catalog comes from
 * /v1/wizard/profiles (built-ins + NVH_HOME/agent-profiles/).
 */

import { useEffect, useState } from 'react';
import AgentAvatar from '@/components/AgentAvatar';
import {
  AUTO_PROFILE,
  GENERAL_PROFILE,
  isAutoProfile,
  listAgentProfiles,
  type AgentProfileSchema,
} from '@/lib/api';

export const AUTO_OPTION_LABEL = 'Auto — the Wizard picks a specialist (recommended)';

interface Props {
  value: string;
  onChange: (name: string) => void;
  onCreateNew?: () => void;
}

/** The general persona is a pin like any other now; say so in its label. */
function optionLabel(p: AgentProfileSchema): string {
  return p.name === GENERAL_PROFILE ? 'AI Wizard (general)' : p.title;
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

  const auto = isAutoProfile(value);
  const active = auto ? undefined : profiles.find(p => p.name === value);
  // A pin that is not in the catalog (deep link to a removed profile) still
  // needs an <option> or the <select> would silently display the first one.
  const unknownPin = !auto && !active ? value : null;

  const core = profiles.filter(p => p.built_in && !p.category);
  const custom = profiles.filter(p => !p.built_in);
  const cats = new Map<string, AgentProfileSchema[]>();
  for (const p of profiles) {
    if (!p.built_in || !p.category) continue;
    const list = cats.get(p.category) ?? [];
    list.push(p);
    cats.set(p.category, list);
  }

  return (
    <div className="flex items-center gap-2 text-[10px] font-mono">
      {active ? (
        <AgentAvatar profile={active} size="sm" />
      ) : (
        <span
          aria-hidden
          title="Auto: the Wizard picks a specialist per turn"
          className="inline-block h-4 w-4 flex-shrink-0"
          style={{
            background: '#76B900',
            clipPath: 'polygon(50% 0%, 100% 25%, 100% 75%, 50% 100%, 0% 75%, 0% 25%)',
            opacity: 0.8,
          }}
        />
      )}
      <label htmlFor="agent-profile" style={{ color: 'var(--text-muted)' }}>
        Agent:
      </label>
      <select
        id="agent-profile"
        value={auto ? AUTO_PROFILE : value}
        onChange={(e) => onChange(e.target.value)}
        title={active?.description ?? 'The Wizard routes each question to the best-fitting specialist.'}
        className="rounded-sm border px-1 py-0.5 text-[10px] font-mono"
        style={{
          background: 'var(--bg-card)',
          borderColor: 'var(--border)',
          color: 'var(--text-primary)',
        }}
      >
        <option value={AUTO_PROFILE}>{AUTO_OPTION_LABEL}</option>
        {unknownPin && <option value={unknownPin}>{unknownPin}</option>}
        {/* Agent Library (2026-08-05): 100+ profiles need grouping —
            Core built-ins first, then one optgroup per library
            category, then Custom user profiles. */}
        {core.map(p => (
          <option key={p.name} value={p.name}>{optionLabel(p)}</option>
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
