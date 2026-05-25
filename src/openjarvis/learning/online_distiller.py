"""Corpus-shim distiller (Round 5.9).

Lightweight end-to-end distillation: collects low-confidence reflections,
refusal-risk turns, and explicit-failure turns into a daily JSONL corpus
at ``~/.openjarvis/learning/corpus_YYYY-MM-DD.jsonl``. Compacts duplicates
by ``(sha256(user_text), day)`` so noisy retries don't bloat the file.

The artifact is ready-to-use training data for any future fine-tune
(local or cloud), with one record per failure-mode turn:

    {"ts": ..., "iso": ..., "session_id": ..., "user": "...",
     "assistant": "...", "confidence": 0.32, "success": false,
     "refusal_risk": true, "tags": ["off-topic"], "domain": "math",
     "trigger": "low_confidence|refusal|explicit_failure"}

The full `DistillationOrchestrator` (which requires teacher_engine +
benchmark_samples + 7 other singletons) is deferred behind a separate
`OPENJARVIS_DISTILLER_FULL` flag for a future round.

Public API:
    queue(reflection, *, user_text, assistant_text, session_id) -> Optional[Path]
    stats() → dict for /v1/_debug/agentic

Env gate: OPENJARVIS_ONLINE_DISTILLER_ENABLED (default false).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


_LOCK = threading.Lock()
_SEEN_TODAY: Dict[str, set] = {}  # date_str -> set of user_text hashes
_LAST_PATH: Optional[Path] = None


def _enabled() -> bool:
    return os.environ.get("OPENJARVIS_ONLINE_DISTILLER_ENABLED", "false").lower() in (
        "1", "true", "yes", "on",
    )


def _corpus_dir() -> Path:
    base = os.environ.get("OPENJARVIS_HOME", "").strip()
    d = Path(base) if base else Path.home() / ".openjarvis"
    d = d / "learning"
    try:
        d.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    return d


def _today_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _today_path() -> Path:
    return _corpus_dir() / f"corpus_{_today_str()}.jsonl"


def _hash_user(text: str) -> str:
    return hashlib.sha256((text or "").strip().lower().encode("utf-8", errors="replace")).hexdigest()[:16]


def _decide_trigger(reflection: Dict[str, Any], explicit_failed: bool = False) -> Optional[str]:
    """Return the trigger label if this turn should be queued, else None."""
    if explicit_failed:
        return "explicit_failure"
    if reflection.get("refusal_risk"):
        return "refusal"
    if not reflection.get("success", False):
        return "not_success"
    try:
        conf = float(reflection.get("confidence", 1.0))
    except Exception:
        conf = 1.0
    if conf < 0.5:
        return "low_confidence"
    return None


def queue(reflection: Dict[str, Any], *, user_text: str, assistant_text: str,
          session_id: str = "", explicit_failed: bool = False) -> Optional[Path]:
    """Decide-and-write. Returns the corpus file path on append, else None."""
    if not _enabled():
        return None
    if not user_text or not assistant_text:
        return None

    trigger = _decide_trigger(reflection, explicit_failed=explicit_failed)
    if trigger is None:
        return None

    # De-dup: one record per (user_hash, day). Different reflections of the
    # same query on the same day collapse to the first one written.
    day = _today_str()
    h = _hash_user(user_text)
    with _LOCK:
        seen = _SEEN_TODAY.setdefault(day, set())
        if h in seen:
            return None
        seen.add(h)

    record = {
        "ts": time.time(),
        "iso": datetime.now(timezone.utc).isoformat(),
        "session_id": session_id,
        "user": user_text[:1200],
        "assistant": assistant_text[:2400],
        "confidence": float(reflection.get("confidence", 0.0)),
        "success": bool(reflection.get("success", False)),
        "refusal_risk": bool(reflection.get("refusal_risk", False)),
        "follow_up_needed": bool(reflection.get("follow_up_needed", False)),
        "tags": reflection.get("tags", []),
        "domain": reflection.get("domain", "general"),
        "trigger": trigger,
        "user_hash": h,
    }
    path = _today_path()
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        global _LAST_PATH
        _LAST_PATH = path
    except Exception as exc:
        logger.debug("online_distiller: write failed: %s", exc)
        return None
    logger.info(
        "openjarvis.online_distiller.queued trigger=%s domain=%s conf=%.2f session=%s",
        trigger, record["domain"], record["confidence"], session_id,
    )
    return path


def stats() -> Dict[str, Any]:
    out: Dict[str, Any] = {"enabled": _enabled()}
    try:
        d = _corpus_dir()
        files = sorted(d.glob("corpus_*.jsonl"))
        out["dir"] = str(d)
        out["files"] = len(files)
        out["latest"] = str(files[-1]) if files else None
        # Count rows in today's file
        today = _today_path()
        if today.exists():
            with open(today, "r", encoding="utf-8") as f:
                n_today = sum(1 for _ in f)
            out["today_rows"] = n_today
            out["today_bytes"] = today.stat().st_size
        else:
            out["today_rows"] = 0
            out["today_bytes"] = 0
        # Per-trigger breakdown over all files (cheap; few files)
        triggers: Dict[str, int] = {}
        domains: Dict[str, int] = {}
        for fp in files[-3:]:  # last 3 days only — keeps probe fast
            try:
                with open(fp, "r", encoding="utf-8") as f:
                    for line in f:
                        try:
                            rec = json.loads(line)
                            triggers[rec.get("trigger", "?")] = triggers.get(rec.get("trigger", "?"), 0) + 1
                            domains[rec.get("domain", "?")] = domains.get(rec.get("domain", "?"), 0) + 1
                        except Exception:
                            continue
            except Exception:
                continue
        out["triggers_last_3d"] = triggers
        out["domains_last_3d"] = domains
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
    return out


def recent_failures(domain: Optional[str] = None, limit: int = 50) -> List[Dict[str, Any]]:
    """Return recent records (used by auto-skill-proposer + evolver)."""
    out: List[Dict[str, Any]] = []
    try:
        d = _corpus_dir()
        files = sorted(d.glob("corpus_*.jsonl"))[-3:]
        for fp in files:
            try:
                with open(fp, "r", encoding="utf-8") as f:
                    for line in f:
                        try:
                            rec = json.loads(line)
                            if domain and rec.get("domain") != domain:
                                continue
                            out.append(rec)
                        except Exception:
                            continue
            except Exception:
                continue
    except Exception:
        pass
    return out[-limit:]
