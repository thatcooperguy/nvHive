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
 */

import { useEffect, useState } from 'react';
import { listAgentProfiles, type AgentProfileSchema } from '@/lib/api';

interface Props {
  value: string;
  onChange: (name: string) => void;
}

export default function AgentProfilePicker({ value, onChange }: Props) {
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
    <div className="flex items-center gap-1 text-[10px] font-mono">
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
        {profiles.map(p => (
          <option key={p.name} value={p.name}>
            {p.title}
            {p.built_in ? '' : ' (custom)'}
          </option>
        ))}
      </select>
    </div>
  );
}
