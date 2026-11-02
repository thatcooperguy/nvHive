'use client';

/**
 * The empty Wizard thread: a title, an optional repair nudge, the agent
 * cards (each seeds an on-persona starter) and a grid of adaptive starters
 * from the reconnect payload. Extracted from the Wizard thread
 * (chat/WizardThread.tsx); the thread owns the draft and the profile, this
 * component only reports picks.
 */

import { useEffect, useState } from 'react';
import AgentAvatar from '@/components/AgentAvatar';
import type { AgentProfileSchema } from '@/lib/api';

// Per-agent starter prompts shown on the empty Wizard state. Picking a card
// both swaps the active profile AND seeds the composer with an on-persona
// question, so first-time users get a "click → ready to send" experience.
const AGENT_STARTERS: Record<string, string> = {
  wizard: 'What can my GPU run right now, and what should I fix first?',
  coder: 'Review the diff in my current branch — call out the riskiest change.',
  researcher: 'Summarize the latest on Llama 4 release vs Llama 3, with sources.',
  writer: 'Help me draft a launch announcement for an internal tool.',
  ops: 'Run a safe repair pass and tell me what changed.',
  'vault-rag': 'What did I write about my mount-autopilot setup last week?',
};

export default function EmptyState({
  onPickPrompt,
  profileMap,
  activeProfile,
  onPickAgent,
}: {
  onPickPrompt: (prompt: string) => void;
  profileMap: Map<string, AgentProfileSchema>;
  activeProfile: string;
  onPickAgent: (name: string) => void;
}) {
  // Adaptive starters — seeded from the reconnect payload so the Wizard's
  // suggestions match the user's actual workspace state. Falls back to the
  // generic set when reconnect is unavailable (cold first boot, offline, etc.).
  const [starters, setStarters] = useState<string[]>([
    'What can my GPU run right now?',
    "Why isn't Ollama responding?",
    'Help me pick a mission for image gen.',
    'Refresh the local model list.',
  ]);
  // Proactive repair nudge — when reconnect surfaces unhealthy receipts or
  // attention items we render a single-line banner above the agent grid so
  // the user sees the problem before they pick an agent. One-click = fire
  // off the repair-workspace tool via a "Fix this" prompt.
  const [proactiveNudge, setProactiveNudge] = useState<{ text: string; prompt: string } | null>(null);
  const agentList = Array.from(profileMap.values()).slice(0, 6);

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const { wizardReconnect } = await import('@/lib/api');
        const r = await wizardReconnect();
        if (cancelled) return;
        const adaptive: string[] = [];
        // First-run user → walk them through setup directly.
        if (r.first_run) {
          adaptive.push('Walk me through setting up this workspace from scratch.');
        }
        // Concrete attention items → surface the top one as a starter.
        for (const item of r.needs_attention.slice(0, 2)) {
          const title = (item as { title?: string }).title;
          if (typeof title === 'string' && title.length > 0) {
            adaptive.push(`Help me with: ${title}`);
          }
        }
        // Auto-repair already ran? Offer to summarize what just happened.
        if (r.auto_repaired.length > 0) {
          adaptive.push('What did you just auto-repair, and what should I check?');
        }
        // Vault present → invite recall-style questions.
        const vaultNotes = (r as unknown as { vault?: { memory_files?: number } })?.vault?.memory_files ?? 0;
        if (vaultNotes > 0) {
          adaptive.push('What did I write about in my vault recently?');
        }
        if (adaptive.length > 0) {
          // Always keep one generic fallback so the grid never looks empty.
          adaptive.push('What can my GPU run right now?');
          setStarters(adaptive.slice(0, 4));
        }

        // Proactive nudge: build a one-line "I noticed X looks broken — want
        // me to fix it?" message from the most important attention item. This
        // is the "self-healing" promise made visible.
        if (r.needs_attention.length > 0) {
          const top = r.needs_attention[0] as { title?: string; summary?: string };
          const summary = top.summary || top.title;
          if (summary) {
            setProactiveNudge({
              text: `Heads up — ${summary}.`,
              prompt: `Run a safe repair on the issue: ${top.title ?? summary}`,
            });
          }
        } else if ((r.auto_repaired ?? []).length > 0) {
          setProactiveNudge({
            text: `I auto-repaired ${r.auto_repaired.length} item(s) on reconnect.`,
            prompt: 'What did you just auto-repair, and is anything still broken?',
          });
        }
      } catch {
        // Reconnect unavailable — keep the static starters.
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);
  return (
    <div className="mx-auto max-w-2xl pt-6">
      <div className="text-center">
        <div className="text-[10px] font-mono uppercase tracking-[0.18em]" style={{ color: '#76B900' }}>
          AI Wizard
        </div>
        <div className="mt-2 text-lg font-semibold" style={{ color: 'var(--text-primary)' }}>
          Ask anything — the Wizard picks the specialist.
        </div>
        <div className="mt-1 text-xs leading-relaxed" style={{ color: 'var(--text-secondary)' }}>
          The Wizard reads your live GPU, storage, providers, and vault before
          it answers, and routes each question to a hidden specialist that can
          run repairs, refresh models, and validate keys. Pick a card below to
          pin one persona instead.
        </div>
      </div>

      {proactiveNudge && (
        <button
          type="button"
          onClick={() => onPickPrompt(proactiveNudge.prompt)}
          className="mt-4 flex w-full items-center justify-between gap-3 rounded-md border px-3 py-2 text-left text-xs transition-colors hover:border-[#d97706]"
          style={{
            borderColor: '#d97706',
            background: 'rgba(217,119,6,0.08)',
            color: '#92400e',
          }}
          title="Click to seed a repair request"
        >
          <span>{proactiveNudge.text}</span>
          <span className="font-mono text-[10px] uppercase tracking-[0.18em]" style={{ color: '#92400e' }}>
            Fix it →
          </span>
        </button>
      )}

      {agentList.length > 0 && (
        <div className="mt-4 grid grid-cols-2 gap-2 sm:grid-cols-3">
          {agentList.map(p => {
            const isActive = p.name === activeProfile;
            return (
              <button
                key={p.name}
                type="button"
                onClick={() => {
                  onPickAgent(p.name);
                  const starter = AGENT_STARTERS[p.name];
                  if (starter) onPickPrompt(starter);
                }}
                className="flex items-start gap-2 rounded-md border p-2 text-left transition-colors hover:border-[#76B900]/40"
                style={{
                  borderColor: isActive ? '#76B900' : 'var(--border)',
                  background: isActive ? 'rgba(118,185,0,0.05)' : 'var(--bg-card)',
                }}
                title={p.description}
              >
                <AgentAvatar profile={p} size="md" />
                <div className="min-w-0 flex-1">
                  <div
                    className="truncate text-xs font-mono font-bold"
                    style={{ color: 'var(--text-primary)' }}
                  >
                    {p.title}
                  </div>
                  <div
                    className="mt-0.5 line-clamp-2 text-[10px]"
                    style={{ color: 'var(--text-secondary)' }}
                  >
                    {p.description}
                  </div>
                </div>
              </button>
            );
          })}
        </div>
      )}

      <div className="mt-6 text-center">
        <div className="text-[10px] font-mono uppercase tracking-[0.18em]" style={{ color: 'var(--text-muted)' }}>
          Or jump straight in
        </div>
      </div>
      <div className="mt-4 grid grid-cols-1 gap-2 text-left sm:grid-cols-2">
        {starters.map(s => (
          <button
            key={s}
            type="button"
            onClick={() => onPickPrompt(s)}
            className="rounded-md border px-3 py-2 text-xs transition-colors hover:border-[#76B900]/40"
            style={{
              borderColor: 'var(--border)',
              background: 'var(--bg-card)',
              color: 'var(--text-secondary)',
            }}
          >
            {s}
          </button>
        ))}
      </div>
    </div>
  );
}
