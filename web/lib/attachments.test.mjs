// Regression tests for lib/attachments.ts — the pure rules behind the Wizard
// composer's image attachments (drop / paste / attach button in
// components/WizardChat.tsx) and the wire shape they produce for
// `WizardChatRequest.attachments`.
//
// Runs on Node's built-in runner with native type stripping, like
// privileged.test.mjs and gpu.test.mjs:
//
//     cd web && node --test lib/attachments.test.mjs
//
// .mjs so tsc / next build leave it alone; Node imports the .ts modules by
// explicit extension. No DOM: `File`, `btoa` and `Buffer` are Node globals.

import { test } from 'node:test';
import assert from 'node:assert/strict';

import {
  ATTACHED_IMAGES_NOTE,
  DEFAULT_IMAGE_QUESTION,
  NO_VISION_TOOL_NOTE,
  VISION_TOOL_NAMES,
  WIZARD_ATTACHMENT_MAX_BYTES,
  WIZARD_ATTACHMENT_MAX_COUNT,
  WIZARD_IMAGE_ACCEPT,
  WIZARD_IMAGE_MIME_TYPES,
  attachmentPayload,
  attachmentPayloads,
  attachmentPreview,
  base64ToDataUrl,
  buildAttachment,
  bytesToBase64,
  dataUrlToBase64,
  extensionFor,
  formatSize,
  hasVisionTool,
  imageMimeType,
  isImageFile,
  nextAttachmentId,
  pastedImageFiles,
  questionWithAttachments,
  splitDroppedFiles,
  toAttachment,
  validateAttachment,
  withAttachedImagePaths,
} from './attachments.ts';
import { planLines, planNotes } from './privileged.ts';

const MB = 1024 * 1024;

/** A `File` with only the fields the validators read. */
function fileLike(name, type = 'image/png', size = 1234) {
  return { name, type, size };
}

/** Deterministic pseudo-random bytes so a failure reproduces. */
function bytes(n) {
  const out = new Uint8Array(n);
  for (let i = 0; i < n; i += 1) out[i] = (i * 31 + 7) % 256;
  return out;
}

// ─── Limits and the accept list ───────────────────────────────────────────────

test('limits mirror the server: 6 images per message, 20 MB each (the vision tools\' cap)', () => {
  assert.equal(WIZARD_ATTACHMENT_MAX_COUNT, 6);
  assert.equal(WIZARD_ATTACHMENT_MAX_BYTES, 20 * MB);
});

test('the accept list is exactly png / jpeg / webp / gif, by MIME type and by extension', () => {
  assert.deepEqual([...WIZARD_IMAGE_MIME_TYPES], ['image/png', 'image/jpeg', 'image/webp', 'image/gif']);
  for (const mime of WIZARD_IMAGE_MIME_TYPES) assert.ok(WIZARD_IMAGE_ACCEPT.includes(mime), mime);
  for (const ext of ['.png', '.jpg', '.jpeg', '.webp', '.gif']) assert.ok(WIZARD_IMAGE_ACCEPT.includes(ext), ext);
  assert.equal(WIZARD_IMAGE_ACCEPT.includes('svg'), false);
});

// ─── imageMimeType / isImageFile ──────────────────────────────────────────────

test('imageMimeType: the browser type wins and is normalised through the alias table', () => {
  assert.equal(imageMimeType(fileLike('a.png', 'image/png')), 'image/png');
  assert.equal(imageMimeType(fileLike('a.jpg', 'image/jpeg')), 'image/jpeg');
  assert.equal(imageMimeType(fileLike('a.jpg', 'image/jpg')), 'image/jpeg');
  assert.equal(imageMimeType(fileLike('a.jpg', 'image/pjpeg')), 'image/jpeg');
  assert.equal(imageMimeType(fileLike('a.png', 'image/x-png')), 'image/png');
  assert.equal(imageMimeType(fileLike('a.webp', 'IMAGE/WEBP')), 'image/webp');
  assert.equal(imageMimeType(fileLike('a.gif', 'image/gif; charset=binary')), 'image/gif');
  // A type that names another format is refused even when the name says .png.
  assert.equal(imageMimeType(fileLike('a.png', 'application/pdf')), null);
  assert.equal(imageMimeType(fileLike('a.png', 'image/svg+xml')), null);
  assert.equal(imageMimeType(fileLike('a.bmp', 'image/bmp')), null);
  assert.equal(imageMimeType(fileLike('a.heic', 'image/heic')), null);
});

test('imageMimeType: an empty type falls back to the extension, case-insensitively', () => {
  assert.equal(imageMimeType(fileLike('Screenshot.PNG', '')), 'image/png');
  assert.equal(imageMimeType(fileLike('photo.JPEG', '')), 'image/jpeg');
  assert.equal(imageMimeType(fileLike('photo.jpg', '')), 'image/jpeg');
  assert.equal(imageMimeType(fileLike('anim.gif', '')), 'image/gif');
  assert.equal(imageMimeType(fileLike('pic.webp', '')), 'image/webp');
  assert.equal(imageMimeType(fileLike('notes.pdf', '')), null);
  assert.equal(imageMimeType(fileLike('README', '')), null);
  assert.equal(imageMimeType(fileLike('.png', '')), null);
  assert.equal(imageMimeType(fileLike('trailing.', '')), null);
  assert.equal(imageMimeType(null), null);
  assert.equal(imageMimeType(undefined), null);
  assert.equal(imageMimeType({}), null);
});

test('isImageFile: the four image types attach; everything else is a document for RAG', () => {
  assert.equal(isImageFile(fileLike('a.png')), true);
  assert.equal(isImageFile(fileLike('a.webp', 'image/webp')), true);
  assert.equal(isImageFile(fileLike('a.pdf', 'application/pdf')), false);
  assert.equal(isImageFile(fileLike('a.md', 'text/markdown')), false);
  assert.equal(isImageFile(fileLike('a.svg', 'image/svg+xml')), false);
});

// ─── formatSize ───────────────────────────────────────────────────────────────

test('formatSize: bytes below 1 KB, one decimal above, trailing .0 dropped', () => {
  assert.equal(formatSize(0), '0 B');
  assert.equal(formatSize(512), '512 B');
  assert.equal(formatSize(1023), '1023 B');
  assert.equal(formatSize(1024), '1 KB');
  assert.equal(formatSize(1536), '1.5 KB');
  assert.equal(formatSize(2.5 * MB), '2.5 MB');
  assert.equal(formatSize(25 * MB), '25 MB');
  assert.equal(formatSize(150 * MB), '150 MB');
  assert.equal(formatSize(1.5 * 1024 * MB), '1.5 GB');
  assert.equal(formatSize(-5), '0 B');
  assert.equal(formatSize(Number.NaN), '0 B');
});

// ─── validateAttachment ───────────────────────────────────────────────────────

test('validateAttachment: an accepted image answers ok with the normalised MIME type', () => {
  assert.deepEqual(validateAttachment(fileLike('a.jpg', 'image/jpg', 10), 0), { ok: true, mime_type: 'image/jpeg' });
  assert.deepEqual(validateAttachment(fileLike('a.PNG', '', 10), 0), { ok: true, mime_type: 'image/png' });
});

test('validateAttachment: the count cap — the sixth is fine, the seventh is not', () => {
  assert.equal(validateAttachment(fileLike('a.png'), WIZARD_ATTACHMENT_MAX_COUNT - 1).ok, true);
  const seventh = validateAttachment(fileLike('a.png'), WIZARD_ATTACHMENT_MAX_COUNT);
  assert.equal(seventh.ok, false);
  assert.match(seventh.reason, /at most 6 images/);
  assert.match(seventh.reason, /^a\.png/);
});

test('validateAttachment: a document is refused and pointed at RAG ingest', () => {
  const verdict = validateAttachment(fileLike('notes.pdf', 'application/pdf'), 0);
  assert.equal(verdict.ok, false);
  assert.match(verdict.reason, /^notes\.pdf: not an image/);
  assert.match(verdict.reason, /RAG/);
});

test('validateAttachment: empty and oversized files are refused; exactly 20 MB is accepted', () => {
  assert.deepEqual(validateAttachment(fileLike('a.png', 'image/png', 0), 0), { ok: false, reason: 'a.png is empty' });
  assert.equal(validateAttachment(fileLike('a.png', 'image/png', WIZARD_ATTACHMENT_MAX_BYTES), 0).ok, true);
  const big = validateAttachment(fileLike('a.png', 'image/png', WIZARD_ATTACHMENT_MAX_BYTES + 1), 0);
  assert.equal(big.ok, false);
  assert.match(big.reason, /the limit is 20 MB/);
  assert.match(big.reason, /^a\.png is 20 MB/);
  const huge = validateAttachment(fileLike('a.png', 'image/png', 30.1 * MB), 0);
  assert.match(huge.reason, /is 30\.1 MB/);
});

test('validateAttachment: a nameless file is called "image" in the reason', () => {
  const verdict = validateAttachment({ type: 'application/zip', size: 5 }, 0);
  assert.equal(verdict.ok, false);
  assert.match(verdict.reason, /^image: not an image/);
});

// ─── base64 helpers ───────────────────────────────────────────────────────────

test('bytesToBase64 matches Buffer for empty, tiny and larger-than-one-chunk inputs', () => {
  for (const n of [0, 1, 2, 3, 4, 0x8000 - 1, 0x8000, 0x8000 + 1, 100_000]) {
    const raw = bytes(n);
    assert.equal(bytesToBase64(raw), Buffer.from(raw).toString('base64'), `n=${n}`);
  }
});

test('dataUrlToBase64 strips a data: prefix, drops whitespace and leaves raw base64 alone', () => {
  assert.equal(dataUrlToBase64('data:image/png;base64,QUJD'), 'QUJD');
  assert.equal(dataUrlToBase64('  QUJD  '), 'QUJD');
  assert.equal(dataUrlToBase64('QU\nJD'), 'QUJD');
  assert.equal(dataUrlToBase64('data:image/png;base64'), '');
  assert.equal(dataUrlToBase64(''), '');
});

test('base64ToDataUrl / dataUrlToBase64 round-trip', () => {
  const url = base64ToDataUrl('image/webp', 'QUJD');
  assert.equal(url, 'data:image/webp;base64,QUJD');
  assert.equal(dataUrlToBase64(url), 'QUJD');
});

test('extensionFor maps each accepted MIME type to its file extension', () => {
  assert.equal(extensionFor('image/png'), 'png');
  assert.equal(extensionFor('image/jpeg'), 'jpg');
  assert.equal(extensionFor('image/webp'), 'webp');
  assert.equal(extensionFor('image/gif'), 'gif');
});

// ─── buildAttachment / payload / preview ──────────────────────────────────────

test('buildAttachment: content is raw base64 even when handed a data: URL; preview is a data: URL', () => {
  const a = buildAttachment({ id: 'x1', name: 'shot.png', mime_type: 'image/png', size: 3, content: 'data:image/png;base64,QUJD' });
  assert.equal(a.id, 'x1');
  assert.equal(a.name, 'shot.png');
  assert.equal(a.size, 3);
  assert.equal(a.mime_type, 'image/png');
  assert.equal(a.content, 'QUJD');
  assert.equal(a.is_image, true);
  assert.equal(a.previewUrl, 'data:image/png;base64,QUJD');
});

test('buildAttachment: a blank name becomes image.<ext>; a missing id is generated and unique', () => {
  const a = buildAttachment({ name: '   ', mime_type: 'image/jpeg', size: 3, content: 'QUJD' });
  assert.equal(a.name, 'image.jpg');
  assert.ok(a.id.length > 0);
  const ids = new Set(Array.from({ length: 1000 }, () => nextAttachmentId()));
  assert.equal(ids.size, 1000);
});

test('attachmentPayload: exactly the four wire fields, nothing local', () => {
  const a = buildAttachment({ id: 'x1', name: 'shot.png', mime_type: 'image/png', size: 3, content: 'QUJD' });
  const payload = attachmentPayload(a);
  assert.deepEqual(Object.keys(payload).sort(), ['content', 'is_image', 'mime_type', 'name']);
  assert.deepEqual(payload, { name: 'shot.png', content: 'QUJD', mime_type: 'image/png', is_image: true });
  assert.equal(payload.content.startsWith('data:'), false);
});

test('attachmentPayloads: maps every pending image and never exceeds the count cap', () => {
  const many = Array.from({ length: 8 }, (_, i) =>
    buildAttachment({ id: `a${i}`, name: `s${i}.png`, mime_type: 'image/png', size: 3, content: 'QUJD' }));
  assert.equal(attachmentPayloads([]).length, 0);
  assert.equal(attachmentPayloads(many.slice(0, 2)).length, 2);
  assert.equal(attachmentPayloads(many).length, WIZARD_ATTACHMENT_MAX_COUNT);
  assert.equal(attachmentPayloads(many)[0].name, 's0.png');
});

test('attachmentPreview: what the sent bubble keeps — no payload', () => {
  const a = buildAttachment({ id: 'x1', name: 'shot.png', mime_type: 'image/png', size: 3, content: 'QUJD' });
  const preview = attachmentPreview(a);
  assert.deepEqual(Object.keys(preview).sort(), ['id', 'mime_type', 'name', 'previewUrl', 'size']);
  assert.equal('content' in preview, false);
  assert.equal(preview.previewUrl, a.previewUrl);
});

// ─── toAttachment (a real File) ───────────────────────────────────────────────

test('toAttachment: reads a File into the wire payload with raw base64 content', async () => {
  const raw = bytes(70_000);
  const file = new File([raw], 'board.jpg', { type: 'image/jpg' });
  const a = await toAttachment(file, 'id-1');
  assert.equal(a.id, 'id-1');
  assert.equal(a.name, 'board.jpg');
  assert.equal(a.size, 70_000);
  assert.equal(a.mime_type, 'image/jpeg');
  assert.equal(a.content, Buffer.from(raw).toString('base64'));
  assert.equal(a.previewUrl, `data:image/jpeg;base64,${a.content}`);
  assert.deepEqual(attachmentPayload(a), { name: 'board.jpg', content: a.content, mime_type: 'image/jpeg', is_image: true });
});

test('toAttachment: a File with no type uses its extension; a document is rejected', async () => {
  const a = await toAttachment(new File([bytes(10)], 'shot.PNG', { type: '' }));
  assert.equal(a.mime_type, 'image/png');
  await assert.rejects(
    () => toAttachment(new File([bytes(10)], 'notes.pdf', { type: 'application/pdf' })),
    /notes\.pdf: not an accepted image type/,
  );
});

// ─── splitDroppedFiles / pastedImageFiles ─────────────────────────────────────

test('splitDroppedFiles: images attach, documents go to RAG ingest, order kept', () => {
  const png = fileLike('a.png');
  const pdf = fileLike('b.pdf', 'application/pdf');
  const jpg = fileLike('c.jpg', '');
  const md = fileLike('d.md', 'text/markdown');
  const { images, documents } = splitDroppedFiles([png, pdf, jpg, md]);
  assert.deepEqual(images, [png, jpg]);
  assert.deepEqual(documents, [pdf, md]);
  assert.deepEqual(splitDroppedFiles([]), { images: [], documents: [] });
});

test('pastedImageFiles: only file items of an image type; text pastes give [] so the textarea keeps them', () => {
  const shot = new File([bytes(4)], 'image.png', { type: 'image/png' });
  const items = [
    { kind: 'string', type: 'text/plain', getAsFile: () => null },
    { kind: 'file', type: 'application/pdf', getAsFile: () => new File([bytes(4)], 'x.pdf', { type: 'application/pdf' }) },
    { kind: 'file', type: 'image/png', getAsFile: () => shot },
    { kind: 'file', type: 'image/gif', getAsFile: () => null },
  ];
  assert.deepEqual(pastedImageFiles(items), [shot]);
  assert.deepEqual(pastedImageFiles([{ kind: 'string', type: 'text/plain', getAsFile: () => null }]), []);
  assert.deepEqual(pastedImageFiles(null), []);
  assert.deepEqual(pastedImageFiles(undefined), []);
});

// ─── questionWithAttachments / hasVisionTool ──────────────────────────────────

test('questionWithAttachments: typed text wins; images alone ask the default; nothing stays nothing', () => {
  assert.equal(questionWithAttachments('  what is this?  ', 1), 'what is this?');
  assert.equal(questionWithAttachments('what is this?', 0), 'what is this?');
  assert.equal(questionWithAttachments('', 2), DEFAULT_IMAGE_QUESTION);
  assert.equal(questionWithAttachments('   ', 1), DEFAULT_IMAGE_QUESTION);
  assert.equal(questionWithAttachments('', 0), '');
  assert.ok(DEFAULT_IMAGE_QUESTION.length > 0);
});

test('hasVisionTool: true when the catalog carries analyze_image or read_text_from_image', () => {
  assert.deepEqual([...VISION_TOOL_NAMES], ['analyze_image', 'read_text_from_image']);
  assert.equal(hasVisionTool(['rag_ask', 'analyze_image']), true);
  assert.equal(hasVisionTool(new Set(['read_text_from_image'])), true);
  assert.equal(hasVisionTool(new Map([['shell', {}], ['run_code', {}]]).keys()), false);
  assert.equal(hasVisionTool([]), false);
  assert.ok(NO_VISION_TOOL_NOTE.includes('vision tool'));
});

test('withAttachedImagePaths: the server\'s append_attached_images, byte for byte', () => {
  // Mirrors chat.py: ATTACHED_IMAGES_NOTE is the literal the server appends.
  assert.equal(ATTACHED_IMAGES_NOTE, 'Attached images (use analyze_image or read_text_from_image on these paths):');
  assert.equal(
    withAttachedImagePaths('what is this?\n', ['/h/rag/uploads/wizard/c1/a.png', '/h/rag/uploads/wizard/c1/b.jpg']),
    'what is this?\n\nAttached images (use analyze_image or read_text_from_image on these paths): '
      + '/h/rag/uploads/wizard/c1/a.png, /h/rag/uploads/wizard/c1/b.jpg',
  );
  // No paths, blank paths: the question untouched.
  assert.equal(withAttachedImagePaths('what is this?', []), 'what is this?');
  assert.equal(withAttachedImagePaths('what is this?', ['', '  ']), 'what is this?');
  assert.equal(withAttachedImagePaths('what is this?', undefined), 'what is this?');
  assert.equal(withAttachedImagePaths('what is this?', null), 'what is this?');
});

// ─── Phase 3 bridge cards — what the existing ToolCard shows unchanged ────────
//
// The `shell` card is a privileged card: its plan arrives on the surfaced call
// and PlanDetails renders it through planLines / planNotes. The isolation line
// the planner writes ("Docker sandbox, no network" vs "runs directly on this
// machine as <user>, no Docker") lands in `warning` / `notes`, so it reaches
// the user before the Approve button with no card change.

test('shell card: the exact command and the isolation line render via planLines / planNotes', () => {
  const call = {
    name: 'shell',
    arguments: { command: 'nvidia-smi --query-gpu=name --format=csv' },
    privileged: true,
    plan: {
      ok: true,
      title: 'shell',
      commands: ['nvidia-smi --query-gpu=name --format=csv'],
      sudo: false,
      changes: 'Runs one command and returns its output.',
      warning: 'runs directly on this machine as ccooper, no Docker',
      notes: ['isolation: subprocess (docker info failed)', 'cwd: /home/ccooper/nvhive', 'timeout: 60 s'],
    },
  };
  assert.deepEqual(planLines(call), ['nvidia-smi --query-gpu=name --format=csv']);
  const notes = planNotes(call);
  assert.equal(notes.warning, 'runs directly on this machine as ccooper, no Docker');
  assert.deepEqual(notes.notes, ['isolation: subprocess (docker info failed)', 'cwd: /home/ccooper/nvhive', 'timeout: 60 s']);
  assert.equal(notes.error, '');
});

test('shell card: the Docker variant carries the isolation in notes and no warning', () => {
  const call = {
    name: 'shell',
    arguments: { command: 'ls -la' },
    plan: { ok: true, commands: ['ls -la'], notes: ['isolation: Docker sandbox, no network', 'cwd: /workspace', 'timeout: 120 s'] },
  };
  const notes = planNotes(call);
  assert.equal(notes.warning, '');
  assert.equal(notes.notes[0], 'isolation: Docker sandbox, no network');
});

test('shell card: a deny-list refusal of the plan renders as Refused, with no commands', () => {
  const call = {
    name: 'shell',
    arguments: { command: 'rm -rf /' },
    plan: { ok: false, commands: [], error: 'refusing: matches the deny list (rm -rf /)' },
  };
  assert.deepEqual(planLines(call), []);
  assert.equal(planNotes(call).error, 'refusing: matches the deny list (rm -rf /)');
});
