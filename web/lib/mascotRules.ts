/**
 * Pure mascot rules — no React, no DOM, no `@/` alias and no runtime imports
 * — so they run under plain `node --test lib/mascot.test.mjs`. lib/mascot.ts
 * (the state machine and the store) cannot be imported by Node: it pulls in
 * the sprite manifest JSON and `@/lib/api` at module load. The two rules the
 * 0.46 question card added live here; mascot.ts calls them and re-exports
 * the labels, so every other module keeps importing from `@/lib/mascot`.
 *
 *  1. Which `asking` wording the bubble gets, from the pending cards: a
 *     privileged (sudo) card wins, every card an `ask_user` question → the
 *     question wording, anything else the ordinary confirmation.
 *  2. Whether a streamed `tool_call` event moves the sprite to `working`.
 *     Since 0.46 the event carries the registry's `safety_class`; only an
 *     `auto` call is work in progress. A `confirm` / `privileged` call is
 *     about to become a card, and the card decides — the sprite must not
 *     flash "running a tool" over a question nobody has answered yet.
 */

/** The `asking` bubble when a pending card is a privileged (sudo) change. */
export const MASCOT_ASKING_PRIVILEGED_LABEL = 'the Wizard needs your approval for a privileged change';
/** The `asking` bubble when every pending card is an `ask_user` question. */
export const MASCOT_ASKING_QUESTION_LABEL = 'the Wizard has a question for you';
/** The question tool's name (lib/privileged.ts `QUESTION_TOOL` spells it too;
 * the node test pins the two equal). Exact match. */
export const MASCOT_QUESTION_TOOL = 'ask_user';

export type MascotAskingKind = 'privileged' | 'question' | 'confirm';

/**
 * Which `asking` wording fits the cards still waiting for a click.
 * `pendingSafetyClasses` are the catalog classes of those calls (unknown
 * entries are `undefined`), `pendingNames` their tool names. Exact string
 * matches only: a class this build has never heard of is a confirm card,
 * the same way it gets the ordinary card chrome.
 */
export function askingKind(
  pendingSafetyClasses: readonly (string | undefined | null)[],
  pendingNames: readonly string[] = [],
): MascotAskingKind {
  if (pendingSafetyClasses.some(cls => cls === 'privileged')) return 'privileged';
  if (pendingNames.length > 0 && pendingNames.every(name => name === MASCOT_QUESTION_TOOL)) return 'question';
  return 'confirm';
}

/**
 * Does a `tool_call` event move the mascot to `working`? Yes for an `auto`
 * call and for an event with no class at all (an older server: every
 * `tool_call` it sent was about to run). Any other value — `confirm`,
 * `privileged`, `unknown`, something new — means a card is coming and the
 * card decides, so the event leaves the state alone.
 */
export function toolCallMovesMascot(safetyClass: unknown): boolean {
  return safetyClass === undefined || safetyClass === null || safetyClass === 'auto';
}
