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
import { NeuralOrb } from '../components/NeuralOrb';

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
  { glow: string; label: string; signal: string }
> = {
  listening: { glow: '#1fd5f9', label: 'Listening', signal: 'NOMINAL' },
  thinking: { glow: '#aa6efa', label: 'Thinking', signal: 'PROCESSING' },
  speaking: { glow: '#22e0a1', label: 'Speaking', signal: 'TRANSMIT' },
  initializing: { glow: '#8896b6', label: 'Waking up', signal: 'BOOT' },
  disconnected: { glow: '#6c788e', label: 'Connecting', signal: 'LINK…' },
};

/** Live HH:MM:SS clock for the HUD. */
function useClock() {
  const [t, setT] = useState(() => new Date());
  useEffect(() => {
    const id = setInterval(() => setT(new Date()), 1000);
    return () => clearInterval(id);
  }, []);
  return t.toTimeString().slice(0, 8);
}

/** A single label / value row in a HUD column. */
function HudRow({ label, value, glow }: { label: string; value: string; glow?: string }) {
  return (
    <div className="flex flex-col gap-0.5">
      <span className="hud-label" style={{ fontSize: '0.5625rem', letterSpacing: '0.22em' }}>
        {label}
      </span>
      <span
        className="hud-mono text-sm font-semibold"
        style={{ color: glow ?? 'var(--color-text)' }}
      >
        {value}
      </span>
    </div>
  );
}

/** L-shaped brackets pinned to the four corners of the scene. */
function CornerFrames({ glow }: { glow: string }) {
  const corners = [
    'top-3 left-3 border-l border-t',
    'top-3 right-3 border-r border-t',
    'bottom-3 left-3 border-l border-b',
    'bottom-3 right-3 border-r border-b',
  ];
  return (
    <>
      {corners.map((pos) => (
        <span
          key={pos}
          aria-hidden="true"
          className={`pointer-events-none absolute h-6 w-6 ${pos}`}
          style={{ borderColor: `${glow}66` }}
        />
      ))}
    </>
  );
}

function AliveCore() {
  const { state, audioTrack } = useVoiceAssistant();
  const look = CORE_BY_STATE[state] ?? CORE_BY_STATE.disconnected;
  // Reach into the LiveKit track for the raw MediaStreamTrack — the orb
  // uses it to react to Jarvis's voice in real time.
  const mediaTrack =
    (audioTrack?.publication?.track as { mediaStreamTrack?: MediaStreamTrack } | undefined)
      ?.mediaStreamTrack ?? null;

  return (
    <div className="relative flex h-[clamp(280px,42vh,440px)] w-[clamp(280px,42vh,440px)] items-center justify-center">
      {/* soft bloom behind the canvas */}
      <motion.div
        className="absolute inset-0 rounded-full blur-2xl"
        style={{ background: `radial-gradient(circle, ${look.glow}33 0%, transparent 65%)` }}
        animate={{ opacity: [0.45, 0.8, 0.45], scale: [0.95, 1.05, 0.95] }}
        transition={{ duration: 4, repeat: Infinity, ease: 'easeInOut' }}
      />
      <NeuralOrb state={state} track={mediaTrack} className="absolute inset-0 h-full w-full" />

      {/* audio-reactive bar strip + status label */}
      <div className="absolute -bottom-3 flex flex-col items-center gap-2">
        <BarVisualizer
          state={state}
          trackRef={audioTrack}
          barCount={7}
          className="h-6 w-28"
          style={{ color: look.glow }}
        />
        <div
          className="hud-mono flex items-center gap-2 text-xs uppercase tracking-[0.34em]"
          style={{ color: look.glow }}
        >
          <span className="hud-heartbeat" style={{ background: look.glow }} />
          Jarvis · {look.label}
        </div>
      </div>
    </div>
  );
}

/** Floating telemetry columns flanking the orb. */
function VoiceHud() {
  const { state } = useVoiceAssistant();
  const segments = useTranscriptions();
  const look = CORE_BY_STATE[state] ?? CORE_BY_STATE.disconnected;
  const clock = useClock();

  return (
    <>
      {/* top banner */}
      <div className="absolute left-5 top-5 flex items-center gap-2">
        <span className="hud-reticle" />
        <div className="flex flex-col">
          <span className="hud-mono text-xs font-semibold tracking-[0.3em]" style={{ color: 'var(--color-text)' }}>
            OPENJARVIS
          </span>
          <span className="hud-label" style={{ fontSize: '0.5625rem', letterSpacing: '0.24em' }}>
            NEURAL CORE v2
          </span>
        </div>
      </div>
      <div className="absolute right-5 top-5 hidden flex-col items-end gap-0.5 sm:flex">
        <span className="hud-mono text-xs" style={{ color: look.glow }}>
          {clock}
        </span>
        <span className="hud-label" style={{ fontSize: '0.5625rem', letterSpacing: '0.24em' }}>
          SYS TIME
        </span>
      </div>

      {/* left column */}
      <motion.div
        className="absolute left-5 top-1/2 hidden -translate-y-1/2 flex-col gap-5 lg:flex"
        initial={{ opacity: 0, x: -12 }}
        animate={{ opacity: 1, x: 0 }}
        transition={{ duration: 0.5 }}
      >
        <HudRow label="STATE" value={look.label.toUpperCase()} glow={look.glow} />
        <HudRow label="SIGNAL" value={look.signal} glow={look.glow} />
        <HudRow label="LINK" value="SECURE" />
      </motion.div>

      {/* right column */}
      <motion.div
        className="absolute right-5 top-1/2 hidden -translate-y-1/2 flex-col items-end gap-5 lg:flex"
        initial={{ opacity: 0, x: 12 }}
        animate={{ opacity: 1, x: 0 }}
        transition={{ duration: 0.5 }}
      >
        <HudRow label="EXCHANGES" value={String(segments.length).padStart(3, '0')} />
        <HudRow label="NODES" value="150" />
        <HudRow label="MODE" value="VOICE" glow={look.glow} />
      </motion.div>
    </>
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
      <p className="hud-mono text-center text-xs" style={{ color: 'var(--color-text-tertiary)' }}>
        awaiting voice input<span className="hud-caret" />
      </p>
    );
  }

  return (
    <div className="flex flex-col gap-3">
      {segments.map((seg, i) => {
        const mine = seg.participantInfo?.identity === localParticipant?.identity;
        return (
          <motion.div
            key={`${seg.participantInfo?.identity ?? 'agent'}-${i}`}
            initial={{ opacity: 0, y: 6 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 0.25 }}
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
              <div className="mb-0.5 text-[10px] uppercase tracking-wider opacity-70">
                {mine ? 'You' : 'Jarvis'}
              </div>
              {seg.text}
            </div>
          </motion.div>
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
  const { state } = useVoiceAssistant();
  const look = CORE_BY_STATE[state] ?? CORE_BY_STATE.disconnected;

  return (
    <div className="relative flex h-full w-full flex-col overflow-hidden">
      <CornerFrames glow={look.glow} />
      <VoiceHud />

      {/* drifting scan beam */}
      <motion.div
        aria-hidden="true"
        className="pointer-events-none absolute inset-x-0 h-px"
        style={{ background: `linear-gradient(90deg, transparent, ${look.glow}55, transparent)` }}
        animate={{ top: ['12%', '88%', '12%'] }}
        transition={{ duration: 11, repeat: Infinity, ease: 'easeInOut' }}
      />

      {/* Alive core */}
      <div className="flex flex-1 items-center justify-center">
        <AliveCore />
      </div>

      {/* Transcript */}
      <div className="hud-panel mx-auto mb-4 max-h-48 w-full max-w-2xl overflow-y-auto p-4">
        <Transcript />
      </div>

      {/* Controls */}
      <div className="mb-4 flex flex-wrap items-center justify-center gap-3">
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

  // Auto-connect on page load — no button click. The browser asks for
  // mic permission once (LiveKit WebRTC init); after the first Allow it
  // is automatic. Wake/sleep ("Hey Jarvis" / "goodbye Jarvis") is gated
  // server-side in the worker, not in the browser.
  const startedRef = useRef(false);
  useEffect(() => {
    if (startedRef.current || conn || connecting) return;
    startedRef.current = true;
    void start();
  }, [conn, connecting, start]);

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

  // Pre-connection screen — give it the same alive feel with a static orb.
  return (
    <div className="relative flex h-full w-full flex-col items-center justify-center gap-6 overflow-hidden p-8">
      <div className="relative flex h-64 w-64 items-center justify-center">
        <motion.div
          className="absolute inset-0 rounded-full blur-2xl"
          style={{ background: 'radial-gradient(circle, #6c788e44 0%, transparent 65%)' }}
          animate={{ opacity: [0.4, 0.7, 0.4] }}
          transition={{ duration: 3.5, repeat: Infinity, ease: 'easeInOut' }}
        />
        <NeuralOrb
          state={connecting ? 'initializing' : 'disconnected'}
          className="absolute inset-0 h-full w-full"
        />
      </div>

      <h1
        className="hud-mono text-xl font-semibold tracking-[0.2em]"
        style={{ color: 'var(--color-text)' }}
      >
        TALK TO JARVIS
      </h1>
      <p className="max-w-md text-center text-sm" style={{ color: 'var(--color-text-secondary)' }}>
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

      <p className="hud-label" style={{ fontSize: '0.625rem', letterSpacing: '0.2em' }}>
        CONNECTING AUTOMATICALLY — THEN SAY “HEY JARVIS”
      </p>
    </div>
  );
}
