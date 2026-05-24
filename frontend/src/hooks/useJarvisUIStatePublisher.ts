// Pushes the live HUD widget inventory to the worker over the
// `jarvis-ui-state` LiveKit data topic. The worker reads this to
// disambiguate intents — "close the youtube" closes the panel that's
// actually open; "mute" picks the right audio source among open widgets
// and desktop processes. Throttled to one publish per 250 ms (trailing
// edge) so rapid widget churn doesn't flood the channel.

import { useEffect, useRef } from 'react';
import { useRoomContext } from '@livekit/components-react';
import {
  JARVIS_UI_STATE_TOPIC,
  type JarvisUIStateMessage,
  type OpenWidgetSummary,
} from '@/lib/jarvis-ui/protocol';
import { useJarvisUI } from '@/lib/jarvis-ui/store';

const THROTTLE_MS = 250;

function summarize(widgets: ReturnType<typeof useJarvisUI>['widgets']): OpenWidgetSummary[] {
  return widgets.map((w) => ({ id: w.id, kind: w.kind, title: w.title }));
}

export function useJarvisUIStatePublisher(): void {
  const room = useRoomContext();
  const { widgets } = useJarvisUI();

  const lastSentAt = useRef(0);
  const pendingTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const lastPayload = useRef<string>('');
  const initialSent = useRef(false);

  useEffect(() => {
    if (!room) return;

    const send = (open: OpenWidgetSummary[]) => {
      const msg: JarvisUIStateMessage = { type: 'widget_state', open };
      const json = JSON.stringify(msg);
      if (json === lastPayload.current) return;
      try {
        room.localParticipant.publishData(
          new TextEncoder().encode(json),
          { reliable: true, topic: JARVIS_UI_STATE_TOPIC }
        );
        lastPayload.current = json;
        lastSentAt.current = Date.now();
      } catch {
        /* room not connected yet */
      }
    };

    const snapshot = summarize(widgets);

    if (!initialSent.current) {
      send(snapshot);
      initialSent.current = true;
      return;
    }

    const now = Date.now();
    const sinceLast = now - lastSentAt.current;
    if (pendingTimer.current) clearTimeout(pendingTimer.current);
    if (sinceLast >= THROTTLE_MS) {
      send(snapshot);
    } else {
      pendingTimer.current = setTimeout(() => {
        pendingTimer.current = null;
        send(summarize(widgets));
      }, THROTTLE_MS - sinceLast);
    }
  }, [room, widgets]);

  useEffect(
    () => () => {
      if (pendingTimer.current) clearTimeout(pendingTimer.current);
    },
    []
  );
}
