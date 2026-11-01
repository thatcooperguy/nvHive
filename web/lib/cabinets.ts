/**
 * Council cabinet (agent preset) helpers — pure functions over the
 * GET /v1/agents/presets payload. No JSX here so the module runs under plain
 * `node --test lib/cabinets.test.mjs`; the rendering lives in
 * components/CouncilAdvancedDrawer.tsx (CabinetPicker).
 *
 * The server answers with the dict nvh.core.agents.list_presets() returns:
 * `{ presets: { executive: ["CEO", "CTO", ...], ... } }` — preset name to
 * role list, no description. The picker wants a list, so `presetsFromPayload`
 * walks the dict with Object.entries in the server's order. A value may also
 * be an object `{ roles, description? }`, accepted unchanged so a later server
 * can add blurbs without a client change.
 */

import type { AgentPreset } from './types';

function stringList(value: unknown): string[] {
  return Array.isArray(value)
    ? value.filter((v): v is string => typeof v === 'string' && v.length > 0)
    : [];
}

function nonEmpty(value: unknown): string | undefined {
  return typeof value === 'string' && value.trim().length > 0 ? value : undefined;
}

/**
 * Adapt the wire dict to the picker's list, preserving the server's order.
 * Every key is a preset the council request accepts, even when its value is
 * not the expected role list (roles are then empty). Anything that is not a
 * plain object (null, undefined, an array, a string) yields [] so the picker
 * stays hidden instead of showing invented entries.
 */
export function presetsFromPayload(presets: unknown): AgentPreset[] {
  if (!presets || typeof presets !== 'object' || Array.isArray(presets)) return [];
  const out: AgentPreset[] = [];
  for (const [name, value] of Object.entries(presets as Record<string, unknown>)) {
    if (!name) continue;
    if (Array.isArray(value)) {
      out.push({ name, roles: stringList(value) });
      continue;
    }
    if (value && typeof value === 'object') {
      const wire = value as { roles?: unknown; description?: unknown };
      const roles = stringList(wire.roles);
      const description = nonEmpty(wire.description);
      out.push(description ? { name, roles, description } : { name, roles });
      continue;
    }
    out.push({ name, roles: [] });
  }
  return out;
}

/**
 * The line under the picker: the description when the server sent one,
 * otherwise the roles joined — the fallback nvh/mcp_server.py's list_cabinets
 * uses, so the web and MCP surfaces describe a cabinet the same way.
 */
export function presetSummary(preset: AgentPreset): string {
  return preset.description ?? preset.roles.join(', ');
}
