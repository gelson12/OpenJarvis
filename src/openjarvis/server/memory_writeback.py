"""Auto-store conversation Q&A pairs into the configured memory backend.

Invoked at the end of fast-path responses (``_handle_stream``,
``AgentStreamBridge``) and elaboration resolutions (``elaboration_worker``).

Why
---
The retrieval side (``inject_context``) already pulls relevant snippets
from memory before every chat call, but nothing was writing new content
back. Without writeback, the assistant's "memory" only contains whatever
the user manually stored via the memory_store tool — which is rarely
done. This hook captures every substantive Q&A pair automatically so
future questions can reference past conversations.

Behaviour
---------
- Skips trivial pairs (either side under ``MEMORY_WRITEBACK_MIN_CHARS``,
  default 30, to ignore "hello" / one-word replies).
- Skips errors (the assistant turn shouldn't be archived as wisdom).
- Stores into whichever ``memory_backend`` is on the app state — if
  it's a HybridMemory, it spans every sub-backend (SQLite + Obsidian
  vault + ...) automatically.
- Tagged with ``source`` (fast_path / elaboration), ``conversation_id``,
  ``model`` (so we can retrieve "what did Claude say last time").
- Disabled entirely via ``MEMORY_AUTO_WRITEBACK=false``.
- All failures are non-fatal: the hook logs and continues.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Optional

logger = logging.getLogger(__name__)


def _enabled() -> bool:
    return os.environ.get("MEMORY_AUTO_WRITEBACK", "true").strip().lower() in (
        "true", "1", "yes", "on",
    )


def _min_chars() -> int:
    try:
        return int(os.environ.get("MEMORY_WRITEBACK_MIN_CHARS", "30"))
    except ValueError:
        return 30


def _looks_like_error(text: str) -> bool:
    """Skip writeback for obvious error responses so we don't poison
    memory with stack traces or 'sorry, an error occurred' lines."""
    lower = (text or "").strip().lower()
    return any(
        marker in lower
        for marker in (
            "sorry, an error occurred",
            "error code:",
            "traceback (most recent call last)",
            "client error '4",
            "client error '5",
        )
    )


def store_qa(
    *,
    backend: Any,
    question: str,
    answer: str,
    source: str,
    conversation_id: Optional[str] = None,
    model: Optional[str] = None,
    extra_metadata: Optional[dict[str, Any]] = None,
) -> Optional[str]:
    """Store a Q&A pair to the memory backend. Returns the doc_id on
    success, ``None`` if skipped or failed.

    All errors are caught and logged at DEBUG — writeback failure must
    never break the user-visible chat response.
    """
    if not _enabled():
        return None
    if backend is None:
        return None

    q = (question or "").strip()
    a = (answer or "").strip()
    if len(q) < _min_chars() or len(a) < _min_chars():
        return None
    if _looks_like_error(a):
        return None

    content = f"Q: {q}\n\nA: {a}"
    metadata: dict[str, Any] = {
        "source": source,
        "stored_at": int(time.time()),
        "kind": "conversation_qa",
    }
    if conversation_id:
        metadata["conversation_id"] = conversation_id
    if model:
        metadata["model"] = model
    if extra_metadata:
        metadata.update(extra_metadata)

    try:
        doc_id = backend.store(
            content,
            source=f"chat.{source}",
            metadata=metadata,
        )
        logger.info(
            "memory writeback (%s): stored Q&A (%d chars) -> %s",
            source, len(content), doc_id,
        )
        return doc_id
    except Exception as exc:
        logger.debug("memory writeback failed (non-fatal): %s", exc, exc_info=True)
        return None


__all__ = ["store_qa"]
