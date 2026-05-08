// Renders the queued proactive elaborations from Claude-CLI.
//
// Voice-first flow (Jarvis tone):
//   1. Elaboration arrives in 'proposed' state.
//   2. We pick a polite line at random ("If I may, sir — would you like
//      me to expand on '...'?") and SPEAK it.
//   3. Banner shows the question excerpt + a small "Listening..." badge
//      AND a discreet pair of fallback buttons (Yes, please / Not now)
//      for users who can't reply by voice in the moment.
//   4. While listening, every voice transcript runs through
//      classifyIntent(). On 'affirmative' we accept (claude_answer
//      arrives, gets spoken). On 'negative' or 'stop' we dismiss
//      silently. After 30s with no response, we auto-dismiss so we
//      don't leave a stale prompt sitting forever.
//
// The fallback buttons exist because the voice listener might be:
//   - Off (user disabled mic, on mobile in a meeting, etc.)
//   - Misheard the user's response
//   - Unable to start because of permissions
// They're styled small and unobtrusive so the voice flow feels like
// the primary path, click is the safety net.

import { useEffect, useRef, useState } from 'react';
import { Sparkles, Mic, X } from 'lucide-react';
import { useAppStore } from '../lib/store';
import { acceptElaboration, dismissElaboration } from '../lib/elaborations';
import {
  speak as ttsSpeak,
  speakAsync as ttsSpeakAsync,
  waitUntilQuiet as ttsWaitUntilQuiet,
  isSupported as ttsSupported,
} from '../lib/tts';
import { classifyIntent } from '../lib/voice_intents';
import {
  isSpeechRecognitionSupported,
  useVoiceListener,
} from '../lib/useVoiceListener';

const POLITE_PROMPTS = [
  (excerpt: string) =>
    `If I may, sir — would you like me to expand on "${excerpt}"?`,
  (excerpt: string) =>
    `Sir, may I offer some additional context on "${excerpt}"?`,
  (excerpt: string) =>
    `Pardon me, sir — I have a thought on "${excerpt}". Shall I elaborate?`,
  (excerpt: string) =>
    `Sir, regarding "${excerpt}" — may I add a thought?`,
];

const LISTEN_TIMEOUT_MS = 30_000;

function pickPrompt(excerpt: string): string {
  const trimmed =
    excerpt.length > 60 ? `${excerpt.slice(0, 60)}…` : excerpt;
  const fn =
    POLITE_PROMPTS[Math.floor(Math.random() * POLITE_PROMPTS.length)];
  return fn(trimmed);
}

export function ElaborationBanner() {
  const proposals = useAppStore((s) => s.proposedElaborations);
  const speechEnabled = useAppStore((s) => s.settings.speechEnabled);
  const removeElaboration = useAppStore((s) => s.removeElaboration);

  // Track which proposals we've already announced (spoken the polite
  // prompt for) and which we've already spoken the resolved answer of.
  const announcedRef = useRef<Set<string>>(new Set());
  const resolvedAnnouncedRef = useRef<Set<string>>(new Set());
  // Tracks WHICH proposal is ready to listen on (i.e. polite prompt
  // has finished speaking). Listener stays disabled until then so
  // Jarvis's own voice doesn't echo into the mic and trip the
  // recognizer.
  const [readyToListenFor, setReadyToListenFor] = useState<string | null>(null);
  // Per-proposal auto-dismiss timers so we clear them if the user
  // responds before 30s.
  const timeoutsRef = useRef<Map<string, ReturnType<typeof setTimeout>>>(
    new Map(),
  );

  // The id of the proposal we're currently waiting for a yes/no on.
  // Only one at a time — if a second elaboration lands while we're
  // listening for the first, it queues silently until the first resolves.
  const pendingProposal = proposals.find((p) => p.ui_state === 'proposed');
  // Listener is only enabled AFTER the polite prompt finishes speaking
  // (readyToListenFor === pendingProposal.id). Without this gate, the
  // mic picks up Jarvis's own "may I elaborate?" voice as user input.
  const listenForProposal =
    pendingProposal &&
    speechEnabled &&
    isSpeechRecognitionSupported() &&
    readyToListenFor === pendingProposal.id
      ? pendingProposal.id
      : null;
  const [listening, setListening] = useState(false);

  // Speak the polite prompt once per proposal — chained so we don't
  // overlap the fast-path's TTS, and the listener doesn't enable until
  // the prompt finishes speaking.
  useEffect(() => {
    if (!speechEnabled || !ttsSupported()) return;
    for (const p of proposals) {
      if (p.ui_state === 'proposed' && !announcedRef.current.has(p.id)) {
        announcedRef.current.add(p.id);
        const prompt = pickPrompt(p.original_question_excerpt);
        // 1) Wait for fast-path TTS to drain
        // 2) Speak the polite prompt and wait for IT to finish
        // 3) Flip the listener-ready flag so useVoiceListener engages
        ttsWaitUntilQuiet(20_000)
          .then(() => ttsSpeakAsync(prompt))
          .then(() => {
            // Small grace pause so any tail-end audio/reverb doesn't
            // immediately get re-captured by the mic.
            setTimeout(() => setReadyToListenFor(p.id), 250);
          })
          .catch(() => {
            // Even on failure, fall through to listening so the user
            // isn't stuck without a way to respond. The click buttons
            // remain available regardless.
            setReadyToListenFor(p.id);
          });
      }
    }
  }, [proposals, speechEnabled]);

  // Clear the ready-to-listen flag whenever the proposal it points at
  // resolves or vanishes — otherwise a stale id stays in state.
  useEffect(() => {
    if (
      readyToListenFor !== null &&
      !proposals.some(
        (p) => p.id === readyToListenFor && p.ui_state === 'proposed',
      )
    ) {
      setReadyToListenFor(null);
    }
  }, [proposals, readyToListenFor]);

  // Auto-dismiss any proposal we've been listening to for 30s with no
  // response. Without this, an unanswered prompt sits forever.
  useEffect(() => {
    for (const p of proposals) {
      if (p.ui_state === 'proposed' && !timeoutsRef.current.has(p.id)) {
        const t = setTimeout(() => {
          dismissElaboration(p.id).catch(() => {});
          removeElaboration(p.id);
          timeoutsRef.current.delete(p.id);
        }, LISTEN_TIMEOUT_MS);
        timeoutsRef.current.set(p.id, t);
      }
      // Once a proposal has resolved or been dismissed, clear its timer.
      if (p.ui_state !== 'proposed') {
        const t = timeoutsRef.current.get(p.id);
        if (t) {
          clearTimeout(t);
          timeoutsRef.current.delete(p.id);
        }
      }
    }
    // Clean up timers for proposals that have been removed entirely.
    const liveIds = new Set(proposals.map((p) => p.id));
    for (const [id, t] of timeoutsRef.current) {
      if (!liveIds.has(id)) {
        clearTimeout(t);
        timeoutsRef.current.delete(id);
      }
    }
  }, [proposals, removeElaboration]);

  // Voice listener — fires only while a proposal is awaiting yes/no.
  useVoiceListener({
    enabled: listenForProposal !== null,
    onTranscript: (transcript) => {
      const intent = classifyIntent(transcript);
      const proposalId = listenForProposal;
      if (!proposalId) return;
      if (intent === 'affirmative') {
        // User said "yes / go ahead / please / etc."
        useAppStore.setState((s) => ({
          proposedElaborations: s.proposedElaborations.map((e) =>
            e.id === proposalId ? { ...e, ui_state: 'accepting' } : e,
          ),
        }));
        acceptElaboration(proposalId).catch(() => {});
      } else if (intent === 'negative' || intent === 'stop') {
        dismissElaboration(proposalId).catch(() => {});
        removeElaboration(proposalId);
      }
      // 'speech' / 'wake' falls through — those go to the always-on chat
      // listener (handled elsewhere); we don't act on them here.
    },
  });

  useEffect(() => {
    setListening(listenForProposal !== null);
  }, [listenForProposal]);

  // Speak the elaboration text once it resolves.
  useEffect(() => {
    if (!speechEnabled || !ttsSupported()) return;
    for (const p of proposals) {
      if (
        p.ui_state === 'resolved' &&
        p.claude_answer &&
        !resolvedAnnouncedRef.current.has(p.id)
      ) {
        resolvedAnnouncedRef.current.add(p.id);
        ttsSpeak(p.claude_answer);
      }
    }
  }, [proposals, speechEnabled]);

  if (!proposals.length) return null;

  const handleAccept = (id: string) => {
    useAppStore.setState((s) => ({
      proposedElaborations: s.proposedElaborations.map((e) =>
        e.id === id ? { ...e, ui_state: 'accepting' } : e,
      ),
    }));
    acceptElaboration(id).catch(() => {});
  };

  const handleDismiss = (id: string) => {
    dismissElaboration(id).catch(() => {});
    removeElaboration(id);
  };

  return (
    <div
      style={{
        display: 'flex',
        flexDirection: 'column',
        gap: 8,
        padding: '8px 16px',
        borderTop: '1px solid var(--color-border)',
        borderBottom: '1px solid var(--color-border)',
        background:
          'color-mix(in srgb, var(--color-accent) 6%, transparent)',
      }}
    >
      {proposals.map((p) => (
        <div
          key={p.id}
          style={{
            display: 'flex',
            alignItems: 'flex-start',
            gap: 12,
            padding: 12,
            borderRadius: 8,
            background: 'var(--color-surface)',
            border: '1px solid var(--color-border)',
          }}
        >
          <Sparkles
            size={18}
            style={{
              color: 'var(--color-accent)',
              marginTop: 2,
              flexShrink: 0,
            }}
          />
          <div style={{ flex: 1, minWidth: 0 }}>
            {p.ui_state === 'resolved' && p.claude_answer ? (
              // Resolved — show the deep answer.
              <>
                <div
                  style={{
                    fontSize: 11,
                    color: 'var(--color-text-tertiary)',
                    marginBottom: 4,
                  }}
                >
                  Claude on: "{p.original_question_excerpt}"
                </div>
                <div
                  style={{
                    fontSize: 14,
                    color: 'var(--color-text)',
                    whiteSpace: 'pre-wrap',
                  }}
                >
                  {p.claude_answer}
                </div>
              </>
            ) : p.ui_state === 'accepting' ? (
              // User said yes — waiting for Claude-CLI to finish.
              <>
                <div
                  style={{
                    fontSize: 11,
                    color: 'var(--color-text-tertiary)',
                    marginBottom: 4,
                  }}
                >
                  Elaborating on: "{p.original_question_excerpt}"
                </div>
                <div
                  style={{
                    fontSize: 13,
                    color: 'var(--color-text-tertiary)',
                    fontStyle: 'italic',
                  }}
                >
                  Elaborating…
                </div>
              </>
            ) : (
              // Proposed — polite question, voice listening, plus
              // small click-fallback buttons for non-voice contexts.
              <>
                <div
                  style={{
                    fontSize: 13,
                    color: 'var(--color-text)',
                    marginBottom: 8,
                  }}
                >
                  Sir, may I elaborate on{' '}
                  <em>"{p.original_question_excerpt}"</em>?
                </div>
                <div
                  style={{
                    display: 'flex',
                    alignItems: 'center',
                    gap: 8,
                    fontSize: 11,
                  }}
                >
                  {listening && p.id === listenForProposal && (
                    <span
                      style={{
                        display: 'inline-flex',
                        alignItems: 'center',
                        gap: 4,
                        color: 'var(--color-accent)',
                      }}
                    >
                      <Mic
                        size={11}
                        style={{ animation: 'pulse 1.4s ease-in-out infinite' }}
                      />
                      Listening… (say "yes" or "not now")
                    </span>
                  )}
                  <div style={{ display: 'flex', gap: 6, marginLeft: 'auto' }}>
                    <button
                      onClick={() => handleAccept(p.id)}
                      style={{
                        padding: '3px 10px',
                        borderRadius: 4,
                        background: 'var(--color-accent)',
                        color: 'var(--color-on-accent)',
                        border: 'none',
                        fontSize: 11,
                        cursor: 'pointer',
                      }}
                    >
                      Yes, please
                    </button>
                    <button
                      onClick={() => handleDismiss(p.id)}
                      style={{
                        padding: '3px 10px',
                        borderRadius: 4,
                        background: 'transparent',
                        color: 'var(--color-text-tertiary)',
                        border: '1px solid var(--color-border)',
                        fontSize: 11,
                        cursor: 'pointer',
                      }}
                    >
                      Not now
                    </button>
                  </div>
                </div>
              </>
            )}
          </div>
          {p.ui_state === 'resolved' && (
            <button
              onClick={() => removeElaboration(p.id)}
              style={{
                background: 'transparent',
                border: 'none',
                color: 'var(--color-text-tertiary)',
                cursor: 'pointer',
                padding: 4,
              }}
              aria-label="Dismiss"
            >
              <X size={14} />
            </button>
          )}
        </div>
      ))}
    </div>
  );
}
