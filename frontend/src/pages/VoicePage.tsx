import { useState, useCallback, useEffect, useRef } from 'react';
import {
  LiveKitRoom,
  RoomAudioRenderer,
  StartAudio,
  BarVisualizer,
  VoiceAssistantControlBar,
  TrackToggle,
  VideoTrack,
  useVoiceAssistant,
  useTranscriptions,
  useLocalParticipant,
  useTracks,
  useDataChannel,
} from '@livekit/components-react';
import { Track, MediaDeviceFailure } from 'livekit-client';
import { motion } from 'motion/react';
import {
  useVoiceListener,
  isSpeechRecognitionSupported,
} from '../lib/useVoiceListener';
import { classifyIntent } from '../lib/voice_intents';
import {
  useMicPermission,
  useMicPromptOnGesture,
} from '../lib/useMicPermission';

interface ConnectionDetails {
  serverUrl: string;
  roomName: string;
  participantName: string;
  participantToken: string;
}

// Defense-in-depth only — the real gate is OpenJarvis's server-side HTTP
// Basic Auth, carried automatically by the same-origin fetch. Baked at
// build time via the Dockerfile ARG VITE_VOICE_SECRET.
const VOICE_SECRET: string =
  ((import.meta as unknown as { env?: Record<string, string> }).env
    ?.VITE_VOICE_SECRET as string) || '';

// Per-state look for the "alive" core.
const CORE_BY_STATE: Record<
  string,
  { glow: string; scale: number[]; dur: number; label: string }
> = {
  listening: { glow: '#1fd5f9', scale: [1, 1.06, 1], dur: 2.4, label: 'Listening' },
  thinking: { glow: '#a855f7', scale: [1, 1.12, 1], dur: 1.1, label: 'Thinking' },
  speaking: { glow: '#22e0a1', scale: [1, 1.18, 1], dur: 0.7, label: 'Speaking' },
  initializing: { glow: '#64748b', scale: [1, 1.03, 1], dur: 3, label: 'Waking up' },
  disconnected: { glow: '#64748b', scale: [1, 1.02, 1], dur: 3.5, label: 'Connecting' },
};

function AliveCore() {
  const { state, audioTrack } = useVoiceAssistant();
  const look = CORE_BY_STATE[state] ?? CORE_BY_STATE.disconnected;

  return (
    <div className="relative flex h-72 w-72 items-center justify-center">
      {/* outer breathing halo */}
      <motion.div
        className="absolute inset-0 rounded-full"
        style={{ background: `radial-gradient(circle, ${look.glow}33 0%, transparent 70%)` }}
        animate={{ scale: look.scale, opacity: [0.5, 0.85, 0.5] }}
        transition={{ duration: look.dur, repeat: Infinity, ease: 'easeInOut' }}
      />
      {/* inner core ring */}
      <motion.div
        className="absolute h-44 w-44 rounded-full border"
        style={{ borderColor: `${look.glow}aa`, boxShadow: `0 0 60px ${look.glow}66` }}
        animate={{ scale: look.scale, rotate: state === 'thinking' ? 360 : 0 }}
        transition={{
          scale: { duration: look.dur, repeat: Infinity, ease: 'easeInOut' },
          rotate: { duration: 6, repeat: Infinity, ease: 'linear' },
        }}
      />
      {/* audio-reactive bars at the centre */}
      <BarVisualizer
        state={state}
        trackRef={audioTrack}
        barCount={5}
        className="h-24 w-40"
        style={{ color: look.glow }}
      />
      <div
        className="absolute -bottom-2 text-xs uppercase tracking-[0.3em]"
        style={{ color: 'var(--color-text-muted)' }}
      >
        Jarvis · {look.label}
      </div>
    </div>
  );
}

function Transcript() {
  const segments = useTranscriptions();
  const { localParticipant } = useLocalParticipant();
  const endRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [segments.length]);

  if (segments.length === 0) {
    return (
      <p className="text-center text-xs" style={{ color: 'var(--color-text-muted)' }}>
        Say something — the transcript will appear here.
      </p>
    );
  }

  return (
    <div className="flex flex-col gap-3">
      {segments.map((seg, i) => {
        const mine = seg.participantInfo?.identity === localParticipant?.identity;
        return (
          <div
            key={`${seg.participantInfo?.identity ?? 'agent'}-${i}`}
            className={`flex ${mine ? 'justify-end' : 'justify-start'}`}
          >
            <div
              className="max-w-[80%] rounded-2xl px-4 py-2 text-sm"
              style={{
                background: mine
                  ? 'var(--color-accent, #1fd5f9)'
                  : 'var(--color-bg-secondary, #1e293b)',
                color: mine ? '#04121a' : 'var(--color-text)',
              }}
            >
              <div
                className="mb-0.5 text-[10px] uppercase tracking-wider opacity-70"
              >
                {mine ? 'You' : 'Jarvis'}
              </div>
              {seg.text}
            </div>
          </div>
        );
      })}
      <div ref={endRef} />
    </div>
  );
}

function SelfView() {
  const tracks = useTracks([Track.Source.Camera]);
  const cam = tracks.find((t) => t.participant?.isLocal && t.publication);
  if (!cam) return null;
  return (
    <div
      className="absolute bottom-4 right-4 h-28 w-40 overflow-hidden rounded-lg border"
      style={{ borderColor: 'var(--color-accent, #1fd5f9)' }}
    >
      <VideoTrack trackRef={cam} className="h-full w-full object-cover" />
    </div>
  );
}

// Receives structured UI commands from the worker over the LiveKit data
// channel (topic "ui-command"). Today: voice-controlled camera toggle —
// the worker parses the camera on/off intent server-side and sends a
// structured command, so we never parse transcription text here.
function UiCommandBridge() {
  const { localParticipant } = useLocalParticipant();
  useDataChannel('ui-command', (msg) => {
    try {
      const data = JSON.parse(new TextDecoder().decode(msg.payload));
      if (data?.type === 'camera') {
        void localParticipant.setCameraEnabled(Boolean(data.enabled));
      }
    } catch {
      // Malformed / unknown command — ignore.
    }
  });
  return null;
}

function VoiceSession({ onEnd }: { onEnd: () => void }) {
  return (
    <div className="relative flex h-full w-full flex-col">
      {/* Alive core */}
      <div className="flex flex-1 items-center justify-center">
        <AliveCore />
      </div>

      {/* Transcript */}
      <div
        className="mx-auto mb-4 max-h-52 w-full max-w-2xl overflow-y-auto rounded-xl p-4"
        style={{ background: 'var(--color-surface, #0b1220)' }}
      >
        <Transcript />
      </div>

      {/* Controls */}
      <div className="mb-4 flex items-center justify-center gap-3">
        <VoiceAssistantControlBar />
        <TrackToggle
          source={Track.Source.Camera}
          showIcon
          className="rounded-full px-4 py-2 text-sm font-medium"
          style={{ background: 'var(--color-bg-secondary, #1e293b)', color: 'var(--color-text)' }}
        >
          Camera
        </TrackToggle>
        <StartAudio
          label="🔊 Enable audio"
          className="rounded-full px-4 py-2 text-sm font-medium"
          style={{ background: 'var(--color-accent, #1fd5f9)', color: '#04121a' }}
        />
        <button
          onClick={onEnd}
          className="rounded-full px-5 py-2 text-sm font-medium transition-opacity hover:opacity-80"
          style={{ background: 'var(--color-error, #e5484d)', color: 'white' }}
        >
          End session
        </button>
      </div>

      <RoomAudioRenderer />
      <SelfView />
      <UiCommandBridge />
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
        throw new Error(`Token request failed (HTTP ${res.status}).`);
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
    if (failure === MediaDeviceFailure.PermissionDenied) {
      setError(
        'Microphone/camera blocked. Allow it via the icon in your browser address bar, then try again.'
      );
    } else if (failure === MediaDeviceFailure.NotFound) {
      setError('No microphone found. Connect one and try again.');
    } else {
      setError('Could not access the microphone/camera. Check browser permissions.');
    }
  }, []);

  // ── Hands-free wake-word ────────────────────────────────────────────
  // While disconnected, run continuous browser SpeechRecognition. Saying
  // "Jarvis" / "Hey Jarvis" / "wake up" triggers the exact same start()
  // path as the manual button. The listener auto-tears-down the moment we
  // begin connecting (enabled → false), so it releases the mic before
  // LiveKitRoom grabs it — no contention.
  const listening = !conn && !connecting;
  const { state: micPermissionState } = useMicPermission();
  useMicPromptOnGesture(listening, micPermissionState);
  useVoiceListener({
    enabled: listening,
    micPermissionState,
    onTranscript: (t) => {
      if (connecting || conn) return; // re-entrancy guard
      if (classifyIntent(t) === 'wake') start();
    },
    onError: (code) => {
      if (code === 'not-allowed') {
        setError(
          "Microphone blocked. Allow it via the address-bar icon to use 'Hey Jarvis'."
        );
      }
    },
  });
  const wakeArmed =
    listening &&
    isSpeechRecognitionSupported() &&
    micPermissionState !== 'denied';

  if (conn) {
    return (
      <div className="h-full w-full p-6">
        <LiveKitRoom
          serverUrl={conn.serverUrl}
          token={conn.participantToken}
          connect={true}
          audio={true}
          video={false}
          onDisconnected={handleEnd}
          onError={(e) => setError(e.message)}
          onMediaDeviceFailure={handleMediaFailure}
          className="h-full w-full"
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

      {wakeArmed && (
        <p
          className="text-xs"
          style={{ color: 'var(--color-text-muted)' }}
        >
          🎙️ Listening for “Hey Jarvis”…
        </p>
      )}
    </div>
  );
}
