// Renders the queued proactive elaborations from Claude-CLI.
// Voice-first flow: every 'proposed' elaboration is auto-accepted on
// arrival. The banner shows "Elaborating…" while the worker resolves,
// then displays + speaks the claude_answer when it arrives. No
// "Yes please / Not now" gating — Jarvis just speaks when ready.

import { useEffect, useRef } from 'react';
import { Sparkles, X } from 'lucide-react';
import { useAppStore } from '../lib/store';
import { acceptElaboration } from '../lib/elaborations';
import { speak as ttsSpeak, isSupported as ttsSupported } from '../lib/tts';

export function ElaborationBanner() {
  const proposals = useAppStore((s) => s.proposedElaborations);
  const speechEnabled = useAppStore((s) => s.settings.speechEnabled);
  const removeElaboration = useAppStore((s) => s.removeElaboration);
  const autoAcceptedRef = useRef<Set<string>>(new Set());
  const resolvedAnnouncedRef = useRef<Set<string>>(new Set());

  // Voice-first behaviour: every elaboration that lands in 'proposed'
  // state is auto-accepted. We don't ask the user to click "Yes, please"
  // — we just kick off the acceptance immediately so the actual
  // claude_answer arrives and gets spoken without friction. The
  // banner still shows the question excerpt while we wait, and the
  // user can still dismiss the whole thing if they don't want it.
  useEffect(() => {
    for (const p of proposals) {
      if (p.ui_state === 'proposed' && !autoAcceptedRef.current.has(p.id)) {
        autoAcceptedRef.current.add(p.id);
        // Optimistically transition local state to 'accepting' so the
        // UI shows the elaborating indicator while the network call
        // and the worker pipeline run.
        useAppStore.setState((s) => ({
          proposedElaborations: s.proposedElaborations.map((e) =>
            e.id === p.id ? { ...e, ui_state: 'accepting' } : e,
          ),
        }));
        acceptElaboration(p.id).catch((exc) => {
          console.warn('[elaborations] auto-accept failed', exc);
        });
      }
    }
  }, [proposals]);

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
            style={{ color: 'var(--color-accent)', marginTop: 2, flexShrink: 0 }}
          />
          <div style={{ flex: 1, minWidth: 0 }}>
            {p.ui_state === 'resolved' && p.claude_answer ? (
              <>
                <div
                  style={{
                    fontSize: 11,
                    color: 'var(--color-text-tertiary)',
                    marginBottom: 4,
                  }}
                >
                  Claude elaborates on: "{p.original_question_excerpt}"
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
            ) : (
              <>
                <div
                  style={{
                    fontSize: 11,
                    color: 'var(--color-text-tertiary)',
                    marginBottom: 4,
                  }}
                >
                  Claude elaborates on: "{p.original_question_excerpt}"
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
