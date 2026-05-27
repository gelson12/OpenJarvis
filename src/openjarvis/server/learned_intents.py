"""Learned intents — the SECOND half of the self-improvement loop.

THE WHOLE POINT
---------------
The disavowal_detector logs every "I don't have that tool" event with
the category (email/calendar/watch/unknown) and the user's exact
phrasing. This module READS that log periodically, clusters similar
failing phrasings, and AUTO-PROMOTES a new regex pattern into
``~/.openjarvis/learned_intents.json``.

intent_preexec.py checks the learned-intents store BEFORE its hardcoded
regex on every chat turn. So:

  Turn 1: user says "verify the last email today"
          → no match in hardcoded regex
          → LLM disavows
          → disavowal_detector logs it (category=email)

  (auto-promoter wakes up, sees 3+ similar disavowals tagged "email"
   with the verb "verify", writes a new pattern into learned_intents.json)

  Turn 2 (or 3rd, or 5th): user says same/similar thing
          → intent_preexec checks learned_intents FIRST
          → matches the auto-learned pattern
          → pre-executes outlook_list_messages directly
          → REAL DATA returned
          → no disavowal possible

Cluster threshold: 3 disavowals with overlapping vocabulary (>=50%
shared significant tokens) → promote.

Public API:
    load_patterns() → dict[category, list[regex_pattern_str]]
    match_category(text) → "email" | "calendar" | "watch" | None
    promote_from_disavowals() → list of newly-promoted patterns
    start_promoter_daemon() → starts the background loop

Env:
    OPENJARVIS_LEARNED_INTENTS_ENABLED       default true
    OPENJARVIS_LEARNED_PROMOTE_THRESHOLD     default 3 (clusters of this size promote)
    OPENJARVIS_LEARNED_PROMOTER_INTERVAL_SEC default 300 (5 min cadence)
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)


_LOCK = threading.Lock()
_PROMOTER_THREAD: Optional[threading.Thread] = None


_STOP_WORDS = frozenset({
    "the", "a", "an", "and", "or", "but", "if", "then", "with", "for", "to",
    "of", "in", "on", "at", "by", "from", "as", "is", "are", "was", "were",
    "be", "been", "have", "has", "had", "do", "does", "did", "i", "me", "my",
    "you", "your", "we", "us", "they", "them", "this", "that", "these",
    "those", "can", "could", "would", "should", "will", "shall", "may",
    "might", "must", "please", "today", "tomorrow",
})

_TOK_RE = re.compile(r"[a-z][a-z0-9'-]{1,}")


def _enabled() -> bool:
    return os.environ.get("OPENJARVIS_LEARNED_INTENTS_ENABLED", "true").lower() in (
        "1", "true", "yes", "on",
    )


def _threshold() -> int:
    try:
        return max(2, int(os.environ.get("OPENJARVIS_LEARNED_PROMOTE_THRESHOLD", "3")))
    except Exception:
        return 3


def _interval() -> int:
    try:
        return max(60, int(os.environ.get("OPENJARVIS_LEARNED_PROMOTER_INTERVAL_SEC", "300")))
    except Exception:
        return 300


def _store_path() -> Path:
    base = os.environ.get("OPENJARVIS_HOME", "").strip()
    d = Path(base) if base else Path.home() / ".openjarvis"
    try:
        d.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    return d / "learned_intents.json"


# ---------------------------------------------------------------------------
# Read / write store
# ---------------------------------------------------------------------------

def _load_store() -> Dict[str, Any]:
    p = _store_path()
    if not p.exists():
        return {"patterns": {}, "history": []}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {"patterns": {}, "history": []}


def _save_store(data: Dict[str, Any]) -> None:
    try:
        _store_path().write_text(
            json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8",
        )
    except Exception as exc:
        logger.warning("learned_intents: save failed: %s", exc)


def load_patterns() -> Dict[str, List[str]]:
    """Return {category: [regex_str, ...]} for the intent_preexec to check."""
    if not _enabled():
        return {}
    data = _load_store()
    return data.get("patterns", {}) or {}


def match_category(text: str) -> Optional[str]:
    """Returns the first category whose learned pattern matches `text`, or
    None. Cheap hot-path call — used per chat turn by intent_preexec."""
    if not text or not _enabled():
        return None
    patterns = load_patterns()
    if not patterns:
        return None
    for category, regex_strs in patterns.items():
        for rs in regex_strs:
            try:
                if re.search(rs, text, re.IGNORECASE):
                    return category
            except re.error:
                continue
    return None


# ---------------------------------------------------------------------------
# Clustering + promotion
# ---------------------------------------------------------------------------

def _significant_tokens(text: str) -> Set[str]:
    return {
        t for t in _TOK_RE.findall((text or "").lower())
        if t not in _STOP_WORDS and len(t) >= 3
    }


def _jaccard(a: Set[str], b: Set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / max(len(a | b), 1)


def _cluster_disavowals(
    disavowals: List[Dict[str, Any]], category: str,
) -> List[List[Dict[str, Any]]]:
    """Naive greedy clustering by Jaccard >= 0.5 on significant tokens."""
    by_cat = [d for d in disavowals if d.get("inferred_category") == category]
    if not by_cat:
        return []
    tokens = [_significant_tokens(d.get("user_text", "")) for d in by_cat]
    clusters: List[List[int]] = []
    used: Set[int] = set()
    for i, ti in enumerate(tokens):
        if i in used or not ti:
            continue
        cluster_idx = [i]
        used.add(i)
        for j in range(i + 1, len(tokens)):
            if j in used:
                continue
            if _jaccard(ti, tokens[j]) >= 0.5:
                cluster_idx.append(j)
                used.add(j)
        clusters.append(cluster_idx)
    return [[by_cat[i] for i in c] for c in clusters]


def _pattern_from_cluster(
    cluster: List[Dict[str, Any]],
) -> Optional[str]:
    """Extract a single discriminative regex from the cluster's user
    texts. Strategy: find the common significant tokens (intersection)
    and require them to appear in the matched text, separated by any
    other words. Falls back to None if no signal."""
    token_sets = [_significant_tokens(d.get("user_text", "")) for d in cluster]
    if not token_sets:
        return None
    common = set.intersection(*token_sets) if token_sets else set()
    # Drop noisy common words that nearly always appear
    common.discard("email")
    common.discard("emails")
    common.discard("calendar")
    common.discard("meeting")
    common.discard("meetings")
    # Keep ones that are distinctive (verb-like)
    distinctive = sorted(common)
    if not distinctive:
        return None
    # Build: \bword1\b.*?\bword2\b (order-independent within reason)
    # Pick the 2-3 most-discriminative (longest) tokens.
    distinctive = sorted(distinctive, key=lambda t: -len(t))[:3]
    # Use lookaheads for order-independence
    parts = [f"(?=.*\\b{re.escape(t)}\\b)" for t in distinctive]
    pattern = "^" + "".join(parts) + ".+"
    return pattern


def promote_from_disavowals() -> List[Dict[str, Any]]:
    """Read recent disavowals, cluster them, and add promoted patterns
    to the store. Returns the list of newly-promoted patterns (each:
    {category, pattern, member_count, sample_phrases})."""
    if not _enabled():
        return []
    try:
        from openjarvis.server import disavowal_detector as _dd
    except Exception:
        return []
    disavowals = _dd.read_recent(limit=500)
    if not disavowals:
        return []
    threshold = _threshold()
    new_promotions: List[Dict[str, Any]] = []
    with _LOCK:
        store = _load_store()
        patterns = store.setdefault("patterns", {})
        history = store.setdefault("history", [])
        for category in ("email", "calendar", "watch"):
            clusters = _cluster_disavowals(disavowals, category)
            for cluster in clusters:
                if len(cluster) < threshold:
                    continue
                pat = _pattern_from_cluster(cluster)
                if not pat:
                    continue
                existing = patterns.get(category, [])
                if pat in existing:
                    continue  # already promoted
                existing.append(pat)
                patterns[category] = existing
                promo = {
                    "promoted_at": datetime.now(timezone.utc).isoformat(),
                    "category": category,
                    "pattern": pat,
                    "member_count": len(cluster),
                    "sample_phrases": [c.get("user_text", "")[:120] for c in cluster[:5]],
                }
                history.append(promo)
                new_promotions.append(promo)
                logger.warning(
                    "openjarvis.learned_intents.promoted category=%s pattern=%r members=%d",
                    category, pat, len(cluster),
                )
        if new_promotions:
            _save_store(store)
    return new_promotions


# ---------------------------------------------------------------------------
# Background daemon
# ---------------------------------------------------------------------------

def _promoter_loop() -> None:
    interval = _interval()
    logger.info("openjarvis.learned_intents.promoter started interval=%ds", interval)
    while True:
        try:
            time.sleep(interval)
        except Exception:
            time.sleep(300)
        if not _enabled():
            continue
        try:
            promotions = promote_from_disavowals()
            if promotions:
                logger.warning(
                    "openjarvis.learned_intents.promoter_run promoted=%d",
                    len(promotions),
                )
        except Exception as exc:
            logger.warning("learned_intents.promoter run failed: %s", exc)


def start_promoter_daemon() -> None:
    """Idempotent — starts the daemon thread on first call."""
    global _PROMOTER_THREAD
    if _PROMOTER_THREAD is not None and _PROMOTER_THREAD.is_alive():
        return
    if not _enabled():
        logger.info("openjarvis.learned_intents.promoter disabled by env")
        return
    t = threading.Thread(
        target=_promoter_loop,
        name="openjarvis-learned-intents-promoter",
        daemon=True,
    )
    t.start()
    _PROMOTER_THREAD = t


def snapshot() -> Dict[str, Any]:
    store = _load_store()
    patterns = store.get("patterns", {})
    history = store.get("history", [])
    return {
        "enabled": _enabled(),
        "threshold": _threshold(),
        "interval_sec": _interval(),
        "categories": {cat: len(pats) for cat, pats in patterns.items()},
        "total_patterns": sum(len(p) for p in patterns.values()),
        "total_promotions_ever": len(history),
        "recent_promotions": history[-5:],
        "daemon_running": _PROMOTER_THREAD is not None and _PROMOTER_THREAD.is_alive(),
    }
