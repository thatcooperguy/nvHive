'use client';

/**
 * The empty Wizard thread: a title, an optional repair nudge, the agent
 * cards (each seeds an on-persona starter) and a grid of adaptive starters
 * from the reconnect payload. Extracted from the Wizard thread
 * (chat/WizardThread.tsx); the thread owns the draft and the profile, this
 * component only reports picks.
 */

import Link from 'next/link';
import { useEffect, useState } from 'react';
import AgentAvatar from '@/components/AgentAvatar';
import { getInstallJobs, getInstalledModels, getWizardContext, type AgentProfileSchema, type InstalledModel } from '@/lib/api';
import { FALLBACK_STARTERS, firstRunLines } from '@/lib/firstRun';
import { sayMascotTip } from '@/lib/mascot';
import type { InstallJob } from '@/lib/types';

const FIRST_RUN_TIP_KEY = 'nvh.firstRun.tip';

/** The first installed Ollama chat model (`GET /v1/ollama/models`), skipping
 * embedding models, or null. `GET /v1/models` is the cloud catalog and has no
 * local rows, so it cannot answer this. */
function firstLocalModel(models: InstalledModel[]): string | null {
  const row = models.find(m => !/embed/i.test(m.name)) ?? models[0];
  return row ? row.name : null;
}

/** A running pull job's model and size, from the install job list, or null. */
function runningPull(jobs: InstallJob[]): { model: string; bytes: number } | null {
  const job = jobs.find(
    j => (j.status === 'running' || j.status === 'queued')
      && (j.kind === 'wizard-mission' || j.kind === 'studio-model-install'),
  );
  if (!job) return null;
  const req = job.request ?? {};
  const model = typeof req.model === 'string' && req.model ? req.model : job.title;
  const bytes = typeof req.size_bytes === 'number' ? req.size_bytes : 0;
  return { model, bytes };
}

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
  // suggestions match the user's actual workspace state. Null falls back to
  // the agent profiles' `starter` fields (lib/firstRun.ts holds the
  // last-resort list) when reconnect is unavailable (cold boot, offline).
  const [adaptive, setAdaptive] = useState<string[] | null>(null);
  // Machine state behind the first-run model line.
  const [localModel, setLocalModel] = useState<string | null>(null);
  const [pulling, setPulling] = useState<{ model: string; bytes: number } | null>(null);
  const [gpu, setGpu] = useState(false);
  // Proactive repair nudge — when reconnect surfaces unhealthy receipts or
  // attention items we render a single-line banner above the agent grid so
  // the user sees the problem before they pick an agent. One-click = fire
  // off the repair-workspace tool via a "Fix this" prompt.
  const [proactiveNudge, setProactiveNudge] = useState<{ text: string; prompt: string } | null>(null);
  const agentList = Array.from(profileMap.values()).slice(0, 6);
  const lines = firstRunLines({
    localModel,
    pulling,
    gpu,
    // No call on this surface exposes the key count, so it stays unknown (0)
    // and no line depends on it.
    keyCount: 0,
    starters: Array.from(profileMap.values()).map(p => p.starter ?? ''),
  });
  const starters = adaptive ?? lines.starters;

  // First-run state: local model, running pull, GPU. Each call is
  // best-effort; a failure leaves the line on its "no model yet" wording.
  useEffect(() => {
    let cancelled = false;
    void (async () => {
      const [models, jobs, context] = await Promise.all([
        getInstalledModels().catch(() => null),
        getInstallJobs({ limit: 8, exclude_kind: 'schedule-run' }).catch(() => null),
        getWizardContext().catch(() => null),
      ]);
      if (cancelled) return;
      if (models) setLocalModel(firstLocalModel(models.models));
      if (jobs) setPulling(runningPull(jobs.jobs));
      const gpuBlock = (context as { gpu?: { detected?: boolean } } | null)?.gpu;
      setGpu(gpuBlock?.detected === true);
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  // One mascot tip per session on the first run. The session key is written
  // only once the bubble actually showed (a hidden mascot consumes nothing);
  // storage may be blocked, then the tip's own id limits it to once per load.
  useEffect(() => {
    try {
      if (window.sessionStorage.getItem(FIRST_RUN_TIP_KEY)) return;
    } catch {
      /* storage blocked */
    }
    if (!sayMascotTip('Ask me what this machine can do', { id: 'first-run' })) return;
    try {
      window.sessionStorage.setItem(FIRST_RUN_TIP_KEY, '1');
    } catch {
      /* storage blocked */
    }
  }, []);

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const { wizardReconnect } = await import('@/lib/api');
        const r = await wizardReconnect();
        if (cancelled) return;
        const adaptiveList: string[] = [];
        // First-run user → walk them through setup directly.
        if (r.first_run) {
          adaptiveList.push('Walk me through setting up this workspace from scratch.');
        }
        // Concrete attention items → surface the top one as a starter.
        for (const item of r.needs_attention.slice(0, 2)) {
          const title = (item as { title?: string }).title;
          if (typeof title === 'string' && title.length > 0) {
            adaptiveList.push(`Help me with: ${title}`);
          }
        }
        // Auto-repair already ran? Offer to summarize what just happened.
        if (r.auto_repaired.length > 0) {
          adaptiveList.push('What did you just auto-repair, and what should I check?');
        }
        // Vault present → invite recall-style questions.
        const vaultNotes = (r as unknown as { vault?: { memory_files?: number } })?.vault?.memory_files ?? 0;
        if (vaultNotes > 0) {
          adaptiveList.push('What did I write about in my vault recently?');
        }
        if (adaptiveList.length > 0) {
          // Always keep one generic fallback so the grid never looks empty.
          adaptiveList.push(FALLBACK_STARTERS[0]);
          setAdaptive(adaptiveList.slice(0, 4));
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
                  const starter = p.starter?.trim();
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

      <div
        className="mt-4 text-center text-xs"
        style={{ color: 'var(--text-secondary)' }}
        data-testid="first-run-model-line"
      >
        {lines.model}
      </div>

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
      <div className="mt-4 text-center text-[11px]" style={{ color: 'var(--text-muted)' }}>
        <Link href="/setup" className="underline-offset-2 hover:underline">
          {lines.later}
        </Link>
      </div>
    </div>
  );
}
