// Regression tests for lib/cabinets.ts — the adapter between the server's
// GET /v1/agents/presets dict (`{ presets: { executive: [roles...] } }`, the
// shape nvh.core.agents.list_presets returns and tests/test_api.py asserts)
// and the list the council drawer's cabinet picker renders. Before the
// adapter the web typed the payload as a list, so `presets.length` was
// undefined and the picker never appeared.
//
// Runs on Node's built-in runner with native type stripping, like gpu.test.mjs:
//
//     cd web && node --test lib/cabinets.test.mjs
//
// The render tests use the real CabinetPicker from
// components/CouncilAdvancedDrawer.tsx. Node strips types but not JSX, so the
// component is transpiled with the installed `typescript` (a devDependency),
// its import specifiers rewritten to absolute URLs (lib/api stubbed: the
// effect that calls it never runs in a static render) and the result imported
// as a data: URL. Nothing is written to disk and nothing touches the network.

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, resolve as resolvePath } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

import { presetSummary, presetsFromPayload } from './cabinets.ts';

/** The server's dict as the API test sees it: preset name to role list, no description. */
const SERVER_PRESETS = {
  executive: ['CEO', 'CTO', 'CFO', 'CMO', 'COO'],
  engineering: ['Software Architect', 'Backend Engineer', 'Frontend Engineer', 'DevOps Engineer', 'QA Engineer'],
  code_review: ['Senior Engineer', 'Security Reviewer'],
};

// ─── presetsFromPayload ───────────────────────────────────────────────────────

test('presetsFromPayload: the server dict becomes one entry per key, in the server order, roles copied', () => {
  const list = presetsFromPayload(SERVER_PRESETS);
  assert.deepEqual(list.map(p => p.name), Object.keys(SERVER_PRESETS));
  assert.deepEqual(list, [
    { name: 'executive', roles: SERVER_PRESETS.executive },
    { name: 'engineering', roles: SERVER_PRESETS.engineering },
    { name: 'code_review', roles: SERVER_PRESETS.code_review },
  ]);
  // No description key at all: the server sends none and the picker must not print "undefined".
  for (const p of list) assert.equal('description' in p, false);
});

test('presetsFromPayload: anything that is not a plain object yields [] so the picker stays hidden', () => {
  assert.deepEqual(presetsFromPayload({}), []);
  assert.deepEqual(presetsFromPayload(null), []);
  assert.deepEqual(presetsFromPayload(undefined), []);
  assert.deepEqual(presetsFromPayload('executive'), []);
  assert.deepEqual(presetsFromPayload(42), []);
  // The shape the web used to assume: a list is not the dict, and index keys are not preset names.
  assert.deepEqual(presetsFromPayload([{ name: 'executive', roles: ['CEO'] }]), []);
});

test('presetsFromPayload: object values carry roles and an optional description; a blank description is dropped', () => {
  const list = presetsFromPayload({
    executive: { roles: ['CEO', 'CTO'], description: 'CEO, CTO — strategic decisions' },
    data: { roles: ['Data Scientist'], description: '   ' },
    full_board: { description: 'All expert personas' },
  });
  assert.deepEqual(list, [
    { name: 'executive', roles: ['CEO', 'CTO'], description: 'CEO, CTO — strategic decisions' },
    { name: 'data', roles: ['Data Scientist'] },
    { name: 'full_board', roles: [], description: 'All expert personas' },
  ]);
});

test('presetsFromPayload: non-string roles are dropped and an unexpected value still keeps the preset name', () => {
  const list = presetsFromPayload({
    executive: ['CEO', 7, null, '', 'CTO'],
    odd: 'not a list',
    '': ['ignored: empty name'],
  });
  assert.deepEqual(list, [
    { name: 'executive', roles: ['CEO', 'CTO'] },
    { name: 'odd', roles: [] },
  ]);
});

// ─── presetSummary ────────────────────────────────────────────────────────────

test('presetSummary: the description wins; without one the roles are joined, as the MCP list_cabinets table does', () => {
  assert.equal(presetSummary({ name: 'executive', roles: ['CEO', 'CTO'], description: 'Strategic decisions' }), 'Strategic decisions');
  assert.equal(presetSummary({ name: 'executive', roles: ['CEO', 'CTO'] }), 'CEO, CTO');
  assert.equal(presetSummary({ name: 'odd', roles: [] }), '');
});

// ─── CabinetPicker (the real component, statically rendered) ─────────────────

const here = dirname(fileURLToPath(import.meta.url)); // web/lib
const webRoot = resolvePath(here, '..');
const drawerPath = resolvePath(webRoot, 'components', 'CouncilAdvancedDrawer.tsx');
const API_STUB = 'data:text/javascript,' + encodeURIComponent('export async function getAgentPresets() { return []; }');

function rewriteSpecifier(spec) {
  if (spec === '@/lib/api') return API_STUB;
  if (spec.startsWith('@/')) return pathToFileURL(resolvePath(webRoot, `${spec.slice(2)}.ts`)).href;
  if (spec.startsWith('.')) return pathToFileURL(resolvePath(dirname(drawerPath), spec)).href;
  return import.meta.resolve(spec); // react, react/jsx-runtime: the same copies web/node_modules serves
}

let drawerModule;
async function loadDrawer() {
  if (drawerModule) return drawerModule;
  const { default: ts } = await import('typescript');
  const { outputText, diagnostics } = ts.transpileModule(readFileSync(drawerPath, 'utf8'), {
    fileName: drawerPath,
    reportDiagnostics: true,
    compilerOptions: { jsx: ts.JsxEmit.ReactJSX, module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
  });
  assert.deepEqual((diagnostics ?? []).map(d => d.messageText), [], 'the drawer must transpile cleanly');
  const js = outputText.replace(
    /^(import\s+(?:[^'"\n]*?\s+from\s+)?)(['"])([^'"\n]+)\2/gm,
    (_m, head, quote, spec) => `${head}${quote}${rewriteSpecifier(spec)}${quote}`,
  );
  drawerModule = await import(`data:text/javascript;base64,${Buffer.from(js, 'utf8').toString('base64')}`);
  return drawerModule;
}

async function renderPicker(props) {
  const [{ CabinetPicker }, { createElement }, { default: ReactDOMServer }] = await Promise.all([
    loadDrawer(),
    import('react'),
    import('react-dom/server'),
  ]);
  return ReactDOMServer.renderToStaticMarkup(createElement(CabinetPicker, { onChange() {}, ...props }));
}

/** The rendered <option>s in document order: { value, label, selected }. */
function optionsOf(html) {
  return [...html.matchAll(/<option\b([^>]*)>([^<]*)<\/option>/g)].map(([, attrs, label]) => ({
    value: /value="([^"]*)"/.exec(attrs)?.[1] ?? null,
    label,
    selected: /\bselected\b/.test(attrs),
  }));
}

test('CabinetPicker: renders a select with "None" plus every server preset, in the server order', async () => {
  const presets = presetsFromPayload(SERVER_PRESETS);
  const html = await renderPicker({ presets, value: '' });
  assert.equal((html.match(/<select\b/g) ?? []).length, 1);
  const options = optionsOf(html);
  assert.deepEqual(options.map(o => o.value), ['', ...Object.keys(SERVER_PRESETS)]);
  assert.deepEqual(options.map(o => o.label), ['None', ...Object.keys(SERVER_PRESETS)]);
  // Nothing chosen: "None" is selected and no summary line is printed.
  assert.deepEqual(options.filter(o => o.selected).map(o => o.value), ['']);
  assert.equal(html.includes('undefined'), false);
  assert.equal(html.includes('CEO, CTO'), false);
  // The label is wired to the select.
  const forId = /<label for="([^"]+)"/.exec(html)?.[1];
  assert.ok(forId, 'label carries a for= attribute');
  assert.ok(html.includes(`<select id="${forId}"`), 'select carries the matching id');
});

test('CabinetPicker: the chosen cabinet is selected and summarised by its roles when the server sent no description', async () => {
  const presets = presetsFromPayload(SERVER_PRESETS);
  const html = await renderPicker({ presets, value: 'engineering' });
  assert.deepEqual(optionsOf(html).filter(o => o.selected).map(o => o.value), ['engineering']);
  assert.ok(html.includes(SERVER_PRESETS.engineering.join(', ')), 'roles joined under the select');
  assert.equal(html.includes('undefined'), false);
});

test('CabinetPicker: a description, when the server sends one, replaces the role list; disabled reaches the select', async () => {
  const presets = presetsFromPayload({
    ...SERVER_PRESETS,
    executive: { roles: SERVER_PRESETS.executive, description: 'CEO, CTO, CFO, CMO, COO — strategic decisions' },
  });
  const html = await renderPicker({ presets, value: 'executive', disabled: true });
  assert.deepEqual(optionsOf(html).map(o => o.value), ['', ...Object.keys(SERVER_PRESETS)]);
  assert.ok(html.includes('strategic decisions'));
  assert.ok(/<select\b[^>]*\bdisabled\b/.test(html), 'disabled attribute on the select');
});
