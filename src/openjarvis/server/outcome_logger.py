"""Round 20 Piece 4 — Outcome logging + reward shaping.

The reward signal that turns Round 8/9's silent loop into visible
self-improvement. Every chat turn that involves a tool call gets
logged with:

  * the user's query (what they asked for)
  * the top-K tools the router surfaced (what we suggested)
  * which tool (if any) the LLM actually called
  * whether the call succeeded
  * whether the user followed up with a complaint (frustration signal)
  * latency

A background daemon periodically computes per-(query-cluster, tool)
success rates and writes a ``tool_affinity.json`` map. The tool router
reads that map when ranking, so tools that historically worked for
similar queries get a score bias on top of pure semantic similarity.

This is the ACTUAL feedback loop:

  1. User asks something                          → query
  2. Tool router suggests tools                   → suggestion log
  3. LLM picks one (or none)                      → choice log
  4. Tool runs / fails                            → outcome log
  5. User reacts (next turn: complaint? satisfied?) → reward signal
  6. Background daemon updates affinity map       → tool ranking updated
  7. Next similar query gets better-ranked tools  → visible improvement

Best-effort throughout: every write is wrapped in try/except, every
read is allowed to return empty. The chat path keeps working even
when the logger is fully broken.

Env flags
~~~~~~~~~
``OPENJARVIS_OUTCOME_LOGGING_ENABLED`` (default ``true``)
    Master switch for the logger and the affinity feedback.
``OPENJARVIS_OUTCOME_DAEMON_INTERVAL_SEC`` (default ``1800`` = 30 min)
    How often the background daemon recomputes the affinity map.
``OPENJARVIS_OUTCOME_AFFINITY_BIAS_WEIGHT`` (default ``0.3``)
    How much weight to give historical affinity (0.0 = pure semantic,
    1.0 = pure affinity). Clamped to [0, 1].
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


_LOCK = threading.Lock()
_DAEMON_THREAD: Optional[threading.Thread] = None


# ---------------------------------------------------------------------------
# Env helpers
# ---------------------------------------------------------------------------


def _enabled() -> bool:
    return os.environ.get("OPENJARVIS_OUTCOME_LOGGING_ENABLED", "true").lower() in (
        "1", "true", "yes", "on",
    )


def _interval_sec() -> int:
    try:
        return max(60, int(os.environ.get(
            "OPENJARVIS_OUTCOME_DAEMON_INTERVAL_SEC", "1800",
        )))
    except Exception:
        return 1800


def _affinity_weight() -> float:
    try:
        v = float(os.environ.get(
            "OPENJARVIS_OUTCOME_AFFINITY_BIAS_WEIGHT", "0.3",
        ))
        return max(0.0, min(1.0, v))
    except Exception:
        return 0.3


def _home() -> Path:
    base = os.environ.get("OPENJARVIS_HOME", "").strip()
    d = Path(base) if base else Path.home() / ".openjarvis"
    try:
        d.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    return d


def _log_path() -> Path:
    return _home() / "outcomes.jsonl"


def _affinity_path() -> Path:
    return _home() / "tool_affinity.json"


def _heartbeat_path() -> Path:
    return _home() / "_outcome_daemon_heartbeat.json"


# ---------------------------------------------------------------------------
# Recording API — called from the chat hook
# ---------------------------------------------------------------------------


def record_turn(
    *,
    query: str,
    tools_offered: Optional[List[str]] = None,
    tool_called: Optional[str] = None,
    tool_success: Optional[bool] = None,
    llm_disavowed: bool = False,
    latency_ms: Optional[int] = None,
    session_id: str = "",
) -> None:
    """Append one turn record to outcomes.jsonl + best-effort PG mirror.

    Every arg is optional except ``query`` so the chat hook can call
    this even when partial information is available (e.g. LLM
    disavowed without ever attempting a tool call).
    """
    if not _enabled() or not query:
        return
    event = {
        "ts": time.time(),
        "iso": datetime.now(timezone.utc).isoformat(),
        "query": (query or "").strip()[:600],
        "tools_offered": list(tools_offered or [])[:20],
        "tool_called": tool_called,
        "tool_success": tool_success,
        "llm_disavowed": bool(llm_disavowed),
        "latency_ms": latency_ms,
        "session_id": session_id,
    }
    try:
        with _LOCK:
            with open(_log_path(), "a", encoding="utf-8") as f:
                f.write(json.dumps(event, ensure_ascii=False) + "\n")
    except Exception as exc:
        logger.debug("outcome_logger: jsonl write failed: %s", exc)

    # Best-effort Postgres mirror — async so the chat path never waits.
    try:
        threading.Thread(
            target=_pg_mirror_outcome,
            args=(event,),
            name="openjarvis-outcome-pg-mirror",
            daemon=True,
        ).start()
    except Exception:
        pass


def _pg_mirror_outcome(event: Dict[str, Any]) -> None:
    """Mirror one outcome row to Postgres if DATABASE_URL is set.

    Schema is created on first call (best-effort, idempotent).
    """
    try:
        dsn = os.environ.get("DATABASE_URL", "").strip()
        if not dsn:
            return
        try:
            import psycopg  # type: ignore
        except Exception:
            return
        with psycopg.connect(dsn, connect_timeout=5) as conn:  # type: ignore[arg-type]
            with conn.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS learning_outcomes (
                        id BIGSERIAL PRIMARY KEY,
                        ts DOUBLE PRECISION NOT NULL,
                        iso TEXT NOT NULL,
                        query TEXT,
                        tools_offered TEXT,
                        tool_called TEXT,
                        tool_success BOOLEAN,
                        llm_disavowed BOOLEAN,
                        latency_ms INTEGER,
                        session_id TEXT
                    );
                    CREATE INDEX IF NOT EXISTS learning_outcomes_tool_idx
                        ON learning_outcomes(tool_called);
                    CREATE INDEX IF NOT EXISTS learning_outcomes_ts_idx
                        ON learning_outcomes(ts);
                """)
                cur.execute(
                    """
                    INSERT INTO learning_outcomes
                        (ts, iso, query, tools_offered, tool_called,
                         tool_success, llm_disavowed, latency_ms, session_id)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        float(event.get("ts") or 0.0),
                        event.get("iso") or "",
                        event.get("query"),
                        json.dumps(event.get("tools_offered") or []),
                        event.get("tool_called"),
                        event.get("tool_success"),
                        event.get("llm_disavowed"),
                        event.get("latency_ms"),
                        event.get("session_id") or "",
                    ),
                )
            conn.commit()
    except Exception as exc:
        logger.debug("outcome_logger.pg_mirror failed: %s", exc)


# ---------------------------------------------------------------------------
# Reading API
# ---------------------------------------------------------------------------


def read_recent(limit: int = 1000) -> List[Dict[str, Any]]:
    """Read the most-recent N outcome records from JSONL. Best-effort."""
    p = _log_path()
    if not p.exists():
        return []
    out: List[Dict[str, Any]] = []
    try:
        with open(p, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    out.append(rec)
                except Exception:
                    continue
    except Exception as exc:
        logger.debug("outcome_logger.read_recent: %s", exc)
        return []
    return out[-limit:]


# ---------------------------------------------------------------------------
# Affinity computation (the reward signal)
# ---------------------------------------------------------------------------


_BUCKET_STOPWORDS = frozenset({
    "the", "a", "an", "any", "some", "my", "me", "you", "your", "our",
    "this", "that", "these", "those", "it", "is", "are", "was", "were",
    "be", "been", "to", "of", "on", "in", "at", "by", "for", "with",
    "from", "and", "or", "but", "so", "if", "when", "what", "who", "how",
    "today", "tomorrow", "now", "please", "sir",
})


def _bucket_query(query: str) -> str:
    """Cluster queries into coarse buckets by their first meaningful
    content word. Fast, deterministic, no embeddings needed for this
    rollup. The router still does fine-grained semantic ranking — this
    bucket is just for outcome aggregation.
    """
    import re
    if not query:
        return "_unknown"
    # Lowercase, drop common stop/filler words at the start
    q = query.lower().strip()
    q = re.sub(
        r"^(?:hey\s+|ok\s+|okay\s+|please\s+|jarvis[,\s]+|"
        r"can\s+you\s+|could\s+you\s+|would\s+you\s+|i\s+(?:want|need|wanna)\s+|"
        r"i'?d\s+like\s+to\s+|let'?s\s+|tell\s+me\s+|show\s+me\s+|give\s+me\s+|"
        r"check\s+|verify\s+|find\s+me\s+|search\s+for\s+|look\s+(?:up\s+)?)+",
        "", q,
    )
    # Iterate tokens and skip common stopwords/articles so "show me the
    # news" -> "news" (not "the"), "any meetings tomorrow" -> "meetings"
    # (not "any"), etc.
    for tok in re.findall(r"[a-z][a-z0-9_]{2,}", q):
        if tok not in _BUCKET_STOPWORDS:
            return tok
    return "_unknown"


def compute_affinity_map(*, min_observations: int = 3) -> Dict[str, Dict[str, float]]:
    """Compute success rate per (query_bucket, tool_called) pair.

    Returns ``{bucket: {tool_name: success_rate}}`` for buckets with
    at least ``min_observations`` calls. The router uses this map to
    bias its semantic ranking — historically-successful tools rise.
    """
    recs = read_recent(limit=5000)
    if not recs:
        return {}
    # Aggregate
    bucket_tool_counts: Dict[Tuple[str, str], List[int]] = defaultdict(lambda: [0, 0])
    for r in recs:
        tool = r.get("tool_called")
        if not tool:
            continue
        bucket = _bucket_query(r.get("query") or "")
        success = bool(r.get("tool_success"))
        bucket_tool_counts[(bucket, tool)][0] += 1                       # total
        if success:
            bucket_tool_counts[(bucket, tool)][1] += 1                   # successes
    out: Dict[str, Dict[str, float]] = defaultdict(dict)
    for (bucket, tool), (total, ok) in bucket_tool_counts.items():
        if total < min_observations:
            continue
        out[bucket][tool] = round(ok / max(1, total), 3)
    return dict(out)


def save_affinity_map(m: Dict[str, Dict[str, float]]) -> None:
    try:
        _affinity_path().write_text(
            json.dumps(m, indent=2, ensure_ascii=False, sort_keys=True),
            encoding="utf-8",
        )
    except Exception as exc:
        logger.debug("outcome_logger.save_affinity: %s", exc)


def load_affinity_map() -> Dict[str, Dict[str, float]]:
    p = _affinity_path()
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def affinity_bias_for_tool(query: str, tool_name: str) -> float:
    """Return a bias in [-1, 1] for a (query, tool) pair based on
    historical affinity. Positive = success rate above 0.5, negative
    = below. Used by the router to nudge rankings."""
    if not _enabled():
        return 0.0
    m = load_affinity_map()
    if not m:
        return 0.0
    bucket = _bucket_query(query)
    rate = m.get(bucket, {}).get(tool_name)
    if rate is None:
        return 0.0
    # Centre around 0.5 and scale to [-1, 1].
    return (rate - 0.5) * 2.0


# ---------------------------------------------------------------------------
# Background daemon
# ---------------------------------------------------------------------------


def _daemon_loop() -> None:
    interval = _interval_sec()
    logger.info("openjarvis.outcome_logger.daemon started interval=%ds", interval)
    _write_heartbeat()
    while True:
        try:
            time.sleep(interval)
        except Exception:
            time.sleep(60)
        if not _enabled():
            _write_heartbeat()
            continue
        try:
            m = compute_affinity_map()
            save_affinity_map(m)
            logger.warning(
                "openjarvis.outcome_logger.affinity_recomputed buckets=%d",
                len(m),
            )
        except Exception as exc:
            logger.warning("outcome_logger.daemon run failed: %s", exc)
        _write_heartbeat()


def _write_heartbeat() -> None:
    try:
        _heartbeat_path().write_text(
            json.dumps({
                "ts": time.time(),
                "iso": datetime.now(timezone.utc).isoformat(),
            }),
            encoding="utf-8",
        )
    except Exception:
        pass


def start_daemon() -> None:
    """Idempotent — starts the daemon thread on first call."""
    global _DAEMON_THREAD
    if _DAEMON_THREAD is not None and _DAEMON_THREAD.is_alive():
        return
    if not _enabled():
        logger.info("openjarvis.outcome_logger.daemon disabled by env")
        return
    t = threading.Thread(
        target=_daemon_loop,
        name="openjarvis-outcome-logger-daemon",
        daemon=True,
    )
    t.start()
    _DAEMON_THREAD = t


# ---------------------------------------------------------------------------
# Snapshot
# ---------------------------------------------------------------------------


def snapshot() -> Dict[str, Any]:
    recs = read_recent(limit=5000)
    m = load_affinity_map()
    hb = None
    try:
        if _heartbeat_path().exists():
            hb_raw = json.loads(_heartbeat_path().read_text(encoding="utf-8"))
            hb_ts = hb_raw.get("ts")
            if isinstance(hb_ts, (int, float)):
                hb = {
                    "iso": hb_raw.get("iso"),
                    "seconds_ago": int(time.time() - hb_ts),
                }
    except Exception:
        pass
    # Quick stats over recent records
    by_tool: Dict[str, int] = defaultdict(int)
    disavowal_count = 0
    success_count = 0
    for r in recs:
        if r.get("tool_called"):
            by_tool[r["tool_called"]] += 1
        if r.get("llm_disavowed"):
            disavowal_count += 1
        if r.get("tool_success") is True:
            success_count += 1
    return {
        "enabled": _enabled(),
        "interval_sec": _interval_sec(),
        "affinity_weight": _affinity_weight(),
        "total_turns_logged": len(recs),
        "tool_affinity_clusters": len(m),
        "tool_call_counts_top10": dict(sorted(
            by_tool.items(), key=lambda kv: -kv[1],
        )[:10]),
        "successful_tool_calls": success_count,
        "disavowals_logged": disavowal_count,
        "affinity_map_size_bytes": (
            _affinity_path().stat().st_size if _affinity_path().exists() else 0
        ),
        "daemon_heartbeat": hb,
        "log_path": str(_log_path()),
    }
