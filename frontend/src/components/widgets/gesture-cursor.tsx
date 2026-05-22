// On-screen cursor driven by the hand-tracking controller. Shape and
// colour change with the active gesture; pointer-events-none so it
// never blocks the UI it floats over.

import { GESTURE_LABEL, type CursorState } from '@/lib/jarvis-ui/gestures';
import { cn } from '@/lib/utils';

export function GestureCursor({ cursor }: { cursor: CursorState | null }) {
  if (!cursor || !cursor.visible) return null;

  const grabbing = cursor.gesture === 'fist';
  const label = GESTURE_LABEL[cursor.gesture];

  return (
    <div
      className="pointer-events-none fixed z-[100000] -translate-x-1/2 -translate-y-1/2"
      style={{ left: cursor.x, top: cursor.y }}
      aria-hidden
    >
      <div
        className={cn(
          'rounded-full border-2 transition-all duration-100',
          grabbing
            ? 'h-5 w-5 border-[#3CDFFF] bg-[#3CDFFF]/40 shadow-[0_0_18px_rgba(60,223,255,0.9)]'
            : 'h-9 w-9 border-[#3CDFFF]/85 shadow-[0_0_14px_rgba(60,223,255,0.6)]'
        )}
      />
      <div className="absolute top-1/2 left-1/2 h-1 w-1 -translate-x-1/2 -translate-y-1/2 rounded-full bg-[#eafaff]" />
      {label && (
        <span className="absolute top-full left-1/2 mt-1.5 -translate-x-1/2 rounded bg-[#04101a]/80 px-1.5 py-0.5 font-mono text-[10px] tracking-[0.18em] text-[#bfeefc] uppercase">
          {label}
        </span>
      )}
    </div>
  );
}
