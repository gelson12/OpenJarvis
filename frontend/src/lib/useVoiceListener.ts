// Continuous browser SpeechRecognition wrapper.
//
// Wraps the Web Speech API in a React-friendly hook with the gotchas
// pre-handled:
//   - SpeechRecognition vs webkitSpeechRecognition (Safari/Chrome shim)
//   - "no-speech" / "audio-capture" errors are non-fatal — they fire when
//     the user is silent or briefly mutes; we restart automatically
//   - "not-allowed" (mic permission denied) is fatal — we surface it via
//     onError so the UI can show a "click to grant mic access" hint
//   - the API ends recognition unpredictably (especially on Chrome after
//     a long silence); we restart in onend whenever `enabled` is true,
//     so the listener feels truly continuous from the user's perspective
//
// Recovery note (important):
// Some browsers can emit a transient 'not-allowed' during permission races.
// If the caller tells us mic permission is actually 'granted', we should not
// permanently latch a fatal stopped state for that instance.

import { useEffect, useRef } from 'react';

// Minimal types for the Web Speech API since TS doesn't ship them in lib.dom.
// We declare just what we need.
interface SpeechRecognitionEventLike {
  resultIndex: number;
  results: ArrayLike<{
    0: { transcript: string; confidence: number };
    isFinal: boolean;
    length: number;
  }>;
}

interface SpeechRecognitionErrorEventLike {
  error: string; // 'no-speech' | 'audio-capture' | 'not-allowed' | ...
  message?: string;
}

interface SpeechRecognitionLike {
  continuous: boolean;
  interimResults: boolean;
  lang: string;
  start(): void;
  stop(): void;
  abort(): void;
  onresult: ((ev: SpeechRecognitionEventLike) => void) | null;
  onerror: ((ev: SpeechRecognitionErrorEventLike) => void) | null;
  onend: (() => void) | null;
  onstart: (() => void) | null;
}

declare global {
  interface Window {
    SpeechRecognition?: { new (): SpeechRecognitionLike };
    webkitSpeechRecognition?: { new (): SpeechRecognitionLike };
  }
}

export function isSpeechRecognitionSupported(): boolean {
  return (
    typeof window !== 'undefined' &&
    Boolean(window.SpeechRecognition || window.webkitSpeechRecognition)
  );
}

type MicPermissionState = 'granted' | 'denied' | 'prompt' | 'unknown';

interface UseVoiceListenerOptions {
  enabled: boolean;
  /** Called for every FINAL transcript (interim results are filtered out). */
  onTranscript: (transcript: string) => void;
  /** Optional — fired on permission denial / hardware errors so UI can react. */
  onError?: (errorCode: string) => void;
  /** Optional — fired the first time recognition.onstart fires successfully
   * after enable, so the UI can clear any stale "Mic blocked" toast left
   * over from a previous session that errored before mic perm was granted. */
  onStarted?: () => void;
  /** ISO language tag, default 'en-US'. */
  lang?: string;

  /** Caller-reported mic permission state from Permissions API (when available). */
  micPermissionState?: MicPermissionState;
}

/**
 * Hook that runs continuous speech recognition while ``enabled`` is true.
 * Cleans up on unmount or when ``enabled`` flips to false.
 */
export function useVoiceListener({
  enabled,
  onTranscript,
  onError,
  onStarted,
  micPermissionState = 'unknown',
  lang = 'en-US',
}: UseVoiceListenerOptions): void {
  const recognitionRef = useRef<SpeechRecognitionLike | null>(null);

  // Capture latest callbacks/state so the effect doesn't tear down on every render
  const onTranscriptRef = useRef(onTranscript);
  const onErrorRef = useRef(onError);
  const onStartedRef = useRef(onStarted);
  const micPermissionStateRef = useRef(micPermissionState);

  onTranscriptRef.current = onTranscript;
  onErrorRef.current = onError;
  onStartedRef.current = onStarted;
  micPermissionStateRef.current = micPermissionState;

  useEffect(() => {
    if (!enabled || !isSpeechRecognitionSupported()) return;
    // Don't even construct recognition while the mic is denied — it would
    // just error 'not-allowed' on a loop. We re-run (see deps) the moment
    // the Permissions API reports a different state, so a later 'granted'
    // gets a FRESH recognition instance instead of a dead latched one.
    if (micPermissionState === 'denied') return;

    const Ctor = window.SpeechRecognition || window.webkitSpeechRecognition!;
    const recognition = new Ctor();
    recognition.continuous = true;
    recognition.interimResults = true;
    recognition.lang = lang;

    let stopped = false;

    recognition.onresult = (ev: SpeechRecognitionEventLike) => {
      for (let i = ev.resultIndex; i < ev.results.length; i++) {
        const result = ev.results[i];
        if (!result.isFinal) continue;

        const transcript = result[0]?.transcript ?? '';
        if (!transcript.trim()) continue;

        // Echo guard: if browser TTS is currently speaking, ignore mic input.
        const synth = typeof window !== 'undefined' ? window.speechSynthesis : null;
        if (synth && synth.speaking) continue;

        onTranscriptRef.current(transcript);
      }
    };

    recognition.onstart = () => {
      onStartedRef.current?.();
    };

    recognition.onerror = (ev: SpeechRecognitionErrorEventLike) => {
      // Ignore non-fatal “silence/audio glitch” errors.
      if (ev.error === 'no-speech' || ev.error === 'audio-capture') return;

      onErrorRef.current?.(ev.error);

      // Only latch fatal stopped=true on not-allowed if we *know* permission is denied.
      if (ev.error === 'not-allowed') {
        if (micPermissionStateRef.current !== 'granted') {
          stopped = true;
        }
      } else {
        stopped = true;
      }
    };

    recognition.onend = () => {
      if (stopped) return;
      try {
        recognition.start();
      } catch {
        // start() can throw if already started — ignore.
      }
    };

    try {
      recognition.start();
      recognitionRef.current = recognition;
    } catch {
      onErrorRef.current?.('start-failed');
    }

    return () => {
      stopped = true;
      try {
        recognition.stop();
      } catch {
        // ignore
      }
      recognitionRef.current = null;
    };
    // micPermissionState IS a dependency: when it flips to 'granted'
    // after the user allows the mic, this effect re-runs and creates a
    // fresh, working recognition instance (the old one had latched
    // stopped=true on the initial 'not-allowed'). This is the fix for
    // hands-free wake-word never engaging on a cold first visit.
  }, [enabled, lang, micPermissionState]);
}
