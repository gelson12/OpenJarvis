"""Tests for the augment-with-spoken-answer flow.

Verifies elaboration prompts include the fast-path response + an
explicit "elaborate, don't restate" instruction so Claude-CLI no longer
duplicates the fast answer.
"""

from __future__ import annotations

import pytest

from openjarvis.server.elaboration_worker import (
    _augment_with_spoken_answer,
    _wait_for_spoken_answer,
)


def test_augment_appends_assistant_turn_and_instruction():
    messages = [
        {"role": "user", "content": "hello"},
    ]
    out = _augment_with_spoken_answer(messages, "Hello! How can I help today?")

    # Original message preserved at front.
    assert out[0] == {"role": "user", "content": "hello"}

    # Fast-path answer becomes an assistant turn.
    assert out[1]["role"] == "assistant"
    assert out[1]["content"] == "Hello! How can I help today?"

    # Followed by an explicit elaboration directive as a user turn so
    # Claude-CLI treats it as the new task.
    assert out[2]["role"] == "user"
    instruction = out[2]["content"]
    assert "[ELABORATION TASK]" in instruction
    assert "ELABORATE" in instruction
    assert "not to restate" in instruction.lower() or "do not paraphrase" in instruction.lower()


def test_augment_does_not_mutate_original_messages():
    """The augment helper must return a new list — modifying messages
    in place would corrupt subsequent retries."""
    original = [{"role": "user", "content": "hi"}]
    _augment_with_spoken_answer(original, "fast answer")
    assert original == [{"role": "user", "content": "hi"}]


def test_augment_strips_whitespace_from_fast_answer():
    out = _augment_with_spoken_answer(
        [{"role": "user", "content": "x"}],
        "   answer with trailing whitespace\n\n",
    )
    assert out[1]["content"] == "answer with trailing whitespace"


def test_augment_preserves_long_history():
    history = [
        {"role": "system", "content": "you are jarvis"},
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
        {"role": "user", "content": "what's the weather"},
    ]
    out = _augment_with_spoken_answer(history, "It's 72° and sunny.")
    # Original 4 + assistant + elaboration directive = 6 entries
    assert len(out) == 6
    assert out[:4] == history
    assert out[4]["role"] == "assistant"
    assert out[4]["content"] == "It's 72° and sunny."
    assert out[5]["role"] == "user"
    assert "[ELABORATION TASK]" in out[5]["content"]


@pytest.mark.asyncio
async def test_wait_for_spoken_answer_times_out_gracefully(monkeypatch):
    """If the fast-path never produces an answer, _wait returns None
    rather than blocking forever — so the elaboration still submits."""
    from unittest.mock import AsyncMock, MagicMock

    fake_store = MagicMock()
    fake_store.get = AsyncMock(return_value=None)
    monkeypatch.setattr(
        "openjarvis.server.elaboration_worker.get_store",
        lambda: fake_store,
    )

    out = await _wait_for_spoken_answer("abc", timeout_s=0.05)
    assert out is None


@pytest.mark.asyncio
async def test_wait_for_spoken_answer_returns_when_set(monkeypatch):
    """When the fast-path writes spoken_answer, the wait returns it."""
    from unittest.mock import AsyncMock, MagicMock

    fake_elab = MagicMock()
    fake_elab.spoken_answer = "Hello! How can I help today?"
    fake_store = MagicMock()
    fake_store.get = AsyncMock(return_value=fake_elab)
    monkeypatch.setattr(
        "openjarvis.server.elaboration_worker.get_store",
        lambda: fake_store,
    )

    out = await _wait_for_spoken_answer("abc", timeout_s=2.0)
    assert out == "Hello! How can I help today?"
