// Regression tests for lib/voice.ts — the pure helpers behind the Wizard's
// hold-to-talk mic (components/VoiceButton.tsx) and the Read aloud toggle
// (components/ReadAloudToggle.tsx): the recorder mime order, the voice plan
// read from GET /v1/voice/status plus the browser's capabilities, where a
// transcript lands in the draft, what speechSynthesis says for Markdown, and
// the one sentence each API failure becomes.
//
// Runs on Node's built-in runner with native type stripping, like
// cost.test.mjs:
//
//     cd web && node --test lib/voice.test.mjs
//
// .mjs so tsc / next build leave it alone; Node imports the .ts module by
// explicit extension. lib/voice.ts has `import type` lines only, which both
// Node and tsc erase.

import { test } from 'node:test';
import assert from 'node:assert/strict';

import {
  SPEECH_CODE_OMITTED,
  VOICE_DOWNLOAD_IN_FLIGHT_TEXT,
  VOICE_DOWNLOAD_LABEL,
  VOICE_DOWNLOADING_NOTE,
  VOICE_GROQ_COST_NOTE,
  VOICE_INSECURE_HINT,
  VOICE_INSTALL_HINT,
  VOICE_LISTENING_TIP,
  VOICE_LOCAL_COST_NOTE,
  VOICE_MAX_RECORDING_MS,
  VOICE_MIME_PREFERENCE,
  VOICE_READ_ALOUD_KEY,
  VOICE_SPACE_HOLD_MS,
  VOICE_STATUS_UNAVAILABLE_NOTE,
  VOICE_UNSUPPORTED_HINT,
  baseMime,
  canUseMicrophone,
  microphoneErrorMessage,
  pickRecorderMime,
  stripMarkdownForSpeech,
  transcriptInsertion,
  transcriptStatusLine,
  voiceDownloadConflictFromMessage,
  voiceErrorMessage,
  voiceFilenameForMime,
  voicePlan,
  voiceUnavailableFromDetail,
} from './voice.ts';

const CAPS = { secureContext: true, mediaDevices: true, speechSynthesis: true };

/** A `GET /v1/voice/status` body with every flag off unless overridden. */
function status(stt = {}, extra = {}) {
  return {
    stt: {
      local_installed: false,
      local_model_present: false,
      local_available: false,
      groq_available: false,
      model: 'small.en',
      reason: null,
      ...stt,
    },
    tts: { edge_available: false, system_available: false },
    ...extra,
  };
}

// ─── Fixed strings the docs and the other files name ─────────────────────────

test('the fixed strings are the sentences the design and the docs use', () => {
  assert.equal(VOICE_DOWNLOAD_LABEL, 'Download model (~480 MB)');
  assert.equal(VOICE_READ_ALOUD_KEY, 'nvh.voice.readAloud');
  assert.equal(VOICE_MAX_RECORDING_MS, 60_000);
  assert.equal(VOICE_INSECURE_HINT, 'voice needs https or localhost');
  assert.equal(VOICE_LISTENING_TIP, 'Listening…');
  assert.ok(VOICE_INSTALL_HINT.includes("pip install 'nvhive[voice]'"));
  assert.ok(VOICE_INSTALL_HINT.includes('nvh advisor add groq'));
  assert.equal(VOICE_LOCAL_COST_NOTE, '$0, on this device');
  assert.equal(VOICE_GROQ_COST_NOTE, 'Groq Whisper · cost unknown');
});

// ─── Recorder mime ───────────────────────────────────────────────────────────

test('pickRecorderMime: opus-in-webm first, then webm, ogg/opus, mp4', () => {
  assert.deepEqual([...VOICE_MIME_PREFERENCE], [
    'audio/webm;codecs=opus',
    'audio/webm',
    'audio/ogg;codecs=opus',
    'audio/mp4',
  ]);
  assert.equal(pickRecorderMime(() => true), 'audio/webm;codecs=opus');
  assert.equal(pickRecorderMime(m => m === 'audio/webm'), 'audio/webm');
  assert.equal(pickRecorderMime(m => m.startsWith('audio/ogg')), 'audio/ogg;codecs=opus');
  assert.equal(pickRecorderMime(m => m === 'audio/mp4'), 'audio/mp4');
});

test('pickRecorderMime: nothing supported (or a throwing predicate) → null', () => {
  assert.equal(pickRecorderMime(() => false), null);
  assert.equal(
    pickRecorderMime(() => {
      throw new Error('no MediaRecorder');
    }),
    null,
  );
  const asked = [];
  pickRecorderMime(m => {
    asked.push(m);
    return false;
  });
  assert.deepEqual(asked, [...VOICE_MIME_PREFERENCE]);
});

test('baseMime drops the codecs parameter; filenames follow the bare type', () => {
  assert.equal(baseMime('audio/webm;codecs=opus'), 'audio/webm');
  assert.equal(baseMime('Audio/OGG; codecs=opus'), 'audio/ogg');
  assert.equal(baseMime(null), 'audio/webm');
  assert.equal(baseMime(''), 'audio/webm');
  assert.equal(voiceFilenameForMime('audio/webm;codecs=opus'), 'recording.webm');
  assert.equal(voiceFilenameForMime('audio/ogg;codecs=opus'), 'recording.ogg');
  assert.equal(voiceFilenameForMime('audio/mp4'), 'recording.mp4');
  assert.equal(voiceFilenameForMime('audio/wav'), 'recording.wav');
  assert.equal(voiceFilenameForMime('audio/x-wav'), 'recording.wav');
  assert.equal(voiceFilenameForMime('audio/mpeg'), 'recording.mp3');
  assert.equal(voiceFilenameForMime(undefined), 'recording.webm');
});

// ─── Microphone gate ─────────────────────────────────────────────────────────

test('canUseMicrophone needs both a secure context and a recording API', () => {
  assert.equal(canUseMicrophone(true, true), true);
  assert.equal(canUseMicrophone(false, true), false);
  assert.equal(canUseMicrophone(true, false), false);
  assert.equal(canUseMicrophone(false, false), false);
});

// ─── Plan matrix ─────────────────────────────────────────────────────────────

test('plan: local weights present → server-local, $0 on this device, no download offered', () => {
  const plan = voicePlan(
    status({ local_installed: true, local_model_present: true, local_available: true, groq_available: true }),
    CAPS,
  );
  assert.equal(plan.stt, 'server-local');
  assert.equal(plan.tts, 'browser');
  assert.equal(plan.mic, 'ok');
  assert.equal(plan.costNote, VOICE_LOCAL_COST_NOTE);
  assert.equal(plan.downloadOffered, false);
  assert.ok(plan.notes.some(n => n.includes('small.en')), plan.notes);
});

test('plan: Groq key only → server-groq with the cost-unknown note', () => {
  const plan = voicePlan(status({ groq_available: true }), CAPS);
  assert.equal(plan.stt, 'server-groq');
  assert.equal(plan.costNote, VOICE_GROQ_COST_NOTE);
  assert.equal(plan.downloadOffered, false);
  assert.ok(!plan.notes.includes(VOICE_INSTALL_HINT));
});

test('plan: local installed, weights absent, no key → download-needed with the button offered', () => {
  const plan = voicePlan(status({ local_installed: true }), CAPS);
  assert.equal(plan.stt, 'download-needed');
  assert.equal(plan.downloadOffered, true);
  assert.equal(plan.costNote, '');
  assert.ok(plan.notes.some(n => n.includes('small.en') && n.includes('not downloaded')), plan.notes);
  assert.ok(!plan.notes.includes(VOICE_INSTALL_HINT), 'an installed local path is not an install problem');
});

test('plan: local installed, weights absent, Groq key → Groq answers now and the download is offered beside it', () => {
  const plan = voicePlan(status({ local_installed: true, groq_available: true }), CAPS);
  assert.equal(plan.stt, 'server-groq');
  assert.equal(plan.costNote, VOICE_GROQ_COST_NOTE);
  assert.equal(plan.downloadOffered, true);
  assert.ok(plan.notes.some(n => n.includes('not downloaded yet')), plan.notes);
});

test('plan: a download in flight is noted and the button is not offered twice', () => {
  // `download_job_id` is the key GET /v1/voice/status sends (nvh/api/routes/voice.py `_status_payload`).
  const alone = voicePlan(status({ local_installed: true }, { downloading: true, download_job_id: 'j1' }), CAPS);
  assert.equal(alone.stt, 'download-needed');
  assert.equal(alone.downloadOffered, false);
  assert.ok(alone.notes.includes(VOICE_DOWNLOADING_NOTE));
  const withGroq = voicePlan(status({ local_installed: true, groq_available: true }, { downloading: true }), CAPS);
  assert.equal(withGroq.stt, 'server-groq');
  assert.equal(withGroq.downloadOffered, false);
  assert.ok(withGroq.notes.includes(VOICE_DOWNLOADING_NOTE));
});

test('plan: nothing installed and no key → none with the install hint (the reason first)', () => {
  const bare = voicePlan(status(), CAPS);
  assert.equal(bare.stt, 'none');
  assert.equal(bare.costNote, '');
  assert.equal(bare.downloadOffered, false);
  assert.deepEqual(bare.notes, [VOICE_INSTALL_HINT]);

  const arm = voicePlan(status({ reason: 'no faster-whisper wheel for this platform' }), CAPS);
  assert.equal(arm.stt, 'none');
  assert.deepEqual(arm.notes, ['no faster-whisper wheel for this platform', VOICE_INSTALL_HINT]);
});

test('plan: no status (API offline / older build) is its own note, not an install hint', () => {
  for (const missing of [null, undefined, {}, { stt: null }]) {
    const plan = voicePlan(missing, CAPS);
    assert.equal(plan.stt, 'none');
    assert.deepEqual(plan.notes, [VOICE_STATUS_UNAVAILABLE_NOTE]);
    assert.equal(plan.downloadOffered, false);
  }
});

test('plan: an insecure context hides the mic whatever the server offers', () => {
  const plan = voicePlan(
    status({ local_installed: true, local_model_present: true, local_available: true }),
    { ...CAPS, secureContext: false },
  );
  assert.equal(plan.mic, 'insecure');
  assert.equal(plan.notes[0], VOICE_INSECURE_HINT);
  // The server side of the plan is still reported for the title text.
  assert.equal(plan.stt, 'server-local');
});

test('plan: no getUserMedia / MediaRecorder → unsupported; no speechSynthesis → tts none', () => {
  const noMedia = voicePlan(status({ groq_available: true }), { ...CAPS, mediaDevices: false });
  assert.equal(noMedia.mic, 'unsupported');
  assert.equal(noMedia.notes[0], VOICE_UNSUPPORTED_HINT);
  const noSpeech = voicePlan(status({ groq_available: true }), { ...CAPS, speechSynthesis: false });
  assert.equal(noSpeech.tts, 'none');
  assert.equal(noSpeech.mic, 'ok');
});

test('plan: flags must be real booleans — a truthy string is not "available"', () => {
  const plan = voicePlan(status({ local_available: 'yes', groq_available: 1 }), CAPS);
  assert.equal(plan.stt, 'none');
});

// ─── Transcript insertion ────────────────────────────────────────────────────

test('transcriptInsertion: an empty draft takes the trimmed transcript', () => {
  assert.equal(transcriptInsertion('', '  install comfyui  '), 'install comfyui');
  assert.equal(transcriptInsertion('   ', 'hello'), 'hello');
});

test('transcriptInsertion: appends after one space, or after the newline the draft ends with', () => {
  assert.equal(transcriptInsertion('what is', 'my GPU'), 'what is my GPU');
  assert.equal(transcriptInsertion('what is   ', 'my GPU'), 'what is my GPU');
  assert.equal(transcriptInsertion('first line\n', 'second line'), 'first line\nsecond line');
  assert.equal(transcriptInsertion('first line\n  ', 'second line'), 'first line\nsecond line');
});

test('transcriptInsertion: never drops the draft; an empty transcript changes nothing', () => {
  assert.equal(transcriptInsertion('keep me', ''), 'keep me');
  assert.equal(transcriptInsertion('keep me', '   '), 'keep me');
  assert.equal(transcriptInsertion('keep me', 'and me').startsWith('keep me'), true);
});

// ─── Speech text ─────────────────────────────────────────────────────────────

test('stripMarkdownForSpeech: fenced code becomes "code omitted", inline code keeps its words', () => {
  const text = 'Run this:\n\n```bash\nnvh doctor --fix\n```\n\nthen check `nvh status` again.';
  const spoken = stripMarkdownForSpeech(text);
  assert.equal(SPEECH_CODE_OMITTED, 'code omitted');
  assert.ok(spoken.includes('code omitted'), spoken);
  assert.ok(!spoken.includes('nvh doctor --fix'), spoken);
  assert.ok(!spoken.includes('```'), spoken);
  assert.ok(spoken.includes('check nvh status again'), spoken);
});

test('stripMarkdownForSpeech: an unterminated fence drops the rest as code', () => {
  const spoken = stripMarkdownForSpeech('Try:\n```\nrm -rf build\nmake');
  assert.equal(spoken, 'Try:\ncode omitted');
});

test('stripMarkdownForSpeech: links and images read as their text, headings and emphasis lose their markers', () => {
  const text = '## Next steps\n\nSee [the docs](https://example.com/x) and ![a chart](img.png).\n\n**Bold**, *italic*, _under_ and ~~gone~~.';
  const spoken = stripMarkdownForSpeech(text);
  assert.ok(!spoken.includes('##'), spoken);
  assert.ok(spoken.startsWith('Next steps'), spoken);
  assert.ok(spoken.includes('See the docs and a chart.'), spoken);
  assert.ok(!spoken.includes('https://example.com/x'), spoken);
  assert.ok(spoken.includes('Bold, italic, under and gone.'), spoken);
});

test('stripMarkdownForSpeech: list markers, block quotes and rules go; snake_case and arithmetic stay', () => {
  const text = '- one\n- two\n1. three\n2) four\n> quoted\n\n---\n\ncost_usd is 2 * 3 * 4';
  const spoken = stripMarkdownForSpeech(text);
  assert.equal(spoken, 'one\ntwo\nthree\nfour\nquoted\ncost_usd is 2 * 3 * 4');
});

test('stripMarkdownForSpeech: plain text comes back trimmed; empty stays empty; CRLF and HTML are flattened', () => {
  assert.equal(stripMarkdownForSpeech('  Your GPU is fine.  '), 'Your GPU is fine.');
  assert.equal(stripMarkdownForSpeech(''), '');
  assert.equal(stripMarkdownForSpeech('a\r\n\r\nb<br>c'), 'a\nb c');
});

// ─── API detail readers ──────────────────────────────────────────────────────

test('voiceUnavailableFromDetail reads an object, a JSON string with a suffix, and rejects the rest', () => {
  const body = { downloading: true, hint: 'run nvh voice --download', status: status({ local_installed: true }) };
  const fromObject = voiceUnavailableFromDetail(body);
  assert.equal(fromObject.downloading, true);
  assert.equal(fromObject.hint, 'run nvh voice --download');
  assert.equal(fromObject.status.stt.local_installed, true);

  const fromString = voiceUnavailableFromDetail(`${JSON.stringify(body)} (request abc123)`);
  assert.equal(fromString.hint, 'run nvh voice --download');
  assert.equal(fromString.status.stt.model, 'small.en');

  assert.equal(voiceUnavailableFromDetail('Service Unavailable'), null);
  assert.equal(voiceUnavailableFromDetail(null), null);
  assert.equal(voiceUnavailableFromDetail(['x']), null);
  // A status without `stt` is not a status.
  assert.equal(voiceUnavailableFromDetail({ hint: 'h', status: { nope: 1 } }).status, null);
  assert.equal(voiceUnavailableFromDetail({ hint: 'h' }).downloading, false);
});

test('voiceDownloadConflictFromMessage reads the 409 detail back from the Error message', () => {
  const conflict = voiceDownloadConflictFromMessage(
    '{"message":"A download of small.en is already in flight","job_id":"20261102120000-abcdef01"} (request r1)',
  );
  assert.equal(conflict.job_id, '20261102120000-abcdef01');
  assert.equal(conflict.message, 'A download of small.en is already in flight');
  assert.deepEqual(voiceDownloadConflictFromMessage('plain text'), { message: 'plain text', job_id: '' });
  assert.deepEqual(voiceDownloadConflictFromMessage(''), { message: VOICE_DOWNLOAD_IN_FLIGHT_TEXT, job_id: '' });
  assert.equal(voiceDownloadConflictFromMessage('{"job_id":"j"}').message, VOICE_DOWNLOAD_IN_FLIGHT_TEXT);
});

// ─── Error sentences ─────────────────────────────────────────────────────────

test('voiceErrorMessage maps each HTTP status to one sentence', () => {
  assert.match(voiceErrorMessage({ statusCode: 401, message: 'HTTP 401' }), /Not authorised.*HIVE_API_KEY/);
  assert.match(voiceErrorMessage({ statusCode: 403, message: 'x' }), /HTTP 403/);
  assert.match(voiceErrorMessage({ statusCode: 404, message: 'Not Found' }), /no voice routes/);
  assert.match(voiceErrorMessage({ statusCode: 413, message: 'x' }), /too large/);
  assert.match(voiceErrorMessage({ statusCode: 415, message: 'x' }), /audio format/);
  assert.match(voiceErrorMessage({ statusCode: 502, message: 'x' }), /HTTP 502/);
});

test('voiceErrorMessage: a 503 says the hint, from the parsed detail or the JSON message', () => {
  assert.equal(
    voiceErrorMessage({ statusCode: 503, message: 'ignored', unavailable: { downloading: false, hint: 'Download the model first', status: null } }),
    'Download the model first',
  );
  assert.equal(
    voiceErrorMessage({ statusCode: 503, message: 'x', unavailable: { downloading: true, hint: 'weights absent', status: null } }),
    'weights absent (download in progress)',
  );
  assert.equal(
    voiceErrorMessage({ statusCode: 503, message: '{"downloading":false,"hint":"from the body"}' }),
    'from the body',
  );
  assert.equal(voiceErrorMessage({ statusCode: 503, message: '{"downloading":true}' }), 'The speech model is still downloading');
  assert.equal(voiceErrorMessage({ statusCode: 503, message: 'Service Unavailable' }), 'No speech-to-text path right now (HTTP 503)');
});

test('voiceErrorMessage: a network failure keeps its own words; nothing usable → "Voice failed"', () => {
  assert.equal(voiceErrorMessage(new Error('Hive API is offline. Tried http://localhost:8000')), 'Hive API is offline. Tried http://localhost:8000');
  assert.equal(voiceErrorMessage(null), 'Voice failed');
  assert.equal(voiceErrorMessage({}), 'Voice failed');
  assert.equal(voiceErrorMessage('string'), 'Voice failed');
});

test('microphoneErrorMessage names permission, missing and busy devices', () => {
  assert.match(microphoneErrorMessage({ name: 'NotAllowedError' }), /denied/);
  assert.match(microphoneErrorMessage({ name: 'SecurityError' }), /denied/);
  assert.equal(microphoneErrorMessage({ name: 'NotFoundError' }), 'No microphone found');
  assert.match(microphoneErrorMessage({ name: 'NotReadableError' }), /busy/);
  assert.equal(microphoneErrorMessage({ name: 'Weird', message: 'boom' }), 'Microphone failed: boom');
  assert.equal(microphoneErrorMessage(undefined), 'Microphone failed');
});

// ─── The mic gate is one rule ────────────────────────────────────────────────

test('voicePlan derives mic from canUseMicrophone, so the gate has one home', () => {
  assert.equal(voicePlan(status(), { ...CAPS, secureContext: false }).mic, 'insecure');
  assert.equal(voicePlan(status(), { ...CAPS, mediaDevices: false }).mic, 'unsupported');
  assert.equal(voicePlan(status(), CAPS).mic, canUseMicrophone(true, true) ? 'ok' : 'unsupported');
});

// ─── Space: a hold records, a tap types ──────────────────────────────────────

test('the Space hold threshold is a short, positive number of milliseconds', () => {
  assert.ok(Number.isInteger(VOICE_SPACE_HOLD_MS));
  assert.ok(VOICE_SPACE_HOLD_MS >= 100 && VOICE_SPACE_HOLD_MS <= 500, VOICE_SPACE_HOLD_MS);
});

// ─── Status line after a transcript ──────────────────────────────────────────

test('transcriptStatusLine says "$0, on this device" for a known zero and the pill text otherwise', () => {
  const local = { provider: 'local', model: 'small.en', latency_ms: 812, cost_usd: 0, cost_known: true };
  assert.equal(transcriptStatusLine(local, '$0.0000'), `local small.en · ${VOICE_LOCAL_COST_NOTE} · 812 ms`);
  const groq = { provider: 'groq', model: 'whisper-large-v3', latency_ms: 399.6, cost_usd: null, cost_known: false };
  assert.equal(transcriptStatusLine(groq, 'cost unknown'), 'groq whisper-large-v3 · cost unknown · 400 ms');
  const priced = { provider: 'groq', model: 'whisper-large-v3', latency_ms: 10, cost_usd: 0.0012, cost_known: true };
  assert.equal(transcriptStatusLine(priced, '$0.0012'), 'groq whisper-large-v3 · $0.0012 · 10 ms');
  const bare = { provider: 'local', model: '', latency_ms: Number.NaN, cost_usd: 0, cost_known: true };
  assert.equal(transcriptStatusLine(bare, ''), `local · ${VOICE_LOCAL_COST_NOTE} · 0 ms`);
});
