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
  const continuousListenActive =
    alwaysListenEnabled &&
    speechEnabled &&
    isSpeechRecognitionSupported() &&
    !streamState.isStreaming;
  const [voiceListenError, setVoiceListenError] = useState<string | null>(null);

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

  useVoiceListener({
    enabled: continuousListenActive,
    onTranscript: (transcript) => {
      const text = transcript.trim();
      if (!text) return;

      // ABORT path — user retracts a pending auto-submit, or stops a
      // current command before it submits. Highest priority.
      if (isAbort(text)) {
        cancelAutoSubmit();
        setPendingAutoSubmit(false);
        // If they're aborting, also clear what's in the input box so
        // we don't accidentally submit half a thought.
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
      if (intent === 'wake') {
        const command = stripWakeWord(text);
        if (command) {
          toAppend = command;
          cameFromWakeWord = true;
        }
      } else if (intent === 'speech') {
        toAppend = text;
      }
      if (!toAppend) return;

      // Strip a trailing "over / send it / go" if present — that's
      // the submission cue, not part of the message.
      const hasSendTrigger = endsWithSendTrigger(toAppend);
      if (hasSendTrigger) {
        toAppend = stripCloseTrigger(toAppend);
      }

      // Append to existing input (so two utterances combine), or
      // populate fresh if empty.
      setInput((prev) => {
        const next = prev.trim() ? `${prev} ${toAppend}` : toAppend;

        // SUBMIT TRIGGER 1 — explicit close phrase, fire immediately.
        if (hasSendTrigger && next.trim()) {
          cancelAutoSubmit();
          // Defer one tick so React state settles before submit reads it.
          setTimeout(() => sendMessage(), 0);
          return next;
        }

        // SUBMIT TRIGGER 2 — hands-free mode + wake-word command +
        // substantial content + silence. Schedule a delayed submit.
        // A new transcript will reset this; abort phrase will cancel.
        if (
          handsFreeMode
          && cameFromWakeWord
          && next.trim().split(/\s+/).length >= 3
        ) {
          cancelAutoSubmit();
          setPendingAutoSubmit(true);
          autoSubmitTimerRef.current = setTimeout(() => {
            autoSubmitTimerRef.current = null;
            setPendingAutoSubmit(false);
            sendMessage();
          }, 1500);
        } else {
          // New incoming speech that isn't a wake-word command — reset
          // any pending auto-submit (the user is still speaking).
          cancelAutoSubmit();
          setPendingAutoSubmit(false);
        }

        return next;
      });
    },
    onError: (errorCode) => {
      setVoiceListenError(errorCode);
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

              if (speechEnabled && tts.isSupported()) {
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
              if (speechEnabled && tts.isSupported()) tts.flush();
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
        {voiceListenError === 'not-allowed' && (
          <span style={{ color: 'var(--color-error)' }}>
            Mic blocked — grant permission in browser to enable always-listening
          </span>
        )}
      </div>
    </div>
  );
}
