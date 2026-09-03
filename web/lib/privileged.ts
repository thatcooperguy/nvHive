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
 * Two are new for the privileged tier and neither is a failure:
 * `needs-terminal` (the sudo-password hand-off — settled, but the user
 * finishes it themselves) and `disabled` (the kill switch is off).
 */
export type ToolCardStatus =
  | 'idle'
  | 'running'
  | 'ok'
  | 'error'
  | 'awaiting-confirm'
  | 'needs-terminal'
  | 'disabled'
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
    case 'disabled':
      text = PRIVILEGED_DISABLED_NOTE;
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
