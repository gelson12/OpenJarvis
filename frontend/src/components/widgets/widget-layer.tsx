// Full-screen layer that renders every open widget plus the gesture
// cursor. Mounted once inside the LiveKit room. `pointer-events-none`
// so it never blocks the voice UI underneath; each WidgetShell
// re-enables pointer events for itself.

import { useCallback, useState } from 'react';
import { AnimatePresence } from 'motion/react';
import { useGestureControl } from '@/hooks/useGestureControl';
import { useJarvisUIChannel } from '@/hooks/useJarvisUIChannel';
import { useJarvisUIStatePublisher } from '@/hooks/useJarvisUIStatePublisher';
import type { CursorState } from '@/lib/jarvis-ui/gestures';
import { useJarvisUI } from '@/lib/jarvis-ui/store';
import { GestureCursor } from './gesture-cursor';
import { getWidgetComponent } from './registry';
import { WidgetShell } from './widget-shell';

export function WidgetLayer() {
  useJarvisUIChannel();
  useJarvisUIStatePublisher();
  const { widgets, highlightId, close, focus, move } = useJarvisUI();
  const [cursor, setCursor] = useState<CursorState | null>(null);

  // Clamp moves so a widget's title bar always stays grabbable.
  const clampedMove = useCallback(
    (id: string, x: number, y: number) => {
      const vw = typeof window !== 'undefined' ? window.innerWidth : 1920;
      const vh = typeof window !== 'undefined' ? window.innerHeight : 1080;
      move(id, Math.min(Math.max(x, -40), vw - 80), Math.min(Math.max(y, 0), vh - 56));
    },
    [move]
  );

  useGestureControl(setCursor);

  return (
    <div className="pointer-events-none fixed inset-0 z-40">
      <AnimatePresence>
        {widgets.map((widget) => {
          const Widget = getWidgetComponent(widget.kind);
          return (
            <WidgetShell
              key={widget.id}
              widget={widget}
              highlighted={widget.id === highlightId}
              onClose={() => close(widget.id)}
              onFocus={() => focus(widget.id)}
              onMove={(x, y) => clampedMove(widget.id, x, y)}
            >
              <Widget widget={widget} />
            </WidgetShell>
          );
        })}
      </AnimatePresence>
      <GestureCursor cursor={cursor} />
    </div>
  );
}
