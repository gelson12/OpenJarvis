"""Tests for the auto-writeback hook that stores Q&A pairs into the
configured memory backend after every fast-path / agent / elaboration
response."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from openjarvis.server.memory_writeback import store_qa


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    for k in ("MEMORY_AUTO_WRITEBACK", "MEMORY_WRITEBACK_MIN_CHARS"):
        monkeypatch.delenv(k, raising=False)


def test_stores_substantive_qa():
    fake_backend = MagicMock()
    fake_backend.store.return_value = "doc-1"
    out = store_qa(
        backend=fake_backend,
        question="What's my Stripe revenue this week and how does it compare to last week?",
        answer="Stripe revenue this week is $9,068; last week was $7,400 — up about 22%.",
        source="fast_path",
        model="claude-sonnet-4-6",
    )
    assert out == "doc-1"
    args, kwargs = fake_backend.store.call_args
    assert "Q: " in args[0] and "A: " in args[0]
    assert kwargs["source"] == "chat.fast_path"
    assert kwargs["metadata"]["source"] == "fast_path"
    assert kwargs["metadata"]["model"] == "claude-sonnet-4-6"
    assert kwargs["metadata"]["kind"] == "conversation_qa"


def test_skips_short_qa(monkeypatch):
    """Default min_chars is 30 — 'hello' should be skipped."""
    fake_backend = MagicMock()
    out = store_qa(
        backend=fake_backend,
        question="hi",
        answer="Hello!",
        source="fast_path",
    )
    assert out is None
    fake_backend.store.assert_not_called()


def test_skips_error_responses():
    fake_backend = MagicMock()
    out = store_qa(
        backend=fake_backend,
        question="What's my Stripe revenue this week — should be substantive enough",
        answer=(
            "Sorry, an error occurred: Error code: 429 - "
            "{'error': {'message': 'You exceeded your current quota'}}"
        ),
        source="fast_path",
    )
    assert out is None
    fake_backend.store.assert_not_called()


def test_disabled_via_env(monkeypatch):
    monkeypatch.setenv("MEMORY_AUTO_WRITEBACK", "false")
    fake_backend = MagicMock()
    out = store_qa(
        backend=fake_backend,
        question="What's my calendar showing for next week?" * 3,
        answer="You have 12 events spanning 3 calendars next week — busiest is Tuesday.",
        source="fast_path",
    )
    assert out is None
    fake_backend.store.assert_not_called()


def test_no_backend_returns_none():
    """If no memory backend is configured, we silently do nothing."""
    out = store_qa(
        backend=None,
        question="Something substantive that meets the minimum",
        answer="An equally substantive answer that crosses the threshold",
        source="fast_path",
    )
    assert out is None


def test_backend_failure_is_swallowed():
    """Memory write failures must not propagate — they would otherwise
    break the user-visible chat response."""
    fake_backend = MagicMock()
    fake_backend.store.side_effect = RuntimeError("DB exploded")
    out = store_qa(
        backend=fake_backend,
        question="Substantive question that meets the minimum threshold",
        answer="Substantive answer that also meets the minimum threshold",
        source="fast_path",
    )
    assert out is None  # failure silently swallowed


def test_extra_metadata_passes_through():
    fake_backend = MagicMock()
    fake_backend.store.return_value = "doc-2"
    store_qa(
        backend=fake_backend,
        question="Substantive question that meets the minimum threshold",
        answer="Substantive answer that also meets the minimum threshold",
        source="elaboration",
        conversation_id="conv-7",
        extra_metadata={"experiment": "voice-first"},
    )
    metadata = fake_backend.store.call_args.kwargs["metadata"]
    assert metadata["conversation_id"] == "conv-7"
    assert metadata["experiment"] == "voice-first"


def test_min_chars_overridable_via_env(monkeypatch):
    monkeypatch.setenv("MEMORY_WRITEBACK_MIN_CHARS", "5")
    fake_backend = MagicMock()
    fake_backend.store.return_value = "doc-3"
    out = store_qa(
        backend=fake_backend,
        question="hello?",
        answer="howdy partner",
        source="fast_path",
    )
    assert out == "doc-3"  # short Q&A now allowed because threshold is 5
