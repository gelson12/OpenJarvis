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
// Call sites:
//   - ElaborationBanner: ephemeral listening (~30s window for yes/no after
//     a "may I elaborate?" prompt)
//   - InputArea / dashboard: always-on listening when the user has the
//     toggle enabled — every final transcript gets routed through
//     classifyIntent() to decide what to do with it

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
  error: string;  // 'no-speech' | 'audio-capture' | 'not-allowed' | ...
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
  /**
   * Opaque value — when this changes the recognition instance is torn down
   * and restarted from scratch. Use this to force a restart after the user
   * grants mic permission in browser settings: the Permissions API onChange
   * event fires, the caller increments this key, and the hook re-runs its
   * effect so recognition can succeed where it previously got 'not-allowed'.
   */
  restartKey?: unknown;
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
  lang = 'en-US',
  restartKey,
}: UseVoiceListenerOptions): void {
  const recognitionRef = useRef<SpeechRecognitionLike | null>(null);
  // Capture latest callbacks so the effect doesn't tear down on every render
  const onTranscriptRef = useRef(onTranscript);
  const onErrorRef = useRef(onError);
  const onStartedRef = useRef(onStarted);
  onTranscriptRef.current = onTranscript;
  onErrorRef.current = onError;
  onStartedRef.current = onStarted;

  useEffect(() => {
    if (!enabled || !isSpeechRecognitionSupported()) return;

    const Ctor =
      window.SpeechRecognition || window.webkitSpeechRecognition!;
    const recognition = new Ctor();
    recognition.continuous = true;
    recognition.interimResults = true;
    recognition.lang = lang;

    let stopped = false;

    recognition.onresult = (ev: SpeechRecognitionEventLike) => {
      // Walk the new results from resultIndex onwards; emit only finals
      for (let i = ev.resultIndex; i < ev.results.length; i++) {
        const result = ev.results[i];
        if (result.isFinal) {
          const transcript = result[0]?.transcript ?? '';
          if (!transcript.trim()) continue;
          // Echo guard: if browser TTS is CURRENTLY emitting audio, the
          // mic is almost certainly picking up Jarvis's own voice and
          // SpeechRecognition is treating it as user input. Drop those
          // transcripts to avoid feedback loops where the assistant's
          // own utterance becomes the next user message.
          //
          // Only check `synth.speaking`, NOT `synth.pending`. `pending`
          // means "an utterance is queued but not yet started" — common
          // when a second ElaborationBanner stacks while the first is
          // still awaiting yes/no, leaving `pending` stuck true while
          // the user is trying to confirm. The original guard checked
          // both and produced a dead listener that silently swallowed
          // every "yes". `speaking` alone is the narrow signal that
          // matters: if Jarvis is audibly talking right now, ignore
          // the mic; otherwise let transcripts through.
          const synth =
            typeof window !== 'undefined' ? window.speechSynthesis : null;
          if (synth && synth.speaking) {
            continue;
          }
          onTranscriptRef.current(transcript);
        }
      }
    };

    recognition.onstart = () => {
      // Listener is actually running — clear any sticky 'not-allowed'
      // toast left over from a prior session that errored before mic
      // permission was granted. Without this, the user grants perm,
      // listener starts succeeding, but the red banner persists
      // indefinitely.
      onStartedRef.current?.();
    };

    recognition.onerror = (ev: SpeechRecognitionErrorEventLike) => {
      // Non-fatal — silence/audio-glitch errors auto-recover via onend.
      if (ev.error === 'no-speech' || ev.error === 'audio-capture') {
        return;
      }
      // Fatal — surface so UI can show a hint.
      onErrorRef.current?.(ev.error);
      stopped = true;  // don't auto-restart on permission denial
    };

    recognition.onend = () => {
      // Browsers (especially Chrome) end recognition after periods of
      // silence. While `enabled` is still true, restart so the listener
      // truly stays on.
      if (stopped) return;
      try {
        recognition.start();
      } catch {
        // start() throws if already started — safe to ignore.
      }
    };

    try {
      recognition.start();
      recognitionRef.current = recognition;
    } catch (exc) {
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
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [enabled, lang, restartKey]);
}
