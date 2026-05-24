// Bridges the LiveKit `jarvis-ui` data topic into the widget store.
// The worker publishes JSON commands there; we decode and apply them.

import { useCallback, useRef } from 'react';
import { useDataChannel } from '@livekit/components-react';
import { JARVIS_UI_TOPIC, type JarvisUIMessage } from '@/lib/jarvis-ui/protocol';
import { useJarvisUI } from '@/lib/jarvis-ui/store';
import { publishWidgetVolume } from '@/lib/jarvis-ui/widget-volume-bus';

export function useJarvisUIChannel(): void {
  const { applyMessage } = useJarvisUI();

  // Keep the callback identity stable so useDataChannel doesn't churn.
  const applyRef = useRef(applyMessage);
  applyRef.current = applyMessage;

  const onMessage = useCallback((msg: { payload: Uint8Array }) => {
    try {
      const parsed = JSON.parse(
        new TextDecoder().decode(msg.payload)
      ) as JarvisUIMessage;
      // Volume commands target the widget's player handle directly —
      // route them to the per-kind bus rather than the widget store
      // (which is a pure mount/position reducer).
      if (parsed.type === 'widget_volume') {
        publishWidgetVolume({
          kind: parsed.kind,
          action: parsed.action,
          level: parsed.level,
        });
        return;
      }
      applyRef.current(parsed);
    } catch (err) {
      console.warn('[jarvis-ui] dropped a malformed message', err);
    }
  }, []);

  useDataChannel(JARVIS_UI_TOPIC, onMessage);
}
