"""Predictive answer cache (Round 4.1 — extra impressive layer).

When the reflector scores a turn with confidence >= 0.85 AND success=True,
we hash (normalized_query, domain) → answer and stash it. On the next turn,
if the same hash is seen within the TTL, we can skip the LLM call entirely
and return the cached answer.

The cache key is intentionally semantic-light (just normalized whitespace +
lowercase + punctuation strip) — we want hits only on near-identical
queries, not paraphrases. A wrong cache hit is worse than no hit.

Why this matters: in voice/chat traffic, users routinely re-ask the same
thing ("what's the weather", "what time is it", "open settings"). Each
hit saves ~300-800ms and one LLM call.

Storage: ~/.openjarvis/answer_cache.jsonl (append-only, periodically
compacted) + in-memory dict for hot-path O(1) lookup.

Env gate: OPENJARVIS_ANSWER_CACHE_ENABLED (default false until verified).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


_TTL_SECONDS = 7 * 24 * 60 * 60  # 7 days
_MAX_ENTRIES = 2000
_MIN_CONFIDENCE = 0.85
_LOCK = threading.Lock()
_CACHE: Dict[str, Dict[str, Any]] = {}
_LOADED = False


def _enabled() -> bool:
    return os.environ.get("OPENJARVIS_ANSWER_CACHE_ENABLED", "false").lower() in (
        "1", "true", "yes", "on",
    )


def _cache_path() -> Path:
    base = os.environ.get("OPENJARVIS_HOME", "").strip()
    d = Path(base) if base else Path.home() / ".openjarvis"
    try:
        d.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    return d / "answer_cache.jsonl"


_NORMALIZE_RE = re.compile(r"[^a-z0-9\s]+")
_WS_RE = re.compile(r"\s+")


def _normalize(query: str) -> str:
    if not query:
        return ""
    s = _NORMALIZE_RE.sub(" ", query.lower())
    s = _WS_RE.sub(" ", s).strip()
    return s[:400]


def _make_key(query: str, domain: str) -> str:
    norm = _normalize(query)
    if not norm:
        return ""
    raw = f"{domain or 'general'}::{norm}".encode("utf-8", errors="replace")
    return hashlib.sha256(raw).hexdigest()[:24]


def _ensure_loaded() -> None:
    global _LOADED
    if _LOADED:
        return
    with _LOCK:
        if _LOADED:
            return
        try:
            path = _cache_path()
            if path.exists():
                with open(path, "r", encoding="utf-8") as f:
                    for line in f:
                        try:
                            entry = json.loads(line)
                            k = entry.get("key")
                            if k:
                                _CACHE[k] = entry
                        except Exception:
                            continue
        except Exception as exc:
            logger.debug("answer_cache: load failed: %s", exc)
        _LOADED = True


def lookup(query: str, domain: str = "general") -> Optional[Dict[str, Any]]:
    """Returns cached entry if a fresh hit exists, else None.

    Entry shape: {answer, confidence, ts, hits, domain, query}
    """
    if not _enabled() or not query:
        return None
    _ensure_loaded()
    key = _make_key(query, domain)
    if not key:
        return None
    with _LOCK:
        entry = _CACHE.get(key)
        if not entry:
            return None
        if (time.time() - entry.get("ts", 0)) > _TTL_SECONDS:
            _CACHE.pop(key, None)
            return None
        entry["hits"] = int(entry.get("hits", 0)) + 1
        entry["last_hit"] = time.time()
    logger.info(
        "openjarvis.answer_cache.hit key=%s domain=%s hits=%d",
        key, domain, entry["hits"],
    )
    return entry


def maybe_store(query: str, answer: str, *, domain: str = "general",
                confidence: float = 0.0, success: bool = False) -> bool:
    """Store iff reflection confidence/success qualify. Returns True if stored."""
    if not _enabled():
        return False
    if not query or not answer:
        return False
    if confidence < _MIN_CONFIDENCE or not success:
        return False
    if len(answer) > 8000:
        return False  # skip oversized — likely not cache-worthy
    _ensure_loaded()
    key = _make_key(query, domain)
    if not key:
        return False
    entry = {
        "key": key,
        "query": query[:240],
        "domain": domain or "general",
        "answer": answer,
        "confidence": float(confidence),
        "ts": time.time(),
        "iso": datetime.now(timezone.utc).isoformat(),
        "hits": 0,
    }
    with _LOCK:
        _CACHE[key] = entry
        # Evict oldest if over budget
        if len(_CACHE) > _MAX_ENTRIES:
            victims = sorted(_CACHE.items(), key=lambda kv: kv[1].get("ts", 0))
            for k, _ in victims[: _MAX_ENTRIES // 8]:
                _CACHE.pop(k, None)
    try:
        with open(_cache_path(), "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as exc:
        logger.debug("answer_cache: append failed: %s", exc)
    logger.info(
        "openjarvis.answer_cache.store key=%s domain=%s conf=%.2f",
        key, domain, confidence,
    )
    return True


def stats() -> Dict[str, Any]:
    """Snapshot of cache health — used by /v1/_debug/agentic + dashboard."""
    _ensure_loaded()
    with _LOCK:
        entries = list(_CACHE.values())
    total_hits = sum(int(e.get("hits", 0)) for e in entries)
    by_domain: Dict[str, int] = {}
    for e in entries:
        by_domain[e.get("domain", "general")] = by_domain.get(e.get("domain", "general"), 0) + 1
    top = sorted(entries, key=lambda e: int(e.get("hits", 0)), reverse=True)[:5]
    return {
        "enabled": _enabled(),
        "size": len(entries),
        "total_hits": total_hits,
        "by_domain": by_domain,
        "top_hits": [
            {"query": e.get("query"), "hits": e.get("hits", 0), "domain": e.get("domain")}
            for e in top if int(e.get("hits", 0)) > 0
        ],
    }


def clear() -> int:
    """Wipe in-memory + disk. Returns prior size."""
    _ensure_loaded()
    with _LOCK:
        n = len(_CACHE)
        _CACHE.clear()
    try:
        _cache_path().unlink(missing_ok=True)
    except Exception:
        pass
    return n
