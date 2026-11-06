'use client';

/**
 * /schedules — the durable schedules the API server runs while it is up
 * (0.46 §6; owner decision O8: its own route and sidebar entry).
 *
 * Everything on the page is read from `GET /v1/schedules` (the records plus
 * the runner block: who holds the heartbeat lock and the read-only tool
 * roster an unattended turn may call) and written through the Wave B routes
 * in lib/api.ts: `createSchedule`, `updateSchedule` (the Enable / Disable
 * toggle), `deleteSchedule`, `runSchedule` ("Run now" — a 409 while a run is
 * still in flight surfaces as the in-flight job id, never a silent failure)
 * and `listScheduleRuns` for the run drawer. A run started from here is
 * tailed live through `watchInstallJob<ScheduleRunEvent>` like any other job;
 * when the watcher loses the API the panel says so in amber and offers
 * Reconnect — the run itself keeps going on the server, so that is never
 * drawn as a failed job.
 *
 * The add form offers `ask` and `convene` only: `mode: do` runs tools
 * unattended and the API runner refuses it, so it is CLI-only
 * (`nvh schedule start` in a terminal). The profile list comes from
 * `/v1/wizard/profiles`; the provider override (`advisor`) stays with the CLI,
 * so no provider list is typed here (tests/test_single_provider_table.py).
 *
 * "Open thread" opens the one conversation every run of a schedule appends
 * to. THREAD_LINK_PREFIX is the single place C1b (the merged chat surface)
 * changes to `/?c=`.
 */

import Link from 'next/link';
import { Fragment, useCallback, useEffect, useRef, useState } from 'react';
import PageHeader from '@/components/PageHeader';
import {
  createSchedule,
  deleteSchedule,
  getSchedules,
  listAgentProfiles,
  listScheduleRuns,
  runSchedule,
  updateSchedule,
  watchInstallJob,
  type AgentProfileSchema,
} from '@/lib/api';
import { costPill, type CostPill } from '@/lib/cost';
import {
  EVERY_PLACEHOLDER,
  answerPreview,
  formatInterval,
  lastCostPill,
  lastStatusLabel,
  lastStatusTone,
  nextRunLabel,
  outcomeFromEvent,
  relativeAgo,
  runOutcomeOf,
  runnerLabel,
  runnerTone,
  scheduleDisplayName,
  validateEvery,
  type RunOutcome,
  type StatusTone,
} from '@/lib/schedules';
import type { InstallJob, Schedule, ScheduleMode, ScheduleRunEvent, SchedulesResult } from '@/lib/types';

/** Deep link to a schedule's thread. C1b changes this one constant to `/?c=`. */
const THREAD_LINK_PREFIX = '/?c=';
/** How often the list is re-read while the page is open. */
const REFRESH_MS = 30_000;
/** Rows the run drawer asks for (the server keeps `runner.history` per schedule). */
const RUN_LOG_LIMIT = 20;

function threadHref(conversationId: string): string {
  return `${THREAD_LINK_PREFIX}${encodeURIComponent(conversationId)}`;
}

const TONE_TEXT: Record<StatusTone, string> = {
  green: 'text-[#76B900]',
  amber: 'text-[#d97706]',
  red: 'text-[#dc2626]',
  gray: 'text-[#737373] dark:text-[#a3a3a3]',
};

const TONE_DOT: Record<StatusTone, string> = {
  green: 'status-dot-green',
  amber: 'status-dot-amber',
  red: 'status-dot-red',
  gray: 'status-dot-gray',
};

const LABEL_CLASS = 'text-[10px] font-mono uppercase tracking-[0.18em] text-[#737373] dark:text-[#a3a3a3]';
const PANEL_CLASS = 'border border-[#e5e5e5] bg-white dark:bg-[#0a0a0a] dark:border-[#262626]';
const SMALL_BUTTON = 'px-2 py-1 text-[10px] font-mono uppercase tracking-wider';

/** The three-state cost pill; `null` renders nothing (a never-run schedule shows no cost). */
function CostPillSpan({ pill }: { pill: CostPill | null }) {
  if (!pill) return null;
  const cls =
    pill.state === 'unknown'
      ? 'border-[#d97706]/40 text-[#d97706]'
      : 'border-[#e5e5e5] text-[#525252] dark:border-[#262626] dark:text-[#a3a3a3]';
  return (
    <span title={pill.title} className={`inline-block border px-1.5 py-0.5 font-mono text-[10px] ${cls}`}>
      {pill.text}
    </span>
  );
}

function InlineNotice({ text, tone = 'gray' }: { text: string; tone?: StatusTone }) {
  const cls =
    tone === 'red'
      ? 'border-[#dc2626]/30 bg-[#dc2626]/5 text-[#dc2626]'
      : tone === 'amber'
        ? 'border-[#d97706]/30 bg-[#d97706]/5 text-[#d97706]'
        : tone === 'green'
          ? 'border-[#76B900]/30 bg-[#76B900]/5 text-[#447000] dark:text-[#76B900]'
          : 'border-[#e5e5e5] bg-[#fafafa] text-[#525252] dark:border-[#262626] dark:bg-[#141414] dark:text-[#a3a3a3]';
  return <div className={`border px-3 py-2 text-xs font-mono break-words ${cls}`}>{text}</div>;
}

/** The run started from this page, tailed through `watchInstallJob`. */
interface LiveRun {
  jobId: string;
  status: string;
  /** `step` messages in order (`Running <name> (mode ask)`). */
  steps: string[];
  /** Set by the `complete` or `error` event; null while the run is going. */
  outcome: RunOutcome | null;
  /** The job itself ended badly: `watchInstallJob.onError` WITH the job
   * (failed / canceled / interrupted). Red, settled. */
  error: string | null;
  /** The watcher lost the API (six polls failed) while the job kept running:
   * `onError` WITHOUT a job. Amber, not settled — Reconnect re-tails it. */
  lostContact: string | null;
}

function patchLive(
  prev: Record<string, LiveRun>,
  id: string,
  patch: Partial<LiveRun> | ((current: LiveRun) => Partial<LiveRun>),
): Record<string, LiveRun> {
  const current = prev[id];
  if (!current) return prev;
  const next = typeof patch === 'function' ? patch(current) : patch;
  return { ...prev, [id]: { ...current, ...next } };
}

function OutcomeDetails({ outcome, previewLines, previewChars }: { outcome: RunOutcome; previewLines: number; previewChars: number }) {
  const preview = answerPreview(outcome.answer, previewLines, previewChars);
  return (
    <>
      {outcome.outcome === 'error' && outcome.message && (
        <div className="mt-1 whitespace-pre-wrap break-words font-mono text-xs text-[#dc2626]">{outcome.message}</div>
      )}
      {outcome.outcome === 'skipped' && outcome.message && (
        <div className="mt-1 font-mono text-xs text-[#d97706]">{outcome.message}</div>
      )}
      {preview && (
        <div className="mt-1 whitespace-pre-wrap break-words text-xs text-[#525252] dark:text-[#a3a3a3]">{preview}</div>
      )}
      {outcome.deferredNames.length > 0 && (
        <div className="mt-1 font-mono text-xs text-[#d97706]">
          deferred: {outcome.deferredNames.join(', ')} — waiting for your approval in the thread
        </div>
      )}
    </>
  );
}

function LiveRunPanel({ live, onReconnect }: { live: LiveRun; onReconnect: () => void }) {
  const { outcome } = live;
  const settled = Boolean(outcome) || Boolean(live.error);
  // Lost contact is neither settled nor a failure: the server-side run goes
  // on, only this tab's watcher stopped.
  const lost = !settled && Boolean(live.lostContact);
  const tone: StatusTone = outcome ? lastStatusTone(outcome.outcome) : live.error ? 'red' : 'amber';
  const headline = outcome
    ? lastStatusLabel(outcome.outcome)
    : live.error
      ? 'error'
      : lost
        ? 'lost contact'
        : live.status === 'queued'
          ? 'queued'
          : 'running';
  return (
    <div className={`${PANEL_CLASS} p-3`}>
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 font-mono text-xs">
        <span className={LABEL_CLASS}>This run</span>
        <span className={`inline-flex items-center gap-1.5 ${TONE_TEXT[tone]}`}>
          <span className={`${TONE_DOT[tone]} ${settled || lost ? '' : 'animate-pulse'}`} aria-hidden="true" />
          {headline}
        </span>
        <span className="text-[#a3a3a3] dark:text-[#737373]">job {live.jobId}</span>
        {outcome && <CostPillSpan pill={costPill(outcome.costUsd, outcome.costKnown ?? undefined)} />}
        {outcome?.conversationId && (
          <Link href={threadHref(outcome.conversationId)} className="text-[#76B900] hover:underline">
            Open thread
          </Link>
        )}
      </div>
      {live.steps.length > 0 && (
        <ul className="mt-2 space-y-0.5 font-mono text-[11px] text-[#737373] dark:text-[#a3a3a3]">
          {live.steps.map((step, index) => (
            <li key={`${index}-${step}`}>{step}</li>
          ))}
        </ul>
      )}
      {live.error && !outcome && (
        <div className="mt-2 whitespace-pre-wrap break-words font-mono text-xs text-[#dc2626]">{live.error}</div>
      )}
      {lost && (
        <div className="mt-2 flex flex-wrap items-center gap-2 font-mono text-xs text-[#d97706]">
          <span className="break-words">
            Lost contact with the API while tailing this run ({live.lostContact}); the run itself keeps going on the server.
          </span>
          <button type="button" onClick={onReconnect} className={`btn-ghost ${SMALL_BUTTON}`}>
            Reconnect
          </button>
        </div>
      )}
      {outcome && <OutcomeDetails outcome={outcome} previewLines={6} previewChars={600} />}
    </div>
  );
}

function RunRow({ job, now }: { job: InstallJob; now: number }) {
  const outcome = runOutcomeOf(job);
  const tone = lastStatusTone(outcome.outcome);
  const started = job.started_at || job.created_at;
  return (
    <li className="px-3 py-2 text-xs">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 font-mono">
        <span className={`inline-flex items-center gap-1.5 ${TONE_TEXT[tone]}`}>
          <span className={TONE_DOT[tone]} aria-hidden="true" />
          {lastStatusLabel(outcome.outcome)}
        </span>
        <span className="text-[#737373] dark:text-[#a3a3a3]" title={started || undefined}>
          {relativeAgo(started, now) || 'not started'}
        </span>
        <span className="text-[#a3a3a3] dark:text-[#737373]">job {job.id}</span>
        <CostPillSpan pill={costPill(outcome.costUsd, outcome.costKnown ?? undefined)} />
        {outcome.conversationId && (
          <Link href={threadHref(outcome.conversationId)} className="text-[#76B900] hover:underline">
            Open thread
          </Link>
        )}
      </div>
      <OutcomeDetails outcome={outcome} previewLines={3} previewChars={240} />
    </li>
  );
}

function RunDrawer({
  live,
  runs,
  loading,
  now,
  onReload,
  onReconnect,
}: {
  live: LiveRun | undefined;
  runs: InstallJob[] | undefined;
  loading: boolean;
  now: number;
  onReload: () => void;
  /** Re-tail the live run after the watcher lost the API. */
  onReconnect: () => void;
}) {
  return (
    <div className="space-y-4">
      {live && <LiveRunPanel live={live} onReconnect={onReconnect} />}
      <div>
        <div className="flex items-center justify-between gap-3">
          <div className={LABEL_CLASS}>Run log</div>
          <button type="button" onClick={onReload} disabled={loading} className={`btn-ghost ${SMALL_BUTTON}`}>
            {loading ? 'Loading…' : 'Reload'}
          </button>
        </div>
        {!runs || runs.length === 0 ? (
          <div className="mt-2 font-mono text-xs text-[#a3a3a3] dark:text-[#737373]">
            {loading ? 'Loading runs… they appear here in a moment.' : 'No runs recorded yet. Press Run now on the schedule to make the first one.'}
          </div>
        ) : (
          <ul className={`${PANEL_CLASS} mt-2 divide-y divide-[#e5e5e5] dark:divide-[#262626]`}>
            {runs.map(job => (
              <RunRow key={job.id} job={job} now={now} />
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}

export default function SchedulesPage() {
  const [data, setData] = useState<SchedulesResult | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [now, setNow] = useState(() => Date.now());
  const [profiles, setProfiles] = useState<AgentProfileSchema[]>([]);

  // Add form.
  const [prompt, setPrompt] = useState('');
  const [every, setEvery] = useState('');
  const [name, setName] = useState('');
  const [mode, setMode] = useState<ScheduleMode>('ask');
  const [profile, setProfile] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);
  const [formNotice, setFormNotice] = useState<string | null>(null);

  // Per-schedule UI state, keyed by schedule id.
  const [busy, setBusy] = useState<Record<string, string>>({});
  const [notices, setNotices] = useState<Record<string, { text: string; tone: StatusTone }>>({});
  const [confirmDelete, setConfirmDelete] = useState<string | null>(null);
  const [openRuns, setOpenRuns] = useState<Record<string, boolean>>({});
  const [runs, setRuns] = useState<Record<string, InstallJob[]>>({});
  const [runsLoading, setRunsLoading] = useState<Record<string, boolean>>({});
  const [live, setLive] = useState<Record<string, LiveRun>>({});
  const watchersRef = useRef(new Map<string, () => void>());

  const refresh = useCallback(async () => {
    try {
      const result = await getSchedules();
      setData(result);
      setLoadError(null);
    } catch (err) {
      setLoadError(err instanceof Error ? err.message : 'Could not load schedules');
    } finally {
      setLoading(false);
      setNow(Date.now());
    }
  }, []);

  useEffect(() => {
    void refresh();
    const timer = setInterval(() => {
      void refresh();
    }, REFRESH_MS);
    return () => clearInterval(timer);
  }, [refresh]);

  useEffect(() => {
    listAgentProfiles()
      .then(result => setProfiles(result.profiles))
      .catch(() => {
        // The select still offers the general Wizard.
      });
  }, []);

  useEffect(() => {
    const watchers = watchersRef.current;
    return () => {
      for (const stop of watchers.values()) stop();
      watchers.clear();
    };
  }, []);

  const loadRuns = useCallback(async (scheduleId: string) => {
    setRunsLoading(prev => ({ ...prev, [scheduleId]: true }));
    try {
      const result = await listScheduleRuns(scheduleId, RUN_LOG_LIMIT);
      setRuns(prev => ({ ...prev, [scheduleId]: result.runs }));
    } catch (err) {
      setNotices(prev => ({
        ...prev,
        [scheduleId]: { text: err instanceof Error ? err.message : 'Could not load the run log', tone: 'red' },
      }));
    } finally {
      setRunsLoading(prev => ({ ...prev, [scheduleId]: false }));
    }
  }, []);

  const toggleRuns = (schedule: Schedule) => {
    const next = !openRuns[schedule.id];
    setOpenRuns(prev => ({ ...prev, [schedule.id]: next }));
    if (next) void loadRuns(schedule.id);
  };

  const clearNotice = (scheduleId: string) => {
    setNotices(prev => {
      if (!(scheduleId in prev)) return prev;
      const next = { ...prev };
      delete next[scheduleId];
      return next;
    });
  };

  const withBusy = async (scheduleId: string, action: string, work: () => Promise<void>) => {
    setBusy(prev => ({ ...prev, [scheduleId]: action }));
    clearNotice(scheduleId);
    try {
      await work();
    } catch (err) {
      setNotices(prev => ({
        ...prev,
        [scheduleId]: { text: err instanceof Error ? err.message : `Could not ${action} the schedule`, tone: 'red' },
      }));
    } finally {
      setBusy(prev => {
        const next = { ...prev };
        delete next[scheduleId];
        return next;
      });
    }
  };

  const toggleEnabled = (schedule: Schedule) =>
    withBusy(schedule.id, schedule.enabled ? 'disable' : 'enable', async () => {
      const updated = await updateSchedule(schedule.id, { enabled: !schedule.enabled });
      setData(prev =>
        prev ? { ...prev, schedules: prev.schedules.map(item => (item.id === updated.id ? updated : item)) } : prev,
      );
    });

  const startWatching = (scheduleId: string, jobId: string) => {
    watchersRef.current.get(scheduleId)?.();
    setLive(prev => ({
      ...prev,
      [scheduleId]: { jobId, status: 'queued', steps: [], outcome: null, error: null, lostContact: null },
    }));
    const stop = watchInstallJob<ScheduleRunEvent>(jobId, {
      onStatus: job => setLive(prev => patchLive(prev, scheduleId, { status: job.status })),
      onEvent: (event, jobEvent) => {
        if (event.event === 'step' && event.message) {
          setLive(prev => patchLive(prev, scheduleId, current => ({ steps: [...current.steps, event.message] })));
        }
        const outcome = outcomeFromEvent(event, jobEvent.timestamp);
        if (outcome) setLive(prev => patchLive(prev, scheduleId, { outcome, status: jobEvent.status }));
      },
      onComplete: () => {
        void refresh();
        void loadRuns(scheduleId);
      },
      onError: (message, job) => {
        if (job) {
          // The job itself ended badly (failed / canceled / interrupted).
          setLive(prev => patchLive(prev, scheduleId, { error: message, status: job.status }));
          void refresh();
          void loadRuns(scheduleId);
          return;
        }
        // Six polls in a row failed and the watcher stopped; the run did not.
        // Amber "lost contact", not a red failure — Reconnect re-tails the job.
        setLive(prev => patchLive(prev, scheduleId, { lostContact: message }));
      },
    });
    watchersRef.current.set(scheduleId, stop);
  };

  const runNow = (schedule: Schedule) =>
    withBusy(schedule.id, 'run', async () => {
      const outcome = await runSchedule(schedule.id);
      if (outcome.conflict) {
        const jobId = outcome.conflict.job_id;
        setNotices(prev => ({
          ...prev,
          [schedule.id]: {
            text: jobId
              ? `A run is already in flight (job ${jobId}); it will show up in the run log when it finishes.`
              : 'A run is already in flight; it will show up in the run log when it finishes.',
            tone: 'amber',
          },
        }));
        return;
      }
      setOpenRuns(prev => ({ ...prev, [schedule.id]: true }));
      startWatching(schedule.id, outcome.started.job_id);
    });

  const remove = (schedule: Schedule) =>
    withBusy(schedule.id, 'delete', async () => {
      await deleteSchedule(schedule.id);
      setConfirmDelete(null);
      watchersRef.current.get(schedule.id)?.();
      watchersRef.current.delete(schedule.id);
      setData(prev =>
        prev
          ? { ...prev, schedules: prev.schedules.filter(item => item.id !== schedule.id), count: Math.max(0, prev.count - 1) }
          : prev,
      );
    });

  const submit = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setFormError(null);
    setFormNotice(null);
    const text = prompt.trim();
    if (!text) {
      setFormError('Write the prompt the Wizard should run.');
      return;
    }
    const check = validateEvery(every);
    if (!check.ok) {
      setFormError(check.error);
      return;
    }
    setSubmitting(true);
    try {
      const created = await createSchedule({
        prompt: text,
        every: check.normalized,
        mode,
        ...(name.trim() ? { name: name.trim() } : {}),
        ...(profile ? { profile } : {}),
      });
      setPrompt('');
      setEvery('');
      setName('');
      setProfile('');
      setMode('ask');
      setFormNotice(`Added "${scheduleDisplayName(created)}" — next run ${nextRunLabel(created.next_run, Date.now(), created.enabled)}.`);
      await refresh();
    } catch (err) {
      setFormError(err instanceof Error ? err.message : 'Could not add the schedule');
    } finally {
      setSubmitting(false);
    }
  };

  const schedules = data?.schedules ?? [];
  const runner = data?.runner ?? null;
  const tone = runnerTone(runner);
  const pollLabel = runner?.poll_s ? formatInterval(runner.poll_s) || `${runner.poll_s}s` : '';

  return (
    <div>
      <PageHeader
        eyebrow="Automation"
        title="Schedules"
        subtitle="Prompts the Wizard runs on a timer while the API server is up. Each run is a job and a turn in one chat thread per schedule."
        trailing={
          <button
            type="button"
            onClick={() => void refresh()}
            disabled={loading}
            className="btn-secondary px-4 py-2 text-xs font-mono uppercase tracking-wider"
          >
            Refresh
          </button>
        }
      />
      <div className="p-6 space-y-6 max-w-6xl mx-auto">
        {loadError && <InlineNotice text={`Could not load schedules: ${loadError}. Start the API from the console at the top, then press Reload.`} tone="red" />}

        <section className={`${PANEL_CLASS} p-4`}>
          <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
            <span className={TONE_DOT[tone]} aria-hidden="true" />
            <span className={LABEL_CLASS}>Runner</span>
            <span className={`font-mono text-sm ${TONE_TEXT[tone]}`}>
              {runner ? runnerLabel(runner) : loading ? 'checking…' : runnerLabel(null)}
            </span>
            {pollLabel && (
              <span className="font-mono text-[10px] text-[#a3a3a3] dark:text-[#737373]">polls every {pollLabel}</span>
            )}
            {runner?.history ? (
              <span className="font-mono text-[10px] text-[#a3a3a3] dark:text-[#737373]">
                keeps the last {runner.history} runs per schedule
              </span>
            ) : null}
          </div>
          {runner && runner.tools.length > 0 && (
            <div className="mt-3">
              <div className={LABEL_CLASS}>Tools a scheduled turn may run on its own</div>
              <div className="mt-2 flex flex-wrap gap-1.5">
                {runner.tools.map(tool => (
                  <span
                    key={tool}
                    className="border border-[#e5e5e5] bg-[#fafafa] px-2 py-0.5 font-mono text-[10px] text-[#525252] dark:border-[#262626] dark:bg-[#141414] dark:text-[#a3a3a3]"
                  >
                    {tool}
                  </span>
                ))}
              </div>
              <p className="mt-2 font-mono text-[10px] text-[#a3a3a3] dark:text-[#737373]">
                Anything else the model asks for is recorded on the run as deferred — no plan, no token — and waits for you in the thread.
              </p>
            </div>
          )}
        </section>

        <section className={PANEL_CLASS}>
          <div className="flex items-center justify-between gap-3 border-b border-[#e5e5e5] px-4 py-3 dark:border-[#262626]">
            <h2 className="flex items-center gap-2 font-mono text-xs font-bold uppercase tracking-widest text-[#0a0a0a] dark:text-[#fafafa]">
              <span className="inline-block h-4 w-1 bg-[#76B900]" />
              Schedules
            </h2>
            {data && (
              <span className="font-mono text-[10px] text-[#a3a3a3] dark:text-[#737373]">{data.count} total</span>
            )}
          </div>
          {schedules.length === 0 ? (
            <div className="px-4 py-10 text-center">
              <div className="font-mono text-sm text-[#525252] dark:text-[#a3a3a3]">
                {loading ? 'Loading schedules… your list appears here in a moment.' : 'No schedules yet.'}
              </div>
              {!loading && (
                <p className="mt-1 text-xs text-[#737373] dark:text-[#a3a3a3]">
                  Create one below — ask or convene, every 30m, 1h or 1d.
                </p>
              )}
            </div>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full font-mono text-xs">
                <thead>
                  <tr className="border-b border-[#e5e5e5] text-left text-[#a3a3a3] dark:border-[#262626] dark:text-[#737373]">
                    <th className="px-4 py-2 font-medium">Name</th>
                    <th className="px-2 py-2 font-medium">Every</th>
                    <th className="px-2 py-2 font-medium">Mode</th>
                    <th className="px-2 py-2 font-medium">Profile</th>
                    <th className="px-2 py-2 font-medium">Next</th>
                    <th className="px-2 py-2 font-medium">Last</th>
                    <th className="px-2 py-2 font-medium">Last cost</th>
                    <th className="px-4 py-2 text-right font-medium">Actions</th>
                  </tr>
                </thead>
                <tbody>
                  {schedules.map(schedule => {
                    const action = busy[schedule.id];
                    const notice = notices[schedule.id];
                    const drawerOpen = Boolean(openRuns[schedule.id]);
                    const statusTone = lastStatusTone(schedule.last_status);
                    const runLabel = `${schedule.run_count} run${schedule.run_count === 1 ? '' : 's'}`;
                    const lastRun = schedule.last_run ? relativeAgo(schedule.last_run, now) : '';
                    return (
                      <Fragment key={schedule.id}>
                        <tr
                          className={`border-b border-[#f0f0f0] align-top dark:border-[#1f1f1f] ${schedule.enabled ? '' : 'opacity-70'}`}
                        >
                          <td className="max-w-[280px] px-4 py-3">
                            <div className="truncate font-semibold text-[#0a0a0a] dark:text-[#fafafa]" title={schedule.prompt}>
                              {scheduleDisplayName(schedule)}
                            </div>
                            {schedule.name && (
                              <div className="mt-0.5 truncate text-[10px] text-[#737373] dark:text-[#a3a3a3]" title={schedule.prompt}>
                                {schedule.prompt}
                              </div>
                            )}
                            <div className="mt-0.5 text-[10px] text-[#a3a3a3] dark:text-[#737373]">
                              {runLabel}
                              {lastRun ? ` · last ${lastRun}` : ''}
                            </div>
                          </td>
                          <td className="whitespace-nowrap px-2 py-3">{formatInterval(schedule.interval_seconds) || '—'}</td>
                          <td className="whitespace-nowrap px-2 py-3">{schedule.mode || 'ask'}</td>
                          <td className="whitespace-nowrap px-2 py-3">
                            <div>{schedule.profile || 'general Wizard'}</div>
                            {schedule.advisor && (
                              <div className="text-[10px] text-[#a3a3a3] dark:text-[#737373]">via {schedule.advisor}</div>
                            )}
                          </td>
                          <td className="whitespace-nowrap px-2 py-3">
                            {nextRunLabel(schedule.next_run, now, schedule.enabled)}
                          </td>
                          <td className={`max-w-[220px] px-2 py-3 ${TONE_TEXT[statusTone]}`}>
                            <span className="block truncate" title={schedule.last_error || undefined}>
                              {lastStatusLabel(schedule.last_status, schedule.last_error)}
                            </span>
                          </td>
                          <td className="whitespace-nowrap px-2 py-3">
                            <CostPillSpan pill={lastCostPill(schedule, costPill)} />
                          </td>
                          <td className="px-4 py-3">
                            <div className="flex flex-wrap justify-end gap-1.5">
                              <button
                                type="button"
                                onClick={() => void toggleEnabled(schedule)}
                                disabled={Boolean(action)}
                                className={`btn-secondary ${SMALL_BUTTON}`}
                              >
                                {action === 'enable' || action === 'disable' ? 'Saving…' : schedule.enabled ? 'Disable' : 'Enable'}
                              </button>
                              <button
                                type="button"
                                onClick={() => void runNow(schedule)}
                                disabled={Boolean(action)}
                                className={`btn-primary ${SMALL_BUTTON}`}
                              >
                                {action === 'run' ? 'Starting…' : 'Run now'}
                              </button>
                              <button type="button" onClick={() => toggleRuns(schedule)} className={`btn-secondary ${SMALL_BUTTON}`}>
                                {drawerOpen ? 'Hide runs' : 'Runs'}
                              </button>
                              {schedule.conversation_id && (
                                <Link
                                  href={threadHref(schedule.conversation_id)}
                                  className={`btn-secondary inline-flex items-center ${SMALL_BUTTON}`}
                                >
                                  Open thread
                                </Link>
                              )}
                              {confirmDelete === schedule.id ? (
                                <>
                                  <button
                                    type="button"
                                    onClick={() => void remove(schedule)}
                                    disabled={Boolean(action)}
                                    className={`border border-[#dc2626]/40 bg-[#dc2626]/10 text-[#dc2626] ${SMALL_BUTTON}`}
                                  >
                                    {action === 'delete' ? 'Deleting…' : 'Confirm delete'}
                                  </button>
                                  <button type="button" onClick={() => setConfirmDelete(null)} className={`btn-ghost ${SMALL_BUTTON}`}>
                                    Cancel
                                  </button>
                                </>
                              ) : (
                                <button
                                  type="button"
                                  onClick={() => setConfirmDelete(schedule.id)}
                                  disabled={Boolean(action)}
                                  className={`border border-[#dc2626]/30 text-[#dc2626] hover:bg-[#dc2626]/10 ${SMALL_BUTTON}`}
                                >
                                  Delete
                                </button>
                              )}
                            </div>
                          </td>
                        </tr>
                        {notice && (
                          <tr className="border-b border-[#f0f0f0] dark:border-[#1f1f1f]">
                            <td colSpan={8} className="px-4 pb-3">
                              <InlineNotice text={notice.text} tone={notice.tone} />
                            </td>
                          </tr>
                        )}
                        {drawerOpen && (
                          <tr className="border-b border-[#e5e5e5] dark:border-[#262626]">
                            <td colSpan={8} className="bg-[#fafafa] px-4 py-4 dark:bg-[#141414]">
                              <RunDrawer
                                live={live[schedule.id]}
                                runs={runs[schedule.id]}
                                loading={Boolean(runsLoading[schedule.id])}
                                now={now}
                                onReload={() => void loadRuns(schedule.id)}
                                onReconnect={() => {
                                  const current = live[schedule.id];
                                  if (current) startWatching(schedule.id, current.jobId);
                                }}
                              />
                            </td>
                          </tr>
                        )}
                      </Fragment>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
        </section>

        <section className={`${PANEL_CLASS} p-5`}>
          <div className="section-label">Add a schedule</div>
          <h2 className="mt-1 text-xl font-bold text-[#0a0a0a] dark:text-[#fafafa]">Run a prompt on a timer</h2>
          <p className="mt-1 text-sm text-[#737373] dark:text-[#a3a3a3]">
            <code className="font-mono">ask</code> runs an unattended Wizard turn with the read-only roster above; anything
            that needs approval waits in the thread. <code className="font-mono">convene</code> runs the council. do-mode
            schedules run only under <code className="font-mono">nvh schedule start</code> in a terminal — the API runner
            refuses them.
          </p>
          <form onSubmit={event => void submit(event)} className="mt-4 space-y-3">
            <label className="block">
              <span className={LABEL_CLASS}>Prompt</span>
              <textarea
                value={prompt}
                onChange={event => setPrompt(event.target.value)}
                maxLength={4000}
                className="input-base mt-1 min-h-[110px] w-full px-3 py-2 text-sm"
                placeholder="What should the Wizard do each time?"
              />
            </label>
            <div className="grid gap-3 md:grid-cols-4">
              <label className="block">
                <span className={LABEL_CLASS}>Every</span>
                <input
                  value={every}
                  onChange={event => setEvery(event.target.value)}
                  maxLength={16}
                  className="input-base mt-1 w-full px-3 py-2 font-mono text-sm"
                  placeholder={EVERY_PLACEHOLDER}
                />
              </label>
              <label className="block">
                <span className={LABEL_CLASS}>Name (optional)</span>
                <input
                  value={name}
                  onChange={event => setName(event.target.value)}
                  maxLength={120}
                  className="input-base mt-1 w-full px-3 py-2 text-sm"
                  placeholder="Morning status"
                />
              </label>
              <label className="block">
                <span className={LABEL_CLASS}>Mode</span>
                <select
                  value={mode}
                  onChange={event => setMode(event.target.value === 'convene' ? 'convene' : 'ask')}
                  className="input-base mt-1 w-full px-3 py-2 text-sm"
                >
                  <option value="ask">ask — unattended Wizard turn</option>
                  <option value="convene">convene — council</option>
                </select>
              </label>
              <label className="block">
                <span className={LABEL_CLASS}>Profile</span>
                <select
                  value={profile}
                  onChange={event => setProfile(event.target.value)}
                  className="input-base mt-1 w-full px-3 py-2 text-sm"
                >
                  <option value="">general Wizard</option>
                  {profiles.map(item => (
                    <option key={item.name} value={item.name}>
                      {item.title ? `${item.name} — ${item.title}` : item.name}
                    </option>
                  ))}
                </select>
              </label>
            </div>
            {formError && <InlineNotice text={formError} tone="red" />}
            {formNotice && <InlineNotice text={formNotice} tone="green" />}
            <div className="flex flex-wrap items-center justify-between gap-3">
              <span className="font-mono text-[10px] text-[#a3a3a3] dark:text-[#737373]">
                Intervals use the CLI syntax ({EVERY_PLACEHOLDER}). A new schedule is due on the runner&apos;s next poll.
              </span>
              <button
                type="submit"
                disabled={submitting}
                className="btn-primary px-4 py-2 text-xs font-mono uppercase tracking-wider"
              >
                {submitting ? 'Adding…' : 'Add schedule'}
              </button>
            </div>
          </form>
        </section>
      </div>
    </div>
  );
}
