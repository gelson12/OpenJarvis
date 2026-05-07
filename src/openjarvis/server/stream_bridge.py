"""Bridge sync agent.run() + EventBus events to an async SSE generator.

Subscribes to EventBus callbacks that push events into an asyncio.Queue,
runs agent.run() in a background thread, and yields SSE-formatted strings
from the queue for consumption by FastAPI's StreamingResponse.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from typing import AsyncGenerator

from fastapi.responses import StreamingResponse

from openjarvis.agents._stubs import AgentContext, BaseAgent
from openjarvis.core.events import Event, EventBus, EventType
from openjarvis.server.models import (
    ChatCompletionChunk,
    ChatCompletionRequest,
    DeltaMessage,
    StreamChoice,
    UsageInfo,
)


# Order is intentional: prefer tool-reliable models with healthy keys.
# OpenAI is LAST because users frequently land here with broken billing /
# exhausted free tiers, and a 429-or-quota failure on the agent path is
# unrecoverable mid-run (no streaming-style fallback). Override the order
# at runtime via TOOL_CAPABLE_AGENT_MODELS=anthropic,deepseek,google,openai
# or by explicitly picking a single model in the chat dropdown.
_AGENT_MODEL_PREFERENCE: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("claude-sonnet-4-6", ("ANTHROPIC_API_KEY",)),
    ("deepseek-chat", ("DEEPSEEK_API_KEY",)),
    ("gemini-2.5-flash", ("GEMINI_API_KEY", "GOOGLE_API_KEY")),
    ("gpt-4o", ("OPENAI_API_KEY",)),
)


def _is_auto(model: str) -> bool:
    """Match the same set of aliases tier_cascade.is_auto_model() accepts."""
    if not model:
        return False
    return model.strip().lower() in ("auto", "openjarvis-auto", "openjarvis/auto")


def resolve_auto_model_for_agent(model: str) -> str:
    """Return the *primary* candidate for a literal 'auto' model id.

    For non-auto inputs this is a passthrough. For 'auto', it returns
    the first preference whose API key is present — but the agent's
    engine is also wrapped with :class:`FallbackEngine` (see
    :func:`get_agent_fallback_chain`) so a generate() failure on the
    primary transparently moves to the next candidate.
    """
    chain = get_agent_fallback_chain(model)
    return chain[0] if chain else model


def get_agent_fallback_chain(model: str) -> list[str]:
    """Return the ordered list of candidate models for this request.

    Always honours an explicit ``TOOL_CAPABLE_AGENT_MODEL`` override
    (single-element chain — operator wants exactly that model). For
    'auto', returns every preference whose API key is set in
    descending preference order. For a concrete model id, returns
    [model] (no fallback — the user picked something specific, respect
    it; surface the error if it fails so the misconfiguration is
    visible).
    """
    if not _is_auto(model):
        return [model] if model else []

    override = os.environ.get("TOOL_CAPABLE_AGENT_MODEL", "").strip()
    if override:
        return [override]

    chain: list[str] = []
    for candidate, key_envs in _AGENT_MODEL_PREFERENCE:
        if any(os.environ.get(env) for env in key_envs):
            chain.append(candidate)
    return chain


class FallbackEngine:
    """Engine wrapper that tries a sequence of models, returning the first
    successful generate() result. Anything else is forwarded to the inner
    engine unchanged.

    When the agent calls generate() on this wrapper, the ``model`` keyword
    is overridden by each candidate in turn. On any exception (HTTP 4xx/5xx,
    quota errors, network failures, etc.) the wrapper logs the failure
    and tries the next candidate. If every candidate fails, the *last*
    exception is re-raised so the user sees a real error rather than a
    misleading first-failure message.

    Used by :class:`AgentStreamBridge` for ``model='auto'`` requests so a
    transient OpenAI 429 / Anthropic 529 / network blip silently moves
    to the next available provider instead of failing the whole agent run.
    """

    def __init__(self, inner: object, candidates: list[str]) -> None:
        if not candidates:
            raise ValueError("FallbackEngine needs at least one candidate")
        self._inner = inner
        self._candidates = list(candidates)
        self._log = logging.getLogger("openjarvis.server")

    def __getattr__(self, name: str) -> object:
        # Delegate stream(), stream_full(), health(), close(), list_models(),
        # and any other method we haven't overridden to the inner engine.
        return getattr(self._inner, name)

    def generate(self, messages, *, model: str = "", **kwargs):  # type: ignore[no-untyped-def]
        """Try each candidate in order; return the first successful result."""
        last_exc: BaseException | None = None
        attempted: list[str] = []
        for candidate in self._candidates:
            try:
                return self._inner.generate(messages, model=candidate, **kwargs)
            except Exception as exc:
                attempted.append(candidate)
                last_exc = exc
                self._log.warning(
                    "FallbackEngine: %s failed (%s: %s); trying next candidate",
                    candidate, type(exc).__name__, exc,
                )
                continue
        # Every candidate failed — surface the last exception with
        # context about everything we tried.
        self._log.error(
            "FallbackEngine: all %d candidates failed (%s); raising last error",
            len(self._candidates), attempted,
        )
        assert last_exc is not None
        raise last_exc

# EventTypes we subscribe to and their corresponding SSE event names
_EVENT_MAP = {
    EventType.AGENT_TURN_START: "agent_turn_start",
    EventType.INFERENCE_START: "inference_start",
    EventType.INFERENCE_END: "inference_end",
    EventType.TOOL_CALL_START: "tool_call_start",
    EventType.TOOL_CALL_END: "tool_call_end",
}

# Sentinel signalling that the agent thread has finished
_DONE = object()


def _estimate_prompt_tokens(messages: list) -> int:
    """Estimate prompt tokens from request messages (incl. system, user, context).

    Uses ~4 chars per token heuristic. Ensures system and all context are counted.
    """
    total = 0
    for m in messages:
        text = getattr(m, "content", None) or ""
        if isinstance(text, str):
            total += max(1, len(text) // 4)
        # Tool call messages may have structured content; still count what we can
        if hasattr(m, "tool_calls") and m.tool_calls:
            for tc in m.tool_calls:
                fn = tc.get("function") if isinstance(tc, dict) else {}
                args = fn.get("arguments", "") if isinstance(fn, dict) else ""
                total += max(1, len(str(args)) // 4)
    return total


class AgentStreamBridge:
    """Bridge between a synchronous agent and an async SSE stream.

    Pattern:
    1. Subscribe EventBus callbacks that push events into an asyncio.Queue
       via ``loop.call_soon_threadsafe()``.
    2. Run ``agent.run()`` in a thread via ``asyncio.to_thread()``.
    3. Async generator reads from queue and yields SSE-formatted strings.
    4. Unsubscribe from EventBus in ``finally`` block.
    """

    def __init__(
        self,
        agent: BaseAgent,
        bus: EventBus,
        model: str,
        request: ChatCompletionRequest,
    ) -> None:
        self._agent = agent
        self._bus = bus
        self._model = model
        self._request = request
        self._chunk_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
        self._queue: asyncio.Queue = asyncio.Queue()
        self._callbacks: dict[EventType, object] = {}
        # Holder for the in-flight Claude-CLI elaboration record so we
        # can write the spoken_answer back to the store after agent.run()
        # finishes. Set in stream(); consumed in the finally block.
        self._elaboration: object | None = None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _make_callback(self, event_type: EventType):
        """Create a callback that pushes the event onto the async queue."""
        loop = asyncio.get_event_loop()

        def _cb(event: Event) -> None:
            loop.call_soon_threadsafe(self._queue.put_nowait, event)

        self._callbacks[event_type] = _cb
        return _cb

    def _subscribe_all(self) -> None:
        """Subscribe to all relevant EventBus event types."""
        for et in _EVENT_MAP:
            self._bus.subscribe(et, self._make_callback(et))

    def _unsubscribe_all(self) -> None:
        """Remove all registered subscriptions."""
        for et, cb in self._callbacks.items():
            self._bus.unsubscribe(et, cb)
        self._callbacks.clear()

    def _format_named_event(self, name: str, data: dict) -> str:
        """Format an SSE event with an explicit ``event:`` field."""
        return f"event: {name}\ndata: {json.dumps(data)}\n\n"

    def _run_agent(self) -> object:
        """Execute the agent synchronously (called via asyncio.to_thread)."""
        ctx = AgentContext()
        # Build conversation context from prior messages
        if len(self._request.messages) > 1:
            from openjarvis.core.types import Message, Role

            for m in self._request.messages[:-1]:
                role = Role(m.role) if m.role in {r.value for r in Role} else Role.USER
                ctx.conversation.add(
                    Message(
                        role=role,
                        content=m.content or "",
                        name=m.name,
                        tool_call_id=m.tool_call_id,
                    )
                )

        input_text = (
            self._request.messages[-1].content if self._request.messages else ""
        )

        # Override agent model + engine for this request.
        #
        # For 'auto' requests we additionally wrap the agent's engine
        # with FallbackEngine so a quota / rate-limit / auth failure on
        # the primary provider transparently retries the next candidate.
        # For a concrete model id (user picked gpt-4o etc.), we pass it
        # through unchanged — respect explicit user choice.
        original_model = self._agent._model
        original_engine = self._agent._engine
        if self._model:
            chain = get_agent_fallback_chain(self._model)
            if chain:
                self._agent._model = chain[0]
                if len(chain) > 1:
                    # Multiple candidates — wrap engine for silent fallback.
                    self._agent._engine = FallbackEngine(
                        original_engine, chain,
                    )
            else:
                # No keys at all — leave model as-is so the request
                # surfaces a real error rather than silently using a
                # stale value.
                self._agent._model = self._model
        try:
            return self._agent.run(input_text, context=ctx)
        finally:
            self._agent._model = original_model
            self._agent._engine = original_engine

    # ------------------------------------------------------------------
    # Public streaming interface
    # ------------------------------------------------------------------

    async def stream(self) -> AsyncGenerator[str, None]:
        """Async generator that yields SSE-formatted strings."""
        self._subscribe_all()

        # Spawn the Claude-CLI elaboration in parallel — Inspiring-Cat
        # processes the prompt independently of the fast-path agent so
        # the user always gets a deeper, more deliberate answer surfaced
        # via the elaboration SSE channel. This used to only run on the
        # cascade path; now the agent path engages it too so tool-using
        # requests are not invisible to Claude-CLI.
        try:
            from openjarvis.server import elaboration_worker

            original_question = ""
            for m in reversed(self._request.messages):
                if m.role == "user" and m.content:
                    original_question = m.content
                    break
            messages_json = [
                {"role": m.role, "content": m.content or ""}
                for m in self._request.messages
            ]
            self._elaboration = await elaboration_worker.spawn_elaboration(
                messages=messages_json,
                original_question=original_question,
                conversation_id=None,
            )
        except Exception as exc:
            logging.getLogger("openjarvis.server").debug(
                "Elaboration spawn failed in agent path (non-fatal): %s", exc,
            )

        # Kick off agent.run() in a background thread
        loop = asyncio.get_event_loop()
        agent_task = asyncio.ensure_future(asyncio.to_thread(self._run_agent))

        def _on_done(fut):
            loop.call_soon_threadsafe(self._queue.put_nowait, _DONE)

        agent_task.add_done_callback(_on_done)

        try:
            # Send initial role chunk (OpenAI-compatible)
            first_chunk = ChatCompletionChunk(
                id=self._chunk_id,
                model=self._model,
                choices=[
                    StreamChoice(
                        delta=DeltaMessage(role="assistant"),
                    )
                ],
            )
            yield f"data: {first_chunk.model_dump_json()}\n\n"

            # Drain queue until the agent finishes
            while True:
                item = await self._queue.get()

                if item is _DONE:
                    break

                if isinstance(item, Event):
                    sse_name = _EVENT_MAP.get(item.event_type)
                    if sse_name:
                        yield self._format_named_event(sse_name, item.data)

            # Agent is done -- retrieve result
            try:
                agent_result = agent_task.result()
            except Exception as exc:
                import logging

                logger = logging.getLogger("openjarvis.server")
                logger.error("Agent stream error: %s", exc, exc_info=True)

                error_str = str(exc)
                if "context length" in error_str.lower() or (
                    "400" in error_str and "too long" in error_str.lower()
                ):
                    error_content = (
                        "The input is too long for the model's context window. "
                        "Please try a shorter message."
                    )
                elif "400" in error_str:
                    error_content = f"The model returned an error: {error_str}"
                else:
                    error_content = f"Sorry, an error occurred: {error_str}"
                error_chunk = ChatCompletionChunk(
                    id=self._chunk_id,
                    model=self._model,
                    choices=[
                        StreamChoice(
                            delta=DeltaMessage(content=error_content),
                            finish_reason="stop",
                        )
                    ],
                )
                yield f"data: {error_chunk.model_dump_json()}\n\n"
                yield "data: [DONE]\n\n"
                return

            # Emit tool results metadata if any
            tool_results_data = []
            for tr in agent_result.tool_results:
                tool_results_data.append(
                    {
                        "tool_name": tr.tool_name,
                        "success": tr.success,
                        "output": tr.content,
                        "latency_ms": tr.latency_seconds * 1000,
                    }
                )

            if tool_results_data:
                yield self._format_named_event(
                    "tool_results",
                    {"results": tool_results_data},
                )

            # Stream content using real LLM token streaming via
            # engine.stream_full() when the engine is available.
            content = agent_result.content or ""
            engine = getattr(self._agent, "_engine", None)
            used_real_streaming = False

            if engine is not None and hasattr(engine, "stream_full") and content:
                # Re-stream using the engine for real token delivery.
                # Build the same messages the agent used for its final turn.
                try:
                    from openjarvis.core.types import Message as MsgType
                    from openjarvis.core.types import Role as RoleType

                    replay_messages = []
                    for m in self._request.messages:
                        role = (
                            RoleType(m.role)
                            if m.role in {r.value for r in RoleType}
                            else RoleType.USER
                        )
                        replay_messages.append(
                            MsgType(
                                role=role,
                                content=m.content or "",
                                name=m.name,
                                tool_call_id=m.tool_call_id,
                            )
                        )

                    async for sc in engine.stream_full(
                        replay_messages,
                        model=self._model,
                    ):
                        if sc.content:
                            chunk = ChatCompletionChunk(
                                id=self._chunk_id,
                                model=self._model,
                                choices=[
                                    StreamChoice(
                                        delta=DeltaMessage(content=sc.content),
                                    )
                                ],
                            )
                            yield f"data: {chunk.model_dump_json()}\n\n"
                    used_real_streaming = True
                except Exception as stream_exc:
                    import logging as _logging

                    _logger = _logging.getLogger("openjarvis.server")
                    _logger.warning(
                        "Real streaming failed, falling back to word replay: %s",
                        stream_exc,
                    )

            # Fallback: word-by-word replay if real streaming was not used
            if not used_real_streaming and content:
                words = content.split(" ")
                for i, word in enumerate(words):
                    token = word if i == 0 else " " + word
                    chunk = ChatCompletionChunk(
                        id=self._chunk_id,
                        model=self._model,
                        choices=[
                            StreamChoice(
                                delta=DeltaMessage(content=token),
                            )
                        ],
                    )
                    yield f"data: {chunk.model_dump_json()}\n\n"
                    await asyncio.sleep(0.012)

            # Final chunk: finish_reason + usage
            prompt_tokens = agent_result.metadata.get("prompt_tokens", 0)
            completion_tokens = agent_result.metadata.get(
                "completion_tokens",
                0,
            )
            total_tokens = agent_result.metadata.get("total_tokens", 0)
            if total_tokens == 0:
                # Fallback: estimate from request messages (incl. system) + content
                completion_tokens = max(len(content) // 4, 1)
                prompt_tokens = _estimate_prompt_tokens(self._request.messages)
                total_tokens = prompt_tokens + completion_tokens

            final_chunk = ChatCompletionChunk(
                id=self._chunk_id,
                model=self._model,
                choices=[
                    StreamChoice(
                        delta=DeltaMessage(),
                        finish_reason="stop",
                    )
                ],
            )
            final_data = json.loads(final_chunk.model_dump_json())
            final_data["usage"] = UsageInfo(
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=total_tokens,
            ).model_dump()
            yield f"data: {json.dumps(final_data)}\n\n"

            yield "data: [DONE]\n\n"

            # Write the agent's final answer back into the elaboration
            # record so the Claude-CLI worker has something to diff
            # against (and so the elaboration store knows the
            # conversation actually had a spoken answer to surface
            # alongside).
            if self._elaboration is not None:
                try:
                    from openjarvis.server.elaboration_store import (
                        get_store as _elab_store,
                    )
                    await _elab_store().set_spoken_answer(
                        self._elaboration.id, content,
                    )
                except Exception as exc:
                    logging.getLogger("openjarvis.server").debug(
                        "set_spoken_answer failed (non-fatal): %s", exc,
                    )

        except Exception:
            # On error, cancel the agent task if still running
            if not agent_task.done():
                agent_task.cancel()
            raise
        finally:
            self._unsubscribe_all()


async def create_agent_stream(
    agent: BaseAgent,
    bus: EventBus,
    model: str,
    request: ChatCompletionRequest,
) -> StreamingResponse:
    """Create an AgentStreamBridge and return a FastAPI StreamingResponse."""
    bridge = AgentStreamBridge(agent, bus, model, request)
    return StreamingResponse(
        bridge.stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
    )


__all__ = ["AgentStreamBridge", "create_agent_stream"]
