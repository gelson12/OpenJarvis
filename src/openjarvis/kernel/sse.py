"""Render a deterministic :class:`Outcome` as the response the caller expects.

The voice worker calls ``/v1/chat/completions`` with ``stream=True`` and an
OpenAI client, so a final kernel answer must be delivered as a strict OpenAI
SSE stream. Non-streaming callers get a normal ``ChatCompletionResponse``.

Keeping this in one place means the deterministic path produces byte-identical
framing to the LLM path — the worker can't tell the difference, which is the
point: the user just gets a correct, instant answer.
"""

from __future__ import annotations

import uuid

from fastapi.responses import StreamingResponse

from openjarvis.kernel.contracts import Outcome
from openjarvis.server.models import (
    ChatCompletionChunk,
    ChatCompletionResponse,
    Choice,
    ChoiceMessage,
    DeltaMessage,
    StreamChoice,
    UsageInfo,
)


def as_stream(outcome: Outcome, model: str) -> StreamingResponse:
    """Yield the outcome message as a minimal, valid OpenAI SSE stream."""
    chunk_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
    text = outcome.message

    async def generate():
        # 1) role
        yield "data: " + ChatCompletionChunk(
            id=chunk_id, model=model,
            choices=[StreamChoice(delta=DeltaMessage(role="assistant"))],
        ).model_dump_json() + "\n\n"
        # 2) content (single chunk — these replies are short)
        yield "data: " + ChatCompletionChunk(
            id=chunk_id, model=model,
            choices=[StreamChoice(delta=DeltaMessage(content=text))],
        ).model_dump_json() + "\n\n"
        # 3) finish
        yield "data: " + ChatCompletionChunk(
            id=chunk_id, model=model,
            choices=[StreamChoice(delta=DeltaMessage(), finish_reason="stop")],
        ).model_dump_json() + "\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")


def as_response(outcome: Outcome, model: str) -> ChatCompletionResponse:
    return ChatCompletionResponse(
        model=model,
        choices=[Choice(
            message=ChoiceMessage(role="assistant", content=outcome.message),
            finish_reason="stop",
        )],
        usage=UsageInfo(prompt_tokens=0, completion_tokens=0, total_tokens=0),
        complexity=None,
    )
