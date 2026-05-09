// Client-side Voice Activity Detection for barge-in.
//
// Wraps @ricky0123/vad-web (Silero ONNX) so the agent can detect when
// the user starts speaking while Jarvis is mid-response. Fires
// ``onBargeIn`` once per detected onset; the caller is responsible for
// cancelling TTS + the in-flight LLM stream.
//
// Why a custom hook instead of @ricky0123/vad-react:
//   - We need a 150 ms cooldown after the most recent TTS activity to
//     suppress self-echo (browser AEC alone is insufficient — TTS goes
//     through the OS audio path, not the in-page WebRTC graph).
//   - We need lazy gesture-bound init for Safari/iOS PWA (AudioContext
//     starts ``suspended`` without a user gesture).
//   - We need pause/resume (not destroy/recreate) between utterances
//     to avoid 50-200 ms ONNX warmup on every sentence flush.
//   - We need StrictMode-safe cleanup (vad.destroy() + track.stop()).
//
// Three-state machine:
//   1. Idle (no streaming, no TTS)        — VAD paused
//   2. AI speaking (active === true)      — VAD running, listens for onset
//   3. User talking (post-barge-in)       — VAD paused; SpeechRecognition
//                                           captures the actual transcript

import { useEffect, useRef } from 'react';

interface UseBargeInVADOptions {
  /** Master enable toggle (Settings.bargeInEnabled). When false, VAD is
   *  destroyed entirely and the mic indicator turns off. */
  enabled: boolean;
  /** Whether VAD should be actively listening right now. Caller computes
   *  this from ``streamState.isStreaming || tts.isSpeaking()``. */
  active: boolean;
  /** Fired when a real (non-echo) speech onset is detected. */
  onBargeIn: () => void;
  /** Optional: caller-supplied function returning the timestamp of the
   *  last TTS activity (token-stream callback or speaking transition).
   *  Used for the 150 ms self-echo cooldown. Default: never (no
   *  cooldown). */
  lastTtsActivityAt?: () => number;
}

// MicVAD/RealTimeVAD types are not exported in a friendly shape; we
// type the bits we touch.
interface VADInstance {
  start: () => void;
  pause: () => void;
  destroy: () => Promise<void> | void;
}

const COOLDOWN_MS = 150; // self-echo suppression after TTS activity
const MIN_SPEECH_FRAMES = 7; // ~224 ms at 32 ms/frame — filters speaker reflections
const POSITIVE_SPEECH_THRESHOLD = 0.8;
const NEGATIVE_SPEECH_THRESHOLD = 0.35;
const PAUSE_DELAY_MS = 500; // wait this long after active flips false before pausing

export function useBargeInVAD(opts: UseBargeInVADOptions): void {
  const { enabled, active, onBargeIn, lastTtsActivityAt } = opts;

  const vadRef = useRef<VADInstance | null>(null);
  const userGestureSeenRef = useRef(false);
  const initInFlightRef = useRef(false);
  const pauseTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  // Capture the latest props in refs so the inside-of-callback closures
  // see fresh values without re-creating MicVAD on every render.
  const onBargeInRef = useRef(onBargeIn);
  const lastTtsActivityRef = useRef(lastTtsActivityAt);
  onBargeInRef.current = onBargeIn;
  lastTtsActivityRef.current = lastTtsActivityAt;

  // ── Gesture detection ───────────────────────────────────────────
  // Safari/iOS require a user gesture before AudioContext can leave the
  // ``suspended`` state. We listen for the first interaction once per
  // session and only then allow VAD to initialize. On Chrome/desktop
  // gestures are usually already satisfied (users click the page first
  // before voice matters), but guarding everywhere keeps the path
  // identical on every platform.
  useEffect(() => {
    if (typeof window === 'undefined') return;
    if (userGestureSeenRef.current) return;
    const onGesture = () => {
      userGestureSeenRef.current = true;
      window.removeEventListener('pointerdown', onGesture);
      window.removeEventListener('keydown', onGesture);
    };
    window.addEventListener('pointerdown', onGesture, { once: true });
    window.addEventListener('keydown', onGesture, { once: true });
    return () => {
      window.removeEventListener('pointerdown', onGesture);
      window.removeEventListener('keydown', onGesture);
    };
  }, []);

  // ── VAD lifecycle ───────────────────────────────────────────────
  // Init lazily on first activation; pause/start on subsequent
  // ``active`` toggles; destroy on disable / unmount.
  useEffect(() => {
    if (typeof window === 'undefined') return;
    let cancelled = false;

    async function ensureInit(): Promise<void> {
      if (vadRef.current || initInFlightRef.current) return;
      if (!userGestureSeenRef.current) return; // wait for gesture
      initInFlightRef.current = true;
      try {
        // Dynamic import keeps the bundle lean — VAD/ONNX assets only
        // load when the feature is first activated. MicVAD manages
        // getUserMedia + AudioContext + AudioWorklet itself; we only
        // wire callbacks. SpeechRecognition opens its own mic handle
        // separately; Chrome multiplexes the underlying device.
        const { MicVAD } = await import('@ricky0123/vad-web');

        const vad = await MicVAD.new({
          // Assets shipped to the site root by vite-plugin-static-copy.
          baseAssetPath: '/',
          onnxWASMBasePath: '/',
          model: 'v5',
          frameSamples: 512,
          minSpeechFrames: MIN_SPEECH_FRAMES,
          positiveSpeechThreshold: POSITIVE_SPEECH_THRESHOLD,
          negativeSpeechThreshold: NEGATIVE_SPEECH_THRESHOLD,
          // Forwarded to MicVAD's internal getUserMedia call.
          additionalAudioConstraints: {
            echoCancellation: true,
            noiseSuppression: true,
            autoGainControl: true,
          },
          // The library exposes onSpeechStart for the onset event; we
          // ignore onSpeechEnd / onVADMisfire because we delegate
          // transcript capture to SpeechRecognition.
          onSpeechStart: () => {
            // Self-echo suppression: drop any onset that fires within
            // COOLDOWN_MS of the most recent TTS activity. The polling
            // loop in the caller refreshes lastTtsActivityAt while
            // synth.speaking is true; this catches both mid-utterance
            // and inter-sentence-gap cases.
            const lastFn = lastTtsActivityRef.current;
            if (lastFn) {
              const since = Date.now() - lastFn();
              if (since < COOLDOWN_MS) return;
            }
            onBargeInRef.current();
          },
        }) as unknown as VADInstance;

        if (cancelled) {
          await vad.destroy();
          return;
        }
        vadRef.current = vad;
      } catch (err) {
        // Silent fail — log to console but don't break the rest of the
        // app. Most likely causes: mic permission denied, ONNX asset
        // 404 (missing static-copy), or unsupported browser.
        console.warn('[bargeInVAD] init failed:', err);
      } finally {
        initInFlightRef.current = false;
      }
    }

    if (!enabled) {
      // Fully tear down — user toggled off. MicVAD.destroy() releases
      // its internal mic handle so the indicator turns off.
      if (pauseTimerRef.current) {
        clearTimeout(pauseTimerRef.current);
        pauseTimerRef.current = null;
      }
      if (vadRef.current) {
        const v = vadRef.current;
        vadRef.current = null;
        Promise.resolve(v.destroy()).catch(() => {});
      }
      return;
    }

    // Enabled — make sure VAD exists, then drive start/pause based on
    // ``active``.
    void ensureInit().then(() => {
      if (cancelled || !vadRef.current) return;
      if (active) {
        if (pauseTimerRef.current) {
          clearTimeout(pauseTimerRef.current);
          pauseTimerRef.current = null;
        }
        try {
          vadRef.current.start();
        } catch (err) {
          console.warn('[bargeInVAD] start failed:', err);
        }
      } else {
        // Defer pause briefly so we don't thrash on rapid streaming
        // boundary toggles (sentence flushes flip tts.isSpeaking()
        // briefly false during inter-sentence gaps).
        if (pauseTimerRef.current) clearTimeout(pauseTimerRef.current);
        pauseTimerRef.current = setTimeout(() => {
          pauseTimerRef.current = null;
          try {
            vadRef.current?.pause();
          } catch {
            /* ignore */
          }
        }, PAUSE_DELAY_MS);
      }
    });

    return () => {
      cancelled = true;
      if (pauseTimerRef.current) {
        clearTimeout(pauseTimerRef.current);
        pauseTimerRef.current = null;
      }
    };
  }, [enabled, active]);

  // Final unmount cleanup — release mic + worklet so the indicator
  // turns off and the next mount can re-init cleanly under StrictMode.
  useEffect(() => {
    return () => {
      if (vadRef.current) {
        const v = vadRef.current;
        vadRef.current = null;
        Promise.resolve(v.destroy()).catch(() => {});
      }
    };
  }, []);
}
