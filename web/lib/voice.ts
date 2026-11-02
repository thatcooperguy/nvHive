/**
 * Pure helpers behind the hold-to-talk mic and the Read aloud toggle — 0.46 §8.
 *
 * Everything the browser decides without the DOM lives here: which recorder
 * mime to ask for, which speech-to-text path `GET /v1/voice/status` leaves us
 * (local faster-whisper, Groq Whisper, "download the model first", nothing),
 * how a transcript lands in the composer's draft (inserted, never sent), what
 * an answer sounds like once the Markdown is stripped for `speechSynthesis`,
 * and the one sentence each API failure becomes under the button.
 *
 * No React, no DOM, no `@/` alias and no runtime import, so Node's type
 * stripping can run lib/voice.test.mjs against this file directly: `import
 * type` lines are erased by both Node and tsc (see lib/schedules.ts for why a
 * runtime `./cost` import is impossible here). components/VoiceButton.tsx and
 * components/ReadAloudToggle.tsx own the MediaRecorder, the fetches and the
 * speech queue.
 */

import type { VoiceDownloadConflict, VoiceStatus, VoiceTranscript, VoiceUnavailableDetail } from './types';

// ─── Fixed strings ───────────────────────────────────────────────────────────

/** localStorage key of the Read aloud preference; app/settings/page.tsx clears it. */
export const VOICE_READ_ALOUD_KEY = 'nvh.voice.readAloud';

/** A recording stops itself after this long (design §8: 60 s cap). */
export const VOICE_MAX_RECORDING_MS = 60_000;

/**
 * Space in the empty, focused composer records only once it has been HELD this
 * long; a shorter tap types the space it meant to (`onSpaceTap`), so the
 * textarea's first keystroke still works and a tap never opens the microphone.
 */
export const VOICE_SPACE_HOLD_MS = 200;

/** The button that starts the `voice-model-download` job. */
export const VOICE_DOWNLOAD_LABEL = 'Download model (~480 MB)';

/** Shown, and set as the disabled button's title, when no STT path exists. */
export const VOICE_INSTALL_HINT =
  "Voice needs `pip install 'nvhive[voice]'` or a Groq key (`nvh advisor add groq`)";

/** Replaces the mic in an insecure context (a LAN `--host` over http). */
export const VOICE_INSECURE_HINT = 'voice needs https or localhost';

/** Replaces the mic when the browser has no `getUserMedia` / `MediaRecorder`. */
export const VOICE_UNSUPPORTED_HINT = 'this browser cannot record audio';

/** `GET /v1/voice/status` did not answer (API offline, or a build without the voice routes). */
export const VOICE_STATUS_UNAVAILABLE_NOTE = 'voice status unavailable — is the API running?';

export const VOICE_LOCAL_COST_NOTE = '$0, on this device';
export const VOICE_GROQ_COST_NOTE = 'Groq Whisper · cost unknown';
export const VOICE_DOWNLOADING_NOTE = 'downloading the speech model…';

/** The mascot's bubble while the mic is held (tip only; the sprite does not move). */
export const VOICE_LISTENING_TIP = 'Listening…';

/** What `speechSynthesis` says in place of a fenced code block. */
export const SPEECH_CODE_OMITTED = 'code omitted';

/** Preferred recorder mimes, best first — the server accepts webm, ogg, wav and mp4. */
export const VOICE_MIME_PREFERENCE: readonly string[] = [
  'audio/webm;codecs=opus',
  'audio/webm',
  'audio/ogg;codecs=opus',
  'audio/mp4',
];

// ─── Recorder ────────────────────────────────────────────────────────────────

/**
 * The first mime in `VOICE_MIME_PREFERENCE` that `supported` accepts
 * (`MediaRecorder.isTypeSupported` in the browser), or null when none is —
 * the caller then lets the recorder pick its default and sends whatever
 * `blob.type` says.
 */
export function pickRecorderMime(supported: (mime: string) => boolean): string | null {
  for (const mime of VOICE_MIME_PREFERENCE) {
    try {
      if (supported(mime)) return mime;
    } catch {
      // a throwing predicate counts as "not supported"
    }
  }
  return null;
}

/** `audio/webm;codecs=opus` → `audio/webm`: the part type the server checks. */
export function baseMime(mime: string | null | undefined): string {
  const head = (mime ?? '').split(';')[0]?.trim().toLowerCase() ?? '';
  return head || 'audio/webm';
}

/** The multipart filename for a recording of this mime (the server reads the suffix too). */
export function voiceFilenameForMime(mime: string | null | undefined): string {
  const base = baseMime(mime);
  if (base === 'audio/ogg') return 'recording.ogg';
  if (base === 'audio/mp4' || base === 'audio/m4a' || base === 'audio/x-m4a') return 'recording.mp4';
  if (base === 'audio/wav' || base === 'audio/x-wav' || base === 'audio/wave') return 'recording.wav';
  if (base === 'audio/mpeg' || base === 'audio/mp3') return 'recording.mp3';
  return 'recording.webm';
}

// ─── Plan ────────────────────────────────────────────────────────────────────

/** What the browser can do, read once on mount. */
export interface BrowserVoiceCaps {
  /** `window.isSecureContext` — https or localhost. */
  secureContext: boolean;
  /** `navigator.mediaDevices.getUserMedia` and `MediaRecorder` both exist. */
  mediaDevices: boolean;
  /** `window.speechSynthesis` exists. */
  speechSynthesis: boolean;
}

export type VoiceSttPath = 'server-local' | 'server-groq' | 'download-needed' | 'none';
export type VoiceTtsPath = 'browser' | 'none';
export type VoiceMicState = 'ok' | 'insecure' | 'unsupported';

export interface VoicePlan {
  /** Which server path a recording takes, or why none does. */
  stt: VoiceSttPath;
  /** Read aloud is browser-only in 0.46 (server TTS is optional and unused by the web). */
  tts: VoiceTtsPath;
  /** Whether the mic can render at all. */
  mic: VoiceMicState;
  /** Status-line sentences, in display order. */
  notes: string[];
  /** The active path and what it costs — '' when there is no path. */
  costNote: string;
  /** Offer the Download model button: the local path is installed but the weights are not. */
  downloadOffered: boolean;
}

/** The mic renders only in a secure context with a recording API. */
export function canUseMicrophone(isSecureContext: boolean, hasMediaDevices: boolean): boolean {
  return Boolean(isSecureContext) && Boolean(hasMediaDevices);
}

function flag(value: unknown): boolean {
  return value === true;
}

/**
 * Decide the voice plan from `GET /v1/voice/status` and the browser's
 * capabilities. Local-first, as the server's `auto` provider is: the local
 * faster-whisper path when it is installed and the weights are present;
 * else Groq Whisper when a key exists (with the download offered beside it
 * when the local package is installed); else "download the model" when only
 * the local package is installed; else nothing, with the install hint.
 * `null` status (the API never answered) is its own note, not an install hint.
 */
export function voicePlan(status: VoiceStatus | null | undefined, caps: BrowserVoiceCaps): VoicePlan {
  const mic: VoiceMicState = canUseMicrophone(caps.secureContext, caps.mediaDevices)
    ? 'ok'
    : !caps.secureContext
      ? 'insecure'
      : 'unsupported';
  const tts: VoiceTtsPath = caps.speechSynthesis ? 'browser' : 'none';
  const notes: string[] = [];
  if (mic === 'insecure') notes.push(VOICE_INSECURE_HINT);
  else if (mic === 'unsupported') notes.push(VOICE_UNSUPPORTED_HINT);

  if (!status || typeof status !== 'object' || !status.stt || typeof status.stt !== 'object') {
    notes.push(VOICE_STATUS_UNAVAILABLE_NOTE);
    return { stt: 'none', tts, mic, notes, costNote: '', downloadOffered: false };
  }

  const stt = status.stt;
  const localInstalled = flag(stt.local_installed);
  const localPresent = flag(stt.local_model_present);
  const localAvailable = flag(stt.local_available);
  const groqAvailable = flag(stt.groq_available);
  const downloading = flag(status.downloading);
  const model = typeof stt.model === 'string' && stt.model ? stt.model : '';
  const reason = typeof stt.reason === 'string' ? stt.reason.trim() : '';

  if (localAvailable) {
    notes.push(model ? `local faster-whisper (${model})` : 'local faster-whisper');
    return { stt: 'server-local', tts, mic, notes, costNote: VOICE_LOCAL_COST_NOTE, downloadOffered: false };
  }

  const weightsMissing = localInstalled && !localPresent;
  if (downloading) notes.push(VOICE_DOWNLOADING_NOTE);
  const downloadOffered = weightsMissing && !downloading;

  if (groqAvailable) {
    if (downloadOffered) {
      notes.push(model ? `local ${model} not downloaded yet` : 'local model not downloaded yet');
    }
    return { stt: 'server-groq', tts, mic, notes, costNote: VOICE_GROQ_COST_NOTE, downloadOffered };
  }

  if (weightsMissing) {
    notes.push(
      downloading
        ? 'the mic works once the download completes'
        : model
          ? `local ${model} is installed but not downloaded`
          : 'the local model is installed but not downloaded',
    );
    return { stt: 'download-needed', tts, mic, notes, costNote: '', downloadOffered };
  }

  if (reason) notes.push(reason);
  notes.push(VOICE_INSTALL_HINT);
  return { stt: 'none', tts, mic, notes, costNote: '', downloadOffered: false };
}

// ─── Composer ────────────────────────────────────────────────────────────────

/**
 * Where a transcript lands: appended to the draft after one space (or after
 * the newline the draft already ends with), trimmed, never replacing what the
 * user typed. An empty transcript leaves the draft untouched. Nothing here
 * sends — the thread inserts and focuses; the user still presses Enter.
 */
export function transcriptInsertion(current: string, transcript: string): string {
  const spoken = (transcript ?? '').trim();
  if (!spoken) return current ?? '';
  const draft = current ?? '';
  const head = draft.replace(/\s+$/, '');
  if (!head) return spoken;
  const separator = /\n[ \t]*$/.test(draft) ? '\n' : ' ';
  return `${head}${separator}${spoken}`;
}

/**
 * The status line after a transcript: `provider model · cost · N ms`. The cost
 * is the pill's text (`costPill(cost_usd, cost_known).text`, passed in because
 * this module cannot import lib/cost at runtime), except that a known zero —
 * the local path — reads `$0, on this device`, the same words the CLI prints
 * and WEBUI.md promises, rather than the pill's `$0.0000`.
 */
export function transcriptStatusLine(
  result: Pick<VoiceTranscript, 'provider' | 'model' | 'latency_ms' | 'cost_usd' | 'cost_known'>,
  pillText: string,
): string {
  const cost = result.cost_known && !result.cost_usd ? VOICE_LOCAL_COST_NOTE : pillText;
  const ms = Math.round(Number(result.latency_ms) || 0);
  const model = result.model ? ` ${result.model}` : '';
  return `${result.provider}${model}${cost ? ` · ${cost}` : ''} · ${ms} ms`;
}

// ─── Speech ──────────────────────────────────────────────────────────────────

/**
 * What `speechSynthesis` should say for a Markdown answer: fenced code becomes
 * "code omitted", inline code is read as its words, a link or image is read as
 * its text, heading / emphasis / list / quote markers go, whitespace collapses.
 * Plain text comes back as itself (trimmed).
 */
export function stripMarkdownForSpeech(text: string): string {
  if (!text) return '';
  let out = text.replace(/\r\n?/g, '\n');
  // Fenced blocks (``` or ~~~, any info string) → one spoken placeholder.
  out = out.replace(/(^|\n)[ \t]*(```|~~~)[^\n]*\n[\s\S]*?\n[ \t]*\2[ \t]*(?=\n|$)/g, `$1${SPEECH_CODE_OMITTED}`);
  // An unterminated fence drops the rest of the text as code.
  out = out.replace(/(^|\n)[ \t]*(```|~~~)[^\n]*\n[\s\S]*$/g, `$1${SPEECH_CODE_OMITTED}`);
  // Images before links: the alt text is what is said.
  out = out.replace(/!\[([^\]]*)\]\([^)]*\)/g, '$1');
  out = out.replace(/\[([^\]]+)\]\([^)]*\)/g, '$1');
  // Inline code keeps its words.
  out = out.replace(/`+([^`\n]+?)`+/g, '$1');
  // Headings, block quotes, list markers, horizontal rules.
  out = out.replace(/^[ \t]{0,3}#{1,6}[ \t]+/gm, '');
  out = out.replace(/^[ \t]*>+[ \t]?/gm, '');
  out = out.replace(/^[ \t]*(?:[-*+]|\d+[.)])[ \t]+/gm, '');
  out = out.replace(/^[ \t]*(?:-{3,}|\*{3,}|_{3,})[ \t]*$/gm, '');
  // Emphasis and strikethrough markers.
  out = out.replace(/(\*\*|__)(?=\S)([\s\S]*?\S)\1/g, '$2');
  out = out.replace(/(^|[^\w*])\*(?=\S)([^*\n]*?\S)\*(?=[^\w*]|$)/g, '$1$2');
  out = out.replace(/(^|[^\w_])_(?=\S)([^_\n]*?\S)_(?=[^\w_]|$)/g, '$1$2');
  out = out.replace(/~~([^~\n]+)~~/g, '$1');
  // Bare HTML tags (the Wizard rarely emits them, but a <br> would be spelled out).
  out = out.replace(/<\/?[a-zA-Z][^>]*>/g, ' ');
  // Collapse: blank lines become a sentence pause, runs of spaces one space.
  out = out.replace(/[ \t]+\n/g, '\n').replace(/\n{2,}/g, '\n').replace(/[ \t]{2,}/g, ' ');
  return out.trim();
}

// ─── Errors ──────────────────────────────────────────────────────────────────

/** The `getUserMedia` failure under the mic: permission, no device, a busy device, or the browser's own words. */
export function microphoneErrorMessage(err: unknown): string {
  const like = (err && typeof err === 'object' ? err : {}) as { name?: string; message?: string };
  switch (like.name) {
    case 'NotAllowedError':
    case 'PermissionDeniedError':
    case 'SecurityError':
      return "Microphone access was denied — allow it in the browser's site settings and try again";
    case 'NotFoundError':
    case 'DevicesNotFoundError':
      return 'No microphone found';
    case 'NotReadableError':
    case 'TrackStartError':
      return 'The microphone is busy in another app';
    default:
      return like.message && like.message.trim() ? `Microphone failed: ${like.message.trim()}` : 'Microphone failed';
  }
}

function record(value: unknown): Record<string, unknown> | null {
  return value && typeof value === 'object' && !Array.isArray(value) ? (value as Record<string, unknown>) : null;
}

/** Read `{downloading, hint, status}` from a 503 `detail` (object or JSON string); null when it is neither. */
export function voiceUnavailableFromDetail(detail: unknown): VoiceUnavailableDetail | null {
  let parsed = record(detail);
  if (!parsed && typeof detail === 'string') {
    const start = detail.indexOf('{');
    const end = detail.lastIndexOf('}');
    if (start !== -1 && end > start) {
      try {
        parsed = record(JSON.parse(detail.slice(start, end + 1)));
      } catch {
        parsed = null;
      }
    }
  }
  if (!parsed) return null;
  const statusRecord = record(parsed.status);
  return {
    downloading: parsed.downloading === true,
    hint: typeof parsed.hint === 'string' ? parsed.hint.trim() : '',
    status: statusRecord && record(statusRecord.stt) ? (statusRecord as unknown as VoiceStatus) : null,
  };
}

/** Shown when a 409 arrived without a readable `message`. */
export const VOICE_DOWNLOAD_IN_FLIGHT_TEXT = 'a model download is already in flight';

/**
 * The 409 `detail` of `POST /v1/voice/download` — `{message, job_id}` — read
 * back from the message `apiFetch` put on its Error (`JSON.stringify(detail)`
 * plus an optional ` (request <id>)` suffix), the way the schedules page reads
 * its run conflict. A message that does not parse is kept verbatim.
 */
export function voiceDownloadConflictFromMessage(message: string): VoiceDownloadConflict {
  const text = typeof message === 'string' ? message : String(message ?? '');
  const start = text.indexOf('{');
  const end = text.lastIndexOf('}');
  if (start !== -1 && end > start) {
    try {
      const parsed = record(JSON.parse(text.slice(start, end + 1)));
      if (parsed) {
        return {
          message:
            typeof parsed.message === 'string' && parsed.message.trim()
              ? parsed.message.trim()
              : VOICE_DOWNLOAD_IN_FLIGHT_TEXT,
          job_id: typeof parsed.job_id === 'string' ? parsed.job_id : '',
        };
      }
    } catch {
      // not JSON — fall through to the verbatim message
    }
  }
  return { message: text.trim() || VOICE_DOWNLOAD_IN_FLIGHT_TEXT, job_id: '' };
}

/** What `transcribeAudio` / `startVoiceDownload` throw: an Error with the HTTP status and, for a 503, the parsed detail. */
export interface VoiceErrorLike {
  message?: string;
  statusCode?: number;
  unavailable?: VoiceUnavailableDetail | null;
}

/**
 * One sentence for the text under the mic. Never throws; the fallback is the
 * error's own message (a network failure says which API bases were tried).
 */
export function voiceErrorMessage(err: unknown): string {
  const like = (err && typeof err === 'object' ? err : {}) as VoiceErrorLike;
  const message = typeof like.message === 'string' && like.message.trim() ? like.message.trim() : '';
  switch (like.statusCode) {
    case 401:
    case 403:
      return `Not authorised to use voice (HTTP ${like.statusCode}) — the API wants your HIVE_API_KEY`;
    case 404:
      return 'This API has no voice routes (HTTP 404) — update nvHive';
    case 413:
      return 'Recording too large for the server (HTTP 413) — hold the mic for less time';
    case 415:
      return 'The server does not accept this audio format (HTTP 415)';
    case 503: {
      const parsed = like.unavailable ?? voiceUnavailableFromDetail(message);
      const hint = parsed?.hint ?? '';
      const downloading = parsed?.downloading ?? false;
      if (hint) return downloading ? `${hint} (download in progress)` : hint;
      return downloading ? 'The speech model is still downloading' : 'No speech-to-text path right now (HTTP 503)';
    }
    default:
      if (like.statusCode && like.statusCode >= 500) return `Voice failed on the server (HTTP ${like.statusCode})`;
      return message || 'Voice failed';
  }
}
