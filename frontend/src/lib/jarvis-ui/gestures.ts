// Pure gesture-control helpers — no React, no MediaPipe. The detection
// loop (useGestureControl) and the cursor overlay both build on these.

import type { WidgetInstance } from './protocol';

/** The hand gestures we act on (mapped from MediaPipe categories). */
export type HandGesture = 'none' | 'open' | 'fist' | 'point' | 'thumbdown' | 'victory';

/** Map a raw MediaPipe GestureRecognizer category to our vocabulary. */
export function normalizeGesture(raw: string): HandGesture {
  switch (raw) {
    case 'Open_Palm':
      return 'open';
    case 'Closed_Fist':
      return 'fist';
    case 'Pointing_Up':
      return 'point';
    case 'Thumb_Down':
      return 'thumbdown';
    case 'Victory':
      return 'victory';
    default:
      return 'none';
  }
}

/** State of the on-screen gesture cursor, published to <GestureCursor>. */
export interface CursorState {
  x: number;
  y: number;
  visible: boolean;
  gesture: HandGesture;
}

/** Topmost widget whose rectangle contains the point, or null. */
export function widgetAtPoint(
  widgets: WidgetInstance[],
  x: number,
  y: number
): WidgetInstance | null {
  let hit: WidgetInstance | null = null;
  for (const w of widgets) {
    const inside = x >= w.x && x <= w.x + w.w && y >= w.y && y <= w.y + w.h;
    if (inside && (hit === null || w.z > hit.z)) {
      hit = w;
    }
  }
  return hit;
}

/** Exponential smoothing toward a target — damps hand-tracking jitter. */
export function lerp(current: number, target: number, alpha: number): number {
  return current + (target - current) * alpha;
}

/** Human-readable label per gesture, shown under the cursor. */
export const GESTURE_LABEL: Record<HandGesture, string> = {
  none: '',
  open: 'Move',
  fist: 'Grab',
  point: 'Focus',
  thumbdown: 'Close',
  victory: 'Cycle',
};
