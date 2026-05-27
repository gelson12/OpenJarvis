"""Obsidian-Mind index + semantic-search helper (Round 8.7-C).

Why: the planned ``_cluster_disavowals`` in ``learned_intents`` uses
Jaccard token overlap, which misses semantic equivalents (e.g. "When
was the Roman Empire's collapse?" vs "What year did Rome fall?" share
almost no tokens but are the same failure). Obsidian Mind hosts an
embedding-indexed vault on Railway — pushing disavowals into it and
querying for top-K similar past failures gives us a much better cluster
candidate set, with the Jaccard path remaining as the offline fallback.

This module is **synchronous, best-effort, never raises**. If
``OBSIDIAN_MIND_URL`` is unset or the service is unreachable, every
public function returns the empty/None result silently and we fall back
to the existing local clustering.

Env:
    OBSIDIAN_MIND_URL                       — base URL (Railway internal preferred)
    OBSIDIAN_MIND_TOKEN                     — optional bearer token
    OPENJARVIS_LEARNING_MIND_SEARCH_ENABLED — default true

Public API:
    index_disavowal(event)                  — POST to obsidian-mind notes
    semantic_cluster_candidates(text, k=20) — GET search → list of events
    snapshot()                              — for /v1/_debug/agentic
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


_URL_ENV = "OBSIDIAN_MIND_URL"
_TOKEN_ENV = "OBSIDIAN_MIND_TOKEN"
_FLAG_ENV = "OPENJARVIS_LEARNING_MIND_SEARCH_ENABLED"

_NOTE_PREFIX = "jarvis-disavowals"


def _enabled() -> bool:
    if os.environ.get(_FLAG_ENV, "true").lower() not in ("1", "true", "yes", "on"):
        return False
    return bool(os.environ.get(_URL_ENV, "").strip())


def _base_url() -> str:
    return os.environ.get(_URL_ENV, "").strip().rstrip("/")


def _headers() -> Dict[str, str]:
    h: Dict[str, str] = {"Content-Type": "application/json"}
    tok = os.environ.get(_TOKEN_ENV, "").strip()
    if tok:
        h["Authorization"] = f"Bearer {tok}"
    return h


_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slug(text: str, max_len: int = 50) -> str:
    s = _SLUG_RE.sub("-", (text or "").lower()).strip("-")
    return (s or "x")[:max_len]


def _httpx():
    try:
        import httpx  # type: ignore
        return httpx
    except Exception as exc:
        logger.debug("learning_mind: httpx unavailable (%s)", exc)
        return None


def index_disavowal(event: Dict[str, Any]) -> None:
    """POST a disavowal note to obsidian-mind. Best-effort, never raises."""
    if not _enabled():
        return
    httpx = _httpx()
    if httpx is None:
        return
    base = _base_url()
    if not base:
        return
    ts = int(float(event.get("ts") or 0))
    domain = event.get("inferred_domain") or event.get("inferred_category") or "general"
    slug = _slug(event.get("user_text") or "")
    path = f"{_NOTE_PREFIX}/{domain}/{ts}-{slug}.md"
    body = (
        f"---\n"
        f"domain: {domain}\n"
        f"ts: {ts}\n"
        f"iso: {event.get('iso') or ''}\n"
        f"---\n\n"
        f"# Disavowal\n\n"
        f"**User:** {event.get('user_text') or ''}\n\n"
        f"**Assistant:** {event.get('assistant_text') or ''}\n\n"
        f"**Injected tool groups:** {event.get('injected_tool_groups') or []}\n"
    )
    try:
        with httpx.Client(timeout=5.0) as client:
            client.post(
                f"{base}/api/notes/{path}",
                headers=_headers(),
                json={"content": body},
            )
    except Exception as exc:
        logger.debug("learning_mind.index_disavowal failed: %s", exc)


def semantic_cluster_candidates(
    text: str, *, k: int = 20, domain: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Query obsidian-mind for past disavowals semantically similar to
    `text`. Returns a list of minimal event-shaped dicts
    (``{user_text, inferred_domain, iso}``) or an empty list on any
    failure. Caller should fall back to local clustering when empty."""
    if not _enabled() or not text:
        return []
    httpx = _httpx()
    if httpx is None:
        return []
    base = _base_url()
    if not base:
        return []
    path_filter = f"{_NOTE_PREFIX}/{domain}/" if domain else f"{_NOTE_PREFIX}/"
    try:
        with httpx.Client(timeout=5.0) as client:
            resp = client.get(
                f"{base}/api/search",
                headers=_headers(),
                params={"q": text[:300], "path": path_filter, "limit": k},
            )
            if resp.status_code != 200:
                return []
            data = resp.json()
    except Exception as exc:
        logger.debug("learning_mind.semantic_cluster_candidates failed: %s", exc)
        return []

    # Tolerate either {"results": [...]} or a bare list response.
    rows = data.get("results") if isinstance(data, dict) else data
    if not isinstance(rows, list):
        return []

    out: List[Dict[str, Any]] = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        # Try to extract the user_text from frontmatter or body.
        body = r.get("content") or r.get("body") or ""
        m = re.search(r"\*\*User:\*\*\s*(.+?)\n", body)
        user_text = (m.group(1).strip() if m else (r.get("title") or ""))[:600]
        fm = r.get("frontmatter") or {}
        out.append({
            "user_text": user_text,
            "inferred_domain": fm.get("domain") or domain or "general",
            "iso": fm.get("iso") or "",
        })
    return out


def snapshot() -> Dict[str, Any]:
    return {
        "enabled": _enabled(),
        "url": _base_url(),
        "note_prefix": _NOTE_PREFIX,
    }
