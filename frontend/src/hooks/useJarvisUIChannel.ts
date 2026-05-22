// Bridges the LiveKit `jarvis-ui` data topic into the widget store.
// The worker publishes JSON commands there; we decode and apply them.

import { useCallback, useRef } from 'react';
import { useDataChannel } from '@livekit/components-react';
import { JARVIS_UI_TOPIC, type JarvisUIMessage } from '@/lib/jarvis-ui/protocol';
import { useJarvisUI } from '@/lib/jarvis-ui/store';

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
      applyRef.current(parsed);
    } catch (err) {
      console.warn('[jarvis-ui] dropped a malformed message', err);
    }
  }, []);

  useDataChannel(JARVIS_UI_TOPIC, onMessage);
}
