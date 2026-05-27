"""Learned prompt hints — universal action target (Round 8).

Companion to ``learned_intents``. When the promoter daemon clusters
disavowals in a domain that has NO server-side tool pre-executor
(``history``, ``science``, ``code``, ``math``, ``language``, ``logic``,
``multistep``, ``automation``, ``geography``, ``general``, etc.), there
is no tool to fire — but the failure is still actionable. We auto-draft
a one-paragraph HINT and inject it into the LLM's system context the
next time a similar query is seen. The hint nudges the LLM to use the
fallback path (web_search, knowledge_search, honest reasoning) rather
than disavow again.

Storage: ``~/.openjarvis/learned_prompt_hints.json`` shape::

    {
      "history": [
        {
          "regex": "^(?=.*\\bempire\\b)(?=.*\\bfall\\b).+",
          "hint_text": "When the user asks about ...",
          "member_count": 3,
          "promoted_at": "2026-05-27T..."
        },
        ...
      ],
      "science": [ ... ]
    }

Env:
    OPENJARVIS_LEARNED_PROMPT_HINTS_ENABLED  default true
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


_LOCK = threading.Lock()


def _enabled() -> bool:
    return os.environ.get("OPENJARVIS_LEARNED_PROMPT_HINTS_ENABLED", "true").lower() in (
        "1", "true", "yes", "on",
    )


def _store_path() -> Path:
    base = os.environ.get("OPENJARVIS_HOME", "").strip()
    d = Path(base) if base else Path.home() / ".openjarvis"
    try:
        d.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    return d / "learned_prompt_hints.json"


def _load() -> Dict[str, List[Dict[str, Any]]]:
    p = _store_path()
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save(data: Dict[str, List[Dict[str, Any]]]) -> None:
    try:
        _store_path().write_text(
            json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8",
        )
    except Exception as exc:
        logger.warning("learned_prompt_hints: save failed: %s", exc)


# ---------------------------------------------------------------------------
# Hint drafting — deterministic, no LLM call
# ---------------------------------------------------------------------------

# Domain → suggested fallback tactic. Keep this table conservative: we
# only nudge toward tools the LLM provably has, and the worst case is
# "use honest reasoning". The hint never hardcodes an answer.
_DOMAIN_FALLBACK_TACTIC: Dict[str, str] = {
    "history":    "use web_search or knowledge_search to find an authoritative date/fact",
    "science":    "use web_search, knowledge_search, or compute it from first principles",
    "geography":  "use web_search or knowledge_search for an authoritative location/fact",
    "math":       "compute the result step-by-step or use the code-exec tool if available",
    "code":       "use web_search for docs/stack-trace context, or reason through the code yourself",
    "language":   "answer from linguistic knowledge or use web_search for a current usage example",
    "logic":      "reason step-by-step in the response — no external tool needed",
    "automation": "check the n8n / webhook tools or describe the workflow the user should build",
    "multistep":  "break the question into numbered steps and answer each in turn",
    "general":    "use web_search if the answer is factual, otherwise reason honestly",
}


def _domain_tactic(domain: str) -> str:
    return _DOMAIN_FALLBACK_TACTIC.get(
        domain, _DOMAIN_FALLBACK_TACTIC["general"],
    )


def draft_hint_for_cluster(cluster: List[Dict[str, Any]], domain: str) -> str:
    """Produce a one-paragraph hint to inject as a system message when a
    similar query is seen again. Template-driven (deterministic — no
    LLM call, no API cost)."""
    samples = [c.get("user_text", "")[:120] for c in cluster[:3] if c.get("user_text")]
    sample_blob = " | ".join(samples) if samples else "(no samples)"
    tactic = _domain_tactic(domain)
    n = len(cluster)
    return (
        f"PAST-FAILURE HINT (domain={domain}, {n} prior disavowals matched "
        f"this pattern; sample phrasings: {sample_blob}). "
        f"When you see a question like this, DO NOT say you can't help, "
        f"don't have a tool, or lack the capability. Instead: {tactic}. "
        f"If the answer is genuinely uncertain, say so honestly with what "
        f"you do know — never claim the capability itself is missing."
    )


# ---------------------------------------------------------------------------
# Promotion API — called by learned_intents.promote_from_disavowals
# ---------------------------------------------------------------------------

def add_hint(
    *,
    domain: str,
    regex: str,
    hint_text: str,
    member_count: int,
) -> bool:
    """Add a new hint for `domain`. Returns True if added, False if a hint
    with the same regex already exists for this domain."""
    if not _enabled():
        return False
    with _LOCK:
        store = _load()
        bucket = store.setdefault(domain, [])
        for h in bucket:
            if h.get("regex") == regex:
                return False
        bucket.append({
            "regex": regex,
            "hint_text": hint_text,
            "member_count": member_count,
            "promoted_at": datetime.now(timezone.utc).isoformat(),
        })
        _save(store)
    logger.warning(
        "openjarvis.learned_prompt_hints.added domain=%s regex=%r members=%d",
        domain, regex, member_count,
    )
    return True


# ---------------------------------------------------------------------------
# Runtime lookup — called by intent_preexec on every chat turn
# ---------------------------------------------------------------------------

def lookup(text: str) -> Optional[Dict[str, Any]]:
    """If any stored hint's regex matches `text`, return the hint dict
    plus its domain: ``{"domain", "regex", "hint_text", ...}``. Else None."""
    if not text or not _enabled():
        return None
    store = _load()
    if not store:
        return None
    for domain, hints in store.items():
        for h in hints:
            rs = h.get("regex")
            if not rs:
                continue
            try:
                if re.search(rs, text, re.IGNORECASE):
                    return {"domain": domain, **h}
            except re.error:
                continue
    return None


def snapshot() -> Dict[str, Any]:
    store = _load()
    return {
        "enabled": _enabled(),
        "domains": {d: len(hs) for d, hs in store.items()},
        "total_hints": sum(len(hs) for hs in store.values()),
        "store_path": str(_store_path()),
    }
