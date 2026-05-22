// Hand-gesture control for the JARVIS widgets.
//
// Runs MediaPipe's GestureRecognizer on the user's LiveKit camera track
// (so it is active exactly when the camera is on — no second getUserMedia)
// and translates gestures into widget actions:
//
//   Open palm    → move a cursor; hover-highlights the widget beneath it
//   Closed fist  → grab & drag the widget under the cursor
//   Pointing up  → bring the hovered widget to the front
//   Thumb down   → close the widget under the cursor
//   Victory ✌    → cycle the widget stack
//
// MediaPipe (WASM + model) is loaded lazily from a CDN the first time
// the camera turns on, and torn down when it turns off.

import { useEffect, useRef } from 'react';
import { Track } from 'livekit-client';
import { useLocalParticipant } from '@livekit/components-react';
import type { GestureRecognizer } from '@mediapipe/tasks-vision';
import {
  type CursorState,
  type HandGesture,
  lerp,
  normalizeGesture,
  widgetAtPoint,
} from '@/lib/jarvis-ui/gestures';
import { useJarvisUI } from '@/lib/jarvis-ui/store';

const WASM_URL = 'https://cdn.jsdelivr.net/npm/@mediapipe/tasks-vision@0.10.35/wasm';
const MODEL_URL =
  'https://storage.googleapis.com/mediapipe-models/gesture_recognizer/gesture_recognizer/float16/1/gesture_recognizer.task';

const DETECT_INTERVAL_MS = 45; // ~22 fps — plenty for hand tracking
const STABLE_FRAMES = 3; // a gesture must persist this long to commit
const ACTION_COOLDOWN_MS = 900; // min gap between discrete actions
const CURSOR_SMOOTHING = 0.4; // 0..1 — higher snaps faster, jitters more
const FINGERTIP = 8; // MediaPipe landmark index: tip of the index finger

export function useGestureControl(onCursor: (c: CursorState) => void): void {
  const { localParticipant } = useLocalParticipant();
  const ui = useJarvisUI();

  // The detection loop is long-lived; it reads live values through refs
  // so it never needs to be torn down just because a widget moved.
  const widgetsRef = useRef(ui.widgets);
  widgetsRef.current = ui.widgets;
  const apiRef = useRef({ ...ui, onCursor });
  apiRef.current = { ...ui, onCursor };

  const camPub = localParticipant.getTrackPublication(Track.Source.Camera);
  const camTrack = camPub?.track;
  const camOn = !!camTrack && !camPub?.isMuted;

  useEffect(() => {
    if (!camTrack || !camOn) return;

    let cancelled = false;
    let raf = 0;
    let recognizer: GestureRecognizer | null = null;
    let video: HTMLVideoElement | null = null;

    // Per-session loop state.
    let lastDetect = 0;
    let lastVideoTime = -1;
    const history: HandGesture[] = [];
    let committed: HandGesture = 'none';
    let lastActionAt = 0;
    let draggingId: string | null = null;
    let grabDx = 0;
    let grabDy = 0;
    let hoveredId: string | null = null;
    let cx = typeof window !== 'undefined' ? window.innerWidth / 2 : 640;
    let cy = typeof window !== 'undefined' ? window.innerHeight / 2 : 360;

    function loop() {
      if (cancelled) return;
      raf = requestAnimationFrame(loop);

      const now = performance.now();
      if (now - lastDetect < DETECT_INTERVAL_MS) return;
      lastDetect = now;
      if (!recognizer || !video || video.readyState < 2 || video.videoWidth === 0) {
        return;
      }
      if (video.currentTime === lastVideoTime) return;
      lastVideoTime = video.currentTime;

      let result;
      try {
        result = recognizer.recognizeForVideo(video, now);
      } catch {
        return;
      }

      const hands = result.landmarks;
      const api = apiRef.current;

      // ── No hand in view — reset everything ──────────────────────
      if (!hands || hands.length === 0) {
        history.length = 0;
        committed = 'none';
        draggingId = null;
        if (hoveredId !== null) {
          hoveredId = null;
          api.setHighlight(null);
        }
        api.onCursor({ x: cx, y: cy, visible: false, gesture: 'none' });
        return;
      }

      // ── Cursor follows the index fingertip (x mirrored) ─────────
      const tip = hands[0][FINGERTIP];
      cx = lerp(cx, (1 - tip.x) * window.innerWidth, CURSOR_SMOOTHING);
      cy = lerp(cy, tip.y * window.innerHeight, CURSOR_SMOOTHING);

      // ── Debounce the gesture: require STABLE_FRAMES agreement ───
      const raw = result.gestures?.[0]?.[0]?.categoryName ?? '';
      const g = normalizeGesture(raw);
      history.push(g);
      if (history.length > STABLE_FRAMES) history.shift();
      const stable =
        history.length === STABLE_FRAMES && history.every((h) => h === g)
          ? g
          : committed;
      const prev = committed;
      committed = stable;

      const widgets = widgetsRef.current;

      // ── Closed fist → grab & drag ───────────────────────────────
      if (committed === 'fist') {
        if (draggingId === null && prev !== 'fist') {
          const hit = widgetAtPoint(widgets, cx, cy);
          if (hit) {
            draggingId = hit.id;
            grabDx = cx - hit.x;
            grabDy = cy - hit.y;
            api.focus(hit.id);
          }
        }
        if (draggingId) api.move(draggingId, cx - grabDx, cy - grabDy);
      } else {
        draggingId = null;
      }

      // ── Hover highlight (only in pointer-style gestures) ────────
      const hoverWidget =
        committed === 'open' || committed === 'point'
          ? widgetAtPoint(widgets, cx, cy)
          : null;
      const hoverId = hoverWidget?.id ?? null;
      if (hoverId !== hoveredId) {
        hoveredId = hoverId;
        api.setHighlight(hoverId);
      }

      // ── Discrete actions — fire once on the rising edge ─────────
      if (committed !== prev && now - lastActionAt > ACTION_COOLDOWN_MS) {
        if (committed === 'point' && hoverWidget) {
          api.focus(hoverWidget.id);
          lastActionAt = now;
        } else if (committed === 'thumbdown') {
          const target = widgetAtPoint(widgets, cx, cy);
          if (target) {
            api.close(target.id);
            lastActionAt = now;
          }
        } else if (committed === 'victory') {
          api.cycleFocus();
          lastActionAt = now;
        }
      }

      api.onCursor({ x: cx, y: cy, visible: true, gesture: committed });
    }

    void (async () => {
      // Attach the LiveKit camera track to an off-screen <video>.
      try {
        video = camTrack.attach() as HTMLVideoElement;
      } catch {
        return;
      }
      video.muted = true;
      video.playsInline = true;
      Object.assign(video.style, {
        position: 'fixed',
        left: '-10000px',
        top: '0px',
        width: '160px',
        height: '120px',
        opacity: '0',
        pointerEvents: 'none',
      });
      document.body.appendChild(video);
      try {
        await video.play();
      } catch {
        /* autoplay race — recognizeForVideo tolerates it */
      }

      // Lazily load MediaPipe (WASM + model) from the CDN.
      try {
        const vision = await import('@mediapipe/tasks-vision');
        const fileset = await vision.FilesetResolver.forVisionTasks(WASM_URL);
        const opts = (delegate: 'GPU' | 'CPU') => ({
          baseOptions: { modelAssetPath: MODEL_URL, delegate },
          runningMode: 'VIDEO' as const,
          numHands: 1,
        });
        try {
          recognizer = await vision.GestureRecognizer.createFromOptions(
            fileset,
            opts('GPU')
          );
        } catch {
          recognizer = await vision.GestureRecognizer.createFromOptions(
            fileset,
            opts('CPU')
          );
        }
      } catch (err) {
        console.warn('[gesture] could not load hand tracking — disabled', err);
        return;
      }
      if (cancelled) {
        recognizer.close();
        recognizer = null;
        return;
      }
      raf = requestAnimationFrame(loop);
    })();

    return () => {
      cancelled = true;
      if (raf) cancelAnimationFrame(raf);
      recognizer?.close();
      if (video) {
        try {
          camTrack.detach(video);
        } catch {
          /* already detached */
        }
        video.remove();
      }
      apiRef.current.setHighlight(null);
      apiRef.current.onCursor({ x: 0, y: 0, visible: false, gesture: 'none' });
    };
  }, [camTrack, camOn]);
}
