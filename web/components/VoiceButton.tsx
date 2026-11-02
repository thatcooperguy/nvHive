'use client';

/**
 * VoiceButton — the hold-to-talk mic in the Wizard composer (0.46 §8).
 *
 * The browser records (MediaRecorder; pointer hold on the button, or Space
 * HELD for `VOICE_SPACE_HOLD_MS` while the composer textarea is focused and
 * empty — a shorter tap types its space through `onSpaceTap` and never opens
 * the mic; 60 s cap) and the API transcribes (`POST /v1/voice/transcribe`).
 * On release the transcript
 * goes to `onTranscript`, and the thread INSERTS it into the draft — nothing
 * here ever sends: a privileged request still has to be read and clicked.
 *
 * Which server path answers comes from `GET /v1/voice/status` through the
 * pure `voicePlan` (lib/voice.ts): local faster-whisper ($0, on this
 * device), Groq Whisper (`cost unknown` — no catalog row), "download the
 * model first" (the local package is installed, the weights are not — the
 * **Download model (~480 MB)** button starts the explicit
 * `voice-model-download` job and tails it with `watchInstallJob`; the first
 * download is never implicit), or nothing (the button is disabled with the
 * install hint). The status line under the mic names the path and its cost;
 * errors (401/413/415/503/network) are rendered as text there, never thrown.
 *
 * The mic is hidden without a secure context (localhost qualifies; a LAN
 * `--host` over http shows "voice needs https or localhost" instead). Audio
 * is never stored: the Blob goes from the recorder into the request body,
 * no object URL and no storage key. While recording, the mascot shows a
 * "Listening…" tip and nothing else — `thinking` means the model is working.
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import { getVoiceStatus, startVoiceDownload, transcribeAudio, watchInstallJob, type VoiceApiError } from '@/lib/api';
import { costPill } from '@/lib/cost';
import { dismissMascotTip, sayMascotTip } from '@/lib/mascot';
import type { VoiceDownloadEvent, VoiceStatus, VoiceTranscript } from '@/lib/types';
import {
  VOICE_DOWNLOAD_LABEL,
  VOICE_INSECURE_HINT,
  VOICE_LISTENING_TIP,
  VOICE_MAX_RECORDING_MS,
  VOICE_SPACE_HOLD_MS,
  VOICE_UNSUPPORTED_HINT,
  microphoneErrorMessage,
  pickRecorderMime,
  transcriptStatusLine,
  voiceErrorMessage,
  voicePlan,
  type BrowserVoiceCaps,
} from '@/lib/voice';

export interface VoiceButtonProps {
  /** Called once per recording with the trimmed transcript; the caller inserts it and never sends. */
  onTranscript: (text: string, transcript: VoiceTranscript) => void;
  /** The composer textarea: Space held while it is focused and empty records too. */
  composerRef?: React.RefObject<HTMLTextAreaElement | null>;
  /** A Space tap in the empty composer, shorter than the hold: the caller types the space it meant to. */
  onSpaceTap?: () => void;
  /** A turn is streaming — no new recording until it settles. */
  disabled?: boolean;
}

function readCaps(): BrowserVoiceCaps {
  if (typeof window === 'undefined') {
    return { secureContext: false, mediaDevices: false, speechSynthesis: false };
  }
  return {
    secureContext: window.isSecureContext === true,
    mediaDevices:
      typeof navigator.mediaDevices?.getUserMedia === 'function' &&
      typeof window.MediaRecorder === 'function',
    speechSynthesis: 'speechSynthesis' in window,
  };
}

export default function VoiceButton({ onTranscript, onSpaceTap, composerRef, disabled = false }: VoiceButtonProps) {
  const [caps, setCaps] = useState<BrowserVoiceCaps | null>(null);
  const [status, setStatus] = useState<VoiceStatus | null>(null);
  const [recording, setRecording] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // "groq · cost unknown · 812 ms" after a transcript — the status line's proof of which path answered.
  const [lastHeard, setLastHeard] = useState<string | null>(null);
  const [downloadJobId, setDownloadJobId] = useState<string | null>(null);
  const [downloadMessage, setDownloadMessage] = useState<string | null>(null);

  const recorderRef = useRef<MediaRecorder | null>(null);
  const streamRef = useRef<MediaStream | null>(null);
  const chunksRef = useRef<Blob[]>([]);
  const capTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  // True between press and release (pointer or Space). getUserMedia may resolve
  // after a short tap released — then the stream is dropped, nothing records.
  const heldRef = useRef(false);
  // Space in the empty composer: the hold timer while the key is down but the
  // threshold has not passed, and whether the current recording is Space's.
  const spaceTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const spaceHeldRef = useRef(false);
  const canRecordRef = useRef(false);
  const mountedRef = useRef(true);
  const stopWatchRef = useRef<(() => void) | null>(null);
  const onTranscriptRef = useRef(onTranscript);
  const onSpaceTapRef = useRef(onSpaceTap);
  useEffect(() => {
    onTranscriptRef.current = onTranscript;
    onSpaceTapRef.current = onSpaceTap;
  }, [onTranscript, onSpaceTap]);

  const refreshStatus = useCallback(async () => {
    try {
      const next = await getVoiceStatus();
      if (mountedRef.current) setStatus(next);
    } catch {
      // Offline, or an API without the voice routes: the plan says so.
      if (mountedRef.current) setStatus(null);
    }
  }, []);

  useEffect(() => {
    mountedRef.current = true;
    setCaps(readCaps());
    void refreshStatus();
    return () => {
      mountedRef.current = false;
    };
  }, [refreshStatus]);

  const plan = caps ? voicePlan(status, caps) : null;
  const pathReady = plan?.stt === 'server-local' || plan?.stt === 'server-groq';
  const canRecord = Boolean(plan && plan.mic === 'ok' && pathReady && !disabled && !busy);
  useEffect(() => {
    canRecordRef.current = canRecord;
  }, [canRecord]);

  // ─── Download job ─────────────────────────────────────────────────────────

  const watchDownload = useCallback(
    (jobId: string) => {
      stopWatchRef.current?.();
      setDownloadJobId(jobId);
      setDownloadMessage(null);
      stopWatchRef.current = watchInstallJob<VoiceDownloadEvent>(jobId, {
        onEvent: (event) => {
          if (event.message) setDownloadMessage(event.message);
        },
        onComplete: () => {
          setDownloadJobId(null);
          setDownloadMessage(null);
          void refreshStatus();
        },
        onError: (message) => {
          setDownloadJobId(null);
          setDownloadMessage(null);
          setError(message);
          void refreshStatus();
        },
      });
    },
    [refreshStatus],
  );

  // A download another tab started shows up in the status as `download_job_id`
  // (the key `_status_payload` sends); tail it so this tab learns when it lands.
  useEffect(() => {
    if (status?.downloading && status.download_job_id && status.download_job_id !== downloadJobId) {
      watchDownload(status.download_job_id);
    }
  }, [status, downloadJobId, watchDownload]);

  useEffect(
    () => () => {
      stopWatchRef.current?.();
    },
    [],
  );

  const startDownload = async () => {
    setError(null);
    try {
      const outcome = await startVoiceDownload();
      const jobId = outcome.started?.job_id ?? outcome.conflict?.job_id ?? '';
      if (jobId) watchDownload(jobId);
      else setError(outcome.conflict?.message ?? 'The download did not start');
    } catch (err) {
      setError(voiceErrorMessage(err));
    }
  };

  // ─── Recording ────────────────────────────────────────────────────────────

  const releaseStream = () => {
    streamRef.current?.getTracks().forEach((track) => track.stop());
    streamRef.current = null;
  };

  const finish = useCallback(
    async (blob: Blob, mime: string) => {
      if (blob.size === 0) {
        setError('Nothing was recorded — hold the button while you speak');
        return;
      }
      setBusy(true);
      try {
        const result = await transcribeAudio(blob, mime);
        if (!mountedRef.current) return;
        const text = (result.text ?? '').trim();
        const pill = costPill(result.cost_usd, result.cost_known);
        setLastHeard(transcriptStatusLine(result, pill?.text ?? ''));
        if (text) onTranscriptRef.current(text, result);
        else setError('Nothing heard — try again, closer to the microphone');
      } catch (err) {
        if (!mountedRef.current) return;
        setError(voiceErrorMessage(err));
        const failure = err as VoiceApiError | null;
        if (failure?.statusCode === 503) {
          // The 503's `status` is the bare voice_status() snapshot; `downloading`
          // sits beside it in the detail, so merge it (else the plan would offer
          // the Download button while a download is in flight) and re-read the
          // full status for the job id.
          if (failure.unavailable?.status) {
            setStatus({ ...failure.unavailable.status, downloading: failure.unavailable.downloading });
          }
          void refreshStatus();
        }
      } finally {
        if (mountedRef.current) setBusy(false);
      }
    },
    [refreshStatus],
  );

  const stop = useCallback(() => {
    heldRef.current = false;
    spaceHeldRef.current = false;
    if (capTimerRef.current) {
      clearTimeout(capTimerRef.current);
      capTimerRef.current = null;
    }
    const recorder = recorderRef.current;
    if (!recorder) return;
    dismissMascotTip();
    if (recorder.state !== 'inactive') {
      try {
        recorder.stop(); // onstop releases the stream and transcribes
        return;
      } catch {
        // fall through: release by hand
      }
    }
    recorderRef.current = null;
    releaseStream();
    setRecording(false);
  }, []);

  const start = useCallback(async () => {
    if (!canRecordRef.current || recorderRef.current || streamRef.current) return;
    heldRef.current = true;
    setError(null);
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      if (!mountedRef.current || !heldRef.current || recorderRef.current) {
        // Released during the permission prompt, or a second press won.
        stream.getTracks().forEach((track) => track.stop());
        return;
      }
      streamRef.current = stream;
      const mime = pickRecorderMime((candidate) => MediaRecorder.isTypeSupported(candidate));
      const recorder = mime ? new MediaRecorder(stream, { mimeType: mime }) : new MediaRecorder(stream);
      chunksRef.current = [];
      recorder.ondataavailable = (event: BlobEvent) => {
        if (event.data && event.data.size > 0) chunksRef.current.push(event.data);
      };
      recorder.onstop = () => {
        const type = recorder.mimeType || mime || 'audio/webm';
        const blob = new Blob(chunksRef.current, { type });
        chunksRef.current = [];
        recorderRef.current = null;
        releaseStream();
        if (!mountedRef.current) return;
        setRecording(false);
        void finish(blob, type);
      };
      recorderRef.current = recorder;
      recorder.start();
      setRecording(true);
      // Tip only — the sprite stays where it is (MASCOT.md: `thinking` is the model working).
      sayMascotTip(VOICE_LISTENING_TIP, { id: `voice:listening:${Date.now()}`, ttlMs: 0 });
      capTimerRef.current = setTimeout(() => stop(), VOICE_MAX_RECORDING_MS);
    } catch (err) {
      heldRef.current = false;
      releaseStream();
      if (mountedRef.current) setError(microphoneErrorMessage(err));
    }
  }, [finish, stop]);

  // Space HELD in the empty, focused composer records too (design §8). The
  // first keydown only arms a timer: released before VOICE_SPACE_HOLD_MS it was
  // a tap, and the caller types the space; still down when the timer fires, the
  // recording starts. Every auto-repeat keydown while armed or recording is
  // swallowed, so the textarea never fills with spaces during a hold.
  useEffect(() => {
    const clearSpaceTimer = () => {
      if (spaceTimerRef.current !== null) {
        clearTimeout(spaceTimerRef.current);
        spaceTimerRef.current = null;
      }
    };
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.code !== 'Space') return;
      if (spaceTimerRef.current !== null || spaceHeldRef.current) {
        event.preventDefault();
        return;
      }
      if (event.repeat || heldRef.current) return;
      const composer = composerRef?.current;
      if (!composer || document.activeElement !== composer || composer.value !== '') return;
      if (!canRecordRef.current) return;
      event.preventDefault();
      spaceTimerRef.current = setTimeout(() => {
        spaceTimerRef.current = null;
        spaceHeldRef.current = true;
        void start();
      }, VOICE_SPACE_HOLD_MS);
    };
    const onKeyUp = (event: KeyboardEvent) => {
      if (event.code !== 'Space') return;
      if (spaceTimerRef.current !== null) {
        clearSpaceTimer();
        event.preventDefault();
        onSpaceTapRef.current?.();
        return;
      }
      if (!spaceHeldRef.current) return;
      event.preventDefault();
      stop();
    };
    document.addEventListener('keydown', onKeyDown);
    document.addEventListener('keyup', onKeyUp);
    return () => {
      clearSpaceTimer();
      document.removeEventListener('keydown', onKeyDown);
      document.removeEventListener('keyup', onKeyUp);
    };
  }, [composerRef, start, stop]);

  // Unmount mid-recording: stop the tracks, drop the audio, clear the tip.
  useEffect(
    () => () => {
      if (capTimerRef.current) clearTimeout(capTimerRef.current);
      if (spaceTimerRef.current !== null) clearTimeout(spaceTimerRef.current);
      const recorder = recorderRef.current;
      recorderRef.current = null;
      if (recorder) {
        dismissMascotTip();
        try {
          recorder.onstop = null;
          if (recorder.state !== 'inactive') recorder.stop();
        } catch {
          // already stopped
        }
      }
      streamRef.current?.getTracks().forEach((track) => track.stop());
      streamRef.current = null;
    },
    [],
  );

  // ─── Render ───────────────────────────────────────────────────────────────

  if (!caps || !plan) return null;

  if (plan.mic !== 'ok') {
    return (
      <span
        className="self-center max-w-[10rem] text-right text-[10px] font-mono leading-tight"
        style={{ color: 'var(--text-faint)' }}
        role="note"
        title={
          plan.mic === 'insecure'
            ? 'Browsers only open the microphone on https or on localhost. Open the WebUI on this machine, or put it behind https.'
            : 'This browser has no getUserMedia / MediaRecorder.'
        }
      >
        {plan.mic === 'insecure' ? VOICE_INSECURE_HINT : VOICE_UNSUPPORTED_HINT}
      </span>
    );
  }

  const noPath = !pathReady;
  const buttonDisabled = disabled || busy || noPath;
  const notes = plan.notes.join(' — ');
  const title = recording
    ? 'Release to transcribe'
    : busy
      ? 'Transcribing…'
      : plan.stt === 'none'
        ? notes
        : plan.stt === 'download-needed'
          ? `${VOICE_DOWNLOAD_LABEL} first — ${notes}`
          : `Hold to talk (${plan.costNote}); or hold Space in the empty box. ` +
            'What you said lands in the box — you still press Send.';
  const statusLine = lastHeard ?? (plan.costNote || plan.notes[0] || '');

  return (
    <div className="flex flex-col items-end gap-0.5" data-voice-plan={plan.stt}>
      <button
        type="button"
        aria-label="Hold to talk"
        aria-pressed={recording}
        title={title}
        disabled={buttonDisabled}
        onContextMenu={(event) => event.preventDefault()}
        onPointerDown={(event) => {
          if (event.pointerType === 'mouse' && event.button !== 0) return;
          // Keep the composer focused: the transcript lands there on release.
          event.preventDefault();
          try {
            event.currentTarget.setPointerCapture(event.pointerId);
          } catch {
            // capture unsupported — pointerup on the button still fires
          }
          void start();
        }}
        onPointerUp={() => stop()}
        onPointerCancel={() => stop()}
        onKeyDown={(event) => {
          if ((event.key === ' ' || event.key === 'Enter') && !event.repeat) {
            event.preventDefault();
            void start();
          }
        }}
        onKeyUp={(event) => {
          if (event.key === ' ' || event.key === 'Enter') {
            event.preventDefault();
            stop();
          }
        }}
        onBlur={() => {
          if (recorderRef.current) stop();
        }}
        className="rounded-md border px-2 py-2 text-xs font-mono transition-colors hover:bg-[var(--bg-hover)] disabled:opacity-50"
        style={{
          borderColor: recording ? '#dc2626' : 'var(--border)',
          color: recording ? '#dc2626' : busy ? '#76B900' : 'var(--text-muted)',
          touchAction: 'none',
        }}
      >
        <svg aria-hidden className="h-3.5 w-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2}>
          <rect x="9" y="3" width="6" height="11" rx="3" />
          <path strokeLinecap="round" strokeLinejoin="round" d="M5 11a7 7 0 0014 0M12 18v3M8 21h8" />
        </svg>
      </button>
      <div
        className="max-w-[12rem] text-right text-[10px] font-mono leading-tight"
        style={{ color: 'var(--text-faint)' }}
        aria-live="polite"
      >
        {recording ? (
          <span style={{ color: '#dc2626' }}>Listening… release to stop</span>
        ) : busy ? (
          <span>Transcribing…</span>
        ) : error ? (
          <span style={{ color: '#dc2626' }} role="alert">
            {error}
          </span>
        ) : (
          <span title={notes}>{statusLine}</span>
        )}
        {plan.downloadOffered && !downloadJobId && !recording && !busy && (
          <>
            {' · '}
            <button
              type="button"
              onClick={() => void startDownload()}
              className="underline transition-colors hover:text-[#76B900]"
              style={{ color: 'var(--text-muted)' }}
              title={`Fetch the ${status?.stt?.model || 'faster-whisper'} weights into the API's cache once, as a job you can watch. Until then the mic ${plan.stt === 'server-groq' ? 'uses Groq Whisper' : 'stays off'}.`}
            >
              {VOICE_DOWNLOAD_LABEL}
            </button>
          </>
        )}
        {downloadJobId && (
          <span title={`job ${downloadJobId}`}>
            {' · '}downloading{downloadMessage ? `: ${downloadMessage}` : '…'}
          </span>
        )}
      </div>
    </div>
  );
}
