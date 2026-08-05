/**
 * Wizard-meta tail handling.
 *
 * The backend persists each Wizard assistant message with a machine tail —
 * `<!-- wizard-meta: {...} -->` — carrying the tool trace, cost, and latency
 * (nvh/integrations/wizard/chat.py). Anything that renders or exports a
 * persisted wizard message must strip it; anything that wants the metadata
 * can parse it.
 */

/** Split a persisted assistant message into display text + the wizard-meta
 * JSON tail. Returns the original text untouched when no tail is present. */
export function parseWizardMeta(content: string): {
  text: string;
  meta: Record<string, unknown> | null;
} {
  const match = content.match(/\n*<!-- wizard-meta: (\{[\s\S]*?\}) -->\s*$/);
  if (!match) return { text: content, meta: null };
  let meta: Record<string, unknown> | null = null;
  try {
    meta = JSON.parse(match[1]) as Record<string, unknown>;
  } catch {
    // Tail unparseable — show the text without metadata.
  }
  return { text: content.slice(0, match.index).trimEnd(), meta };
}

/** Coerce a wizard-meta value (number or stringified number) to a positive
 * finite number, else undefined. */
export function metaNumber(value: unknown): number | undefined {
  const n = Number(value);
  return Number.isFinite(n) && n > 0 ? n : undefined;
}
