"""Postgres mirror for the self-improvement loop (Round 8.7-A).

Local JSONL stays canonical for the read path (fast, no network). After
each successful local write, we best-effort mirror to Postgres so:

  * a Railway redeploy doesn't wipe yesterday's learnings,
  * multi-replica deployments share the same learned patterns,
  * humans can SQL-query what the system has learned.

Failure isolation: every public function is **synchronous, best-effort,
never raises**. If ``DATABASE_URL`` is unset, ``psycopg`` is missing, or
the connection fails, the helpers log once at debug level and return
silently. The local JSONL/JSON files keep working unchanged.

Public API:
    mirror_disavowal(event)      — append a disavowal row
    mirror_pattern(domain, p)    — upsert a learned pattern / hint row
    snapshot()                   — for /v1/_debug/agentic
"""

from __future__ import annotations

import json
import logging
import os
import threading
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


_PG_DSN_ENV = "DATABASE_URL"
_FLAG_ENV = "OPENJARVIS_LEARNING_PG_MIRROR_ENABLED"

_LOCK = threading.Lock()
_init_attempted = False
_pg_ready: Optional[bool] = None
_conn_factory = None  # callable returning a fresh psycopg connection


_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS learning_disavowals (
    id            BIGSERIAL PRIMARY KEY,
    ts            DOUBLE PRECISION NOT NULL,
    iso           TEXT NOT NULL,
    user_text     TEXT,
    assistant_text TEXT,
    injected_tool_groups TEXT,
    injected_tool_names  TEXT,
    inferred_domain TEXT,
    session_id    TEXT
);
CREATE INDEX IF NOT EXISTS learning_disavowals_domain_idx
    ON learning_disavowals(inferred_domain);
CREATE INDEX IF NOT EXISTS learning_disavowals_ts_idx
    ON learning_disavowals(ts);

CREATE TABLE IF NOT EXISTS learning_patterns (
    domain        TEXT NOT NULL,
    regex         TEXT NOT NULL,
    action        TEXT NOT NULL,
    hint_text     TEXT,
    member_count  INTEGER,
    promoted_at   TEXT NOT NULL,
    PRIMARY KEY (domain, regex)
);
CREATE INDEX IF NOT EXISTS learning_patterns_action_idx
    ON learning_patterns(action);
"""


def _enabled() -> bool:
    if os.environ.get(_FLAG_ENV, "true").lower() not in ("1", "true", "yes", "on"):
        return False
    return bool(os.environ.get(_PG_DSN_ENV, "").strip())


def _ensure_init() -> bool:
    """Lazy, idempotent connect + schema bootstrap. Returns True if PG usable."""
    global _init_attempted, _pg_ready, _conn_factory
    if _pg_ready is not None:
        return _pg_ready
    with _LOCK:
        if _pg_ready is not None:
            return _pg_ready
        _init_attempted = True
        if not _enabled():
            _pg_ready = False
            return False
        dsn = os.environ.get(_PG_DSN_ENV, "").strip()
        try:
            import psycopg  # type: ignore
        except Exception as exc:
            logger.debug("learning_pg: psycopg unavailable (%s)", exc)
            _pg_ready = False
            return False
        try:
            # One short-lived connection to ensure the schema. Subsequent
            # writes open a fresh connection each call (low write rate;
            # safer than holding a pool across worker forks).
            with psycopg.connect(dsn, connect_timeout=5) as conn:  # type: ignore[arg-type]
                with conn.cursor() as cur:
                    cur.execute(_SCHEMA_SQL)
                conn.commit()
            _conn_factory = lambda: psycopg.connect(dsn, connect_timeout=5)
            _pg_ready = True
            logger.info("learning_pg: Postgres mirror ready")
            return True
        except Exception as exc:
            logger.debug("learning_pg: init failed (%s) — staying offline", exc)
            _pg_ready = False
            return False


def mirror_disavowal(event: Dict[str, Any]) -> None:
    """Append a disavowal record to Postgres. Best-effort, never raises."""
    if not _ensure_init() or _conn_factory is None:
        return
    try:
        with _conn_factory() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO learning_disavowals
                        (ts, iso, user_text, assistant_text,
                         injected_tool_groups, injected_tool_names,
                         inferred_domain, session_id)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        float(event.get("ts") or 0.0),
                        str(event.get("iso") or ""),
                        event.get("user_text"),
                        event.get("assistant_text"),
                        json.dumps(event.get("injected_tool_groups") or []),
                        json.dumps(event.get("injected_tool_names") or []),
                        event.get("inferred_domain") or event.get("inferred_category") or "general",
                        event.get("session_id") or "",
                    ),
                )
            conn.commit()
    except Exception as exc:
        logger.debug("learning_pg.mirror_disavowal failed: %s", exc)


def mirror_pattern(
    *,
    domain: str,
    regex: str,
    action: str,
    hint_text: Optional[str],
    member_count: int,
    promoted_at: str,
) -> None:
    """Upsert a learned pattern / hint. Best-effort, never raises."""
    if not _ensure_init() or _conn_factory is None:
        return
    try:
        with _conn_factory() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO learning_patterns
                        (domain, regex, action, hint_text, member_count, promoted_at)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (domain, regex) DO UPDATE SET
                        action = EXCLUDED.action,
                        hint_text = EXCLUDED.hint_text,
                        member_count = EXCLUDED.member_count,
                        promoted_at = EXCLUDED.promoted_at
                    """,
                    (domain, regex, action, hint_text, int(member_count), promoted_at),
                )
            conn.commit()
    except Exception as exc:
        logger.debug("learning_pg.mirror_pattern failed: %s", exc)


def snapshot() -> Dict[str, Any]:
    """Counts for /v1/_debug/agentic. Never raises."""
    out: Dict[str, Any] = {
        "enabled": _enabled(),
        "ready": False,
        "disavowals": 0,
        "patterns": 0,
    }
    if not _ensure_init() or _conn_factory is None:
        return out
    out["ready"] = True
    try:
        with _conn_factory() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT count(*) FROM learning_disavowals")
                out["disavowals"] = int(cur.fetchone()[0])
                cur.execute("SELECT count(*) FROM learning_patterns")
                out["patterns"] = int(cur.fetchone()[0])
    except Exception as exc:
        logger.debug("learning_pg.snapshot failed: %s", exc)
    return out
