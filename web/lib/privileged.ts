/**
 * Privileged-tool card helpers — pure functions over a Wizard tool schema and
 * an execute result. No JSX and no runtime imports, so the module runs under
 * plain `node --test lib/privileged.test.mjs`; the card that renders them is
 * `ToolCard` in components/WizardChat.tsx.
 *
 * The `privileged` safety class is the sudo tier from
 * docs/proposals/SPARK_CONCIERGE_2026-09.md §3.4: a tool that changes the
 * machine, approved by a human click, never auto-approved, and switched off
 * entirely by `NVH_ALLOW_PRIVILEGED=0`. Four rules the UI must not lose:
 *
 *  1. **Every card needs a click.** Nothing here decides to *run* anything —
 *     these functions only pick chrome and read a result. `clickable` is true
 *     for every pending card whatever its class, including `auto`: the server
 *     has already run the auto-class calls it was willing to, so anything
 *     that reached the UI is waiting on a human. There is no client-side
 *     execute the user did not press a button for — not even an "unconfirmed
 *     preview" to fetch the plan. The plan on the card is the server's dry
 *     run, delivered on the surfaced call (`plan`) together with the
 *     `approval_token` the click sends back; it is never read from the
 *     model's own `arguments`, so a prompt-injected "plan" cannot dress up a
 *     different command as the one being approved.
 *  2. **An unknown safety class behaves as confirm.** `isPrivilegedCall`
 *     matches the exact string `privileged` — no case folding, no prefixes —
 *     and `cardChrome` gives every other class, known or not, byte-identical
 *     confirm chrome. An old UI meeting a class invented after it shipped
 *     draws an ordinary card with a Run button: never red, never auto-run.
 *     Matches the server's `_split_by_safety_class`, which buckets anything
 *     not exactly `auto` as confirm.
 *  3. **A password prompt is not an error.** When the owner is in the sudo
 *     group but `sudo -n` fails, the backend hands back the exact command
 *     instead of running it (`needs_terminal`). That is a successful
 *     hand-off, so it gets its own status and amber chrome — never the red
 *     "✗ Failed" of a genuine failure. nvHive never prompts for, sees or
 *     stores a password, and there is no password parameter anywhere.
 *  4. **The kill switch is honoured from whichever side reports it**
 *     (`isPrivilegedDisabled`), and a disabled card offers no Run button.
 */

import type {
  WizardChatToolOutcome,
  WizardToolExecuteResult,
  WizardToolPlan,
  WizardToolSafetyClass,
  WizardToolSchema,
} from './api';
import type { PlaybookCatalogueEntry, PlaybookRunEvent, PlaybookRunStart } from './types';

/** Longest `summary` a history tool outcome carries to the server (which cuts
 * there too). Lives here rather than in api.ts so this module keeps no
 * runtime import and still runs under plain `node --test`; api.ts imports it. */
export const WIZARD_TOOL_OUTCOME_CHARS = 300;

/** The one safety class that gets the red card. Compared by exact equality. */
export const PRIVILEGED_CLASS = 'privileged';
/** Red for privileged chrome and for genuine failures (`--accent-red`). */
export const PRIVILEGED_COLOR = '#dc2626';
/** Pill next to the tool name on every privileged card. */
export const PRIVILEGED_BADGE = 'PRIVILEGED';
/** The destructive button's label — never a bare "Run". */
export const PRIVILEGED_RUN_LABEL = 'Approve and run';
/** One-line warning under the tool name on every privileged card. */
export const PRIVILEGED_SUDO_NOTE = 'runs with sudo on this machine';
/** Shown instead of the Run button when the kill switch is off. */
export const PRIVILEGED_DISABLED_NOTE = 'privileged tools are disabled (NVH_ALLOW_PRIVILEGED=0)';
/** Fallback hint for a `needs_terminal` hand-off that carried none. */
export const NEEDS_TERMINAL_HINT = 'sudo needs a password here, so nvHive did not run it — run this yourself in a terminal.';

/**
 * Card lifecycle state.
 *
 * Four are settled without being a failure: `needs-terminal` (the
 * sudo-password hand-off — the user finishes it themselves), `halted` (a
 * playbook run stopped on purpose after a step that needs the user to act
 * before it can go on — the docker-group re-login — neither done nor
 * failed), `disabled` (the kill switch is off) and `refused` (the tool
 * declined in band — `run_code` without Docker, a vision path outside the
 * allowed roots, a shell card whose isolation changed — so nothing ran).
 */
export type ToolCardStatus =
  | 'idle'
  | 'running'
  | 'ok'
  | 'error'
  | 'awaiting-confirm'
  | 'needs-terminal'
  | 'halted'
  | 'disabled'
  | 'refused'
  | 'dismissed';

/** Statuses that still want a Run / Skip click. Everything else is settled. */
export function isPendingStatus(status: ToolCardStatus | string | undefined): boolean {
  return status === 'idle' || status === 'awaiting-confirm';
}

/**
 * Is this card a privileged one?
 *
 * Either side of the exchange may say so: the catalog entry
 * (`safety_class: 'privileged'`, known before the user clicks anything) or
 * the execute result (`privileged: true`, which also covers a tool the local
 * catalog is too old to know about). A missing schema is NOT privileged — it
 * is an ordinary confirm card, which still needs a click.
 */
export function isPrivilegedCall(
  schema: Pick<WizardToolSchema, 'safety_class'> | undefined | null,
  result?: Pick<WizardToolExecuteResult, 'privileged' | 'safety_class'> | undefined | null,
): boolean {
  if (schema?.safety_class === PRIVILEGED_CLASS) return true;
  if (result?.privileged === true) return true;
  return result?.safety_class === PRIVILEGED_CLASS;
}

/**
 * True when the backend refused to run the command itself and handed the user
 * the exact line to paste into a terminal (sudo wants a password).
 *
 * Deliberately independent of `ok`: the backend may report the hand-off as a
 * success (it did what it could) or as a non-ok refusal, and either way the
 * card must show a copyable command, never a red error.
 */
export function needsTerminal(
  result: Pick<WizardToolExecuteResult, 'needs_terminal'> | undefined | null,
): boolean {
  return result?.needs_terminal === true;
}

/** The exact command for the user to run, trimmed; `''` when there is none. */
export function terminalCommand(
  result: Pick<WizardToolExecuteResult, 'command'> | undefined | null,
): string {
  const command = result?.command;
  return typeof command === 'string' ? command.trim() : '';
}

/**
 * True when privileged tools are switched off for this workspace.
 *
 * Three independent signals, because it is not settled which one the backend
 * will use and a stale one must not unlock the button:
 *  - `result.disabled` — the refusal from an execute that got that far;
 *  - `schema.enabled === false` — the catalog row flagged rather than hidden;
 *  - `listEnabled === false` — `privileged_enabled` from the tool list.
 * The list flag is advisory (the registry is a per-process singleton, so a
 * switch read at registration time is restart-scoped) and is consulted only
 * for privileged cards; a refusal from execute is always honoured, for any
 * card. Absent flags mean enabled — an older API that sends none of them must
 * not grey out the UI.
 */
export function isPrivilegedDisabled(
  schema?: Pick<WizardToolSchema, 'safety_class' | 'enabled'> | undefined | null,
  result?: Pick<WizardToolExecuteResult, 'disabled' | 'privileged' | 'safety_class'> | undefined | null,
  listEnabled?: boolean,
): boolean {
  if (result?.disabled === true) return true;
  if (!isPrivilegedCall(schema, result)) return false;
  if (schema?.enabled === false) return true;
  return listEnabled === false;
}

/** Only-strings, trimmed, blanks dropped; anything that is not an array → `[]`. */
function stringList(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return value
    .filter((line): line is string => typeof line === 'string' && line.trim() !== '')
    .map(line => line.trim());
}

/**
 * The exact commands to show above the buttons, or `[]` for "no plan".
 *
 * Reads `plan` off whatever it is given — the surfaced call (the server put
 * its dry run there) or an execute result (the `needs_confirmation` card) —
 * and accepts the server's plan object (`{ok, commands, …}`, see
 * `WizardToolPlan`) as well as the bare command list older builds sent.
 * Never a tool call's `arguments`: those are the model's words. Never
 * fetched: showing a plan must cost no execute, because an unconfirmed
 * privileged call is only side-effect-free if the *server* defers it.
 *
 * Non-string and blank entries are dropped rather than stringified, so a
 * malformed plan degrades to "no plan" instead of printing `[object Object]`,
 * and a bare string is not mistaken for a one-command plan.
 */
export function planLines(
  source: { plan?: WizardToolPlan | string[] | unknown } | undefined | null,
): string[] {
  const plan = source?.plan;
  if (Array.isArray(plan)) return stringList(plan);
  if (plan && typeof plan === 'object') return stringList((plan as { commands?: unknown }).commands);
  return [];
}

export interface PlanNotes {
  /** What the setting changes, in one or two sentences. */
  changes: string;
  /** How to undo it, one command per line. */
  undo: string[];
  /** A caution the user must read before approving (e.g. "this disconnects you"). */
  warning: string;
  /** Anything else worth knowing (log out and back in, no reboot needed …). */
  notes: string[];
  /** The plan itself was refused (deny list / validation): why. */
  error: string;
}

/**
 * The rest of the server's plan, for the compact lines under the commands.
 * A bare command list (older builds) or no plan gives empty notes.
 */
export function planNotes(
  source: { plan?: WizardToolPlan | string[] | unknown } | undefined | null,
): PlanNotes {
  const plan = source?.plan;
  const empty: PlanNotes = { changes: '', undo: [], warning: '', notes: [], error: '' };
  if (!plan || typeof plan !== 'object' || Array.isArray(plan)) return empty;
  const p = plan as Partial<WizardToolPlan>;
  return {
    changes: typeof p.changes === 'string' ? p.changes.trim() : '',
    undo: stringList(p.undo),
    warning: typeof p.warning === 'string' ? p.warning.trim() : '',
    notes: stringList(p.notes),
    error: p.ok === false && typeof p.error === 'string' ? p.error.trim() : '',
  };
}

/** A handler's answer as the card reads it — the envelope, or what it wrapped. */
export type ToolOutcome = Partial<WizardToolExecuteResult> & Record<string, unknown>;

/**
 * The result the card should read verdicts from.
 *
 * The registry answers a confirmed call with `{ok: true, result: <handler
 * result>, tool, safety_class}`: `ok: true` there means only "the tool
 * answered". A sudo hand-off (`needs_terminal`), a deny-list refusal
 * (`denied`), a validation error (`ok: false` + `error`) and the receipt of a
 * real apply all live in the nested `result`. This returns that nested object
 * when the envelope is `ok: true` with a dict result, and passes everything
 * else — the confirmation card, the kill-switch refusal, an approval-token
 * refusal, an HTTP-layer refusal, a thrown handler — through unchanged.
 */
export function unwrapToolResult(
  envelope: WizardToolExecuteResult | undefined | null,
): ToolOutcome {
  if (!envelope) return {};
  const nested = envelope.result;
  if (envelope.ok === true && nested && typeof nested === 'object' && !Array.isArray(nested)) {
    return nested as ToolOutcome;
  }
  return envelope as ToolOutcome;
}

/**
 * The line the next turn's history carries for one settled card, or `null`
 * while the card is still pending or running.
 *
 * `ok` is true only for a clean run. The hand-off is reported as
 * `needs a terminal: <exact command>` — the prefix the server's prompt tells
 * the model to look for before repeating the command verbatim — and never as
 * a success. Skipped and disabled cards are reported too, so the model does
 * not assume its proposal happened. Cut to `WIZARD_TOOL_OUTCOME_CHARS`.
 */
export function historyToolOutcome(
  name: string,
  status: ToolCardStatus | string | undefined,
  summary: string | undefined,
  detail?: Pick<WizardToolExecuteResult, 'command'> | undefined | null,
): WizardChatToolOutcome | null {
  if (status === undefined || isPendingStatus(status) || status === 'running') return null;
  let ok = false;
  let text: string;
  switch (status) {
    case 'ok':
      ok = true;
      text = summary?.trim() || 'ran';
      break;
    case 'needs-terminal':
      text = `needs a terminal: ${terminalCommand(detail) || '(no command was returned)'}`;
      break;
    case 'halted':
      // Not done: the run stopped for the user to act (the summary carries the instruction).
      text = `stopped for the user to act — not installed yet: ${summary?.trim() || 'see the card'}`;
      break;
    case 'disabled':
      text = PRIVILEGED_DISABLED_NOTE;
      break;
    case 'refused':
      // The tool declined in band; the summary carries the server's reason
      // (what to do: install Docker, attach the image, ask again).
      text = summary?.trim() || 'refused — nothing ran';
      break;
    case 'dismissed':
      text = 'skipped by the user; not run';
      break;
    default:
      text = summary?.trim() || 'failed';
  }
  return { name, ok, summary: text.slice(0, WIZARD_TOOL_OUTCOME_CHARS) };
}

export interface CardChrome {
  /** Render the PRIVILEGED pill, the sudo note and the destructive button. */
  privileged: boolean;
  /** Card border, or `null` to keep the themed default. */
  borderColor: string | null;
  /** Card background tint, or `null` for the themed default. */
  background: string | null;
  /** Safety-class pill (`PRIVILEGED`), or `null` for classes that need none. */
  classBadge: string | null;
  /** Status badge text next to the tool name. */
  badge: string;
  badgeColor: string;
  /** Label for the run button. */
  runLabel: string;
  /** Does this card still show Run / Skip? True for every pending card. */
  clickable: boolean;
}

/**
 * Chrome for one card: colours, badges, button label and whether the buttons
 * show at all.
 *
 * Status wins over class for the badge *text* (a finished privileged tool
 * reads "✓ Done"), while class wins for the border: a privileged card stays
 * red for the life of the conversation, so scrolling back shows at a glance
 * which cards touched the machine with sudo.
 */
export function cardChrome(
  safetyClass: WizardToolSafetyClass | string | undefined | null,
  status: ToolCardStatus | string,
): CardChrome {
  const privileged = safetyClass === PRIVILEGED_CLASS;
  const chrome: CardChrome = {
    privileged,
    borderColor: privileged ? PRIVILEGED_COLOR : null,
    background: privileged ? 'rgba(220,38,38,0.06)' : null,
    classBadge: privileged ? PRIVILEGED_BADGE : null,
    badge: '',
    badgeColor: '#737373',
    runLabel: privileged ? PRIVILEGED_RUN_LABEL : 'Run',
    clickable: isPendingStatus(status),
  };

  switch (status) {
    case 'running':
      chrome.badge = 'Running...';
      chrome.badgeColor = '#d97706';
      break;
    case 'ok':
      chrome.badge = '✓ Done';
      chrome.badgeColor = '#16a34a';
      break;
    case 'error':
      chrome.badge = '✗ Failed';
      chrome.badgeColor = PRIVILEGED_COLOR;
      break;
    case 'needs-terminal':
      // A password is needed — the user finishes this one in a terminal.
      // Amber, like any other "your turn", never the red of a failure.
      chrome.badge = 'Run it in a terminal';
      chrome.badgeColor = '#d97706';
      break;
    case 'refused':
      // The tool declined in band and nothing ran: not done, not failed.
      // Amber; the summary line under the badge carries the reason.
      chrome.badge = 'Refused';
      chrome.badgeColor = '#d97706';
      break;
    case 'halted':
      // The run stopped on purpose for the user to act (log out and back in,
      // then run it again). Amber: your turn — not done, not failed.
      chrome.badge = 'Stopped — your turn';
      chrome.badgeColor = '#d97706';
      break;
    case 'disabled':
      chrome.badge = 'Disabled';
      chrome.badgeColor = 'var(--text-muted)';
      break;
    case 'dismissed':
      chrome.badge = 'Skipped';
      chrome.badgeColor = '#737373';
      break;
    case 'awaiting-confirm':
      chrome.badge = privileged ? 'Needs your approval' : 'Needs confirmation';
      chrome.badgeColor = privileged ? PRIVILEGED_COLOR : '#d97706';
      break;
    case 'idle':
    default:
      chrome.badge = privileged ? 'Needs your approval' : 'Click to run';
      chrome.badgeColor = privileged ? PRIVILEGED_COLOR : '#d97706';
      break;
  }
  return chrome;
}

// ─── Spark playbooks ─────────────────────────────────────────────────────────
//
// Playbooks (nvh/integrations/installs/playbooks.py) are the privileged
// install tier: curated, multi-step installs from NVIDIA/dgx-spark-playbooks
// that may need sudo. They are NOT studio packs and never run through the
// pack installer. The UI reaches them through three Wizard tools —
// `playbook_list` (auto: the catalogue), `playbook_plan` (auto: one compiled
// plan) and `playbook_install` (privileged: the red card, then a job) — so
// every rule above applies unchanged: the plan on the card is the server's,
// the approval token binds the click, a `needs_terminal` hand-off is not a
// failure, and nothing here runs anything. A confirmed `playbook_install`
// answers `{job_id, playbook, steps_total}`; the card then streams the
// `playbook-run` job and folds its events with the reducers below.

/** The three tool names, so a rename is one edit. */
export const PLAYBOOK_LIST_TOOL = 'playbook_list';
export const PLAYBOOK_PLAN_TOOL = 'playbook_plan';
export const PLAYBOOK_INSTALL_TOOL = 'playbook_install';
/** How many log lines a step keeps on screen (the newest win). */
export const PLAYBOOK_LOG_LINES_PER_STEP = 12;
/** The one-line hand-off when sudo needs a password: the CLI runs sudo
 * interactively in the user's own terminal; nvHive never sees the password. */
export const PLAYBOOK_HANDOFF_PREFIX = 'nvh playbook install';
/** Note under a plan whose steps carry `MANUAL:` lines. */
export const PLAYBOOK_MANUAL_NOTE = 'manual steps are shown, never run by nvHive';
/** Note under a plan the server expects to stop at the first sudo step. */
export const PLAYBOOK_NEEDS_TERMINAL_EXPECTED_NOTE =
  'sudo needs a password on this machine: the run stops at the first sudo step and hands you one command to finish in a terminal.';

/** The exact hand-off command for one playbook; a blank id keeps a visible placeholder. */
export function playbookHandoffCommand(id: string | undefined | null): string {
  const clean = typeof id === 'string' ? id.trim() : '';
  return `${PLAYBOOK_HANDOFF_PREFIX} ${clean || '<id>'}`;
}

/** A finite integer ≥ 0, or `null`. Accepts numeric strings ("3") but not booleans. */
function nonNegativeInt(value: unknown): number | null {
  if (typeof value === 'boolean') return null;
  const n = typeof value === 'number' ? value : typeof value === 'string' && value.trim() !== '' ? Number(value) : NaN;
  if (!Number.isFinite(n) || n < 0) return null;
  return Math.floor(n);
}

/** A finite number ≥ 0 (fractions allowed), or `null`. */
function nonNegativeNumber(value: unknown): number | null {
  if (typeof value === 'boolean') return null;
  const n = typeof value === 'number' ? value : typeof value === 'string' && value.trim() !== '' ? Number(value) : NaN;
  if (!Number.isFinite(n) || n < 0) return null;
  return n;
}

/** A rendered command: a string as-is, an argv joined with spaces, anything else `''`. */
function commandText(value: unknown): string {
  if (typeof value === 'string') return value.trim();
  if (Array.isArray(value)) return stringList(value).join(' ');
  return '';
}

/**
 * Did a confirmed `playbook_install` start a job? Reads the unwrapped handler
 * answer (`unwrapToolResult`) — or the merged card detail — and returns the
 * start record only when `job_id` is a non-empty string. `steps_total`
 * defaults to 0 and `playbook` to `''` when the server left them out; the
 * card fills the id from the call's arguments in that case.
 */
export function playbookRunStart(
  outcome: Record<string, unknown> | undefined | null,
): PlaybookRunStart | null {
  const jobId = outcome?.job_id;
  if (typeof jobId !== 'string' || jobId.trim() === '') return null;
  return {
    ok: outcome?.ok !== false,
    job_id: jobId.trim(),
    playbook: typeof outcome?.playbook === 'string' ? outcome.playbook.trim() : '',
    steps_total: nonNegativeInt(outcome?.steps_total) ?? 0,
  };
}

/**
 * The catalogue rows out of whatever `playbook_list` answered: a bare list,
 * or an object carrying it under `playbooks` (also `items` / `catalogue`).
 * Rows without a string `id` are dropped; every other field is coerced to
 * the documented type with a safe default, and unknown fields ride along.
 * Junk → `[]`, never a throw.
 */
export function playbookCatalogue(outcome: unknown): PlaybookCatalogueEntry[] {
  let rows: unknown[] = [];
  if (Array.isArray(outcome)) rows = outcome;
  else if (outcome && typeof outcome === 'object') {
    const o = outcome as Record<string, unknown>;
    const candidate = o.playbooks ?? o.items ?? o.catalogue;
    if (Array.isArray(candidate)) rows = candidate;
  }
  const entries: PlaybookCatalogueEntry[] = [];
  for (const row of rows) {
    if (!row || typeof row !== 'object' || Array.isArray(row)) continue;
    const r = row as Record<string, unknown>;
    if (typeof r.id !== 'string' || r.id.trim() === '') continue;
    const id = r.id.trim();
    const sudoSteps = nonNegativeInt(r.sudo_steps) ?? 0;
    entries.push({
      ...r,
      id,
      title: typeof r.title === 'string' && r.title.trim() ? r.title.trim() : id,
      category: typeof r.category === 'string' ? r.category.trim() : '',
      summary: typeof r.summary === 'string' ? r.summary.trim() : '',
      requires_sudo: r.requires_sudo === true || sudoSteps > 0,
      sudo_steps: sudoSteps,
      manual_steps: nonNegativeInt(r.manual_steps) ?? 0,
      estimated_minutes: nonNegativeNumber(r.estimated_minutes),
      estimated_disk_gb: nonNegativeNumber(r.estimated_disk_gb),
      rootless_alternative:
        typeof r.rootless_alternative === 'string' && r.rootless_alternative.trim() ? r.rootless_alternative.trim() : null,
      installed: r.installed === true,
      receipt_path: typeof r.receipt_path === 'string' && r.receipt_path.trim() ? r.receipt_path : null,
    });
  }
  return entries;
}

/**
 * The compact chips under a catalogue card, in display order: the sudo chip
 * (`sudo: N steps` / `no sudo`), the manual-steps chip when any, then the
 * estimates when known. The sudo chip is always first and always present so
 * the two kinds of install are told apart at a glance.
 */
export function playbookChips(entry: Pick<PlaybookCatalogueEntry, 'sudo_steps' | 'requires_sudo' | 'manual_steps' | 'estimated_minutes' | 'estimated_disk_gb'>): string[] {
  const chips: string[] = [];
  const sudo = nonNegativeInt(entry.sudo_steps) ?? 0;
  if (sudo > 0) chips.push(`sudo: ${sudo} step${sudo === 1 ? '' : 's'}`);
  else if (entry.requires_sudo) chips.push('sudo');
  else chips.push('no sudo');
  const manual = nonNegativeInt(entry.manual_steps) ?? 0;
  if (manual > 0) chips.push(`${manual} manual step${manual === 1 ? '' : 's'}`);
  const minutes = nonNegativeNumber(entry.estimated_minutes);
  if (minutes !== null && minutes > 0) chips.push(`~${Math.round(minutes)} min`);
  const disk = nonNegativeNumber(entry.estimated_disk_gb);
  if (disk !== null && disk > 0) chips.push(`~${disk} GB`);
  return chips;
}

/** What `plan_dict` adds on top of `Plan.to_dict()`; empty defaults for a bare plan. */
export interface PlaybookPlanExtras {
  id: string;
  /** The `MANUAL:` steps as their own list (they also ride in `notes`). */
  manualSteps: string[];
  verify: string[];
  estimatedMinutes: number | null;
  estimatedDiskGb: number | null;
  /** The server expects sudo to want a password here (facts say no passwordless sudo). */
  needsTerminalExpected: boolean;
}

/**
 * The playbook-specific extras off a plan (`plan_dict`: `id`, `manual_steps`,
 * `verify`, `estimates`, `needs_terminal_expected`). Reads the same `plan`
 * key `planLines` / `planNotes` read, so it accepts a surfaced call or an
 * execute result, and degrades to empty for a bare command list or junk.
 */
export function playbookPlanExtras(
  source: { plan?: WizardToolPlan | string[] | unknown } | undefined | null,
): PlaybookPlanExtras {
  const empty: PlaybookPlanExtras = {
    id: '', manualSteps: [], verify: [], estimatedMinutes: null, estimatedDiskGb: null, needsTerminalExpected: false,
  };
  const plan = source?.plan;
  if (!plan || typeof plan !== 'object' || Array.isArray(plan)) return empty;
  const p = plan as Record<string, unknown>;
  const estimates = p.estimates && typeof p.estimates === 'object' && !Array.isArray(p.estimates)
    ? (p.estimates as Record<string, unknown>)
    : {};
  return {
    id: typeof p.id === 'string' ? p.id.trim() : '',
    manualSteps: stringList(p.manual_steps),
    verify: Array.isArray(p.verify) ? p.verify.map(commandText).filter(line => line !== '') : [],
    // The one estimates shape `plan_dict` sends (the catalogue rows carry flat `estimated_*` fields instead).
    estimatedMinutes: nonNegativeNumber(estimates.minutes),
    estimatedDiskGb: nonNegativeNumber(estimates.disk_gb),
    needsTerminalExpected: p.needs_terminal_expected === true,
  };
}

/** The notes a plan carries minus the `MANUAL:` lines, which render separately. */
export function planNotesWithoutManual(notes: string[]): string[] {
  return notes.filter(note => !/^MANUAL:/i.test(note.trim()));
}

/** The `MANUAL:` lines of a plan's notes, prefix stripped. */
export function manualStepsFromNotes(notes: string[]): string[] {
  return notes
    .filter(note => /^MANUAL:/i.test(note.trim()))
    .map(note => note.trim().replace(/^MANUAL:\s*/i, ''))
    .filter(note => note !== '');
}

/** Per-step state inside a running card. */
export type PlaybookStepStatus = 'pending' | 'running' | 'ok' | 'failed' | 'skipped' | 'needs-terminal';

export interface PlaybookStepView {
  /** 0-based index into the compiled plan (the wire format). */
  index: number;
  title: string;
  command: string;
  sudo: boolean;
  status: PlaybookStepStatus;
  exitCode: number | null;
  /** Newest `PLAYBOOK_LOG_LINES_PER_STEP` log lines. */
  log: string[];
}

/**
 * How a run ended, as the card reads it. `halted` is the docker-group stop:
 * the `usermod` step ran and the runner stopped on purpose so the user can log
 * out and back in before running the playbook again — the remaining steps
 * never ran, so it is neither `done` nor `failed`; `message` carries the
 * instruction.
 */
export type PlaybookRunOutcome =
  | { kind: 'done'; message: string }
  | { kind: 'halted'; message: string; step: number | null }
  | { kind: 'needs-terminal'; command: string; hint: string; step: number | null }
  | { kind: 'failed'; step: number | null; error: string };

export interface PlaybookRunState {
  jobId: string;
  playbook: string;
  stepsTotal: number;
  steps: PlaybookStepView[];
  outcome: PlaybookRunOutcome | null;
}

/** A fresh run record for the card, from the start answer (and the id the card knows). */
export function initialPlaybookRun(start: PlaybookRunStart, fallbackId = ''): PlaybookRunState {
  return {
    jobId: start.job_id,
    playbook: start.playbook || fallbackId,
    stepsTotal: nonNegativeInt(start.steps_total) ?? 0,
    steps: [],
    outcome: null,
  };
}

/** The event's 0-based step index, or `null` when it carries none. */
export function playbookStepIndex(event: Pick<PlaybookRunEvent, 'step'> | Record<string, unknown> | undefined | null): number | null {
  if (!event) return null;
  const e = event as Record<string, unknown>;
  return nonNegativeInt(e.step ?? e.step_index ?? e.index);
}

/** The step number a human reads (1-based), or `null`. */
export function playbookStepNumber(index: number | null | undefined): number | null {
  return typeof index === 'number' && Number.isFinite(index) && index >= 0 ? Math.floor(index) + 1 : null;
}

/**
 * How one job event settles the run, or `null` while it is still going.
 *
 *  - `complete` → done — unless it carries `halted: true`, the runner's
 *    deliberate stop after a step the user must follow up on (the
 *    docker-group re-login): then `halted`, with the instruction in
 *    `message`, and never "Done";
 *  - `needs_terminal` → the hand-off (its `command`, or the CLI line for the
 *    playbook when the event carried none) — a success, never a failure;
 *  - `error`, or any event whose `status` is `failed` → failed at that step.
 *
 * `step` and `log` events never end a run, whatever their status says,
 * except a `step` that reports `failed` (the runner stops at the first
 * failing step). Only the exact event names count.
 */
export function jobOutcome(
  event: PlaybookRunEvent | Record<string, unknown> | undefined | null,
  playbook = '',
): PlaybookRunOutcome | null {
  if (!event || typeof event !== 'object') return null;
  const e = event as Record<string, unknown>;
  const name = typeof e.event === 'string' ? e.event : '';
  const status = typeof e.status === 'string' ? e.status : '';
  const message = typeof e.message === 'string' ? e.message.trim() : '';
  const step = playbookStepIndex(e);
  if (name === 'complete') {
    if (e.halted === true) return { kind: 'halted', message, step };
    return { kind: 'done', message };
  }
  if (name === 'needs_terminal') {
    const id = typeof e.playbook === 'string' && e.playbook.trim() ? e.playbook.trim() : playbook;
    return {
      kind: 'needs-terminal',
      command: commandText(e.command) || playbookHandoffCommand(id),
      hint: typeof e.hint === 'string' && e.hint.trim() ? e.hint.trim() : message,
      step,
    };
  }
  if (name === 'error' || status === 'failed') {
    const error = (typeof e.error === 'string' && e.error.trim()) || message || 'failed';
    return { kind: 'failed', step, error };
  }
  return null;
}

function stepStatusFrom(status: string, event: Record<string, unknown>): PlaybookStepStatus {
  if (event.skipped === true || status === 'skipped') return 'skipped';
  if (status === 'failed' || status === 'error') return 'failed';
  if (status === 'complete' || status === 'ok' || status === 'done') return 'ok';
  return 'running';
}

/**
 * Fold one job event into the run. Pure: returns a new state, never mutates.
 *
 *  - `plan` refreshes `stepsTotal` / `playbook` when the event carries them;
 *  - `step` upserts the step at `step` (title, command, sudo, exit code,
 *    status from `status` / `skipped`);
 *  - `log` appends `message` to the step it names, else to the running step,
 *    else to the last step, keeping only the newest lines
 *    (`PLAYBOOK_LOG_LINES_PER_STEP`); a log with no step at all is dropped;
 *  - `needs_terminal` / `complete` / `error` settle the outcome (first one
 *    wins — a later `complete` never overwrites a hand-off) and mark the step
 *    they name; `done` and `halted` close any step still running as `ok`
 *    (the halting step itself completed — the run stopped *after* it).
 * Events with a step index the plan has not announced still create a row, so
 * a runner that skips `plan` renders fine.
 */
export function applyPlaybookEvent(state: PlaybookRunState, event: PlaybookRunEvent | Record<string, unknown>): PlaybookRunState {
  const e = event as Record<string, unknown>;
  const name = typeof e.event === 'string' ? e.event : '';
  const status = typeof e.status === 'string' ? e.status : '';
  const message = typeof e.message === 'string' ? e.message : '';
  const index = playbookStepIndex(e);
  let next: PlaybookRunState = { ...state, steps: state.steps.slice() };

  const total = nonNegativeInt(e.steps_total);
  if (total !== null && total > 0) next.stepsTotal = total;
  if (typeof e.playbook === 'string' && e.playbook.trim() && !next.playbook) next.playbook = e.playbook.trim();

  const upsert = (i: number, patch: Partial<PlaybookStepView>) => {
    const pos = next.steps.findIndex(s => s.index === i);
    const base: PlaybookStepView = pos >= 0
      ? next.steps[pos]
      : { index: i, title: '', command: '', sudo: false, status: 'pending', exitCode: null, log: [] };
    const merged: PlaybookStepView = { ...base, ...patch };
    if (pos >= 0) next.steps[pos] = merged;
    else {
      next.steps.push(merged);
      next.steps.sort((a, b) => a.index - b.index);
    }
  };

  if (name === 'step' && index !== null) {
    const patch: Partial<PlaybookStepView> = { status: stepStatusFrom(status, e) };
    if (typeof e.title === 'string' && e.title.trim()) patch.title = e.title.trim();
    const command = commandText(e.command);
    if (command) patch.command = command;
    if (typeof e.sudo === 'boolean') patch.sudo = e.sudo;
    const exit = e.exit_code;
    if (typeof exit === 'number' && Number.isFinite(exit)) patch.exitCode = exit;
    upsert(index, patch);
  } else if (name === 'log') {
    const target = index
      ?? next.steps.find(s => s.status === 'running')?.index
      ?? (next.steps.length > 0 ? next.steps[next.steps.length - 1].index : null);
    if (target !== null && message.trim() !== '') {
      const existing = next.steps.find(s => s.index === target);
      const log = [...(existing?.log ?? []), message.trimEnd()].slice(-PLAYBOOK_LOG_LINES_PER_STEP);
      upsert(target, { log });
    }
  }

  const outcome = jobOutcome(e, next.playbook);
  if (outcome && !next.outcome) {
    next = { ...next, outcome };
    const settledAfterStep = outcome.kind === 'done' || outcome.kind === 'halted';
    const at = settledAfterStep ? null : outcome.step;
    if (at !== null) {
      const patch: Partial<PlaybookStepView> = {
        status: outcome.kind === 'needs-terminal' ? 'needs-terminal' : 'failed',
      };
      if (typeof e.title === 'string' && e.title.trim()) patch.title = e.title.trim();
      const command = commandText(e.command);
      if (command && outcome.kind === 'failed') patch.command = command;
      const exit = e.exit_code;
      if (typeof exit === 'number' && Number.isFinite(exit)) patch.exitCode = exit;
      upsert(at, patch);
    }
    if (settledAfterStep) {
      next.steps = next.steps.map(s => (s.status === 'running' ? { ...s, status: 'ok' } : s));
    }
  }
  return next;
}

/**
 * The job itself ended (the poller saw a terminal status) without an event
 * that settled the run: `complete` → done, anything else → failed with the
 * job's message. An outcome already on the state always wins, so a hand-off
 * followed by a `failed` job status stays a hand-off.
 */
export function settlePlaybookRun(state: PlaybookRunState, jobStatus: string, message = ''): PlaybookRunState {
  if (state.outcome) return state;
  const trimmed = message.trim();
  if (jobStatus === 'complete') {
    return { ...state, outcome: { kind: 'done', message: trimmed }, steps: state.steps.map(s => (s.status === 'running' ? { ...s, status: 'ok' } : s)) };
  }
  const running = state.steps.find(s => s.status === 'running');
  return {
    ...state,
    outcome: { kind: 'failed', step: running?.index ?? null, error: trimmed || `job ${jobStatus || 'ended'}` },
    steps: state.steps.map(s => (s.status === 'running' ? { ...s, status: 'failed' } : s)),
  };
}

/** Verdict prefix for a run the runner stopped on purpose for the user to act. */
export const PLAYBOOK_HALTED_LABEL = 'Stopped for you to act';

/** The one-line verdict the card prints once the run has settled. */
export function playbookOutcomeLabel(outcome: PlaybookRunOutcome | null | undefined): string {
  if (!outcome) return '';
  switch (outcome.kind) {
    case 'done':
      return 'Done';
    case 'halted':
      // Never "Done": the remaining steps have not run. The message is the instruction.
      return `${PLAYBOOK_HALTED_LABEL}${outcome.message ? `: ${outcome.message}` : ' — then run the playbook again'}`;
    case 'needs-terminal':
      return `Stopped: needs a terminal — run: ${outcome.command}`;
    case 'failed': {
      const n = playbookStepNumber(outcome.step);
      return `${n === null ? 'Failed' : `Failed at step ${n}`}${outcome.error ? `: ${outcome.error}` : ''}`;
    }
  }
}

/** The card status a settled run maps to; `running` while it has not settled. */
export function playbookCardStatus(outcome: PlaybookRunOutcome | null | undefined): ToolCardStatus {
  if (!outcome) return 'running';
  if (outcome.kind === 'done') return 'ok';
  if (outcome.kind === 'halted') return 'halted';
  if (outcome.kind === 'needs-terminal') return 'needs-terminal';
  return 'error';
}

/** Progress text while the run is going: `step 2 of 7` (or just `step 2`). */
export function playbookProgressLabel(state: Pick<PlaybookRunState, 'steps' | 'stepsTotal'>): string {
  const running = state.steps.find(s => s.status === 'running');
  const settled = state.steps.filter(s => s.status === 'ok' || s.status === 'skipped').length;
  const current = running ? playbookStepNumber(running.index) : null;
  const n = current ?? (settled > 0 ? settled : null);
  if (n === null) return state.stepsTotal > 0 ? `0 of ${state.stepsTotal} steps` : 'starting';
  return state.stepsTotal > 0 ? `step ${n} of ${state.stepsTotal}` : `step ${n}`;
}
