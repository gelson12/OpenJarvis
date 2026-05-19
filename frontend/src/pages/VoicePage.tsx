import { useState, useCallback } from 'react';
import {
  LiveKitRoom,
  RoomAudioRenderer,
  StartAudio,
  BarVisualizer,
  VoiceAssistantControlBar,
  useVoiceAssistant,
} from '@livekit/components-react';
import { MediaDeviceFailure } from 'livekit-client';

interface ConnectionDetails {
  serverUrl: string;
  roomName: string;
  participantName: string;
  participantToken: string;
}

// Defense-in-depth only — the real gate is OpenJarvis's server-side HTTP
// Basic Auth, which the same-origin fetch carries automatically. Baked at
// build time via the Dockerfile ARG VITE_VOICE_SECRET.
const VOICE_SECRET: string =
  ((import.meta as unknown as { env?: Record<string, string> }).env
    ?.VITE_VOICE_SECRET as string) || '';

function VoiceSession({ onEnd }: { onEnd: () => void }) {
  const { state, audioTrack } = useVoiceAssistant();

  return (
    <div className="flex flex-col items-center gap-8">
      <div
        className="text-sm uppercase tracking-widest"
        style={{ color: 'var(--color-text-muted)' }}
      >
        {state === 'disconnected' ? 'Connecting…' : `Jarvis · ${state}`}
      </div>

      <BarVisualizer
        state={state}
        trackRef={audioTrack}
        barCount={7}
        className="h-40 w-72"
        style={{ color: 'var(--color-accent, #1fd5f9)' }}
      />

      {/* Plays the agent's audio track */}
      <RoomAudioRenderer />

      {/* Only renders when the browser autoplay policy blocks playback;
          hides itself once the user taps and audio is unblocked. This is
          the fix for "browser is blocking the audio". */}
      <StartAudio
        label="🔊 Tap to enable Jarvis audio"
        className="rounded-full px-6 py-2 text-sm font-medium"
        style={{ background: 'var(--color-accent, #1fd5f9)', color: '#04121a' }}
      />

      <VoiceAssistantControlBar />

      <button
        onClick={onEnd}
        className="rounded-full px-6 py-2 text-sm font-medium transition-opacity hover:opacity-80"
        style={{ background: 'var(--color-error, #e5484d)', color: 'white' }}
      >
        End session
      </button>
    </div>
  );
}

export function VoicePage() {
  const [conn, setConn] = useState<ConnectionDetails | null>(null);
  const [connecting, setConnecting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const start = useCallback(async () => {
    setConnecting(true);
    setError(null);
    try {
      const res = await fetch('/v1/livekit/token', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-Voice-Secret': VOICE_SECRET,
        },
      });
      if (!res.ok) {
        const detail =
          res.status === 403
            ? 'Voice secret mismatch (set VITE_VOICE_SECRET and LIVEKIT_TOKEN_SHARED_SECRET to the same value, then redeploy).'
            : `Token request failed (HTTP ${res.status}).`;
        throw new Error(detail);
      }
      setConn((await res.json()) as ConnectionDetails);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to connect');
    } finally {
      setConnecting(false);
    }
  }, []);

  const handleEnd = useCallback(() => setConn(null), []);

  const handleMediaFailure = useCallback((failure?: MediaDeviceFailure) => {
    setConn(null);
    if (failure === MediaDeviceFailure.PermissionDenied) {
      setError(
        'Microphone blocked. Click the camera/mic icon in your browser address bar, allow the microphone, then try again.'
      );
    } else if (failure === MediaDeviceFailure.NotFound) {
      setError('No microphone found. Connect a mic and try again.');
    } else {
      setError('Could not access the microphone. Check browser permissions.');
    }
  }, []);

  if (conn) {
    return (
      <div className="flex h-full w-full items-center justify-center p-8">
        <LiveKitRoom
          serverUrl={conn.serverUrl}
          token={conn.participantToken}
          connect={true}
          audio={true}
          video={false}
          onDisconnected={handleEnd}
          onError={(e) => setError(e.message)}
          onMediaDeviceFailure={handleMediaFailure}
          className="flex items-center justify-center"
        >
          <VoiceSession onEnd={handleEnd} />
        </LiveKitRoom>
      </div>
    );
  }

  return (
    <div className="flex h-full w-full flex-col items-center justify-center gap-5 p-8">
      <h1 className="text-2xl font-semibold" style={{ color: 'var(--color-text)' }}>
        Talk to Jarvis
      </h1>
      <p
        className="max-w-md text-center text-sm"
        style={{ color: 'var(--color-text-muted)' }}
      >
        Real-time voice, powered by LiveKit and the OpenJarvis agent.
      </p>

      {error && (
        <div
          className="max-w-md rounded-md px-4 py-2 text-center text-sm"
          style={{ color: 'var(--color-error, #e5484d)' }}
        >
          {error}
        </div>
      )}

      <button
        onClick={start}
        disabled={connecting}
        className="rounded-full px-8 py-3 text-sm font-medium transition-opacity hover:opacity-90 disabled:opacity-50"
        style={{ background: 'var(--color-accent, #1fd5f9)', color: '#04121a' }}
      >
        {connecting ? 'Connecting…' : 'Talk to Jarvis'}
      </button>
    </div>
  );
}
