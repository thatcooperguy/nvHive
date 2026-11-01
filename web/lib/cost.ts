/**
 * Cost pill: one decision for every place the UI shows what a reply cost.
 *
 * The API sends `cost_usd` with `cost_known` beside it (nvh/api/server.py
 * `_serialize_completion`, the Wizard chat envelope and its `done` event).
 * `cost_known: false` means the catalog has no price for the model that
 * answered, so the $0 is a placeholder, not "free" — the pill says so instead
 * of hiding the value or printing a misleading $0. Three states:
 *
 *   priced  — cost_usd > 0                        → "$0.0123" (caller's precision)
 *   zero    — cost_usd is 0 and the model is priced → "$0.0000" (never hidden)
 *   unknown — cost_known === false                → "cost unknown" + a title
 *
 * No cost at all (undefined / null / unparsable, e.g. a deterministic Wizard
 * turn or an older API without the field) renders nothing, as before.
 */

export type CostPillState = 'priced' | 'zero' | 'unknown';

export interface CostPill {
  state: CostPillState;
  /** Text to render. */
  text: string;
  /** Hover explanation; set only for the unknown state. */
  title?: string;
}

export const COST_UNKNOWN_TEXT = 'cost unknown';
export const COST_UNKNOWN_TITLE =
  'This model has no price in the catalog (LiteLLM table, model_overrides.json or capabilities.yaml), ' +
  'so nvHive recorded $0 for this reply. The real cost is unknown, not zero.';

/** A finite, non-negative cost from a number or stringified number; undefined
 * otherwise (null, '', NaN, negative, boolean). Unlike `metaNumber` it keeps 0. */
export function costNumber(value: unknown): number | undefined {
  if (value === null || value === undefined || value === '' || typeof value === 'boolean') return undefined;
  const n = typeof value === 'number' ? value : Number(value);
  return Number.isFinite(n) && n >= 0 ? n : undefined;
}

/** `cost_known` as the API sends it: a real boolean, else undefined (older
 * API, persisted row without the field). */
export function costKnownFlag(value: unknown): boolean | undefined {
  return typeof value === 'boolean' ? value : undefined;
}

/** Wizard footer precision: 3 decimals from a cent up, 5 below. */
export function formatWizardCost(n: number): string {
  return n.toFixed(n >= 0.01 ? 3 : 5);
}

/** Chat card precision: 4 decimals, 5 below a tenth of a cent. */
export function formatCardCost(n: number): string {
  return n < 0.001 ? n.toFixed(5) : n.toFixed(4);
}

/**
 * Decide the pill for a reply. A non-zero cost was priced by something, so it
 * is shown as a price whatever `costKnown` says; `costKnown === false` turns
 * a zero (or missing) cost into "cost unknown"; a zero cost on a priced model
 * is "$0.0000"; no cost and no verdict is nothing to show.
 */
export function costPill(
  costUsd: unknown,
  costKnown: boolean | undefined,
  format: (n: number) => string = formatCardCost,
): CostPill | null {
  const n = costNumber(costUsd);
  if (n !== undefined && n > 0) return { state: 'priced', text: `$${format(n)}` };
  if (costKnown === false) return { state: 'unknown', text: COST_UNKNOWN_TEXT, title: COST_UNKNOWN_TITLE };
  if (n === undefined) return null;
  return { state: 'zero', text: '$0.0000' };
}
