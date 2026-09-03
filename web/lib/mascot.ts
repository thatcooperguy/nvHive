/**
 * Mascot state machine + store.
 *
 * The mascot (components/Mascot.tsx) is a sprite-sheet character that mirrors
 * what the Wizard is doing. This module owns three things:
 *
 *  1. The sprite manifest types + the bundled manifest. There is ONE copy of
 *     the timing data — public/mascot/manifest.json — imported here at build
 *     time (tsconfig `resolveJsonModule`) and validated with
 *     `isMascotManifest`, so the sprite renders before the runtime fetch lands
 *     and keeps rendering if that fetch fails. A manifest that fails
 *     validation degrades to a static frame-0 sprite rather than a crash.
 *  2. `deriveMascotState` — the pure mapping from Wizard SSE events to states.
 *     It reads the payload, not just the type: `done` that still carries
 *     confirm-class tool calls keeps the mascot *asking* until the user runs
 *     or skips them (WizardChat publishes those follow-ups). `error` whose
 *     `fallback_reason` is a specialist's deliberate refusal is an answer
 *     and settles like `done`; a genuine `error` with pending cards plays
 *     the error strip and then *resumes asking* instead of idling
 *     (`deriveMascotResume`; `applyMascotEvent` does both).
 *  3. A tiny pub/sub store (`useMascotStore` via useSyncExternalStore) so
 *     WizardChat publishes and Mascot subscribes without prop drilling.
 *     Timed transitions (happy → idle, error → idle or back to asking, idle
 *     → sleeping after 90s, tip auto-hide) live here too, so the component
 *     only renders.
 *
 * Swapping the art (e.g. an approved likeness) is a file swap — see
 * docs/MASCOT.md. Nothing here is specific to the placeholder drawing.
 */

import { useSyncExternalStore } from 'react';
import { isWizardDeliberateRefusal } from '@/lib/api';
import bundledManifest from '@/public/mascot/manifest.json';

export const MASCOT_STATES = [
  'idle',
  'thinking',
  'working',
  'asking',
  'happy',
  'error',
  'sleeping',
] as const;

export type MascotState = (typeof MASCOT_STATES)[number];

export function isMascotState(value: unknown): value is MascotState {
  return typeof value === 'string' && (MASCOT_STATES as readonly string[]).includes(value);
}

export interface MascotStateDef {
  /** Frame indices into the sheet, `index = row * columns + column`. Frames
   * must be contiguous and sit in one row (CSS steps() walks a strip). */
  frames: number[];
  /** Repeat the strip forever; `false` plays once and holds the last frame. */
  loop: boolean;
  /** State to advance to after `holdMs` (default: one pass of the frames). */
  next?: MascotState;
  holdMs?: number;
  /** Per-state frame rate; falls back to the sheet-wide `fps`. */
  fps?: number;
}

export interface MascotManifest {
  /** URL of the sprite sheet, served from web/public. */
  sheet: string;
  frameWidth: number;
  frameHeight: number;
  /** Default frames per second. */
  fps: number;
  /** Frames per row in the sheet (default 4). */
  columns?: number;
  states: Record<MascotState, MascotStateDef>;
}

/**
 * Screen-reader label per state (the sprite's aria-label, re-announced on
 * every state change). `error` is reached only by a genuine failure — a
 * failed stream, or the offline helper standing in for a broken model path.
 * A specialist's deliberate refusal is an answer, so it announces as `happy`
 * ("finished") like any other `done`, never as "something went wrong": see
 * `deriveMascotState`.
 */
export const MASCOT_STATE_LABELS: Record<MascotState, string> = {
  idle: 'idle and ready',
  thinking: 'the Wizard is thinking',
  working: 'the Wizard is running a tool',
  asking: 'the Wizard needs your confirmation',
  happy: 'the Wizard finished',
  error: 'something went wrong',
  sleeping: 'asleep — click to wake',
};

/**
 * What `asking` means when one of the pending cards is a privileged (sudo)
 * one — a stronger sentence than the generic "needs your confirmation",
 * because the click ahead changes the machine rather than the workspace.
 * Used for the bubble WizardChat raises when a red card first appears and as
 * the accessible name of the card's approval region.
 */
export const MASCOT_ASKING_PRIVILEGED_LABEL = 'the Wizard needs your approval for a privileged change';

/**
 * Label for the `asking` state given the safety classes of the calls still
 * waiting for a click. Cheap by construction: the caller already holds the
 * schemas (WizardChat's tool catalog), so this is a scan of a short list, not
 * a lookup. Anything that is not exactly `privileged` — including a class
 * this build has never heard of — keeps the ordinary confirmation wording,
 * the same way an unknown class keeps the ordinary card.
 */
export function mascotAskingLabel(
  pendingSafetyClasses: readonly (string | undefined | null)[],
): string {
  return pendingSafetyClasses.some(cls => cls === 'privileged')
    ? MASCOT_ASKING_PRIVILEGED_LABEL
    : MASCOT_STATE_LABELS.asking;
}

function isPositive(n: unknown): n is number {
  return typeof n === 'number' && Number.isFinite(n) && n > 0;
}

/** Structural check for a manifest (bundled or runtime-fetched). Extra keys
 * are tolerated. */
export function isMascotManifest(value: unknown): value is MascotManifest {
  if (!value || typeof value !== 'object') return false;
  const m = value as Record<string, unknown>;
  if (typeof m.sheet !== 'string' || !m.sheet) return false;
  if (!isPositive(m.frameWidth) || !isPositive(m.frameHeight) || !isPositive(m.fps)) return false;
  if (m.columns !== undefined && !isPositive(m.columns)) return false;
  const states = m.states as Record<string, unknown> | undefined;
  if (!states || typeof states !== 'object') return false;
  return MASCOT_STATES.every(name => {
    const def = states[name] as Record<string, unknown> | undefined;
    if (!def || typeof def !== 'object') return false;
    const frames = def.frames;
    return (
      Array.isArray(frames) &&
      frames.length > 0 &&
      frames.every(f => Number.isInteger(f) && (f as number) >= 0) &&
      typeof def.loop === 'boolean' &&
      (def.next === undefined || isMascotState(def.next)) &&
      (def.holdMs === undefined || isPositive(def.holdMs)) &&
      (def.fps === undefined || isPositive(def.fps))
    );
  });
}

/**
 * The manifest shipped in the bundle: public/mascot/manifest.json, validated
 * once at module load. If someone commits a malformed manifest the mascot
 * degrades to frame 0 of a 64px sheet (and logs once) instead of taking every
 * page down with it — LayoutShell mounts the mascot everywhere.
 */
export const DEFAULT_MANIFEST: MascotManifest = loadBundledManifest();

function loadBundledManifest(): MascotManifest {
  if (isMascotManifest(bundledManifest)) return bundledManifest;
  if (typeof console !== 'undefined') {
    console.error('[mascot] public/mascot/manifest.json failed validation; rendering a static sprite');
  }
  const frozen: MascotStateDef = { frames: [0], loop: false };
  const states = Object.fromEntries(
    MASCOT_STATES.map(s => [s, frozen]),
  ) as Record<MascotState, MascotStateDef>;
  return { sheet: '/mascot/sheet.png', frameWidth: 64, frameHeight: 64, fps: 1, columns: 4, states };
}

export interface MascotStrip {
  row: number;
  startCol: number;
  count: number;
  fps: number;
  loop: boolean;
}

/** Resolve a state's frames to a contiguous strip the CSS animation can walk.
 * Non-contiguous or row-wrapping frame lists degrade to a static first frame. */
export function resolveStrip(manifest: MascotManifest, state: MascotState): MascotStrip {
  const cols = manifest.columns ?? 4;
  const def = manifest.states[state] ?? manifest.states.idle;
  const frames = def.frames.length > 0 ? def.frames : [0];
  const first = frames[0];
  const row = Math.floor(first / cols);
  const startCol = first % cols;
  const contiguous = frames.every((f, i) => f === first + i) && startCol + frames.length <= cols;
  return {
    row,
    startCol,
    count: contiguous ? frames.length : 1,
    fps: def.fps ?? manifest.fps,
    loop: def.loop,
  };
}

// ─── Event → state ───────────────────────────────────────────────────────────

/** The slice of a Wizard SSE event the mapping needs. `tool_calls` is the
 * confirm-class list that `confirm_required` and `done` both carry. The
 * server's `error` event does NOT carry it, so WizardChat attaches the list
 * from the `confirm_required` it received earlier in the same turn before
 * handing the event over — the mapping treats all three alike.
 * `fallback_reason` rides along on `error` untouched: its value, not its
 * presence, says whether the event is a failure (`isWizardDeliberateRefusal`). */
export interface MascotEventLike {
  type: string;
  tool_calls?: unknown;
  /** `error` only: why no LLM answered. `WIZARD_LOCAL_ONLY_FALLBACK_REASON`
   * is a specialist deliberately declining — an answer, not a failure. */
  fallback_reason?: string | null;
}

/**
 * Map a Wizard SSE event to a mascot state. Returns `null` for events that
 * should not change the state (`token` streams keep whatever is showing).
 *
 * The backend emits `confirm_required` immediately before `done`, so a
 * type-only mapping would flip asking → happy in the same flush and the
 * mascot would idle while confirm cards wait. `done` therefore reads its
 * payload: pending confirm-class calls → `asking` (WizardChat publishes
 * `working` when the user runs one and `idle` when they skip); none → `happy`.
 *
 * `error` reads its payload too. The server sets `fallback_reason` on every
 * error event that ended in deterministic text, and one value —
 * `WIZARD_LOCAL_ONLY_FALLBACK_REASON`, a local-only specialist declining an
 * explicit pin because no local provider was up — is the turn's *answer*:
 * WizardChat shows that text attributed and draws no banner, so the mascot
 * must not flinch either. Such an event lands exactly like `done` (asking
 * while cards are pending, else happy) and never announces "something went
 * wrong". Every other `error` — the LLM exception, "engine not initialized",
 * a failed stream — shows the error strip; the failure deserves its moment,
 * and where it settles afterwards depends on the same pending-cards check:
 * see `deriveMascotResume`. Timed follow-ups (happy → idle, error → idle)
 * come from the manifest unless a resume overrides them.
 */
export function deriveMascotState(event: MascotEventLike | null | undefined): MascotState | null {
  switch (event?.type) {
    case 'iteration':
      return 'thinking';
    case 'tool_call':
    case 'tool_result':
      return 'working';
    case 'confirm_required':
      return 'asking';
    case 'done':
      return settledState(event);
    case 'error':
      // Same split as WizardChat's `wizardErrorBanner`: a deliberate refusal
      // is an answer, so it settles like `done`; only a genuine failure
      // plays the error strip.
      return isWizardDeliberateRefusal(event) ? settledState(event) : 'error';
    default:
      return null;
  }
}

/** Where a finished turn lands: `asking` while confirm-class cards still
 * wait for Run / Skip, otherwise `happy`. Shared by `done` and by an `error`
 * that is really a specialist's deliberate refusal. */
function settledState(event: MascotEventLike): MascotState {
  return hasPendingToolCalls(event) ? 'asking' : 'happy';
}

/**
 * Where a transient state should settle instead of the manifest's `next`, or
 * `undefined` to leave the manifest in charge.
 *
 * The backend also emits `confirm_required` immediately before `error` when a
 * later follow-up iteration fails: the cards an earlier iteration surfaced
 * still need the user's Run / Skip. A plain error → idle would leave the
 * mascot idling under pending cards, exactly the bug `done` avoids. So an
 * `error` that still carries confirm-class calls plays the error strip for
 * its hold and then resumes `asking` — the same `tool_calls` check `done`
 * uses — and WizardChat's settle-on-Run/Skip logic takes over from there.
 *
 * A deliberate refusal never enters `error` (`deriveMascotState` lands it on
 * `asking` or `happy` directly), so it has nothing to resume from and the
 * manifest stays in charge.
 */
export function deriveMascotResume(event: MascotEventLike | null | undefined): MascotState | undefined {
  if (event?.type !== 'error') return undefined;
  if (isWizardDeliberateRefusal(event)) return undefined;
  return hasPendingToolCalls(event) ? 'asking' : undefined;
}

/**
 * Publish a Wizard SSE event to the store: derive the state and its resume
 * target and set both. The one call WizardChat makes per stream event.
 */
export function applyMascotEvent(event: MascotEventLike | null | undefined): void {
  const state = deriveMascotState(event);
  if (!state) return;
  setMascotState(state, { resume: deriveMascotResume(event) });
}

function hasPendingToolCalls(event: MascotEventLike): boolean {
  return Array.isArray(event.tool_calls) && event.tool_calls.length > 0;
}

/**
 * State the mascot should show when the user types in the composer, given
 * what it is showing now. Typing only *settles* the mascot: a finished
 * outcome (`happy` / `error`) or a doze (`sleeping`) goes back to `idle`.
 * It never interrupts an in-flight turn (`thinking` / `working`) or a
 * pending confirmation (`asking`) — those end when the stream or the cards
 * do, not when the user starts drafting the next question. Returns `null`
 * for "leave it alone".
 */
export function mascotStateOnTyping(current: MascotState): MascotState | null {
  switch (current) {
    case 'happy':
    case 'error':
    case 'sleeping':
      return 'idle';
    case 'idle':
      return 'idle'; // no-op transition; re-arms the sleep timer
    case 'thinking':
    case 'working':
    case 'asking':
      return null;
  }
}

// ─── Store ───────────────────────────────────────────────────────────────────

export interface MascotTip {
  id: string;
  text: string;
  ttlMs: number;
}

export interface MascotSnapshot {
  state: MascotState;
  tip: MascotTip | null;
  manifest: MascotManifest;
}

/** Idle this long with nothing happening → the mascot dozes off. */
export const MASCOT_SLEEP_AFTER_MS = 90_000;
export const MASCOT_TIP_DEFAULT_TTL_MS = 12_000;
export const MASCOT_HIDDEN_KEY = 'nvh.mascot.hidden';
/** Session flag: the diagnostics probe behind the "Heads up" tip already ran. */
export const MASCOT_DIAG_TIP_PROBED_KEY = 'nvh.mascot.diagtip.probed';
const TIP_SEEN_PREFIX = 'nvh.mascot.tip.';

const INITIAL_SNAPSHOT: MascotSnapshot = { state: 'idle', tip: null, manifest: DEFAULT_MANIFEST };

let snapshot: MascotSnapshot = INITIAL_SNAPSHOT;
const listeners = new Set<() => void>();
const seenTipsThisLoad = new Set<string>();
let transitionTimer: ReturnType<typeof setTimeout> | null = null;
/**
 * Where the current transient state settles instead of its manifest `next`
 * (see the `resume` option of `setMascotState`). Set only for the duration of
 * that state: any state change without a new `resume` clears it. Read by the
 * transition timer and by `noteMascotTyping`, so a keystroke during the error
 * hold also lands on `asking` rather than `idle` while cards are pending.
 */
let resumeTarget: MascotState | null = null;
let sleepTimer: ReturnType<typeof setTimeout> | null = null;
let tipTimer: ReturnType<typeof setTimeout> | null = null;

function publish(patch: Partial<MascotSnapshot>): void {
  snapshot = { ...snapshot, ...patch };
  for (const listener of listeners) listener();
}

function clearTransition(): void {
  if (transitionTimer) clearTimeout(transitionTimer);
  transitionTimer = null;
}

/** Any activity postpones sleep; sleep only ever interrupts `idle`. */
function armSleepTimer(): void {
  if (typeof window === 'undefined') return;
  if (sleepTimer) clearTimeout(sleepTimer);
  sleepTimer = setTimeout(() => {
    sleepTimer = null;
    if (snapshot.state === 'idle') {
      clearTransition();
      publish({ state: 'sleeping' });
    }
  }, MASCOT_SLEEP_AFTER_MS);
}

/** Tell the store the user is around (arms the 90s sleep timer). */
export function noteMascotActivity(): void {
  armSleepTimer();
}

/**
 * The composer's keystroke hook: settle a finished / dozing mascot back to
 * `idle` (see `mascotStateOnTyping`) and postpone sleep either way. A stream
 * in flight or confirm cards pending keep their state — including an `error`
 * parked over pending cards, which settles to its `asking` resume target
 * rather than to `idle`.
 */
export function noteMascotTyping(): void {
  const next = mascotStateOnTyping(snapshot.state);
  if (!next) {
    armSleepTimer();
    return;
  }
  setMascotState(next === 'idle' && resumeTarget ? resumeTarget : next);
}

export interface SetMascotStateOptions {
  /**
   * Where to settle after this state's hold instead of the manifest's `next`.
   * Used for `error` while confirm cards are pending (→ `asking`), see
   * `deriveMascotResume`. A resume always schedules a follow-up, even for a
   * manifest state with no `next` of its own, so a custom manifest cannot
   * strand the mascot in `error` over cards that still need a click.
   */
  resume?: MascotState;
}

/**
 * Set the mascot state. `null`/`undefined` are no-ops so callers can write
 * `setMascotState(deriveMascotState(evt))` directly. Re-setting the current
 * state only postpones sleep. States with a manifest `next` (or an explicit
 * `resume`) schedule their own follow-up after `holdMs` (default: one pass
 * of the frames). The follow-up target is fixed when the state is entered;
 * anything that changes the state before the hold elapses (Run → working,
 * Skip → settle, a new turn → thinking) cancels it.
 */
export function setMascotState(
  next: MascotState | null | undefined,
  options: SetMascotStateOptions = {},
): void {
  if (!isMascotState(next)) return;
  armSleepTimer();
  if (next === snapshot.state) return;
  clearTransition();
  resumeTarget = options.resume ?? null;
  publish({ state: next });
  const def = snapshot.manifest.states[next];
  const target = resumeTarget ?? def?.next;
  if (target) {
    const fps = def?.fps ?? snapshot.manifest.fps;
    const frames = def?.frames.length ?? 1;
    const hold = def?.holdMs ?? Math.max(250, Math.round((frames / fps) * 1000));
    transitionTimer = setTimeout(() => {
      transitionTimer = null;
      setMascotState(target);
    }, hold);
  }
}

/**
 * Show a speech-bubble tip. Each `id` shows at most once per browser session
 * (sessionStorage; in-memory fallback when storage is blocked). Returns
 * whether the tip was shown. A new tip replaces the current one.
 *
 * A hidden mascot has no bubble to render into, so the call is a no-op that
 * does NOT burn the id — the tip stays available for when the user brings the
 * mascot back. Ids are only marked seen when the bubble actually renders.
 */
export function sayMascotTip(text: string, options: { id: string; ttlMs?: number }): boolean {
  if (typeof window === 'undefined') return false;
  if (readMascotHidden()) return false;
  const { id } = options;
  if (seenTipsThisLoad.has(id)) return false;
  const key = TIP_SEEN_PREFIX + id;
  try {
    if (window.sessionStorage.getItem(key)) return false;
    window.sessionStorage.setItem(key, '1');
  } catch {
    /* storage blocked — fall back to once-per-page-load */
  }
  seenTipsThisLoad.add(id);
  const ttlMs = options.ttlMs ?? MASCOT_TIP_DEFAULT_TTL_MS;
  if (tipTimer) clearTimeout(tipTimer);
  tipTimer = null;
  publish({ tip: { id, text, ttlMs } });
  if (ttlMs > 0) {
    tipTimer = setTimeout(() => {
      tipTimer = null;
      if (snapshot.tip?.id === id) publish({ tip: null });
    }, ttlMs);
  }
  armSleepTimer();
  return true;
}

export function dismissMascotTip(): void {
  if (tipTimer) clearTimeout(tipTimer);
  tipTimer = null;
  if (snapshot.tip) publish({ tip: null });
}

/**
 * Should the Wizard page run the diagnostics probe that feeds the "Heads up"
 * tip? Only while the mascot is visible (a hidden mascot cannot show it) and
 * at most once per browser session — `markMascotTipProbed()` sets the flag
 * once a probe has completed and the tip had its chance to render.
 */
export function mascotTipProbeDue(): boolean {
  if (typeof window === 'undefined') return false;
  if (readMascotHidden()) return false;
  try {
    return !window.sessionStorage.getItem(MASCOT_DIAG_TIP_PROBED_KEY);
  } catch {
    return true;
  }
}

export function markMascotTipProbed(): void {
  if (typeof window === 'undefined') return;
  try {
    window.sessionStorage.setItem(MASCOT_DIAG_TIP_PROBED_KEY, '1');
  } catch {
    /* storage blocked — the probe may repeat on the next mount */
  }
}

/** Replace the manifest (called after fetching /mascot/manifest.json). */
export function setMascotManifest(manifest: MascotManifest): void {
  publish({ manifest });
}

export function readMascotHidden(): boolean {
  if (typeof window === 'undefined') return false;
  try {
    return window.localStorage.getItem(MASCOT_HIDDEN_KEY) === '1';
  } catch {
    return false;
  }
}

export function writeMascotHidden(hidden: boolean): void {
  if (typeof window === 'undefined') return;
  try {
    if (hidden) window.localStorage.setItem(MASCOT_HIDDEN_KEY, '1');
    else window.localStorage.removeItem(MASCOT_HIDDEN_KEY);
  } catch {
    /* localStorage unavailable */
  }
}

/** Test hook: clear timers + listeners and return to the initial snapshot. */
export function resetMascotStore(): void {
  clearTransition();
  resumeTarget = null;
  if (sleepTimer) clearTimeout(sleepTimer);
  sleepTimer = null;
  if (tipTimer) clearTimeout(tipTimer);
  tipTimer = null;
  seenTipsThisLoad.clear();
  listeners.clear();
  snapshot = INITIAL_SNAPSHOT;
}

export function subscribeMascot(listener: () => void): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

export function getMascotSnapshot(): MascotSnapshot {
  return snapshot;
}

const getServerSnapshot = (): MascotSnapshot => INITIAL_SNAPSHOT;

/** Subscribe a component to the mascot store. */
export function useMascotStore(): MascotSnapshot {
  return useSyncExternalStore(subscribeMascot, getMascotSnapshot, getServerSnapshot);
}
