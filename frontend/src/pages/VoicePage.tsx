import { useState, useCallback } from 'react';
import {
  LiveKitRoom,
  RoomAudioRenderer,
  BarVisualizer,
  VoiceAssistantControlBar,
  useVoiceAssistant,
} from '@livekit/components-react';

interface ConnectionDetails {
  serverUrl: string;
  roomName: string;
  participantName: string;
  participantToken: string;
}

// The shared secret is baked at build time. It is defense-in-depth only —
// the real gate is OpenJarvis's server-side HTTP Basic Auth, which the
// same-origin fetch carries automatically via the browser credential cache.
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

      <RoomAudioRenderer />
      <VoiceAssistantControlBar />

      <button
        onClick={onEnd}
        className="rounded-full px-6 py-2 text-sm font-medium transition-opacity hover:opacity-80"
        style={{
          background: 'var(--color-error, #e5484d)',
          color: 'white',
        }}
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
        throw new Error(`Token request failed (HTTP ${res.status})`);
      }
      setConn((await res.json()) as ConnectionDetails);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to connect');
    } finally {
      setConnecting(false);
    }
  }, []);

  const handleEnd = useCallback(() => setConn(null), []);

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
          className="flex items-center justify-center"
        >
          <VoiceSession onEnd={handleEnd} />
        </LiveKitRoom>
      </div>
    );
  }

  return (
    <div className="flex h-full w-full flex-col items-center justify-center gap-5 p-8">
      <h1
        className="text-2xl font-semibold"
        style={{ color: 'var(--color-text)' }}
      >
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
          className="rounded-md px-4 py-2 text-sm"
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
