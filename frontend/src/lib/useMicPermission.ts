// Shared microphone-permission plumbing.
//
// Extracted from the proven logic in components/Chat/InputArea.tsx so the
// Voice page can reuse it for hands-free wake-word activation without
// duplicating ~60 lines. InputArea still has its own copy for now (zero-risk
// to the working chat); it can adopt this hook later as pure cleanup.

import { useEffect, useState } from 'react';

export type MicPermissionState = 'granted' | 'denied' | 'prompt' | 'unknown';

/**
 * Track the live microphone permission state via the Permissions API.
 * Returns 'unknown' when the API is unsupported (older Safari) — callers
 * should treat 'unknown' as "try anyway, the listener will error if denied".
 */
export function useMicPermission(): { state: MicPermissionState } {
  const [state, setState] = useState<MicPermissionState>('unknown');

  useEffect(() => {
    if (typeof navigator === 'undefined' || !('permissions' in navigator)) {
      return;
    }
    let cancelled = false;
    let status: PermissionStatus | null = null;
    const onChange = () => {
      if (cancelled || !status) return;
      setState(status.state as MicPermissionState);
    };
    navigator.permissions
      .query({ name: 'microphone' as PermissionName })
      .then((s) => {
        if (cancelled) return;
        status = s;
        onChange();
        s.addEventListener('change', onChange);
      })
      .catch(() => {
        // Permissions API not supported — leave 'unknown'.
      });
    return () => {
      cancelled = true;
      if (status) {
        try {
          status.removeEventListener('change', onChange);
        } catch {
          /* ignore */
        }
      }
    };
  }, []);

  return { state };
}

/**
 * When mic permission is in the 'prompt' state and `enabled` is true,
 * proactively trigger the browser's mic permission UI on the first user
 * gesture (pointerdown / keydown). Without this, Chrome / Edge silently sit
 * in 'prompt' forever and SpeechRecognition just errors with 'not-allowed'
 * before the user ever sees a prompt. After the user clicks Allow, the
 * Permissions API onchange flips state to 'granted' and the listener engages.
 *
 * Pass the current mic state from useMicPermission() as `micState`.
 */
export function useMicPromptOnGesture(
  enabled: boolean,
  micState: MicPermissionState,
): void {
  useEffect(() => {
    if (!enabled) return;
    if (micState !== 'prompt') return;
    if (typeof navigator === 'undefined' || !navigator.mediaDevices) return;
    let cancelled = false;
    const triggerPrompt = async () => {
      window.removeEventListener('pointerdown', triggerPrompt);
      window.removeEventListener('keydown', triggerPrompt);
      try {
        const stream = await navigator.mediaDevices.getUserMedia({
          audio: true,
        });
        // We don't need this stream; SpeechRecognition opens its own.
        // Releasing immediately keeps the mic indicator off until the
        // listener actually needs it.
        if (!cancelled) stream.getTracks().forEach((t) => t.stop());
      } catch {
        // User denied — Permissions API onchange will flip to 'denied'.
      }
    };
    window.addEventListener('pointerdown', triggerPrompt, { once: true });
    window.addEventListener('keydown', triggerPrompt, { once: true });
    return () => {
      cancelled = true;
      window.removeEventListener('pointerdown', triggerPrompt);
      window.removeEventListener('keydown', triggerPrompt);
    };
  }, [enabled, micState]);
}
