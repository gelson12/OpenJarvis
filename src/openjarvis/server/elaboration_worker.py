"""Background worker: poll Claude-CLI, compare to spoken answer, surface banner.

For each chat request that opts into elaboration, `_handle_stream` calls
`spawn_elaboration()` which:
  1. Submits the prompt to inspiring-cat (Claude-CLI worker).
  2. Creates an Elaboration record in the store.
  3. Launches an asyncio task that polls until done, then runs the diff
     heuristic, and either marks PROPOSED (broadcast over SSE) or DISCARDED.

The chat handler keeps a reference to the Elaboration record so it can write
the spoken_answer back into it when the immediate stream finishes.
"""

from __future__ import annotations

import asyncio
import difflib
import logging
import os
import re
from typing import Any, Optional

from openjarvis.server import claude_cli_client
from openjarvis.server.elaboration_store import (
    Elaboration,
    get_store,
)

logger = logging.getLogger(__name__)


def _length_ratio_threshold() -> float:
    try:
        return float(os.environ.get("ELABORATION_DIFF_LENGTH_RATIO", "1.5"))
    except ValueError:
        return 1.5


def _similarity_threshold() -> float:
    try:
        return float(os.environ.get("ELABORATION_DIFF_LEVENSHTEIN", "0.6"))
    except ValueError:
        return 0.6


def _timeout_s() -> float:
    try:
        return float(os.environ.get("CLAUDE_CLI_TIMEOUT_S", "180"))
    except ValueError:
        return 180.0


def is_enabled() -> bool:
    return os.environ.get("ELABORATION_ENABLED", "true").strip().lower() in (
        "true", "1", "yes", "on",
    )


def _always_surface() -> bool:
    """When true (the default), every Claude-CLI elaboration is marked
    PROPOSED and broadcast over SSE regardless of similarity to the spoken
    answer. The original heuristic — surface only when materially
    different — left the user thinking Claude-CLI never ran. Set
    ELABORATION_ALWAYS_SURFACE=false to opt back into the diff heuristic.
    """
    return os.environ.get("ELABORATION_ALWAYS_SURFACE", "true").strip().lower() in (
        "true", "1", "yes", "on",
    )


def _is_materially_different(spoken: str, claude: str) -> bool:
    """Heuristic: claude answer is meaningfully different from the spoken one.

    Both checks must trigger to avoid false positives:
      - length ratio: claude/spoken > threshold (default 1.5x)
      - similarity:   SequenceMatcher.ratio() < threshold (default 0.6)
    """
    spoken = (spoken or "").strip()
    claude = (claude or "").strip()
    if not claude:
        return False
    if not spoken:
        # No spoken answer to compare against — surface anyway, user may want it
        return True

    length_ratio = len(claude) / max(len(spoken), 1)
    similarity = difflib.SequenceMatcher(a=spoken, b=claude).ratio()

    materially = (
        length_ratio > _length_ratio_threshold()
        and similarity < _similarity_threshold()
    )
    logger.info(
        "Elaboration diff: len_ratio=%.2f sim=%.2f -> materially_different=%s",
        length_ratio, similarity, materially,
    )
    return materially


def _spoken_wait_seconds() -> float:
    """How long to wait for the fast-path's answer before submitting to
    Claude-CLI without it. Default 15s; the fast-path usually settles in
    2-5s. Configurable via env so ops can tune for slow agent runs."""
    try:
        return float(os.environ.get("ELABORATION_SPOKEN_WAIT_S", "15"))
    except ValueError:
        return 15.0


async def _wait_for_spoken_answer(
    elab_id: str, *, timeout_s: float,
) -> Optional[str]:
    """Poll the elaboration store until ``spoken_answer`` is non-None or
    ``timeout_s`` elapses. Returns the answer text, or None if we time out
    (so Claude-CLI still runs — just without the fast-path context)."""
    store = get_store()
    deadline = asyncio.get_event_loop().time() + timeout_s
    while asyncio.get_event_loop().time() < deadline:
        elab = await store.get(elab_id)
        if elab is not None and elab.spoken_answer is not None:
            return elab.spoken_answer
        await asyncio.sleep(0.5)
    return None


def _augment_with_spoken_answer(
    messages: list[dict[str, Any]], spoken: str,
) -> list[dict[str, Any]]:
    """Append the fast-path's answer as an [ASSISTANT] turn AND a final
    [USER] instruction telling Claude-CLI to elaborate on it without
    restating. The instruction is what turns Claude from a duplicate
    responder into an actual elaborator.

    Tone constraints come from production iteration:
    - Earlier versions produced 4 numbered sections of generic
      best-practice advice for a simple 'send an email' prompt. Too
      verbose, not personalised.
    - Earlier versions used markdown — bold, bullets, headers — which
      browser TTS reads literally ("asterisk asterisk Late asterisk
      asterisk") because there's no markdown→speech parser in
      window.speechSynthesis.

    The new prompt enforces terse, plain-prose, personally-grounded
    elaborations that work both as written text AND as spoken audio.
    """
    augmented = list(messages)
    augmented.append({"role": "assistant", "content": spoken.strip()})
    augmented.append({
        "role": "user",
        "content": (
            "[ELABORATION TASK]\n"
            "Provide a SHORT, sharp follow-up to the assistant's answer "
            "above. This will be SPOKEN aloud by browser TTS, so:\n"
            "\n"
            "VOICE-SAFE OUTPUT — strict:\n"
            "  - Plain prose only. NO markdown. NO asterisks for bold. "
            "NO hash-mark headers. NO bullet lists. NO numbered lists. "
            "NO code fences.\n"
            "  - NO emoji. None. Not 🎉, not 🔍, not 📝, not anything. "
            "Browser TTS reads emoji aloud as their unicode names "
            "('party popper', 'memo'), which is unbearable to listen to "
            "and obscures the actual message.\n"
            "  - Write as if speaking it aloud to a thoughtful colleague.\n"
            "  - Maximum 2 short paragraphs. Often 1-2 sentences is plenty.\n"
            "\n"
            "CONTENT — required:\n"
            "  - ONE specific observation grounded in this user's actual "
            "context (their stored conversations, their workflows, the "
            "tool results above) — NEVER generic best-practice advice "
            "like 'consider adding an ETA' or 'check your scope'.\n"
            "  - If the fast answer is already complete and correct, say "
            "one short line acknowledging that and stop.\n"
            "  - Do not paraphrase the fast answer back. Do not greet. "
            "Do not start with 'Hello' or 'Sir' (the polite-prompt "
            "wrapper handles that).\n"
            "\n"
            "Be J.A.R.V.I.S.: dry, brief, demonstrably useful. If you "
            "have nothing concrete to add beyond what the fast answer "
            "said, just say 'Nothing further to add, sir.' That's a "
            "valid response."
        ),
    })
    return augmented


async def _run(
    elab_id: str,
    messages: list[dict[str, Any]],
    *,
    memory_backend: Any = None,
    original_question: str = "",
) -> None:
    """The actual background coroutine.

    Order matters: we wait briefly for the fast-path's spoken_answer
    BEFORE submitting to Claude-CLI, so the elaboration prompt includes
    what was already said. Without this, Claude-CLI sees only the user's
    question and answers it from scratch — producing duplicates of the
    fast response. With it, Claude-CLI's job is explicitly to extend
    rather than restate.
    """
    store = get_store()

    # Step 1: wait for the fast-path responder to finish. Soft timeout —
    # if the fast-path is taking longer than expected (or errored), we
    # still submit to Claude-CLI so the user gets *something* deeper.
    spoken_answer = await _wait_for_spoken_answer(
        elab_id, timeout_s=_spoken_wait_seconds(),
    )

    # Step 2: build the prompt that goes to Claude-CLI. If we have the
    # fast-path answer, append it + an explicit elaboration instruction.
    if spoken_answer:
        elab_messages = _augment_with_spoken_answer(messages, spoken_answer)
        logger.info(
            "Elaboration %s: submitting with fast-path context (%d chars)",
            elab_id, len(spoken_answer),
        )
    else:
        elab_messages = messages
        logger.info(
            "Elaboration %s: fast-path didn't settle within %ss; "
            "submitting bare prompt",
            elab_id, _spoken_wait_seconds(),
        )

    try:
        task_id = await claude_cli_client.submit(elab_messages)
    except Exception as exc:
        logger.warning("Claude-CLI submit failed for %s: %s", elab_id, exc)
        await store.mark_failed(elab_id, error=f"submit failed: {exc}")
        return

    try:
        result = await claude_cli_client.await_completion(
            task_id,
            timeout_s=_timeout_s(),
        )
    except asyncio.TimeoutError:
        await store.mark_failed(elab_id, error="claude-cli timeout")
        return
    except Exception as exc:
        logger.warning("Claude-CLI poll failed for %s: %s", elab_id, exc)
        await store.mark_failed(elab_id, error=f"poll failed: {exc}")
        return

    if result.status != "done" or not result.result:
        await store.mark_failed(
            elab_id,
            error=result.error or f"claude-cli returned status={result.status}",
        )
        return

    claude_text = result.result.strip()

    if _always_surface() or _is_materially_different(spoken_answer or "", claude_text):
        await store.mark_proposed(elab_id, claude_answer=claude_text)
    else:
        await store.mark_discarded(elab_id, claude_answer=claude_text)

    # Auto-store the elaboration into long-term memory so it can be
    # retrieved in future conversations. Source-tagged 'elaboration'
    # so future retrieves can distinguish the deeper Claude answer
    # from the fast-path response stored under 'fast_path' / 'agent'.
    if memory_backend is not None and original_question:
        try:
            from openjarvis.server.memory_writeback import store_qa

            store_qa(
                backend=memory_backend,
                question=original_question,
                answer=claude_text,
                source="elaboration",
                model="claude-cli",
            )
        except Exception:
            pass


# Trivial messages where elaborating adds zero value — greetings,
# acknowledgements, single-word affirmations / negations. The agent
# should NOT propose "may I expand on Hello?" — that's annoying and
# wastes a Claude-CLI run. Match the user's raw message
# (case-insensitive, trim whitespace + trailing punctuation) against
# this set; if it hits, skip elaboration spawn entirely.
_TRIVIAL_MESSAGES: frozenset[str] = frozenset({
    "hi", "hello", "hey", "yo", "sup",
    "thanks", "thank you", "thx", "ty", "cheers",
    "ok", "okay", "k", "kk",
    "yes", "yep", "yeah", "yup", "sure",
    "no", "nope", "nah",
    "cool", "nice", "great", "awesome", "perfect", "good",
    "bye", "goodbye", "later", "cya", "see you",
    "lol", "haha", "hehe",
    "got it", "understood", "noted",
})


# ── Topic-coherence gate (Phase B, opt-in) ──────────────────────────
# Fires elaboration only when the user has stayed on a coherent topic
# across the recent turns. Lightweight Jaccard on content words — no
# embedding model needed; the worker stays lean.
_STOPWORDS = frozenset({
    "a", "an", "and", "are", "as", "at", "be", "but", "by", "for", "from",
    "has", "have", "he", "her", "him", "his", "how", "i", "if", "in", "is",
    "it", "its", "me", "my", "of", "on", "or", "our", "she", "so", "that",
    "the", "their", "them", "they", "this", "to", "was", "we", "were",
    "what", "when", "where", "which", "who", "why", "will", "with", "you",
    "your", "do", "does", "did", "can", "could", "would", "should", "shall",
    "would", "may", "might", "just", "about", "into", "out", "up", "down",
    "over", "under", "again", "also", "than", "then", "there", "here", "now",
})


def _content_words(text: str) -> set[str]:
    if not text:
        return set()
    tokens = re.findall(r"[a-z0-9]+", text.lower())
    return {t for t in tokens if len(t) > 2 and t not in _STOPWORDS}


def _topic_coherent(
    messages: list[dict[str, Any]],
    *,
    window: int,
    threshold: float,
) -> bool:
    """True if the last ``window`` user turns share a topic.

    Computes Jaccard similarity over content-word sets between adjacent
    pairs of recent user turns and requires AT LEAST ONE pair to clear
    ``threshold``. That's the "2-3 consecutive contextual topics"
    intent: not three turns in lockstep, but a coherent chain that
    connects at least two of the recent turns.
    """
    user_texts: list[str] = []
    for msg in messages:
        if msg.get("role") != "user":
            continue
        content = msg.get("content")
        if isinstance(content, str):
            user_texts.append(content)
        elif isinstance(content, list):
            # Multi-modal: keep text parts only.
            parts = [
                p.get("text", "")
                for p in content
                if isinstance(p, dict) and p.get("type") == "text"
            ]
            if parts:
                user_texts.append(" ".join(parts))
    recent = user_texts[-window:]
    if len(recent) < 2:
        return False  # need at least two turns to be 'coherent'
    sets = [_content_words(t) for t in recent]
    for a, b in zip(sets, sets[1:]):
        union = a | b
        if not union:
            continue
        jaccard = len(a & b) / len(union)
        if jaccard >= threshold:
            return True
    return False


def _is_trivial_question(text: str) -> bool:
    """True if the user's message is a greeting/ack and elaboration
    would be noise. Compares lowercased + stripped + punctuation-stripped
    text against ``_TRIVIAL_MESSAGES``."""
    if not text:
        return True
    cleaned = text.strip().lower()
    cleaned = cleaned.rstrip("!?.,;:#。！？").strip()
    if not cleaned:
        return True
    if cleaned in _TRIVIAL_MESSAGES:
        return True
    # Very short messages (≤ 2 words) that aren't actually questions —
    # e.g. "ok cool", "yes please", "thank you very much". Word count
    # under 3 + no question mark in the original = nothing substantive
    # to elaborate on.
    word_count = len(cleaned.split())
    if word_count < 3 and "?" not in text:
        return True
    return False


async def spawn_elaboration(
    *,
    messages: list[dict[str, Any]],
    original_question: str,
    conversation_id: Optional[str] = None,
    memory_backend: Any = None,
) -> Optional[Elaboration]:
    """Create an Elaboration record + launch the background worker.

    Returns the Elaboration so the caller (chat handler) can later write
    spoken_answer back into it. Returns None if elaboration is disabled
    or the user's message is too trivial to elaborate on.

    ``memory_backend`` is an optional reference to the configured
    memory layer; when provided, the resolved Claude-CLI answer is
    auto-stored alongside the original question so future retrievals
    benefit from the deeper analysis.
    """
    if not is_enabled():
        return None

    # Skip elaboration spawning for trivial messages (greetings, "thanks",
    # "ok") — there's nothing substantive to elaborate on, and the
    # "Sir, may I expand on 'Hello?'" prompt is more annoying than
    # useful. Better to stay quiet on these.
    if _is_trivial_question(original_question):
        logger.debug(
            "Elaboration: skipping trivial question %r", original_question[:60],
        )
        return None

    # Phase B opt-in: only fire elaboration when the recent user turns
    # share a topic. Off by default so existing Phase A behaviour is
    # unchanged unless explicitly enabled.
    if os.environ.get("ELABORATION_REQUIRE_TOPIC_COHERENCE", "").lower() in (
        "1", "true", "yes",
    ):
        try:
            window = int(os.environ.get("ELABORATION_TOPIC_WINDOW", "3"))
        except ValueError:
            window = 3
        try:
            threshold = float(
                os.environ.get("ELABORATION_TOPIC_THRESHOLD", "0.25")
            )
        except ValueError:
            threshold = 0.25
        if not _topic_coherent(messages, window=window, threshold=threshold):
            logger.debug(
                "Elaboration: skipping — last %d user turns not topic-coherent",
                window,
            )
            return None

    store = get_store()
    elab = await store.create(
        original_question=original_question,
        conversation_id=conversation_id,
    )

    # Fire-and-forget the worker. asyncio holds a strong reference for as long
    # as the task is running.
    asyncio.create_task(
        _run(
            elab.id,
            messages,
            memory_backend=memory_backend,
            original_question=original_question,
        )
    )
    return elab


__all__ = [
    "is_enabled",
    "spawn_elaboration",
]
