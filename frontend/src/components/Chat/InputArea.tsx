import { useState, useRef, useCallback, useEffect } from 'react';
import { Send, Square, Paperclip, Mic } from 'lucide-react';
import { useAppStore, generateId } from '../../lib/store';
import { streamChat } from '../../lib/sse';
import { fetchSavings, getBase } from '../../lib/api';
import { MicButton } from './MicButton';
import { useSpeech } from '../../hooks/useSpeech';
import * as tts from '../../lib/tts';
import {
  isSpeechRecognitionSupported,
  useVoiceListener,
} from '../../lib/useVoiceListener';
import { useBargeInVAD } from '../../lib/useBargeInVAD';
import {
  classifyIntent,
  endsWithSendTrigger,
  isAbort,
  stripCloseTrigger,
  stripWakeWord,
} from '../../lib/voice_intents';
import type { ChatMessage, ToolCallInfo, TokenUsage, MessageTelemetry } from '../../types';

export function InputArea() {
  const [input, setInput] = useState('');
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const abortRef = useRef<AbortController | null>(null);
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const activeId = useAppStore((s) => s.activeId);
  const selectedModel = useAppStore((s) => s.selectedModel);
  const streamState = useAppStore((s) => s.streamState);
  const messages = useAppStore((s) => s.messages);
  const speechEnabled = useAppStore((s) => s.settings.speechEnabled);
  const alwaysListenEnabled = useAppStore(
    (s) => s.settings.alwaysListenEnabled,
  );
  const handsFreeMode = useAppStore((s) => s.settings.handsFreeMode);
  const bargeInEnabled = useAppStore((s) => s.settings.bargeInEnabled);
  const maxTokens = useAppStore((s) => s.settings.maxTokens);
  const temperature = useAppStore((s) => s.settings.temperature);
  const createConversation = useAppStore((s) => s.createConversation);
  const addMessage = useAppStore((s) => s.addMessage);
  const updateLastAssistant = useAppStore((s) => s.updateLastAssistant);
  const setStreamState = useAppStore((s) => s.setStreamState);
  const resetStream = useAppStore((s) => s.resetStream);
  const modelLoading = useAppStore((s) => s.modelLoading);

  const { state: speechState, available: speechAvailable, startRecording, stopRecording } = useSpeech();

  // Abort in-flight stream when the user switches models mid-generation.
  // This prevents errors from trying to continue a stream with a stale model.
  const prevModelRef = useRef(selectedModel);
  useEffect(() => {
    if (prevModelRef.current !== selectedModel && streamState.isStreaming) {
      abortRef.current?.abort();
      if (timerRef.current) {
        clearInterval(timerRef.current);
        timerRef.current = null;
      }
      resetStream();
      abortRef.current = null;
    }
    prevModelRef.current = selectedModel;
  }, [selectedModel, streamState.isStreaming, resetStream]);

  const micDisabled = !speechEnabled || !speechAvailable || streamState.isStreaming;
  const micReason: 'not-enabled' | 'no-backend' | 'streaming' | undefined =
    !speechEnabled ? 'not-enabled'
    : !speechAvailable ? 'no-backend'
    : streamState.isStreaming ? 'streaming'
    : undefined;

  const handleMicClick = useCallback(async () => {
    if (speechState === 'recording') {
      try {
        const text = await stopRecording();
        if (text) {
          setInput((prev) => (prev ? prev + ' ' + text : text));
        }
      } catch {
        // Error is captured in useSpeech
      }
    } else {
      await startRecording();
    }
  }, [speechState, startRecording, stopRecording]);

  // Always-on dashboard listening — separate from the push-to-talk
  // useSpeech path above. This uses the browser's SpeechRecognition
  // (free, low-latency, no backend round-trip) so the agent can pick up
  // voice commands without the user clicking the mic. Yes/no responses
  // to ElaborationBanner prompts are handled inside that component;
  // here we only act on wake-word commands and substantive speech that
  // looks like a chat message.
  const isPendingElaboration = useAppStore(
    (s) => s.proposedElaborations.some((p) => p.ui_state === 'proposed'),
  );
  // The listener stays on continuously — even during streaming, even
  // before an ElaborationBanner appears. The user explicitly asked for
  // "always actively listening", so the only off-switches are:
  //   - alwaysListenEnabled toggle (Settings)
  //   - speechEnabled toggle (Settings)
  //   - missing browser SpeechRecognition support
  //   - an ElaborationBanner's own listener is active (single
  //     SpeechRecognition instance constraint — two concurrent
  //     listeners on the same page collide and silently fail).
  // Removing the `!isStreaming` predicate that previously turned the
  // listener off mid-response: there's no functional reason to mute
  // the mic while the model is streaming, and the echo guard in
  // useVoiceListener already drops transcripts captured while Jarvis
  // is audibly speaking.
  const continuousListenActive =
    alwaysListenEnabled &&
    speechEnabled &&
    isSpeechRecognitionSupported() &&
    !isPendingElaboration;
  const [voiceListenError, setVoiceListenError] = useState<string | null>(null);
  // Cache the browser's actual mic permission state via the Permissions
  // API. If permission is 'granted', we suppress the "Mic blocked" toast
  // regardless of any stale 'not-allowed' error from a transient
  // SpeechRecognition failure (e.g. a race with VAD's getUserMedia
  // grabbing the mic first on page load). Without this check, the toast
  // lies to the user after permission is granted but the listener
  // errored once.
  const [micPermissionState, setMicPermissionState] = useState<
    'granted' | 'denied' | 'prompt' | 'unknown'
  >('unknown');
  useEffect(() => {
    if (typeof navigator === 'undefined' || !('permissions' in navigator)) {
      return;
    }
    let cancelled = false;
    let status: PermissionStatus | null = null;
    const onChange = () => {
      if (cancelled || !status) return;
      setMicPermissionState(status.state as 'granted' | 'denied' | 'prompt');
      if (status.state === 'granted') {
        // Browser says we have mic; clear any stale "Mic blocked" toast.
        setVoiceListenError((prev) => (prev === 'not-allowed' ? null : prev));
      }
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
        // Permissions API not supported (older Safari) — leave 'unknown'.
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

  // Proactively trigger the browser's mic permission UI on first user
  // interaction when state is 'prompt'. Without this, Chrome / Edge
  // silently sit in 'prompt' state forever — VAD and SpeechRecognition
  // both error with 'not-allowed' before the user ever sees the prompt.
  // After the user clicks Allow, the Permissions API onchange fires and
  // micPermissionState flips to 'granted', which clears the toast and
  // (via the useBargeInVAD hook's userGestureSeenRef) starts VAD.
  useEffect(() => {
    if (micPermissionState !== 'prompt') return;
    if (typeof navigator === 'undefined' || !navigator.mediaDevices) return;
    if (!alwaysListenEnabled && !bargeInEnabled) return;
    let cancelled = false;
    const triggerPrompt = async () => {
      window.removeEventListener('pointerdown', triggerPrompt);
      window.removeEventListener('keydown', triggerPrompt);
      try {
        const stream = await navigator.mediaDevices.getUserMedia({
          audio: true,
        });
        // We don't need this stream; MicVAD and SpeechRecognition open
        // their own. Releasing immediately keeps the mic indicator off
        // until those consumers actually need it.
        if (!cancelled) stream.getTracks().forEach((t) => t.stop());
      } catch {
        // User denied — Permissions API onchange will flip to 'denied'
        // and the toast will appear correctly.
      }
    };
    window.addEventListener('pointerdown', triggerPrompt, { once: true });
    window.addEventListener('keydown', triggerPrompt, { once: true });
    return () => {
      cancelled = true;
      window.removeEventListener('pointerdown', triggerPrompt);
      window.removeEventListener('keydown', triggerPrompt);
    };
  }, [micPermissionState, alwaysListenEnabled, bargeInEnabled]);

  // Hands-free auto-submit timer. Cleared if the user says "wait /
  // cancel / scratch that" within ~1.5s, OR if a new transcript
  // arrives (means the user is still speaking).
  const autoSubmitTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const cancelAutoSubmit = useCallback(() => {
    if (autoSubmitTimerRef.current) {
      clearTimeout(autoSubmitTimerRef.current);
      autoSubmitTimerRef.current = null;
    }
  }, []);
  const [pendingAutoSubmit, setPendingAutoSubmit] = useState(false);

  // ── Barge-in plumbing ────────────────────────────────────────────
  // bargedInRef gates token-stream + flush calls so an in-flight SSE
  // delta that arrives between the moment we trigger barge-in and the
  // moment AbortError propagates does NOT speak after "cancel". Reset
  // at the top of sendMessage so each new turn starts clean.
  const bargedInRef = useRef(false);
  // Tracks the latest TTS activity timestamp for self-echo suppression
  // in the VAD hook. Updated on every onTokenStream call AND on each
  // poll tick where synth.speaking === true.
  const lastTtsActivityAtRef = useRef(0);
  const lastTtsActivityAt = useCallback(() => lastTtsActivityAtRef.current, []);
  // Poll tts.isSpeaking() so we know whether to leave VAD active during
  // inter-sentence gaps (synth.speaking flips false briefly between
  // sentence flushes; without polling we'd pause VAD precisely when
  // the user is most likely to interject).
  const [ttsSpeaking, setTtsSpeaking] = useState(false);
  useEffect(() => {
    if (!bargeInEnabled) return;
    const id = setInterval(() => {
      const speaking = tts.isSupported() && tts.isSpeaking();
      if (speaking) lastTtsActivityAtRef.current = Date.now();
      setTtsSpeaking((prev) => (prev === speaking ? prev : speaking));
    }, 100);
    return () => clearInterval(id);
  }, [bargeInEnabled]);

  // Transient "Interrupted — listening" badge, shown for 1.5 s after a
  // barge-in fires. Lives in component state (not a ref) so it
  // re-renders the badge automatically.
  const [bargeInBadgeUntil, setBargeInBadgeUntil] = useState(0);
  const showBargeInBadge = bargeInBadgeUntil > Date.now();
  useEffect(() => {
    if (bargeInBadgeUntil <= Date.now()) return;
    const id = setTimeout(
      () => setBargeInBadgeUntil(0),
      Math.max(50, bargeInBadgeUntil - Date.now()),
    );
    return () => clearTimeout(id);
  }, [bargeInBadgeUntil]);

  // VAD active when streaming OR TTS audibly playing. Echo cooldown
  // (in the hook) handles the self-trigger problem from speakers.
  const vadActive = streamState.isStreaming || ttsSpeaking;
  const handleBargeIn = useCallback(() => {
    // Order matters: set the gate FIRST so any in-flight SSE delta
    // pushed into the for-await loop doesn't make it into the TTS
    // buffer between now and AbortError propagation.
    bargedInRef.current = true;
    if (tts.isSupported()) tts.cancel();
    if (streamState.isStreaming) {
      try {
        abortRef.current?.abort();
      } catch {
        /* ignore */
      }
    }
    setBargeInBadgeUntil(Date.now() + 1500);
    useAppStore.getState().addLogEntry({
      timestamp: Date.now(),
      level: 'info',
      category: 'chat',
      message: 'Barge-in: user spoke during response (VAD)',
    });
  }, [streamState.isStreaming]);

  useBargeInVAD({
    enabled: bargeInEnabled && speechEnabled,
    active: vadActive,
    onBargeIn: handleBargeIn,
    lastTtsActivityAt,
  });

  useVoiceListener({
    enabled: continuousListenActive,
    micPermissionState,
    onTranscript: (transcript) => {
      const text = transcript.trim();
      if (!text) return;

      // ABORT path — user retracts a pending auto-submit, or stops a
      // current command before it submits. Highest priority.
      if (isAbort(text)) {
        cancelAutoSubmit();
        setPendingAutoSubmit(false);
        setInput('');
        return;
      }

      const intent = classifyIntent(text);

      // ElaborationBanner owns yes/no while a proposal is pending.
      if (
        isPendingElaboration &&
        (intent === 'affirmative' || intent === 'negative' || intent === 'stop')
      ) {
        return;
      }

      // Decide whether THIS transcript should populate the input box.
      let toAppend = '';
      let cameFromWakeWord = false;
      let cameFromShortReply = false;

      if (intent === 'wake') {
        const command = stripWakeWord(text);
        if (command) {
          toAppend = command;
          cameFromWakeWord = true;
        }
      } else if (intent === 'speech') {
        toAppend = text;
      } else if (intent === 'affirmative' || intent === 'negative') {
        toAppend = text;
        cameFromShortReply = true;
      }

      if (!toAppend) return;

      const hasSendTrigger = endsWithSendTrigger(toAppend);
      if (hasSendTrigger) {
        toAppend = stripCloseTrigger(toAppend);
      }

      setInput((prev) => {
        const next = prev.trim() ? `${prev} ${toAppend}` : toAppend;

        if (hasSendTrigger && next.trim()) {
          cancelAutoSubmit();
          setTimeout(() => sendMessage(), 0);
          return next;
        }

        if (
          handsFreeMode &&
          cameFromWakeWord &&
          next.trim().split(/\s+/).length >= 3
        ) {
          cancelAutoSubmit();
          setPendingAutoSubmit(true);
          autoSubmitTimerRef.current = setTimeout(() => {
            autoSubmitTimerRef.current = null;
            setPendingAutoSubmit(false);
            sendMessage();
          }, 1500);
        } else if (cameFromShortReply && prev.trim() === '') {
          cancelAutoSubmit();
          setPendingAutoSubmit(true);
          autoSubmitTimerRef.current = setTimeout(() => {
            autoSubmitTimerRef.current = null;
            setPendingAutoSubmit(false);
            sendMessage();
          }, 800);
        } else {
          cancelAutoSubmit();
          setPendingAutoSubmit(false);
        }

        return next;
      });
    },
    onError: (errorCode) => {
      setVoiceListenError(errorCode);
    },
    onStarted: () => {
      setVoiceListenError(null);
    },
  });


  // Clean up the auto-submit timer on unmount.
  useEffect(() => () => cancelAutoSubmit(), [cancelAutoSubmit]);

  useEffect(() => {
    const el = textareaRef.current;
    if (!el) return;
    el.style.height = 'auto';
    el.style.height = Math.min(el.scrollHeight, 200) + 'px';
  }, [input]);

  const stopStreaming = useCallback(() => {
    abortRef.current?.abort();
    if (timerRef.current) {
      clearInterval(timerRef.current);
      timerRef.current = null;
    }
    resetStream();
  }, [resetStream]);

  const sendMessage = useCallback(async () => {
    const content = input.trim();
    if (!content || streamState.isStreaming) return;

    // Reset barge-in gate so this new turn's TTS / SSE flow normally.
    bargedInRef.current = false;

    setInput('');
    // Stop any in-flight TTS before starting a new turn so the assistant
    // doesn't keep talking over the user.
    if (tts.isSupported()) tts.cancel();

    let convId = activeId;
    if (!convId) {
      convId = createConversation(selectedModel);
    }

    const userMsg: ChatMessage = {
      id: generateId(),
      role: 'user',
      content,
      timestamp: Date.now(),
    };
    addMessage(convId, userMsg);

    // Build API messages before adding assistant placeholder
    const currentMessages = useAppStore.getState().messages;
    const apiMessages = currentMessages.map((m) => ({
      role: m.role,
      content: m.content,
    }));

    const assistantMsg: ChatMessage = {
      id: generateId(),
      role: 'assistant',
      content: '',
      timestamp: Date.now(),
    };
    addMessage(convId, assistantMsg);

    // Start streaming
    const startTime = Date.now();
    const timer = setInterval(() => {
      setStreamState({ elapsedMs: Date.now() - startTime });
    }, 100);
    timerRef.current = timer;

    const controller = new AbortController();
    abortRef.current = controller;

    let accumulatedContent = '';
    let usage: TokenUsage | undefined;
    let complexity: { score: number; tier: string; suggested_max_tokens: number } | undefined;
    const toolCalls: ToolCallInfo[] = [];
    let lastFlush = 0;
    let ttftMs: number | undefined;

    setStreamState({
      isStreaming: true,
      phase: 'Generating...',
      elapsedMs: 0,
      activeToolCalls: [],
      content: '',
    });
    useAppStore.getState().addLogEntry({
      timestamp: Date.now(),
      level: 'info',
      category: 'chat',
      message: `Request: "${content.slice(0, 80)}${content.length > 80 ? '...' : ''}" → ${selectedModel}`,
    });

    // Defensive fallback: if the user hasn't picked a model, use the
    // server's default (visible in the sidebar) so the request goes to a
    // real provider instead of being dispatched as model="".
    const effectiveModel =
      selectedModel ||
      useAppStore.getState().serverInfo?.model ||
      'openrouter/auto';

    try {
      for await (const sseEvent of streamChat(
        { model: effectiveModel, messages: apiMessages, stream: true, temperature, max_tokens: maxTokens },
        controller.signal,
      )) {
        const eventName = sseEvent.event;

        if (eventName === 'agent_turn_start') {
          setStreamState({ phase: 'Agent thinking...' });
        } else if (eventName === 'inference_start') {
          setStreamState({ phase: 'Generating...' });
          useAppStore.getState().addLogEntry({
            timestamp: Date.now(), level: 'info', category: 'chat',
            message: `Generating with ${selectedModel}...`,
          });
        } else if (eventName === 'tool_call_start') {
          try {
            const data = JSON.parse(sseEvent.data);
            const tc: ToolCallInfo = {
              id: generateId(),
              tool: data.tool,
              arguments: data.arguments || '',
              status: 'running',
            };
            toolCalls.push(tc);
            setStreamState({
              phase: `Calling ${data.tool}...`,
              activeToolCalls: [...toolCalls],
            });
            updateLastAssistant(convId, accumulatedContent, [...toolCalls]);
            useAppStore.getState().addLogEntry({
              timestamp: Date.now(), level: 'info', category: 'tool',
              message: `Calling ${data.tool}(${data.arguments || ''})`,
            });
          } catch {}
        } else if (eventName === 'tool_call_end') {
          try {
            const data = JSON.parse(sseEvent.data);
            const tc = toolCalls.find(
              (t) => t.tool === data.tool && t.status === 'running',
            );
            if (tc) {
              tc.status = data.success ? 'success' : 'error';
              tc.latency = data.latency;
              tc.result = data.result;
            }
            setStreamState({
              phase: 'Generating...',
              activeToolCalls: [...toolCalls],
            });
            updateLastAssistant(convId, accumulatedContent, [...toolCalls]);
          } catch {}
        } else {
          try {
            const data = JSON.parse(sseEvent.data);
            const delta = data.choices?.[0]?.delta;
            if (data.usage) usage = data.usage;
            if (data.complexity) complexity = data.complexity;
            if (delta?.content) {
              if (!ttftMs) ttftMs = Date.now() - startTime;
              accumulatedContent += delta.content;
              setStreamState({ content: accumulatedContent, phase: '' });

              if (
                speechEnabled
                && tts.isSupported()
                && !bargedInRef.current
              ) {
                lastTtsActivityAtRef.current = Date.now();
                tts.onTokenStream(delta.content);
              }

              const now = Date.now();
              if (now - lastFlush >= 80) {
                updateLastAssistant(
                  convId,
                  accumulatedContent,
                  toolCalls.length > 0 ? [...toolCalls] : undefined,
                );
                lastFlush = now;
              }
            }
            if (data.choices?.[0]?.finish_reason === 'stop') {
              if (
                speechEnabled
                && tts.isSupported()
                && !bargedInRef.current
              ) {
                tts.flush();
              }
              break;
            }
          } catch {}
        }
      }
    } catch (err: any) {
      if (err.name === 'AbortError') {
        // User cancelled or model switch — keep whatever was accumulated
        if (!accumulatedContent) accumulatedContent = '(Generation stopped)';
      } else {
        const errMsg = err?.message || String(err);
        accumulatedContent =
          accumulatedContent || `Error: ${errMsg}`;
        useAppStore.getState().addLogEntry({
          timestamp: Date.now(), level: 'error', category: 'chat',
          message: `Stream error: ${errMsg}`,
        });
      }
    } finally {
      if (!accumulatedContent) {
        accumulatedContent = 'No response was generated. Please try again.';
      }
      const totalMs = Date.now() - startTime;
      const _CLOUD_PREFIXES = ['gpt-', 'o1-', 'o3-', 'o4-', 'claude-', 'gemini-', 'openrouter/', 'MiniMax-', 'chatgpt-'];
      const engineLabel = _CLOUD_PREFIXES.some(p => selectedModel.startsWith(p)) ? 'cloud' : 'ollama';
      const telemetry: MessageTelemetry = {
        engine: engineLabel,
        model_id: selectedModel,
        total_ms: totalMs,
        ttft_ms: ttftMs,
        tokens_per_sec: usage?.completion_tokens
          ? usage.completion_tokens / (totalMs / 1000)
          : undefined,
        complexity_score: complexity?.score,
        complexity_tier: complexity?.tier,
        suggested_max_tokens: complexity?.suggested_max_tokens,
      };
      // Check if the response has digest audio available
      let audioMeta: { url: string } | undefined;
      try {
        const digestRes = await fetch(`${getBase()}/api/digest`);
        if (digestRes.ok) {
          const digest = await digestRes.json();
          if (digest.audio_available) {
            audioMeta = { url: `${getBase()}/api/digest/audio` };
          }
        }
      } catch {
        // Not a digest response or server unavailable — skip
      }

      updateLastAssistant(
        convId,
        accumulatedContent,
        toolCalls.length > 0 ? toolCalls : undefined,
        usage,
        telemetry,
        audioMeta,
      );
      if (timerRef.current) {
        clearInterval(timerRef.current);
        timerRef.current = null;
      }
      resetStream();
      useAppStore.getState().addLogEntry({
        timestamp: Date.now(), level: 'info', category: 'chat',
        message: `Response: ${accumulatedContent.length} chars`,
      });
      abortRef.current = null;

      fetchSavings()
        .then((data) => useAppStore.getState().setSavings(data))
        .catch(() => {});
    }
  }, [
    input,
    activeId,
    selectedModel,
    streamState.isStreaming,
    createConversation,
    addMessage,
    updateLastAssistant,
    setStreamState,
    resetStream,
  ]);

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      sendMessage();
    }
  };

  return (
    <div className="px-4 pb-4 pt-2" style={{ maxWidth: 'var(--chat-max-width)', margin: '0 auto', width: '100%' }}>
      <div
        className="flex items-center gap-2 rounded-2xl px-4 py-3 transition-shadow"
        style={{
          background: 'var(--color-input-bg)',
          border: '1px solid var(--color-input-border)',
          boxShadow: 'var(--shadow-sm)',
        }}
      >
        <textarea
          ref={textareaRef}
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={handleKeyDown}
          placeholder="Message OpenJarvis..."
          rows={1}
          className="flex-1 bg-transparent outline-none resize-none text-sm leading-relaxed"
          style={{ color: 'var(--color-text)', maxHeight: '200px' }}
          disabled={streamState.isStreaming || modelLoading}
        />
        {streamState.isStreaming ? (
          <button
            onClick={stopStreaming}
            className="p-2 rounded-xl transition-colors shrink-0 cursor-pointer"
            style={{ background: 'var(--color-error)', color: 'var(--color-on-accent)' }}
            title="Stop generating"
          >
            <Square size={16} />
          </button>
        ) : (
          <div className="flex items-center gap-1">
            <MicButton
              state={speechState}
              onClick={handleMicClick}
              disabled={micDisabled}
              reason={micReason}
            />
            <button
              onClick={sendMessage}
              disabled={!input.trim() || modelLoading}
              className="p-2 rounded-xl transition-colors shrink-0 cursor-pointer disabled:opacity-30 disabled:cursor-default"
              style={{
                background: input.trim() ? 'var(--color-accent)' : 'var(--color-bg-tertiary)',
                color: input.trim() ? 'white' : 'var(--color-text-tertiary)',
              }}
              title="Send message"
            >
              <Send size={16} />
            </button>
          </div>
        )}
      </div>
      <div
        className="flex items-center justify-center gap-3 mt-2 text-[11px]"
        style={{ color: 'var(--color-text-tertiary)' }}
      >
        <span>
          <kbd className="font-mono">Enter</kbd> to send &middot;{' '}
          <kbd className="font-mono">Shift+Enter</kbd> for new line
        </span>
        {continuousListenActive && !voiceListenError && !pendingAutoSubmit && (
          <span
            style={{
              display: 'inline-flex',
              alignItems: 'center',
              gap: 4,
              color: 'var(--color-accent)',
            }}
            title={
              handsFreeMode
                ? 'Hands-free: speak after "Jarvis"; auto-submits after a pause OR say "over". "Wait" cancels.'
                : 'Listening for "Jarvis, ..." commands. End with "send it" / "over" to fire without clicking. Costs no tokens.'
            }
          >
            <Mic
              size={11}
              style={{ animation: 'pulse 1.4s ease-in-out infinite' }}
            />
            {handsFreeMode ? 'Hands-free' : 'Listening'}
          </span>
        )}
        {pendingAutoSubmit && (
          <span
            style={{
              display: 'inline-flex',
              alignItems: 'center',
              gap: 4,
              color: 'var(--color-warning, var(--color-accent))',
              fontWeight: 600,
            }}
            title='Auto-submitting in 1.5s. Say "wait" or "cancel" to abort.'
          >
            <Mic size={11} style={{ animation: 'pulse 0.5s ease-in-out infinite' }} />
            Submitting… (say "wait" to cancel)
          </span>
        )}
        {voiceListenError === 'not-allowed'
          && micPermissionState === 'denied' && (
          <span style={{ color: 'var(--color-error)' }}>
            Mic blocked — grant permission in browser to enable always-listening
          </span>
        )}
        {showBargeInBadge && (
          <span
            style={{
              display: 'inline-flex',
              alignItems: 'center',
              gap: 4,
              color: 'var(--color-accent)',
              fontWeight: 600,
            }}
            title="You spoke during Jarvis's response — TTS and the in-flight stream were cancelled."
          >
            <Mic size={11} style={{ animation: 'pulse 0.6s ease-in-out infinite' }} />
            Interrupted — listening
          </span>
        )}
      </div>
    </div>
  );
}
