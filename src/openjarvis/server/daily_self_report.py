"""Daily self-report — surfaces yesterday's learning to the user.

On the FIRST chat turn of a new UTC day, this injects a brief system
message into the LLM's context summarising:
  - How many disavowals were logged
  - How many learned-intent patterns were auto-promoted
  - Which skill drafts were proposed by skill_proposer

The LLM is instructed to mention this BRIEFLY at the start of its
reply if and only if the count is non-trivial. This is the surfacing
side of the self-improvement loop — the system tells the user what it
learned, instead of silently growing.

Public API:
    build_daily_report_if_due() -> Optional[str]
        Returns the report text (caller injects as system message) on
        first chat of a new UTC day; returns None otherwise.

Env: OPENJARVIS_DAILY_SELF_REPORT_ENABLED (default true).
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


_LOCK = threading.Lock()


def _enabled() -> bool:
    return os.environ.get("OPENJARVIS_DAILY_SELF_REPORT_ENABLED", "true").lower() in (
        "1", "true", "yes", "on",
    )


def _state_path() -> Path:
    base = os.environ.get("OPENJARVIS_HOME", "").strip()
    d = Path(base) if base else Path.home() / ".openjarvis"
    try:
        d.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    return d / "daily_report_state.json"


def _today_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _yesterday_str() -> str:
    from datetime import timedelta
    return (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")


def _already_reported_today() -> bool:
    try:
        s = _state_path()
        if not s.exists():
            return False
        data = json.loads(s.read_text(encoding="utf-8"))
        return data.get("last_reported_date") == _today_str()
    except Exception:
        return False


def _mark_reported_today() -> None:
    try:
        _state_path().write_text(
            json.dumps(
                {"last_reported_date": _today_str(),
                 "last_reported_at": datetime.now(timezone.utc).isoformat()},
                indent=2, ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    except Exception:
        pass


def _count_disavowals_yesterday() -> int:
    try:
        from openjarvis.server import disavowal_detector as _dd
        recs = _dd.read_recent(limit=2000)
        yday = _yesterday_str()
        return sum(1 for r in recs if (r.get("iso") or "").startswith(yday))
    except Exception:
        return 0


def _count_promotions_yesterday() -> int:
    try:
        from openjarvis.server import learned_intents as _li
        snap = _li.snapshot()
        history = snap.get("recent_promotions", [])
        yday = _yesterday_str()
        return sum(1 for p in history if (p.get("promoted_at") or "").startswith(yday))
    except Exception:
        return 0


def _count_skills_proposed_yesterday() -> int:
    try:
        base = os.environ.get("OPENJARVIS_HOME", "").strip()
        d = Path(base) if base else Path.home() / ".openjarvis"
        proposed = d / "skills" / "proposed"
        if not proposed.exists():
            return 0
        yday = _yesterday_str()
        return sum(1 for f in proposed.glob(f"{yday}_*.toml"))
    except Exception:
        return 0


def build_daily_report_if_due() -> Optional[str]:
    """Returns the system-prompt block if today is a new day AND there's
    something non-trivial to report. Idempotent within a day."""
    if not _enabled():
        return None
    with _LOCK:
        if _already_reported_today():
            return None
        disavowals = _count_disavowals_yesterday()
        promotions = _count_promotions_yesterday()
        skills = _count_skills_proposed_yesterday()
        total = disavowals + promotions + skills
        if total == 0:
            # Nothing to report — still mark as reported so we don't
            # check again until tomorrow.
            _mark_reported_today()
            return None
        bits = []
        if disavowals:
            bits.append(f"detected {disavowals} tool-disavowal{'s' if disavowals != 1 else ''}")
        if promotions:
            bits.append(
                f"auto-promoted {promotions} new intent pattern{'s' if promotions != 1 else ''} "
                f"so similar requests work next time"
            )
        if skills:
            bits.append(
                f"proposed {skills} new skill draft{'s' if skills != 1 else ''} for review"
            )
        report = (
            "DAILY SELF-IMPROVEMENT REPORT (from yesterday): "
            + ", ".join(bits) + ". "
            "ONLY mention this to the user briefly (one sentence) at the START "
            "of your reply if they greet you. If they ask a direct question, "
            "answer that first and skip this report entirely."
        )
        _mark_reported_today()
        logger.warning(
            "openjarvis.daily_report.injected disavowals=%d promotions=%d skills=%d",
            disavowals, promotions, skills,
        )
        return report


def snapshot() -> dict:
    return {
        "enabled": _enabled(),
        "today": _today_str(),
        "already_reported_today": _already_reported_today(),
        "yesterday_counts": {
            "disavowals": _count_disavowals_yesterday(),
            "promotions": _count_promotions_yesterday(),
            "skills_proposed": _count_skills_proposed_yesterday(),
        },
    }
