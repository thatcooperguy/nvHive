'use client';

/**
 * ReadAloudToggle — the "Read aloud" switch beside the Wizard composer and the
 * hook behind it (0.46 §8).
 *
 * Text-to-speech is the browser's own `speechSynthesis`: zero server work,
 * offline, and the same on Windows on Arm, which is why the web never calls
 * `POST /v1/voice/speak`. `useReadAloud()` owns the preference (localStorage
 * `nvh.voice.readAloud`, read and written inside try/catch — app/settings
 * clears that key with the other browser data) and the speech queue; the
 * thread calls `speak(text)` once per COMPLETED assistant message (the `done`
 * event, never a streaming token), and `speak` is a no-op while the toggle
 * is off. Toggling off and unmounting cancel whatever is still being said.
 * The Markdown is flattened first (`stripMarkdownForSpeech` — fenced code is
 * "code omitted", links read as their text). The toggle renders nothing in a
 * browser without `speechSynthesis`.
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import { VOICE_READ_ALOUD_KEY, stripMarkdownForSpeech } from '@/lib/voice';

export interface ReadAloud {
  /** The preference, hydrated from localStorage after mount (false on the server). */
  enabled: boolean;
  /** `window.speechSynthesis` exists; false until mounted. */
  supported: boolean;
  setEnabled: (value: boolean) => void;
  /** Speak a completed answer once — a no-op while disabled or unsupported. */
  speak: (text: string) => void;
  /** Cancel anything still being said. */
  stop: () => void;
}

function hasSpeechSynthesis(): boolean {
  return (
    typeof window !== 'undefined' &&
    'speechSynthesis' in window &&
    typeof window.SpeechSynthesisUtterance === 'function'
  );
}

export function useReadAloud(): ReadAloud {
  const [supported, setSupported] = useState(false);
  const [enabled, setEnabledState] = useState(false);
  // `speak` is called from the thread's stream loop, a closure made before the
  // latest render — the ref keeps it honest about the current preference.
  const enabledRef = useRef(false);

  useEffect(() => {
    const ok = hasSpeechSynthesis();
    setSupported(ok);
    let on = false;
    try {
      const stored = window.localStorage.getItem(VOICE_READ_ALOUD_KEY);
      on = stored === '1' || stored === 'true';
    } catch {
      // Storage may be blocked in hardened browser profiles — default off.
    }
    enabledRef.current = ok && on;
    setEnabledState(ok && on);
  }, []);

  const stop = useCallback(() => {
    try {
      if (hasSpeechSynthesis()) window.speechSynthesis.cancel();
    } catch {
      // nothing to cancel
    }
  }, []);

  const setEnabled = useCallback(
    (value: boolean) => {
      enabledRef.current = value;
      setEnabledState(value);
      try {
        window.localStorage.setItem(VOICE_READ_ALOUD_KEY, value ? '1' : '0');
      } catch {
        // Storage blocked — the toggle still works for this page.
      }
      if (!value) stop();
    },
    [stop],
  );

  const speak = useCallback((text: string) => {
    if (!enabledRef.current || !hasSpeechSynthesis()) return;
    const spoken = stripMarkdownForSpeech(text);
    if (!spoken) return;
    try {
      window.speechSynthesis.speak(new SpeechSynthesisUtterance(spoken));
    } catch {
      // A browser that lists the API but refuses to speak: stay silent.
    }
  }, []);

  // Unmount (navigating away from the thread) cancels anything mid-sentence.
  useEffect(() => () => stop(), [stop]);

  return { enabled, supported, setEnabled, speak, stop };
}

export default function ReadAloudToggle({ readAloud }: { readAloud: ReadAloud }) {
  if (!readAloud.supported) return null;
  const { enabled } = readAloud;
  return (
    <button
      type="button"
      aria-pressed={enabled}
      onClick={() => readAloud.setEnabled(!enabled)}
      className="flex items-center gap-1 rounded-md border px-2 py-0.5 transition-colors hover:bg-[var(--bg-hover)]"
      style={{
        borderColor: enabled ? 'var(--border-green)' : 'var(--border)',
        color: enabled ? '#76B900' : 'var(--text-muted)',
      }}
      title={
        enabled
          ? "Read aloud is on: each finished Wizard answer is spoken by your browser's own voice. Click to stop and turn it off."
          : "Read each finished Wizard answer aloud with your browser's own voice (nothing leaves this device)."
      }
    >
      <span aria-hidden>{enabled ? '🔊' : '🔈'}</span>
      <span>Read aloud{enabled ? ' · on' : ''}</span>
    </button>
  );
}
