/**
 * Image attachments for the Wizard composer — the pure rules behind the
 * attach button, the drop zone and the paste handler in
 * components/WizardChat.tsx.
 *
 * Two kinds of file reach the Wizard surface and they take different roads:
 *
 *  - images (png / jpeg / webp / gif, at most `WIZARD_ATTACHMENT_MAX_COUNT`
 *    per message, `WIZARD_ATTACHMENT_MAX_BYTES` each) become
 *    `attachments` on the NEXT chat request. The server lands the bytes under
 *    `NVH_HOME/rag/uploads/wizard/<conversation>/`, tells the model the
 *    paths (one `ATTACHED_IMAGES_NOTE` line appended to the user turn), so it
 *    can call `analyze_image` / `read_text_from_image` on them, and echoes
 *    the paths back as `attachment_paths` so `withAttachedImagePaths` can
 *    keep that line on the user turn in the next request's history;
 *  - everything else is a document and keeps going to the RAG ingest path
 *    (`uploadAndIngest`) exactly as before. The server answers 400 to a
 *    non-image attachment, so nothing here ever builds one.
 *
 * Wire shape (`WizardChatAttachment`): `{name, content, mime_type, is_image}`
 * — `content` is RAW standard base64, never a `data:` URL (that is the
 * `/v1/query` chat's convention; the Wizard server decodes this field
 * directly), `mime_type` is one of the four accepted types after
 * normalisation (`image/jpg` → `image/jpeg`) and `is_image` is always true.
 *
 * Everything in this module runs under plain `node --test` (see
 * attachments.test.mjs): no React, no DOM beyond `btoa`, `File.arrayBuffer`
 * and the duck-typed `FileLike` the validators read.
 */

import type { WizardChatAttachment, WizardImageMimeType } from './types';

/** Most images one message may carry (mirrors the server's `max_length=6`). */
export const WIZARD_ATTACHMENT_MAX_COUNT = 6;
/** Largest single image, in bytes (mirrors the server's 20 MB cap — the vision tools' own ceiling). */
export const WIZARD_ATTACHMENT_MAX_BYTES = 20 * 1024 * 1024;

/** The image types the Wizard accepts; anything else is a document. */
export const WIZARD_IMAGE_MIME_TYPES: readonly WizardImageMimeType[] = [
  'image/png',
  'image/jpeg',
  'image/webp',
  'image/gif',
];

/** `accept` for the hidden file input: the four MIME types plus their extensions. */
export const WIZARD_IMAGE_ACCEPT = [...WIZARD_IMAGE_MIME_TYPES, '.png', '.jpg', '.jpeg', '.webp', '.gif'].join(',');

/** The two Wizard vision tools an attached image is meant for. */
export const VISION_TOOL_NAMES: readonly string[] = ['analyze_image', 'read_text_from_image'];

/** Shown under the chips when the tool catalog is loaded and has no vision tool. */
export const NO_VISION_TOOL_NOTE =
  'this server has no vision tool registered — the Wizard will see the file paths but cannot read the images';

/** The question sent when the user attaches images and types nothing. */
export const DEFAULT_IMAGE_QUESTION = 'What is in the attached image(s)?';

/** Extension fallback for files the browser hands over with an empty `type`. */
const EXTENSION_MIME: Readonly<Record<string, WizardImageMimeType>> = {
  png: 'image/png',
  jpg: 'image/jpeg',
  jpeg: 'image/jpeg',
  webp: 'image/webp',
  gif: 'image/gif',
};

/** Spellings browsers and OSes use for the same four types. */
const MIME_ALIASES: Readonly<Record<string, WizardImageMimeType>> = {
  'image/png': 'image/png',
  'image/x-png': 'image/png',
  'image/jpeg': 'image/jpeg',
  'image/jpg': 'image/jpeg',
  'image/pjpeg': 'image/jpeg',
  'image/webp': 'image/webp',
  'image/gif': 'image/gif',
};

/** The fields the helpers read off a `File`, so tests can pass plain objects. */
export interface FileLike {
  name?: string;
  type?: string;
  size?: number;
}

/** Lower-cased extension without the dot, or `''`. */
function extensionOf(name: string | undefined): string {
  const base = (name ?? '').trim();
  const dot = base.lastIndexOf('.');
  if (dot <= 0 || dot === base.length - 1) return '';
  return base.slice(dot + 1).toLowerCase();
}

/**
 * The accepted MIME type for a file, or `null` when it is not an image we
 * take. The browser's `type` wins (normalised through the alias table); an
 * empty `type` — common for drag-drop on Linux and for pasted screenshots on
 * some builds — falls back to the extension. A `type` that names some other
 * image format (svg, bmp, tiff, heic) is refused even if the name lies.
 */
export function imageMimeType(file: FileLike | null | undefined): WizardImageMimeType | null {
  if (!file) return null;
  const type = (file.type ?? '').trim().toLowerCase().split(';')[0];
  if (type) return MIME_ALIASES[type] ?? null;
  return EXTENSION_MIME[extensionOf(file.name)] ?? null;
}

/** Is this one of the four image types the Wizard attaches (vs a document for RAG)? */
export function isImageFile(file: FileLike | null | undefined): boolean {
  return imageMimeType(file) !== null;
}

/** `1.5 KB`, `25 MB`, `512 B` — for the chip and the refusal text. */
export function formatSize(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes < 0) return '0 B';
  if (bytes < 1024) return `${Math.round(bytes)} B`;
  const units = ['KB', 'MB', 'GB'];
  let value = bytes / 1024;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  const text = value >= 100 ? value.toFixed(0) : value.toFixed(1).replace(/\.0$/, '');
  return `${text} ${units[unit]}`;
}

export type AttachmentValidation =
  | { ok: true; mime_type: WizardImageMimeType }
  | { ok: false; reason: string };

/**
 * May this file become an attachment, given how many are already pending?
 *
 * Order matters for the message the user reads: the count cap first (the
 * file may be fine; the message is full), then the type (a document belongs
 * in RAG — say so), then emptiness and the size cap.
 */
export function validateAttachment(file: FileLike, pendingCount: number): AttachmentValidation {
  const name = (file.name ?? '').trim() || 'image';
  if (pendingCount >= WIZARD_ATTACHMENT_MAX_COUNT) {
    return { ok: false, reason: `${name}: at most ${WIZARD_ATTACHMENT_MAX_COUNT} images per message` };
  }
  const mime = imageMimeType(file);
  if (!mime) {
    return {
      ok: false,
      reason: `${name}: not an image (png, jpeg, webp or gif) — drop documents to index them into RAG instead`,
    };
  }
  const size = typeof file.size === 'number' ? file.size : 0;
  if (size <= 0) return { ok: false, reason: `${name} is empty` };
  if (size > WIZARD_ATTACHMENT_MAX_BYTES) {
    return {
      ok: false,
      reason: `${name} is ${formatSize(size)} — the limit is ${formatSize(WIZARD_ATTACHMENT_MAX_BYTES)}`,
    };
  }
  return { ok: true, mime_type: mime };
}

/** Standard base64 of raw bytes; chunked so a 25 MB image never blows the call stack. */
export function bytesToBase64(bytes: Uint8Array): string {
  let binary = '';
  const CHUNK = 0x8000;
  for (let i = 0; i < bytes.length; i += CHUNK) {
    binary += String.fromCharCode.apply(null, Array.from(bytes.subarray(i, i + CHUNK)));
  }
  return btoa(binary);
}

/** `data:<mime>;base64,<content>` — the preview `src` for a chip or a bubble. */
export function base64ToDataUrl(mime: string, content: string): string {
  return `data:${mime};base64,${content}`;
}

/**
 * The base64 payload of a `data:` URL, or the input untouched when it is
 * already raw base64. Whitespace is dropped either way.
 */
export function dataUrlToBase64(value: string): string {
  const text = (value ?? '').trim();
  if (text.startsWith('data:')) {
    const comma = text.indexOf(',');
    return comma === -1 ? '' : text.slice(comma + 1).replace(/\s+/g, '');
  }
  return text.replace(/\s+/g, '');
}

/**
 * One image waiting in the composer: the wire payload plus what the chip and
 * the user bubble show. `attachmentPayload` strips it back to the wire shape.
 */
export interface PendingAttachment extends WizardChatAttachment {
  id: string;
  size: number;
  /** `data:` URL for the thumbnail — derived from `content`, never fetched. */
  previewUrl: string;
}

/** What a sent user bubble keeps of an attachment (no payload — display only). */
export type AttachmentPreview = Pick<PendingAttachment, 'id' | 'name' | 'size' | 'mime_type' | 'previewUrl'>;

let idCounter = 0;

/**
 * A chip id. Not `crypto.randomUUID()` — that needs a secure context and the
 * WebUI is usually served over plain http from the Spark's LAN address.
 */
export function nextAttachmentId(): string {
  idCounter += 1;
  return `att-${Date.now().toString(36)}-${idCounter}-${Math.random().toString(36).slice(2, 7)}`;
}

/** Assemble a pending attachment from already-encoded bytes (the pure half of `toAttachment`). */
export function buildAttachment(input: {
  id?: string;
  name: string;
  mime_type: WizardImageMimeType;
  size: number;
  content: string;
}): PendingAttachment {
  const content = dataUrlToBase64(input.content);
  return {
    id: input.id ?? nextAttachmentId(),
    name: input.name.trim() || `image.${extensionFor(input.mime_type)}`,
    size: input.size,
    mime_type: input.mime_type,
    content,
    is_image: true,
    previewUrl: base64ToDataUrl(input.mime_type, content),
  };
}

/** The file extension for an accepted MIME type. */
export function extensionFor(mime: WizardImageMimeType): string {
  switch (mime) {
    case 'image/png':
      return 'png';
    case 'image/jpeg':
      return 'jpg';
    case 'image/webp':
      return 'webp';
    case 'image/gif':
      return 'gif';
  }
}

/**
 * Read a `File` (or any Blob with a `name`) into a pending attachment. The
 * caller validates first (`validateAttachment`); this throws only when the
 * type is not an accepted image or the bytes cannot be read.
 */
export async function toAttachment(file: File, id?: string): Promise<PendingAttachment> {
  const mime = imageMimeType(file);
  if (!mime) throw new Error(`${file.name || 'file'}: not an accepted image type`);
  const bytes = new Uint8Array(await file.arrayBuffer());
  return buildAttachment({
    id,
    name: file.name,
    mime_type: mime,
    size: bytes.byteLength,
    content: bytesToBase64(bytes),
  });
}

/** Exactly the four wire fields — nothing local (id, size, preview) leaks into the request. */
export function attachmentPayload(attachment: PendingAttachment): WizardChatAttachment {
  return {
    name: attachment.name,
    content: attachment.content,
    mime_type: attachment.mime_type,
    is_image: true,
  };
}

/** The `attachments` array for one chat request (`[]` when nothing is pending). */
export function attachmentPayloads(attachments: readonly PendingAttachment[]): WizardChatAttachment[] {
  return attachments.slice(0, WIZARD_ATTACHMENT_MAX_COUNT).map(attachmentPayload);
}

/** What a user bubble keeps once the message is sent. */
export function attachmentPreview(attachment: PendingAttachment): AttachmentPreview {
  return {
    id: attachment.id,
    name: attachment.name,
    size: attachment.size,
    mime_type: attachment.mime_type,
    previewUrl: attachment.previewUrl,
  };
}

/** Dropped files: images become attachments, everything else goes to RAG ingest. */
export function splitDroppedFiles<T extends FileLike>(files: readonly T[]): { images: T[]; documents: T[] } {
  const images: T[] = [];
  const documents: T[] = [];
  for (const file of files) (isImageFile(file) ? images : documents).push(file);
  return { images, documents };
}

/** A clipboard item as the paste handler reads it (the DOM's `DataTransferItem`). */
export interface ClipboardItemLike {
  kind: string;
  type: string;
  getAsFile(): File | null;
}

/**
 * The image files in a paste, in order; `[]` for a plain text paste so the
 * caller leaves the default paste alone. Non-image files are ignored here —
 * pasting a document is not how RAG ingest works (drop it instead).
 */
export function pastedImageFiles(items: ArrayLike<ClipboardItemLike> | null | undefined): File[] {
  const out: File[] = [];
  if (!items) return out;
  for (let i = 0; i < items.length; i += 1) {
    const item = items[i];
    if (!item || item.kind !== 'file') continue;
    if (!isImageFile({ type: item.type })) continue;
    const file = item.getAsFile();
    if (file) out.push(file);
  }
  return out;
}

/**
 * The question for a turn: the typed text, or — when the user attached
 * images and typed nothing — `DEFAULT_IMAGE_QUESTION`, because the server
 * requires a non-empty question and the intent is obvious. Empty with no
 * attachments stays empty (nothing to send).
 */
export function questionWithAttachments(text: string, attachmentCount: number): string {
  const typed = (text ?? '').trim();
  if (typed) return typed;
  return attachmentCount > 0 ? DEFAULT_IMAGE_QUESTION : '';
}

/** Does the tool catalog carry a vision tool the attached images can reach? */
export function hasVisionTool(toolNames: Iterable<string>): boolean {
  for (const name of toolNames) if (VISION_TOOL_NAMES.includes(name)) return true;
  return false;
}

/**
 * The line the server appends to a user turn that carried images — verbatim
 * `ATTACHED_IMAGES_NOTE` in nvh/integrations/wizard/chat.py; the two must
 * stay identical so the history the client sends reads like the turn the
 * model saw.
 */
export const ATTACHED_IMAGES_NOTE =
  'Attached images (use analyze_image or read_text_from_image on these paths):';

/**
 * The user turn as the server ran it: `question`, a blank line, then the
 * attached-images note with the landed `paths` comma-separated (the server's
 * `append_attached_images`). The question untouched when there are no paths.
 */
export function withAttachedImagePaths(question: string, paths: readonly string[] | undefined | null): string {
  const listed = (paths ?? []).map(p => p.trim()).filter(Boolean);
  if (listed.length === 0) return question;
  return `${question.replace(/\s+$/, '')}\n\n${ATTACHED_IMAGES_NOTE} ${listed.join(', ')}`;
}
